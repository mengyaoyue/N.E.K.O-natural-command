"""自然语言命令插件核心逻辑（与 N.E.K.O SDK 解耦，便于独立测试）"""

from __future__ import annotations

import hmac
import json
import locale
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional


DEFAULT_COMMANDS: dict[str, dict[str, Any]] = {
    "greeting": {
        "id": "greeting",
        "name": "问候",
        "description": "向主人问好",
        "permission": "user",
        "type": "reply",
        "content": "你好喵～今天也要开心呀！",
    },
    "open_notepad": {
        "id": "open_notepad",
        "name": "打开记事本",
        "description": "启动系统记事本",
        "permission": "admin",
        "type": "shell",
        "content": "notepad",
    },
}


def parse_user_input(text: str) -> str:
    """去掉 / 命令前缀，统一返回有效内容。"""
    text = text.strip()
    if text.startswith("/"):
        text = text[1:].strip()
    return text


# 语义是“退出管理员权限”的 /命令（提权后的降权出口）
_EXIT_ADMIN_RE = re.compile(
    r"(退出|解除|取消|关闭|放弃|回到|恢复|drop|exit|leave|revoke|disable)\s*"
    r"(admin|administrator|管理员|管理者|管理|超管|root|sudo|权限)",
    re.IGNORECASE,
)
_EXIT_ADMIN_PHRASES = (
    "exitadmin", "exit admin", "sudo -k", "logout admin",
    "回到user", "回到普通用户", "回到普通权限", "恢复普通", "降权", "退出提权",
)


def is_exit_admin(text: str) -> bool:
    """判断用户输入（已去掉 / 前缀）是否在语义上要求退出管理员权限。"""
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    if lowered in _EXIT_ADMIN_PHRASES:
        return True
    return _EXIT_ADMIN_RE.search(lowered) is not None


def strip_code_fence(text: str) -> str:
    """去除 markdown 代码块包装。"""
    text = text.strip()
    if text.startswith("```"):
        first = text.find("\n")
        last = text.rfind("```")
        if first != -1 and last > first:
            text = text[first + 1 : last].strip()
    return text


def extract_json_object(text: str) -> Optional[str]:
    """从模型输出中提取第一个完整的 JSON 对象字符串。

    兼容以下情况：输出被 markdown 代码块包裹、JSON 前后夹带解释文字。
    """
    if not text:
        return None
    text = strip_code_fence(text)
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def normalize_action(result: Any) -> str:
    """取出并规范化模型返回的 action 字段。"""
    if not isinstance(result, dict):
        return ""
    action = result.get("action")
    if action is None:
        action = result.get("Action")
    return safe_str(action).lower()


def extract_new_command(result: Any) -> Optional[dict[str, Any]]:
    """从模型返回中提取新命令配置。"""
    if not isinstance(result, dict):
        return None
    new_cmd = result.get("new_command") or result.get("command")
    if isinstance(new_cmd, dict):
        return new_cmd
    if result.get("id"):
        keys = ("id", "name", "description", "risk", "permission", "type", "content")
        return {k: result[k] for k in keys if k in result}
    return None


def build_create_prompt(
    user_input: str,
    default_permission: str,
    default_type: str,
) -> str:
    """构造“为用户输入生成一条新命令”的提示词。

    安全审查（威胁等级 risk）与命令生成**并入同一轮输出**，因此不需要再额外调用一次
    大模型：模型在给出命令配置的同时，必须顺手判断它有没有危害。
    """
    return f"""你是自然语言命令生成引擎，同时负责对生成的命令做安全审查。

用户输入：{user_input}
默认类型：{default_type}
默认权限（仅在完全无法判断 risk 时参考）：{default_permission}

请根据用户意图为它创建一条可复用的命令配置，并严格只返回 JSON，不要任何其他内容、解释或 markdown 代码块：
{{"action": "create", "new_command": {{
  "id": "英文唯一标识，仅字母数字下划线",
  "name": "命令名称",
  "description": "功能描述",
  "risk": "harmless/indeterminate/harmful",
  "type": "reply/shell",
  "content": "type=reply 时为要回复给用户的文本；type=shell 时为要执行的完整命令行"
}}}}

risk 是你对该命令威胁等级的独立审查结果（必须自己判断，不要照抄）：
- harmless：无害。只读、打开软件/网页、文本回复、查询信息，不会改动用户设备资料
- indeterminate：无法或难以分辨是否会对用户设备资料造成影响
- harmful：很可能危害设备或资料，例如删除/修改文件、改注册表、安装卸载、关机重启、执行脚本、读取隐私数据等
- 影响：risk=harmful 的命令会自动归入 admin 权限（需提权）；harmless / indeterminate 归入 user 权限（正常即可运行）

规则：
- 若用户输入确实无法转化为一条命令（例如无意义闲聊），返回 {{"action": "not_found"}}
- id 只能包含字母、数字和下划线，不要带空格
- 涉及打开网页/软件优先使用 type=shell
- 【重要】严禁编造不存在的协议（例如 bilibili://、qq://、weixin:// 一律不许出现）
- 打开网页必须写成：start "" https://具体网址（必须带 https://）
- 打开软件优先使用系统自带命令（notepad、calc、mspaint、explorer 等）；其他软件写成 start "" "软件名"，系统会自动在本机查找真实程序
- 【严禁】自己编造 C:\\...\\xx.exe 或 .lnk 完整路径——路径不存在时命令会无声失败
- 不要写 cmd /c 前缀，直接写 start
- 不确定真实路径时，宁可只写软件名，也不要编造协议或路径
- 纯文本回应使用 type=reply，content 不要有多余解释
"""


def safe_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            return True
        if v in {"0", "false", "no", "off"}:
            return False
    return default


def safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    s = str(value).strip()
    return s if s else default


# ── 威胁等级（AI 安全审查的三档判定）────────────────────────────
# 由 AI 在“创建 / 匹配”同一轮里顺手给出，不额外增加一次模型调用。
RISK_HARMLESS = "harmless"          # 无害：只读 / 打开 / 回复，不影响设备资料
RISK_INDETERMINATE = "indeterminate"  # 无法判定：难以分辨是否有害
RISK_HARMFUL = "harmful"            # 有害：可能对设备 / 资料造成危害

_RISK_ALIASES: dict[str, str] = {
    RISK_HARMLESS: RISK_HARMLESS,
    "safe": RISK_HARMLESS,
    "无危害": RISK_HARMLESS,
    "无害": RISK_HARMLESS,
    "安全": RISK_HARMLESS,
    RISK_INDETERMINATE: RISK_INDETERMINATE,
    "unknown": RISK_INDETERMINATE,
    "uncertain": RISK_INDETERMINATE,
    "无法判定": RISK_INDETERMINATE,
    "无法确定": RISK_INDETERMINATE,
    "难以分辨": RISK_INDETERMINATE,
    "不确定": RISK_INDETERMINATE,
    RISK_HARMFUL: RISK_HARMFUL,
    "danger": RISK_HARMFUL,
    "dangerous": RISK_HARMFUL,
    "有害": RISK_HARMFUL,
    "危险": RISK_HARMFUL,
}


def normalize_risk(value: Any) -> str:
    """把 AI 返回的风险等级归一化为 harmless / indeterminate / harmful。

    无法识别时返回空串，调用方据此回退到命令已有的 ``permission`` 字段。
    """
    key = safe_str(value).lower()
    return _RISK_ALIASES.get(key, "")


def permission_for_risk(risk: Any, default: str = "user") -> str:
    """风险等级 → 权限：只有 harmful 需要 admin，其余（含无法判定）都是 user。"""
    normalized = normalize_risk(risk)
    if normalized == RISK_HARMFUL:
        return "admin"
    if normalized in (RISK_HARMLESS, RISK_INDETERMINATE):
        return "user"
    return default


def safe_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def load_settings(section: Any) -> dict[str, Any]:
    """从配置段 dict 中解析插件设置。

    SDK 的 ``self.config.dump()`` 返回整个 plugin.toml 的字典，
    插件自身配置位于 ``cfg["neko_natural_command"]`` 段。
    """
    section = section if isinstance(section, dict) else {}
    return {
        "admin_password": safe_str(section.get("admin_password")),
        "auto_create": safe_bool(section.get("auto_create"), True),
        "auto_clean": safe_bool(section.get("auto_clean"), True),
        "default_permission": safe_str(section.get("default_permission"), "user"),
        "default_type": safe_str(section.get("default_type"), "reply"),
        "llm_timeout": safe_float(section.get("llm_timeout"), 20.0),
        # 0 / 负数意味着"无限等待"，统一钳制到最小 1 秒
        "shell_timeout": max(1.0, safe_float(section.get("shell_timeout"), 30.0)),
    }


def build_endpoint_candidates(base_url: str, model: str) -> list[str]:
    """根据 base_url / model 推导可能可用的对话接口地址。"""
    base = (base_url or "").rstrip("/")
    url: list[str] = []
    if "gemini" in (model or "").lower():
        if base.endswith("/v1beta"):
            url.append(f"{base}/models/{model}:generateContent")
        elif "/v" in base.split("/")[-1]:
            url.append(f'{base[:base.rfind("/")]}/v1beta/models/{model}:generateContent')
        else:
            url.append(f"{base}/v1beta/models/{model}:generateContent")
        return url

    if "chat/completions" in base:
        url.append(base)
        return url

    if "/v" in base.split("/")[-1]:
        url.append(f"{base}/chat/completions")
    else:
        url.extend([f"{base}/chat/completions", f"{base}/v1/chat/completions"])
    return url


_URL_SCHEME_RE = re.compile(r"^(?P<scheme>[^/\s:]+)://")
_START_RE = re.compile(r'^\s*start\s+(?:"")?\s*(?P<target>.*)$', re.IGNORECASE | re.DOTALL)

_KNOWN_URL_SCHEMES = {"http", "https", "file", "ftp", "ftps", "mailto"}

# start 的无参数开关：解析目标前先剥掉，否则会把 /max 当成应用名去搜。
_START_NOARG_FLAGS = {
    "/b", "/i", "/min", "/max", "/normal", "/separate", "/shared", "/wait",
    "/low", "/abovenormal", "/belownormal",
}
# /D <目录> 带一个参数，单独处理。

# cmd 内置命令与常见控制台程序：这些是“要执行的命令”而不是“要打开的应用”，
# 解析应用名时必须放行，否则 dir / tasklist 这类查询会被误改成 start。
_CMD_BUILTINS = {
    "assoc", "call", "cd", "chdir", "cls", "color", "copy", "date", "del",
    "dpath", "echo", "endlocal", "erase", "exit", "for", "ftype", "goto", "if",
    "md", "mkdir", "mklink", "move", "path", "pause", "popd", "prompt", "pushd",
    "rd", "rem", "ren", "rename", "rmdir", "set", "setlocal", "shift", "start",
    "subst", "time", "title", "type", "ver", "verify", "vol",
    # 常见控制台程序（虽是 exe，但永远不会是“打开应用”的目标）
    "find", "findstr", "more", "tree", "where", "tasklist", "taskkill", "sc",
    "net", "netstat", "ping", "ipconfig", "systeminfo", "wmic", "hostname",
    "whoami", "driverquery", "sfc", "dism", "chkdsk", "reg", "rundll32",
}

# 疑似域名但不该当域名的文件扩展名（app.exe / 报告.pdf 不该补 https://）
_DOMAIN_EXT_DENYLIST = (
    ".exe", ".dll", ".lnk", ".url", ".bat", ".cmd", ".msi", ".appref-ms",
    ".txt", ".doc", ".docx", ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".mp3",
    ".mp4", ".avi", ".mkv", ".zip", ".rar", ".7z", ".py", ".js", ".json",
    ".html", ".htm", ".xml", ".csv", ".xlsx", ".pptx",
)

_WINDOWS_BUILTINS = {
    "notepad", "calc", "mspaint", "explorer", "cmd", "control", "taskmgr",
    "regedit", "charmap", "cleanmgr", "dxdiag", "msconfig", "osk",
    "snippingtool", "powershell", "wt",
}

# “打开即结束”的 GUI 程序：启动后不需要、也不该去捕获它的输出。
_LAUNCH_ONLY_BUILTINS = _WINDOWS_BUILTINS - {"cmd", "powershell"}

# 交互式外壳：单独出现（无参数）时是开一个窗口给人用，同样不算“取输出”的命令。
_INTERACTIVE_SHELLS = {"cmd", "powershell", "pwsh", "python", "python3", "py", "node"}

# 会输出结果的 shell 命令最长等待时间（秒），超时即判定失败，避免卡死。
# 运行时可通过配置项 shell_timeout 覆盖。
_SHELL_OUTPUT_TIMEOUT = 30

# 回传给 AI 的 shell 输出最多保留多少字符，超出部分截断（防止撑爆上下文）。
_MAX_SHELL_OUTPUT_CHARS = 4000

# 匹配提示词里最多携带的候选命令条数：本地预筛后只把最相关的一批发给模型，
# 避免命令库越学越大后把整库 JSON 塞进提示词。
_MATCH_CANDIDATE_LIMIT = 30

# 合法命令 id：仅字母 / 数字 / 下划线（提示词里对 AI 的要求一致）。
_COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")

# 纯“启动器”类扩展名：双击即打开，不产生可读输出。
_LAUNCHER_EXTENSIONS = (".lnk", ".url", ".appref-ms")

_SHORTCUT_STOPWORDS = (
    "打开", "启动", "运行", "请", "帮我", "我要",
    "桌面上的", "桌面上面的", "桌面", "上面", "快捷方式", "快捷键", "快捷",
    "客户端", "软件", "应用", "程序", "的",
)

_SHORTCUT_BLACKLIST = (
    "卸载", "uninstall", "remove", "帮助", "help", "文档", "manual",
    "readme", "更新", "update", "setup", "安装", "repair", "修复",
)

_APP_EXTENSIONS = {".lnk", ".exe", ".url", ".bat", ".cmd", ".appref-ms"}

# 应用来源优先级：数字越小越优先（快捷方式最贴近用户直觉，安装目录最泛）
_SOURCE_SHORTCUT = 0
_SOURCE_START_MENU = 1
_SOURCE_APP_PATH = 2
_SOURCE_UNINSTALL = 3
_SOURCE_INSTALL_DIR = 4

# 占位 / 空壳 reply 命令的特征话术：AI 当时没能真的完成任务，只存下一句
# “请提供… / 我找不到… / 抱歉…”的推脱回复，属于典型的垃圾脏数据。
_PLACEHOLDER_RE = re.compile(
    r"请(提供|告诉|说明|补充|指定|给出|输入|上传|发送)"
    r"|才能(帮|为)你"
    r"|(找|搜|查)不到"
    r"|没有找到|未能找到"
    r"|无法(打开|帮|为|找到|识别|执行|完成|处理)"
    r"|抱歉|对不起|不好意思"
    r"|不知道|不清楚"
    r"|暂时(不能|无法|不支持)"
    r"|不支持(该|这个|此)"
)

_VALID_COMMAND_TYPES = ("reply", "shell")


def _dirty_reason(
    cmd: Any, cmd_id: Any, seen: dict[tuple[str, str, str], str]
) -> Optional[str]:
    """判断一条命令是否属于需要清理的脏数据；干净时返回 ``None``。

    三类脏数据：
      - ``invalid``：损坏 / 缺字段 / 字段类型错误的条目；
      - ``placeholder``：占位 / 空壳命令（reply 内容是“请提供…”之类的推脱话术）；
      - ``duplicate``：类型与内容（或名称）完全相同的重复命令。

    ``seen`` 记录已判定为“保留”的命令去重签名，只有干净条目才会写入，
    这样重复项会被判定为与前面那条保留项重复。
    """
    if not isinstance(cmd, dict):
        return "invalid:条目不是对象"
    if not safe_str(cmd.get("id")) and not safe_str(cmd_id):
        return "invalid:缺少 id"

    ctype = safe_str(cmd.get("type"), "reply").lower()
    if ctype not in _VALID_COMMAND_TYPES:
        return f"invalid:类型非法({ctype})"

    content = cmd.get("content")
    if not isinstance(content, str):
        return "invalid:内容不是文本"
    content = content.strip()
    if not content:
        return "placeholder:空壳命令"
    if ctype == "reply" and _PLACEHOLDER_RE.search(content):
        return "placeholder:占位回复"

    cid = safe_str(cmd.get("id"), safe_str(cmd_id))
    content_sig = ("content", ctype, _compact(content))
    if content_sig in seen:
        return f"duplicate:与 {seen[content_sig]} 内容重复"
    name = safe_str(cmd.get("name"))
    name_sig = ("name", ctype, _compact(name))
    if name and name_sig in seen:
        return f"duplicate:与 {seen[name_sig]} 名称重复"

    seen[content_sig] = cid
    if name:
        seen[name_sig] = cid
    return None


def _display_stem(name: str) -> str:
    """从应用名或路径中取出用于匹配的显示名（去掉 .lnk / .exe 等后缀）。"""
    text = (name or "").strip()
    if not text:
        return ""
    path = Path(text)
    if path.suffix.lower() in _APP_EXTENSIONS:
        return path.stem
    return path.name


# 匹配时忽略的字符：空白 + 常见中英分隔符 / 标点。
# 用于把 “TRAE Work CN” 压成 “traeworkcn”，这样用户漏打空格也能命中。
_COMPACT_STRIP_RE = re.compile(
    r"[\s\-_.,，。·・:：;；!！?？'\"“”‘’()（）\[\]【】{}「」『』<>《》/\\|+&~^%$#@*=]+"
)


def _compact(text: str) -> str:
    """去掉空白与常见分隔/标点，得到用于“忽略空格”匹配的紧凑串。

    例：``"TRAE Work CN"`` → ``"traeworkcn"``，因此用户输入 ``TRAEWORKCN`` 也能命中。
    """
    return _COMPACT_STRIP_RE.sub("", (text or "").lower())


def _match_key(name: str, candidates: list[str]) -> Optional[tuple[int, int]]:
    """给候选词与某个应用名的匹配打分；不匹配返回 ``None``。

    返回 ``(是否完全相等, 名称长度)``，越小越优先；命中卸载/帮助类名称直接跳过，
    以免把“卸载哔哩哔哩”当成“哔哩哔哩”。

    匹配同时看两种形态：原始小写（宽松包含）与去掉空格/标点后的紧凑串，
    后者用于兜住“用户漏打空格 / 大小写 / 分隔符不一致”的情况。
    """
    stem = _display_stem(name)
    low = stem.lower()
    if not low:
        return None
    if any(bad in low for bad in _SHORTCUT_BLACKLIST):
        return None
    compact = _compact(stem)
    best: Optional[tuple[int, int]] = None
    for cand in candidates:
        c = (cand or "").strip().lower()
        if not c:
            continue
        c_compact = _compact(c)
        exact = low == c or (len(c_compact) >= 2 and c_compact == compact)
        hit = exact or c in low or low in c
        if not hit and len(c_compact) >= 2 and compact:
            hit = c_compact in compact or compact in c_compact
        if not hit:
            continue
        key = (0 if exact else 1, len(stem))
        if best is None or key < best:
            best = key
    return best


def _shortcut_dirs() -> list[Path]:
    """返回桌面 / 开始菜单等常见快捷方式目录。"""
    dirs: list[Path] = []
    for env in ("PUBLIC", "USERPROFILE"):
        base = os.environ.get(env)
        if base:
            dirs.append(Path(base) / "Desktop")
    for env in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(env)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [d for d in dirs if d.exists()]


def iter_shortcuts(dirs: Optional[list[Path]] = None):
    """遍历桌面 / 开始菜单中的快捷方式（.lnk）。"""
    search = list(dirs) if dirs is not None else _shortcut_dirs()
    seen: set[str] = set()
    for base in search:
        try:
            for path in Path(base).rglob("*.lnk"):
                key = str(path).lower()
                if key in seen:
                    continue
                seen.add(key)
                yield path
        except Exception:
            continue


def _strip_stopwords(text: str) -> str:
    result = text or ""
    for word in _SHORTCUT_STOPWORDS:
        result = result.replace(word, "")
    return result.strip()


def _alias_candidates(scheme: str, hint: str, target: str) -> list[str]:
    candidates: list[str] = []
    if scheme:
        candidates.append(scheme)
    for text in (hint, target):
        stripped = _strip_stopwords(text)
        if stripped:
            candidates.append(stripped)
    return candidates


def find_shortcut_for(
    candidates: list[str],
    shortcuts: Optional[list[Path]] = None,
) -> Optional[Path]:
    """在快捷方式中查找与候选词匹配的一项（保留此函数以兼容旧调用）。"""
    pool = list(shortcuts) if shortcuts is not None else list(iter_shortcuts())
    best: Optional[Path] = None
    best_key: Optional[tuple[int, int]] = None
    for path in pool:
        key = _match_key(str(path), candidates)
        if key is None:
            continue
        if best_key is None or key < best_key:
            best_key = key
            best = Path(path)
    return best


def _is_registered_protocol(scheme: str) -> bool:
    """判断某个 URL 协议是否已在系统中注册（只有注册过的 ``xxx://`` 才能被 start 打开）。"""
    if not scheme:
        return False
    try:
        import winreg
    except Exception:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{scheme}\shell\open\command"):
            return True
    except OSError:
        return False


def iter_app_paths():
    """遍历注册表 App Paths 中登记的可执行文件，产出 (名称, 完整路径)。"""
    try:
        import winreg
    except Exception:
        return
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            base = winreg.OpenKey(root, key_path)
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(base)[0]
            for index in range(count):
                try:
                    name = winreg.EnumKey(base, index)
                except OSError:
                    continue
                try:
                    with winreg.OpenKey(base, name) as sub:
                        value, _ = winreg.QueryValueEx(sub, "")
                except OSError:
                    continue
                if value:
                    yield name, value
        finally:
            base.Close()


def _query_value(key, name: str) -> str:
    """安全读取注册表某个键的字符串值，读不到返回空串。"""
    try:
        import winreg
    except Exception:
        return ""
    try:
        value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value).strip() if value else ""


def find_app_path(candidates: list[str], app_paths=None) -> Optional[str]:
    """在注册表 App Paths 中查找与候选词匹配的可执行文件（保留此函数以兼容旧调用）。

    ``app_paths`` 可注入 ``(名称, 路径)`` 序列，便于测试时避开真实注册表。
    """
    pool = list(app_paths) if app_paths is not None else list(iter_app_paths())
    best: Optional[str] = None
    best_key: Optional[tuple[int, int]] = None
    for name, path in pool:
        key = _match_key(name, candidates)
        if key is None:
            continue
        if best_key is None or key < best_key:
            best_key = key
            best = path
    return best


def _start_app_target(app_id: str) -> str:
    """把 ``Get-StartApps`` 的 AppID 转成可直接 ``start`` 的启动目标。

    - 指向真实文件（.lnk/.exe 等）→ 原样返回；
    - 商店 / UWP 应用（AUMID）→ ``shell:AppsFolder\\<AUMID>``。
    """
    app_id = (app_id or "").strip()
    if not app_id:
        return ""
    low = app_id.lower()
    if low.endswith((".lnk", ".exe", ".url", ".bat", ".cmd", ".appref-ms")):
        return app_id if os.path.exists(app_id) else ""
    if os.path.exists(app_id):
        return app_id
    if low.startswith("shell:"):
        return app_id
    if low.startswith(("http://", "https://")) or "/" in app_id or "\\" in app_id:
        # 有些开始菜单项只是“跳转网页/带路径”，不是真正的应用目标，放弃
        return ""
    return f"shell:AppsFolder\\{app_id}"


def iter_start_apps(timeout: float = 15.0):
    """通过 PowerShell ``Get-StartApps`` 枚举开始菜单应用（含 UWP / 商店应用）。

    这是本机制里最重要、也最“与机器无关”的来源：它读取的是**当前运行插件的这台
    机器**上实际安装的应用，而不是任何写死的清单，因此换一个用户 / 电脑也会自动
    得到对方自己的应用列表。产出 ``(显示名, 启动目标)``。
    """
    if os.name != "nt":
        return
    script = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except Exception:
        return
    if proc.returncode != 0 or not proc.stdout:
        return
    try:
        data = json.loads(proc.stdout)
    except Exception:
        return
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "").strip()
        target = _start_app_target(str(item.get("AppID") or ""))
        if name and target:
            yield name, target


def _clean_icon_path(value: str) -> str:
    """清洗注册表 ``DisplayIcon``（可能是 ``"x.exe",0`` 或 ``x.exe|...``）。"""
    text = (value or "").strip()
    if not text:
        return ""
    if "|" in text:
        text = text.split("|", 1)[0]
    if "," in text:
        text = text.split(",", 1)[0]
    return text.strip().strip('"')


def _find_main_exe(folder: str) -> str:
    """在安装目录第一层里找最像“主程序”的可执行文件（跳过卸载/更新类）。"""
    if not folder:
        return ""
    try:
        base = Path(folder)
        if not base.is_dir():
            return ""
        entries = list(base.iterdir())
    except Exception:
        return ""
    best = ""
    for entry in entries:
        try:
            if not entry.is_file() or entry.suffix.lower() != ".exe":
                continue
        except Exception:
            continue
        if any(bad in entry.stem.lower() for bad in _SHORTCUT_BLACKLIST):
            continue
        if not best or len(entry.stem) < len(Path(best).stem):
            best = str(entry)
    return best


def iter_uninstall_apps():
    """从“程序和功能”的卸载信息里提取应用（覆盖没有快捷方式的安装）。

    读取 ``DisplayIcon`` / ``InstallLocation``，据此定位真实主程序。
    """
    try:
        import winreg
    except Exception:
        return
    key_names = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    targets = [(winreg.HKEY_LOCAL_MACHINE, k) for k in key_names]
    targets.append((winreg.HKEY_CURRENT_USER, key_names[0]))
    for hive, key_path in targets:
        try:
            base = winreg.OpenKey(hive, key_path)
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(base)[0]
            for index in range(count):
                try:
                    sub_name = winreg.EnumKey(base, index)
                except OSError:
                    continue
                try:
                    with winreg.OpenKey(base, sub_name) as sub:
                        name = _query_value(sub, "DisplayName")
                        if not name:
                            continue
                        target = ""
                        icon = _clean_icon_path(_query_value(sub, "DisplayIcon"))
                        if icon.lower().endswith(".exe") and os.path.exists(icon):
                            target = icon
                        if not target:
                            target = _find_main_exe(_query_value(sub, "InstallLocation"))
                        if target:
                            yield name, target
                except OSError:
                    continue
        finally:
            base.Close()


def _install_dirs() -> list[Path]:
    """常见软件安装目录（Program Files / 用户级 Programs）。"""
    dirs: list[Path] = []
    for env, sub in (
        ("ProgramFiles", ""),
        ("ProgramFiles(x86)", ""),
        ("ProgramW6432", ""),
        ("LOCALAPPDATA", "Programs"),
    ):
        base = os.environ.get(env)
        if not base:
            continue
        path = Path(base) / sub if sub else Path(base)
        if path.is_dir():
            dirs.append(path)
    return dirs


def iter_install_dir_apps(max_dirs: int = 600):
    """扫描常见安装目录，补全连卸载信息都没有登记的应用。"""
    scanned = 0
    for base in _install_dirs():
        try:
            children = list(base.iterdir())
        except Exception:
            continue
        for child in children:
            if not child.is_dir():
                continue
            scanned += 1
            if scanned > max_dirs:
                return
            target = _find_main_exe(str(child))
            if target:
                yield child.name, target


def _collect_system_apps():
    """聚合本机所有可用来源的应用清单（运行时采集，代码里不写死任何路径）。"""

    def shortcut_entries():
        for path in iter_shortcuts():
            yield Path(path).stem, str(path), _SOURCE_SHORTCUT

    def start_app_entries():
        for name, target in iter_start_apps():
            yield name, target, _SOURCE_START_MENU

    def app_path_entries():
        for name, path in iter_app_paths():
            yield Path(name).stem, path, _SOURCE_APP_PATH

    def uninstall_entries():
        for name, target in iter_uninstall_apps():
            yield name, target, _SOURCE_UNINSTALL

    def install_dir_entries():
        for name, target in iter_install_dir_apps():
            yield name, target, _SOURCE_INSTALL_DIR

    seen: set[tuple[str, str]] = set()
    for factory in (
        shortcut_entries,
        start_app_entries,
        app_path_entries,
        uninstall_entries,
        install_dir_entries,
    ):
        try:
            for name, target, rank in factory():
                if not name or not target:
                    continue
                key = (name.lower(), target.lower())
                if key in seen:
                    continue
                seen.add(key)
                yield name, target, rank
        except Exception:
            continue


class AppIndex:
    """系统级应用索引：把“本机”多种来源的应用聚合成一张可模糊匹配的表。

    设计目标（解决“不同用户应用不一样”的问题）：
    - 代码里不写死任何应用 / 路径；索引在运行时从操作系统采集，
      所以每个用户得到的都是自己机器上的应用清单；
    - 多来源合并：桌面 / 开始菜单快捷方式、``Get-StartApps``（含 UWP 商店应用）、
      注册表 App Paths、卸载信息里的安装目录、常见安装目录；
    - 结果缓存，避免每条命令都重新枚举系统。

    条目形如 ``(显示名, 启动目标, 来源优先级)``，启动目标可直接用于
    ``start "" "<目标>"``（UWP 应用为 ``shell:AppsFolder\\<AUMID>``）。
    """

    def __init__(self, collector=None):
        self._entries: Optional[list[tuple[str, str, int]]] = None
        self._collector = collector

    @classmethod
    def from_entries(cls, entries) -> "AppIndex":
        """用显式条目构造（测试 / 复用），条目可为二元或三元组。"""
        index = cls()
        normalized: list[tuple[str, str, int]] = []
        for item in entries:
            if len(item) == 3:
                name, target, rank = item
            else:
                name, target = item
                rank = _SOURCE_SHORTCUT
            normalized.append((str(name), str(target), int(rank)))
        index._entries = normalized
        return index

    @classmethod
    def from_shortcuts_and_paths(cls, shortcuts, app_paths) -> "AppIndex":
        """兼容旧的 ``shortcuts`` / ``app_paths`` 注入方式。"""
        entries: list[tuple[str, str, int]] = []
        for path in shortcuts or []:
            entries.append((Path(path).stem, str(path), _SOURCE_SHORTCUT))
        for name, path in app_paths or []:
            entries.append((Path(name).stem, str(path), _SOURCE_APP_PATH))
        return cls.from_entries(entries)

    def entries(self) -> list[tuple[str, str, int]]:
        if self._entries is None:
            collector = self._collector or _collect_system_apps
            try:
                self._entries = list(collector())
            except Exception:
                self._entries = []
        return self._entries

    def find(self, candidates: list[str]) -> Optional[str]:
        """返回与候选词最匹配的应用启动目标，找不到返回 ``None``。"""
        best: Optional[str] = None
        best_key: Optional[tuple[int, int, int]] = None
        for name, target, rank in self.entries():
            key = _match_key(name, candidates)
            if key is None:
                continue
            full = (key[0], rank, key[1])
            if best_key is None or full < best_key:
                best_key = full
                best = target
        return best


_DEFAULT_INDEX: Optional[AppIndex] = None


def get_default_index() -> AppIndex:
    """获取（并缓存）本机的系统级应用索引。"""
    global _DEFAULT_INDEX
    if _DEFAULT_INDEX is None:
        _DEFAULT_INDEX = AppIndex()
    return _DEFAULT_INDEX


def reset_default_index() -> None:
    """清空默认索引缓存（应用安装后想刷新时调用）。"""
    global _DEFAULT_INDEX
    _DEFAULT_INDEX = None


def _resolve_index(shortcuts, app_paths, app_index) -> AppIndex:
    """决定这次解析用哪个应用索引。

    - 显式传入 ``app_index`` → 直接用；
    - 传了 ``shortcuts`` / ``app_paths``（哪怕空列表）→ 只用注入来源，保证测试可预期；
    - 都没传 → 用本机的系统级索引（真实运行时的路径）。
    """
    if app_index is not None:
        return app_index
    if shortcuts is not None or app_paths is not None:
        return AppIndex.from_shortcuts_and_paths(shortcuts or [], app_paths or [])
    return get_default_index()


def _looks_like_domain(token: str) -> bool:
    """判断一个无协议 token 是否是应当补全 https:// 的网址域名。

    ``www.bilibili.com`` → True；``app.exe`` / ``哔哩哔哩`` / ``127.0.0.1`` → False。
    """
    token = (token or "").strip()
    if not token or " " in token or "://" in token:
        return False
    if token.lower().endswith(_DOMAIN_EXT_DENYLIST):
        return False
    return re.match(r"^[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}$", token) is not None


def _strip_start_flags(raw_target: str) -> str:
    """剥掉 start 的开关（/max、/min、/D <目录> 等），返回真正的目标部分。

    原样保留目标的引号形态；剥完什么都不剩时返回空串。
    """
    tokens = raw_target.split()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        low = token.lower()
        if token == '""' or low in _START_NOARG_FLAGS:
            i += 1
        elif low == "/d" and i + 1 < len(tokens):
            i += 2
        else:
            break
    return " ".join(tokens[i:])


def resolve_shell_target(
    content: str,
    hint: str = "",
    shortcuts: Optional[list[Path]] = None,
    app_paths=None,
    protocol_checker=None,
    app_index: Optional["AppIndex"] = None,
) -> str:
    """把 AI 生成的 shell 命令解析成 Windows 真正能执行、且**执行后真的有效**的目标。

    核心目标：**杜绝“命令跑了但什么都没发生”的无效执行**。cmd 的 ``start`` 只搜
    PATH，也从不向调用方报错（fire-and-forget 看不到失败），所以必须在执行前把
    目标验证成确定可用的形态。为此处理五类情况：

    1. 伪协议（如 ``start bilibili://``）：协议已在本机注册则原样保留；否则在本机
       应用索引里找真实程序（快捷方式 / Get-StartApps / 注册表 / 安装目录），
       找不到就返回空串，由上层给友好提示；
    2. 网页地址：补全为 ``start "" <url>``；漏写协议的域名（``start www.bilibili.com``）
       自动补全 ``https://``，避免被当成软件名去找而无声失败；
    3. 文件路径目标：**先验证存在**——不存在的 ``.lnk``/``.exe`` 路径是无声失败的
       最大来源，存在性校验失败时回退到应用索引按名称找真实程序，仍找不到返回空串；
    4. 裸软件名：先看 PATH，再到本机应用索引里找；同样找不到返回空串；
    5. 不带 ``start`` 的裸应用名 / 裸协议 / 裸域名（AI 常见的偷懒写法）：按同样的
       规则解析并改写成 ``start`` 形态；cmd 内置命令（dir / tasklist 等）与带参数的
       查询命令一律放行，绝不被误改。

    ``shortcuts`` / ``app_paths`` / ``protocol_checker`` / ``app_index`` 均可注入，
    便于测试时避开真实文件系统与注册表。
    """
    if not content:
        return content
    raw = content.strip()

    match = _START_RE.match(raw)
    if match is None:
        if _URL_SCHEME_RE.match(raw) and raw.lower().startswith(("http://", "https://")):
            return f'start "" "{raw}"'
        # 只处理“整条命令就是一个 token”的情况；带参数/空格的是查询命令，绝不碰
        if not raw or re.search(r"\s", raw):
            return content
        scheme_match = _URL_SCHEME_RE.match(raw)
        if scheme_match:
            scheme = scheme_match.group("scheme").lower()
            if scheme in _KNOWN_URL_SCHEMES:
                return f'start "" "{raw}"'
            checker = protocol_checker if protocol_checker is not None else _is_registered_protocol
            if checker(scheme):
                return f'start "" "{raw}"'
            index = _resolve_index(shortcuts, app_paths, app_index)
            found = index.find(_alias_candidates(scheme, hint, raw))
            return f'start "" "{found}"' if found else ""
        if _looks_like_domain(raw):
            return f'start "" "https://{raw}"'
        low = raw.lower()
        if low in _WINDOWS_BUILTINS or low in _CMD_BUILTINS or low.startswith("shell:"):
            return content
        if shutil.which(raw):
            return content
        index = _resolve_index(shortcuts, app_paths, app_index)
        found = index.find(_alias_candidates("", hint, raw))
        if found:
            return f'start "" "{found}"'
        if low.endswith((".exe",) + _LAUNCHER_EXTENSIONS) or "\\" in raw or "/" in raw:
            # 明确指向本机文件但既不在 PATH 也找不到同名应用 → 必然失败，提前拦截
            return ""
        return content

    target = _strip_start_flags(match.group("target")).strip()
    if not target:
        return content
    bare_target = target.strip('"').strip()
    if not bare_target:
        return content

    scheme_match = _URL_SCHEME_RE.match(bare_target)
    scheme = scheme_match.group("scheme").lower() if scheme_match else ""

    if scheme and scheme not in _KNOWN_URL_SCHEMES:
        checker = protocol_checker if protocol_checker is not None else _is_registered_protocol
        if checker(scheme):
            return content
        index = _resolve_index(shortcuts, app_paths, app_index)
        candidates = _alias_candidates(scheme, hint, bare_target)
        found = index.find(candidates)
        if found:
            return f'start "" "{found}"'
        return ""

    if scheme in {"http", "https", "file", "ftp", "ftps"}:
        if target.startswith('"'):
            return content
        return f'start "" "{bare_target}"'

    if not scheme and _looks_like_domain(bare_target):
        return f'start "" "https://{bare_target}"'

    low_t = bare_target.lower()
    if low_t in _WINDOWS_BUILTINS or low_t in _CMD_BUILTINS or low_t.startswith("shell:"):
        return content

    path_like = (
        "\\" in bare_target
        or "/" in bare_target
        or re.match(r"^[A-Za-z]:", bare_target) is not None
        or low_t.endswith(_LAUNCHER_EXTENSIONS)
        or low_t.endswith(".exe")
    )
    index = _resolve_index(shortcuts, app_paths, app_index)

    if path_like:
        # 存在性校验：.exe 允许在 PATH 上，其余必须是本机真实文件
        if shutil.which(bare_target) or os.path.exists(bare_target):
            return content
        candidates = _alias_candidates("", hint, bare_target)
        found = index.find(candidates)
        if found:
            return f'start "" "{found}"'
        return ""

    if shutil.which(bare_target):
        return content
    candidates = _alias_candidates("", hint, bare_target)
    found = index.find(candidates)
    if found:
        return f'start "" "{found}"'
    return ""


def _base_name(token: str) -> str:
    """取一个命令名/路径的“裸名”：去引号、去目录、去 .exe/.com 后缀。"""
    name = os.path.basename(token.strip().strip('"')).lower()
    for ext in (".exe", ".com"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return name


def is_launch_command(command: str) -> bool:
    """判断 shell 命令是否属于“打开即结束”的启动类命令。

    启动类命令（打开网页 / 软件 / 快捷方式）是 fire-and-forget 的，本来也没有可读输出；
    其余命令（如 ``powershell -Command ...`` 查询）会打印结果，需要捕获 stdout 回传给 AI。

    判定为启动类的情况：
    - ``start ...``（打开网页 / 软件 / 快捷方式）
    - 纯 GUI 内置程序名（notepad、calc、explorer 等）
    - 以 .lnk / .url / .appref-ms 结尾的快捷方式 / 网址文件
    - 单独出现的交互式外壳（``cmd`` / ``powershell`` / ``python`` 等，开一个窗口给人用）
    """
    raw = (command or "").strip()
    if not raw:
        return False
    if _START_RE.match(raw):
        return True

    parts = raw.split()
    first = parts[0]
    base = _base_name(first)
    if base.endswith(_LAUNCHER_EXTENSIONS):
        return True
    if base in _LAUNCH_ONLY_BUILTINS:
        return True
    if base in _INTERACTIVE_SHELLS and len(parts) == 1:
        return True
    return False


def _decode_shell_output(data: Any) -> str:
    """把子进程输出的字节按本机编码解码，尽量还原可读文本。"""
    if not data:
        return ""
    if isinstance(data, str):
        return data
    encodings = ["utf-8", locale.getpreferredencoding(False), "gbk"]
    for enc in encodings:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _truncate_output(text: str, limit: int = _MAX_SHELL_OUTPUT_CHARS) -> str:
    """截断过长的命令输出，防止把 AI 上下文撑爆。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（输出过长，已截断，仅保留前 {limit} 字符）"


def slugify_command_id(raw: Any, fallback: str = "cmd") -> str:
    """把任意字符串清洗成合法命令 id（仅字母 / 数字 / 下划线）。

    AI 生成的新命令 id 可能带空格、中文或为空；不清洗就直接落库会写出
    ``""`` 这类脏键。清洗后若与期望不同，调用方需要再处理重名。
    """
    text = safe_str(raw)
    if _COMMAND_ID_RE.match(text):
        return text
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_")
    if not slug:
        slug = re.sub(r"[^A-Za-z0-9_]+", "_", safe_str(fallback)).strip("_")
    return slug or "cmd"


def shortlist_commands(
    commands: dict[str, dict[str, Any]],
    user_input: str,
    limit: int = _MATCH_CANDIDATE_LIMIT,
) -> list[dict[str, Any]]:
    """本地预筛与用户输入最相关的候选命令，控制匹配提示词的体积。

    规则（纯本地、确定性，不调用模型）：
    - 命令数不超过 ``limit`` 时全量返回；
    - 否则给每条命令打分：输入分词命中命令的 名称/描述/id/内容 越多分越高，
      名称紧凑匹配（忽略空格与标点）额外加分；
    - 按分数降序取前 ``limit`` 条；全部零分时按原顺序取前 ``limit`` 条，
      保证模型仍然看得到一批候选（实在不匹配它会走 create）。
    """
    items = list(commands.values())
    if len(items) <= limit:
        return items

    compact_input = _compact(user_input)
    tokens = {
        token.lower()
        for token in re.split(r"[^\w\u4e00-\u9fff]+", user_input or "")
        if len(token) >= 2
    }

    def score(cmd: dict[str, Any]) -> int:
        cid = safe_str(cmd.get("id"))
        name = safe_str(cmd.get("name"))
        desc = safe_str(cmd.get("description"))
        content = safe_str(cmd.get("content"))
        blob = _compact(f"{name} {desc} {cid} {content}")
        blob_raw = f"{name} {desc} {cid}".lower()
        points = 0
        for token in tokens:
            if _compact(token) in blob or token.lower() in blob_raw:
                points += 2
        cname = _compact(name)
        if compact_input and cname and (cname in compact_input or compact_input in cname):
            points += 3
        return points

    ranked = sorted(items, key=score, reverse=True)
    if score(ranked[0]) == 0:
        return items[:limit]
    return ranked[:limit]


def format_command_lines(
    commands: dict[str, dict[str, Any]],
    keyword: str = "",
) -> str:
    """把命令库渲染成给用户看的列表文本；``keyword`` 非空时做模糊过滤。"""
    key = _compact(keyword)
    lines = ["📋 已配置命令："]
    shown = 0
    for cmd_id, cmd in commands.items():
        if key:
            blob = _compact(
                f"{cmd.get('name', '')} {cmd.get('description', '')} {cmd_id}"
            )
            if key not in blob:
                continue
        perm = "🔒 admin" if cmd.get("permission") == "admin" else "👤 user"
        risk = safe_str(cmd.get("risk"))
        risk_text = f" 风险:{risk}" if risk else ""
        t = cmd.get("type", "reply")
        lines.append(f"- {cmd.get('name', cmd_id)} ({cmd_id}) [{t}] {perm}{risk_text}")
        shown += 1
    if keyword and not shown:
        return f"没有匹配「{keyword}」的命令，试试 /cmdlist 查看全部。"
    return "\n".join(lines)


class CommandRegistry:
    """命令注册表：加载、保存、执行、权限校验。"""

    def __init__(
        self,
        commands_path: Path,
        admin_password: str = "",
        user_permission: str = "user",
        auto_create: bool = True,
        default_permission: str = "user",
        default_type: str = "reply",
        app_index: Optional["AppIndex"] = None,
        shell_timeout: float = _SHELL_OUTPUT_TIMEOUT,
    ):
        self.commands_path = Path(commands_path)
        self.admin_password = (admin_password or "").strip()
        self.user_permission = user_permission
        self.auto_create = auto_create
        self.default_permission = default_permission
        self.default_type = default_type
        self.app_index = app_index
        self.shell_timeout = max(1.0, float(shell_timeout))
        self.commands: dict[str, dict[str, Any]] = {}
        self._load()

    @property
    def admin_password_valid(self) -> bool:
        return bool(self.admin_password)

    def _load(self) -> None:
        if self.commands_path.exists():
            try:
                with open(self.commands_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    raise ValueError("commands.json 顶层必须是对象")
                self.commands = data
            except Exception:
                self.commands = dict(DEFAULT_COMMANDS)
                self._save()
        else:
            self.commands = dict(DEFAULT_COMMANDS)
            self._save()

    def prune_dirty_commands(self) -> dict[str, Any]:
        """清理本插件命令库里的垃圾脏数据，返回清理报告。

        只作用于本插件自己的 ``commands.json``，绝不触碰插件范围以外的任何文件。
        清理三类：
          - ``invalid``：损坏 / 缺字段 / 字段类型错误的条目；
          - ``placeholder``：占位 / 空壳命令（reply 内容是“请提供…”之类的推脱话术）；
          - ``duplicate``：类型与内容（或名称）完全相同的重复命令。

        返回值示例::

            {"removed": [{"id": "xxx", "reason": "placeholder:占位回复"}],
             "removed_count": 1, "kept": 9}
        """
        removed: list[dict[str, str]] = []
        seen: dict[tuple[str, str, str], str] = {}
        for cmd_id in list(self.commands.keys()):
            reason = _dirty_reason(self.commands.get(cmd_id), cmd_id, seen)
            if reason is None:
                continue
            removed.append({"id": str(cmd_id), "reason": reason})
            del self.commands[cmd_id]
        if removed:
            self._save()
        return {"removed": removed, "removed_count": len(removed), "kept": len(self.commands)}

    def _save(self) -> None:
        self.commands_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.commands_path, "w", encoding="utf-8") as fh:
            json.dump(self.commands, fh, ensure_ascii=False, indent=2)

    def reload(self) -> None:
        self._load()

    def switch_permission(self, password: str) -> bool:
        if not self.admin_password_valid:
            return False
        # 常数时间比较，避免通过响应耗时侧信道猜测密码
        supplied = (password or "").strip().encode("utf-8")
        expected = self.admin_password.encode("utf-8")
        if hmac.compare_digest(supplied, expected):
            self.user_permission = "admin"
            return True
        return False

    def reset_permission(self) -> bool:
        """退出管理员权限，回到 user 状态；返回之前是否处于 admin。"""
        was_admin = (self.user_permission or "user").lower() == "admin"
        self.user_permission = "user"
        return was_admin

    def check_permission(self, required: str, user_permission: Optional[str] = None) -> bool:
        if not self.admin_password_valid:
            return False
        required = required or "user"
        current = (user_permission or self.user_permission or "user").lower()
        if required.lower() == "admin":
            return current == "admin"
        return True

    def apply_risk(self, cmd_id: str, risk: Any) -> bool:
        """把 AI 复审得到的威胁等级写回命令，并据此重算权限。

        这是“自动重审降级”的落点：一条原本 admin 的命令，只要 AI 复审为
        harmless / indeterminate，就会被降级为 user。
        """
        cmd = self.commands.get(cmd_id)
        normalized = normalize_risk(risk)
        if cmd is None or not normalized:
            return False
        cmd["risk"] = normalized
        cmd["permission"] = permission_for_risk(normalized, cmd.get("permission", self.default_permission))
        self._save()
        return True

    def add_command(self, command: dict[str, Any]) -> dict[str, Any]:
        raw_id = safe_str(command.get("id")) or safe_str(command.get("name"))
        cmd_id = slugify_command_id(raw_id)
        if not _COMMAND_ID_RE.match(safe_str(command.get("id"))) and cmd_id in self.commands:
            # 清洗产生的 id 撞上已有命令时加序号，避免误覆盖别人的配置；
            # 显式给出的合法 id 保持"同名覆盖=更新"的原语义。
            base, n = cmd_id, 2
            while cmd_id in self.commands:
                cmd_id = f"{base}_{n}"
                n += 1
        command["id"] = cmd_id
        command.setdefault("name", cmd_id)
        command.setdefault("description", "")
        command.setdefault("type", self.default_type)
        command.setdefault("content", "")
        risk = normalize_risk(command.get("risk"))
        if risk:
            command["risk"] = risk
            command["permission"] = permission_for_risk(risk, self.default_permission)
        else:
            command.setdefault("permission", self.default_permission)
        self.commands[cmd_id] = command
        self._save()
        return command

    def delete_command(self, cmd_id: str) -> bool:
        if cmd_id in self.commands:
            del self.commands[cmd_id]
            self._save()
            return True
        return False

    def execute_command(
        self,
        cmd_id: str,
        user_permission: Optional[str] = None,
        risk: Any = None,
    ) -> dict[str, Any]:
        if cmd_id not in self.commands:
            return {"success": False, "output": f"未找到命令：{cmd_id}"}

        cmd = self.commands[cmd_id]
        stored = str(cmd.get("permission", "user") or "user").lower()
        audit = normalize_risk(risk)
        if audit:
            # 自动重审降级：AI 复审为无害/无法判定时，把原本 admin 的命令降为 user
            if audit != RISK_HARMFUL and stored == "admin":
                self.apply_risk(cmd_id, audit)
            required = permission_for_risk(audit, stored)
        else:
            required = stored
        if not self.check_permission(required, user_permission):
            return {"success": False, "output": "权限不足，需要管理员权限。请先使用 /su 输入密码切换身份。"}

        cmd_type = cmd.get("type", "reply")
        content = cmd.get("content", "")

        if cmd_type == "reply":
            return {"success": True, "output": content}

        if cmd_type == "shell":
            hint = f"{cmd.get('name', '')} {cmd.get('description', '')}".strip()
            resolved = resolve_shell_target(content, hint=hint, app_index=self.app_index)
            if content and resolved == "":
                target_desc = hint or content
                return {
                    "success": False,
                    "output": (
                        f"命令未执行：我在这台电脑上没找到「{target_desc}」对应的真实应用或快捷方式"
                        f"（原始命令：{content}；已搜索开始菜单、商店应用、注册表和常见安装目录）。"
                        "请换个更准确的应用名，"
                        '或改用网页版（把命令内容改成 start "" https://具体网址）。'
                        "确定可用前不要报告成功。"
                    ),
                }
            try:
                if is_launch_command(resolved):
                    # 打开类命令：启动即结束，不需要（也没有）输出
                    subprocess.Popen(resolved, shell=True)
                    if resolved != content:
                        return {
                            "success": True,
                            "output": f"已执行系统命令：{cmd.get('name', cmd_id)}（已自动解析为：{resolved}）",
                        }
                    return {"success": True, "output": f"已执行系统命令：{cmd.get('name', cmd_id)}"}

                # 查询类命令：捕获 stdout/stderr，把结果回传给 AI
                completed = subprocess.run(
                    resolved,
                    shell=True,
                    capture_output=True,
                    timeout=self.shell_timeout,
                )
            except subprocess.TimeoutExpired:
                return {
                    "success": False,
                    "output": f"命令执行超时（{int(self.shell_timeout)} 秒）：{cmd.get('name', cmd_id)}",
                }
            except Exception as exc:
                return {"success": False, "output": f"执行失败：{exc}"}

            stdout = _decode_shell_output(completed.stdout).strip()
            stderr = _decode_shell_output(completed.stderr).strip()
            if completed.returncode != 0 and not stdout:
                detail = stderr or f"退出码 {completed.returncode}"
                return {"success": False, "output": f"命令执行失败：{detail}"}
            text = stdout or stderr
            if not text:
                return {
                    "success": True,
                    "output": f"命令已执行，但没有输出：{cmd.get('name', cmd_id)}",
                }
            return {"success": True, "output": _truncate_output(text)}

        return {"success": False, "output": f"不支持的命令类型：{cmd_type}"}


def build_match_prompt(
    commands: list[dict[str, Any]],
    user_input: str,
    user_permission: str,
    auto_create: bool,
    default_permission: str,
    default_type: str,
) -> str:
    """构造给大模型的命令匹配/创建提示词。

    同时要求模型对匹配到的命令做**独立威胁复审**（risk），用于“自动重审降级”：
    一条原本记成 admin 的无害命令，会被 AI 复审为 harmless 并降为 user。
    """
    return f"""你是自然语言命令路由引擎，同时负责安全审查。

已有命令列表（可能已经过本地预筛，只展示与输入最相关的一部分）：
{json.dumps(commands, ensure_ascii=False, indent=2)}

用户输入：{user_input}
当前用户权限：{user_permission}
是否允许自动创建新命令：{"是" if auto_create else "否"}
新命令默认权限：{default_permission}
新命令默认类型：{default_type}

请严格只返回 JSON，不要任何其他内容、解释或 markdown 代码块。

1. 如果语义匹配到已有命令：
{{"action": "execute", "command_id": "命令ID", "risk": "harmless/indeterminate/harmful"}}

2. 如果未匹配到且允许自动创建，请生成新命令：
{{"action": "create", "new_command": {{
  "id": "英文唯一标识",
  "name": "命令名称",
  "description": "功能描述",
  "risk": "harmless/indeterminate/harmful",
  "type": "reply/shell",
  "content": "回复文本或要执行的命令"
}}}}

3. 如果未匹配到且不允许创建：
{{"action": "not_found"}}

risk 是你对该命令威胁等级的独立审查结果（必须自己判断，不要照抄命令配置里的 permission）：
- harmless：无害。只读、打开软件/网页、文本回复、查询信息，不会改动用户设备资料
- indeterminate：无法或难以分辨是否会对用户设备资料造成影响
- harmful：很可能危害设备或资料，例如删除/修改文件、改注册表、安装卸载、关机重启、执行脚本、读取隐私数据等
- 影响：harmless / indeterminate 的命令在普通 user 状态下即可运行；harmful 的命令必须处于 admin 权限状态

规则：
- 若用户输入明显是新建命令意图（如"帮我加个命令"），优先 create
- id 只能包含字母、数字和下划线，不要带空格
- content 不要有多余解释，reply 就是发给用户的文本，shell 就是完整命令行
- 【重要】shell 命令严禁编造 xxx:// 协议；打开网页用 start "" https://具体网址（必须带 https://）
- 打开软件写 start "" "软件名"，系统会自动在本机查找真实程序；【严禁】自己编造 C:\\...\\xx.exe 或 .lnk 完整路径，路径不存在时会无声失败
- 不要写 cmd /c 前缀，直接写 start；查询类需求用完整命令行（如 powershell -Command "Get-Date"）
"""
