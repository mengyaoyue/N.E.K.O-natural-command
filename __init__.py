"""自然语言命令插件 v0.2（作者：MENGYAOYUE）

用 /自然语言 触发动作：AI 先在命令库里语义匹配，命中即执行；未命中时自动生成
新命令写回 commands.json 长期保存，越用越顺手。支持 reply 文本回复与 shell
系统命令；shell 又区分「打开类」（fire-and-forget）与「查询类」（捕获 stdout
回传结果）。内置无害 / 无法判定 / 有害三档 AI 安全审查，由风险决定所需权限。

v0.2.0：
- 零第三方依赖：LLM 调用改用标准库 urllib，不再依赖 httpx；
- 匹配前先本地预筛候选命令，命令库再大也不会撑爆提示词；
- shell 查询超时（shell_timeout）可配置，输出超长自动截断；
- 新增 /helpcmd 与 /findcmd <关键词>；
- /delcmd 权限门控：删除 admin 级命令需要管理员身份；
- 命令 id 落库前清洗，杜绝空 id / 带空格 id 的脏数据；
- 管理员密码改为常数时间比较。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def _safe_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return default
    return default
from typing import Any, Optional

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    plugin_entry,
    neko_plugin,
)

from ._panel import PanelServer, find_open_port
from ._command_logic import (
    CommandRegistry,
    DEFAULT_ENTRY_REGISTRY,
    build_capability_section,
    build_create_prompt,
    build_endpoint_candidates,
    build_match_prompt,
    discover_installed_plugins,
    extract_json_object,
    extract_new_command,
    format_command_lines,
    format_plugin_result,
    harvest_entries_from_commands,
    is_exit_admin,
    load_entry_registry,
    load_settings,
    merge_entry_registry,
    normalize_action,
    parse_direct_call_args,
    parse_user_input,
    refresh_builtin_examples,
    render_entry_list,
    render_plugin_list,
    safe_str as _safe_str,
    parse_video_id,
    shortlist_commands,
)

from ._deep_search_logic import (
    build_final_report,
    build_page_prompt,
    build_select_prompt,
    cross_check_codes,
    decode_page_body,
    extract_code_candidates,
    html_to_text,
    parse_page_analysis,
    parse_search_cards,
    parse_selection,
)

_PLUGIN_ID = "neko_natural_command"

_RUN_COMMAND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "用户完整输入，例如 '/ 打开记事本' 或 '/问候'",
        },
    },
    "required": ["text"],
}

_HELP_TEXT = (
    "📖 自然语言命令速查喵～\n"
    "- /<任意自然语言>：AI 先在命令库匹配，命中即执行；未命中会自动学一条新命令\n"
    "- 带参数的命令（如「B站搜索 原神」）：搜索词会自动提取，下次任何关键词都能直接用\n"
    "- 「查一查/搜一下」类需求会调用其它插件的能力（联网搜索/运势/陪看…）\n"
    "- /插件：查看本机插件与可直接调用的能力；/调用 插件id:入口id 参数=值：直调插件能力\n"
    "- /cmdlist：列出全部命令；/findcmd <关键词>：按关键词筛选命令\n"
    "- /delcmd <命令ID>：删除命令（admin 级命令需先提权）\n"
    "- /su <密码>：切换管理员权限；/退出管理员 等同义命令可降权\n"
    "- /reloadcmd：重新加载命令库与插件能力表\n"
    "安全：命令分 harmless / indeterminate / harmful 三档，只有 harmful 需要 /su 提权后执行喵。"
)


def _build_endpoint_candidates(base_url: str, model: str) -> list[str]:
    return build_endpoint_candidates(base_url, model)


def _http_post_json(
    endpoint: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> tuple[int, str]:
    """标准库 POST JSON：返回 ``(状态码, 响应文本)``；网络层错误返回 ``(0, 错误信息)``。

    放在模块顶层便于独立测试；由 ``asyncio.to_thread`` 调度，不阻塞事件循环。
    """
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        return exc.code, detail or str(exc)
    except Exception as exc:
        return 0, str(exc)


@neko_plugin
class NaturalCommandPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger

        data_dir = Path(self.data_path())
        self.commands_path = data_dir / "commands.json"

        # 配置在异步 startup 中由 self.config.dump() 注入
        self.admin_password: str = ""
        self.auto_create: bool = True
        self.auto_clean: bool = True
        self.default_permission: str = "user"
        self.default_type: str = "reply"
        self.llm_timeout: float = 20.0
        self.shell_timeout: float = 30.0
        self.request_timeout: float = 20.0
        self.direct_call_permission: str = "user"
        self.deep_search_max_pages: int = 4
        self.deep_search_progress: bool = True
        self.catgirl_name: str = "猫娘"
        self._config_loaded: bool = False

        # 插件互联（v0.4）：能力注册表 + 已安装插件清单 → 注入匹配提示词
        self._entries: list[dict[str, Any]] = []
        self._installed: list[dict[str, Any]] = []
        self._capability_text: str = ""
        self.plugins_dir: Optional[Path] = None
        self._panel_server = None
        self._panel_port: int = 15680
        self._panel_lock = threading.Lock()
        self.entry_registry_path: Optional[Path] = None

        self.registry = CommandRegistry(
            commands_path=self.commands_path,
            admin_password=self.admin_password,
            auto_create=self.auto_create,
            default_permission=self.default_permission,
            default_type=self.default_type,
        )

    # ── 配置加载 ───────────────────────────────────────────────
    async def _load_config(self) -> None:
        """从 plugin.toml / profile 读取插件配置段。

        SDK 的 ``self.config.dump()`` 返回整个 plugin.toml 的字典，
        本插件配置位于 ``[neko_natural_command]`` 段。
        """
        try:
            cfg = await self.config.dump(timeout=5.0)
        except Exception as exc:
            self.logger.warning("[natural_command] 读取配置失败：%s", exc)
            return

        cfg = cfg if isinstance(cfg, dict) else {}
        settings = load_settings(cfg.get(_PLUGIN_ID))

        self.admin_password = settings["admin_password"]
        self.auto_create = settings["auto_create"]
        self.auto_clean = settings["auto_clean"]
        self.default_permission = settings["default_permission"]
        self.default_type = settings["default_type"]
        self.llm_timeout = settings["llm_timeout"]
        self.shell_timeout = settings["shell_timeout"]
        self.request_timeout = settings["request_timeout"]

        self.registry.admin_password = self.admin_password
        self.registry.auto_create = self.auto_create
        self.registry.default_permission = self.default_permission
        self.registry.default_type = self.default_type
        self.registry.shell_timeout = self.shell_timeout
        self.direct_call_permission = settings["direct_call_permission"]
        self.deep_search_max_pages = settings["deep_search_max_pages"]
        self.deep_search_progress = settings["deep_search_progress"]
        self.catgirl_name = settings["catgirl_name"]
        self._config_loaded = True
        try:
            override = self._load_panel_state().get("auto_create")
            if override is not None:
                self.auto_create = bool(override)
                self.registry.auto_create = bool(override)
        except Exception:
            pass
        self._probe_context()

        # 插件互联：数据目录 / 平级插件目录在拿到 data_path 后初始化一次
        if self.entry_registry_path is None:
            data_dir = Path(self.data_path())
            self.entry_registry_path = data_dir / "plugin_links.json"
            self.plugins_dir = data_dir.parent.parent
        self._refresh_capabilities()

    async def _ensure_config_loaded(self) -> None:
        if not self._config_loaded:
            await self._load_config()

    # ── 插件互联（v0.4）────────────────────────────────────────
    async def _call_entry(self, target: str, args: Optional[dict[str, Any]] = None, timeout: float = 30.0) -> Any:
        """跨插件调用：兼容本宿主 SDK 的多种 API 形态。

        当前宿主（SdkContext）实测可用的跨插件入口是
        ctx.trigger_plugin_event(target_plugin_id=…, event_type="adapter_call",
                                 event_id=…, params=…, timeout=…)，
        与官方 mcp_adapter 插件用法一致；它可能返回协程，也可能直接返回结果，
        这里两种都兼容。ctx.plugins.call_entry / ctx.call_plugin_entry 等旧写法
        在当前版本并不存在，只作为兼容回退。
        按优先级探测，成功一种后不再尝试其它形态。
        """
        plugin_id, _, entry_id = target.partition(":")
        args = args or {}
        attempts: list[tuple[str, Any]] = []
        ctx = getattr(self, "ctx", None)
        if ctx is not None:
            if hasattr(ctx, "trigger_plugin_event"):
                attempts.append(
                    (
                        "ctx.trigger_plugin_event",
                        lambda: ctx.trigger_plugin_event(
                            target_plugin_id=plugin_id,
                            event_type="adapter_call",
                            event_id=entry_id,
                            params=dict(args),
                            timeout=float(timeout),
                        ),
                    )
                )
            plugins = getattr(ctx, "plugins", None)
            if plugins is not None and hasattr(plugins, "call_entry"):
                attempts.append(("ctx.plugins.call_entry", lambda: plugins.call_entry(target, args)))
            if hasattr(ctx, "call_plugin_entry"):
                attempts.append(("ctx.call_plugin_entry", lambda: ctx.call_plugin_entry(plugin_id, entry_id, args, timeout)))
            bus = getattr(ctx, "bus", None)
            if bus is not None and hasattr(bus, "call_plugin_entry"):
                attempts.append(("ctx.bus.call_plugin_entry", lambda: bus.call_plugin_entry(plugin_id, entry_id, args, timeout)))
        if hasattr(self, "call_plugin_entry"):
            attempts.append(("self.call_plugin_entry", lambda: self.call_plugin_entry(plugin_id, entry_id, args, timeout)))

        last_error: Optional[Exception] = None
        for name, call in attempts:
            try:
                result = call()
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=timeout)
                return result
            except (AttributeError, TypeError) as exc:
                last_error = exc
                self.logger.warning("[natural_command] 跨插件调用形态 %s 不可用: %s", name, exc)
                continue
            except asyncio.TimeoutError:
                raise SdkError(f"插件能力 [{target}] 响应超时（{int(timeout)} 秒）。")
        raise SdkError(
            f"跨插件调用不可用喵：当前宿主 SDK 没有可用的调用入口（试过 {len(attempts)} 种形态）。最后错误：{last_error}"
        )

    def _panel_state_path(self) -> Path:
        return Path(self.data_path()) / "panel_state.json"

    def _load_panel_state(self) -> dict:
        try:
            data = json.loads(self._panel_state_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_panel_state(self, state: dict) -> None:
        try:
            self._panel_state_path().parent.mkdir(parents=True, exist_ok=True)
            self._panel_state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _start_panel(self) -> None:
        """启动管理面板（仅 127.0.0.1）并注册 static UI。"""
        endpoints = {
            ("GET", "/api/status"): self._panel_status,
            ("POST", "/api/reload"): self._panel_reload,
            ("POST", "/api/toggle_auto_create"): self._panel_toggle_auto_create,
        }
        server = PanelServer(self._panel_port, self._panel_html, endpoints)
        if server.start():
            self._panel_server = server
            self.logger.info("[natural_command] 管理面板已启动: http://127.0.0.1:{}", self._panel_port)
            try:
                registered = self.register_static_ui("static")
                self.logger.info("[natural_command] static UI 注册: {}", registered)
            except Exception as exc:
                self.logger.warning("[natural_command] static UI 注册失败: {}", exc)
        else:
            self.logger.warning("[natural_command] 管理面板启动失败（端口占用）")

    def _panel_status(self, _body: dict) -> dict:
        with self._panel_lock:
            model_info = {}
            try:
                from utils.config_manager import get_config_manager
                cfg = get_config_manager().get_model_api_config("conversation")
                model_info = {"model": _safe_str(cfg.get("model")), "base_url": _safe_str(cfg.get("base_url"))}
            except Exception:
                model_info = {"model": "（未配置）", "base_url": ""}
            state = self._load_panel_state()
            auto_create = bool(state.get("auto_create", self.auto_create))
            return {
                "admin_password_set": bool(self.admin_password),
                "auto_create": auto_create,
                "command_count": len(self.registry.commands),
                "commands": [
                    {"id": cid, "name": c.get("name", cid), "type": str(c.get("type", "reply")),
                     "permission": str(c.get("permission", "user"))}
                    for cid, c in self.registry.commands.items()
                ],
                "entries": [{"id": e["id"], "desc": e.get("desc", "")} for e in self._entries],
                "installed_plugins": [p["id"] for p in self._installed],
                "user_permission": self.registry.user_permission,
                "model": model_info,
                "panel_port": self._panel_port,
            }

    def _panel_reload(self, _body: dict) -> dict:
        self.registry.reload()
        self._refresh_capabilities()
        return {"ok": True, "command_count": len(self.registry.commands)}

    def _panel_toggle_auto_create(self, body: dict) -> dict:
        enabled = bool(body.get("enabled"))
        self.auto_create = enabled
        self.registry.auto_create = enabled
        state = self._load_panel_state()
        state["auto_create"] = enabled
        self._save_panel_state(state)
        return {"ok": True, "auto_create": enabled}

    def _panel_html(self) -> str:
        page = Path(__file__).parent / "static" / "index.html"
        try:
            return page.read_text(encoding="utf-8")
        except Exception:
            return "<h1>面板页缺失喵（static/index.html）</h1>"

    def _probe_context(self) -> None:
        """把 ctx 的真实属性写进日志，便于确认跨插件 API 形态。"""
        ctx = getattr(self, "ctx", None)
        if ctx is None:
            self.logger.warning("[natural_command] ctx 为空，无法探测 SDK 能力")
            return
        attrs = [a for a in dir(ctx) if not a.startswith("_")]
        self.logger.info("[natural_command] ctx(%s) 属性: %s", type(ctx).__name__, ", ".join(attrs[:40]))
        for name in ("plugins", "bus", "call_plugin_entry"):
            obj = getattr(ctx, name, None)
            if obj is not None:
                self.logger.info("[natural_command] ctx.%s -> %s", name, type(obj).__name__)

    # ── 插件互联占位 ──────────────────────────────────────────
    def _refresh_capabilities(self) -> None:
        """重建能力注册表与已安装插件清单，并渲染成提示词文本块。

        数据来源三层合并（用户 plugin_links.json > 精选默认 > 命令库收割）；
        已安装插件只读平级目录的 plugin.toml 清单，绝不改动其它插件。
        任何失败都不影响插件主流程。
        """
        try:
            harvested = harvest_entries_from_commands(self.registry.commands)
            curated = list(DEFAULT_ENTRY_REGISTRY)
            user = load_entry_registry(self.entry_registry_path) if self.entry_registry_path else []
            self._entries = merge_entry_registry(curated, harvested, user)
        except Exception as exc:
            self.logger.warning("[natural_command] 能力注册表构建失败：%s", exc)
            self._entries = list(DEFAULT_ENTRY_REGISTRY)
        try:
            self._installed = discover_installed_plugins(
                str(self.plugins_dir) if self.plugins_dir else None, _PLUGIN_ID
            )
        except Exception as exc:
            self.logger.warning("[natural_command] 已安装插件扫描失败：%s", exc)
            self._installed = []
        self._capability_text = build_capability_section(self._entries, self._installed)
        self.logger.info(
            "[natural_command] 能力表已刷新：%d 个可调用入口，%d 个已安装插件",
            len(self._entries), len(self._installed),
        )

    def _plugin_overview(self) -> str:
        return render_entry_list(self._entries) + "\n\n" + render_plugin_list(self._installed)

    # ── 生命周期 ───────────────────────────────────────────────
    @lifecycle(id="startup")
    async def on_startup(self) -> None:
        await self._load_config()
        if not self.admin_password:
            self.logger.warning(
                "[natural_command] 未设置管理员密码，run_command 将保持禁用。"
                "请在 plugin.toml 的 [%s] 段配置 admin_password。",
                _PLUGIN_ID,
            )
        self._auto_clean_commands()
        self._start_panel()
        try:
            healed = refresh_builtin_examples(self.registry.commands)
            if healed:
                self.registry._save()
                self.logger.info("[natural_command] 内置示例命令文案已自愈：%s", ", ".join(healed))
        except Exception as exc:
            self.logger.warning("[natural_command] 示例命令自愈失败：%s", exc)
        self.logger.info(
            "[natural_command] 启动，已加载 %d 条命令（admin_password=%s, auto_create=%s, "
            "auto_clean=%s, default_permission=%s, default_type=%s）",
            len(self.registry.commands),
            "已设置" if self.admin_password else "未设置",
            self.auto_create,
            self.auto_clean,
            self.default_permission,
            self.default_type,
        )

    def _auto_clean_commands(self) -> None:
        """启动时清理本插件命令库里的垃圾脏数据。

        严格限定在本插件自己的 ``data/commands.json`` 范围内，
        不会读写插件目录以外的任何文件；清理失败也绝不能影响插件启动。
        """
        if not self.auto_clean:
            return
        try:
            report = self.registry.prune_dirty_commands()
        except Exception as exc:
            self.logger.warning("[natural_command] 启动清理失败：%s", exc)
            return
        removed = report.get("removed") or []
        if removed:
            detail = ", ".join(f"{item['id']}({item['reason']})" for item in removed)
            self.logger.info(
                "[natural_command] 启动清理：移除 %d 条脏数据 → %s",
                len(removed),
                detail,
            )
        else:
            self.logger.info("[natural_command] 启动清理：未发现需要清理的脏数据")

    @lifecycle(id="shutdown")
    async def on_shutdown(self) -> None:
        if self._panel_server:
            self._panel_server.stop()
        self.registry._save()
        self.logger.info("[natural_command] 关闭")

    # ── LLM 调用 ────────────────────────────────────────────────
    async def _call_llm_json(self, system: str, user: str) -> dict[str, Any]:
        from utils.config_manager import get_config_manager

        cfg = get_config_manager().get_model_api_config("conversation")
        model = _safe_str(cfg.get("model"))
        base_url = _safe_str(cfg.get("base_url"))
        api_key = _safe_str(cfg.get("api_key"))

        if not model or not base_url or not api_key:
            raise SdkError("N.E.K.O 尚未配置对话模型，无法解析自然语言命令。")

        self.logger.info(
            "[natural_command] 调用模型 model=%s base_url=%s api_key=%s timeout=%s",
            model,
            base_url,
            "已设置" if api_key else "未设置",
            self.llm_timeout,
        )

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }

        endpoints = _build_endpoint_candidates(base_url, model)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "x-api-key": api_key,
        }
        last_text = ""
        for endpoint in endpoints:
            body = payload
            if ":generateContent" in endpoint:
                body = {
                    "contents": [
                        {"role": "user", "parts": [{"text": system + "\n" + user}]}
                    ],
                }
            status, text = await asyncio.to_thread(
                _http_post_json, endpoint, headers, body, self.llm_timeout
            )
            last_text = text
            self.logger.info(
                "[natural_command] endpoint=%s status=%s body=%s",
                endpoint,
                status,
                last_text[:500],
            )
            if status == 200:
                try:
                    data = json.loads(last_text)
                    if ":generateContent" in endpoint:
                        text_out = data["candidates"][0]["content"]["parts"][0]["text"]
                    else:
                        text_out = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, ValueError):
                    continue

                self.logger.info("[natural_command] 模型原始返回：%s", text_out[:500])
                raw_json = extract_json_object(text_out)
                if not raw_json:
                    continue
                try:
                    return json.loads(raw_json)
                except json.JSONDecodeError:
                    continue
        raise SdkError(f"模型返回无法解析为 JSON：{last_text[:200]}")

    async def _match_command(self, user_input: str, user_permission: str) -> dict[str, Any]:
        # 本地预筛：只把与输入最相关的一批候选发给模型，命令库大了也不会撑爆提示词
        candidates = shortlist_commands(self.registry.commands, user_input)
        prompt = build_match_prompt(
            commands=candidates,
            user_input=user_input,
            user_permission=user_permission,
            auto_create=self.auto_create,
            default_permission=self.default_permission,
            default_type=self.default_type,
            capability_text=self._capability_text,
        )
        return await self._call_llm_json("你是一个命令路由引擎。", prompt)

    # ── 核心入口：处理 /命令 ────────────────────────────────────
    @llm_tool(
        name="neko_run_command",
        description=(
            "当用户发送以 / 开头的自然语言命令时调用。"
            "例如用户输入 '/ 打开记事本'、'/问候'、'/cmdlist'、'/退出admin权限'。"
            "AI 会先语义匹配已有命令配置；未命中且允许时自动创建新命令并保存，然后执行。"
            "同时会对命令做威胁审查：无害/无法判定的命令普通状态即可运行（无需提权），"
            "只有可能造成危害的命令才需要在 /su 提权后的管理员状态下执行。"
        ),
        parameters=_RUN_COMMAND_SCHEMA,
        timeout=60.0,
    )
    @plugin_entry(
        id="run_command",
        name="执行自然语言命令",
        description=(
            "当用户发送 / + 自然语言时调用。AI 会先语义匹配已有的命令配置；"
            "若未命中且配置允许，会自动生成新命令并保存到 commands.json，然后执行。"
            "AI 会自动审查命令威胁等级：无害/无法判定的命令 user 状态即可执行，"
            "可能造成危害的命令需要先 /su 提权；发送语义为“退出管理员权限”的 /命令可回到 user。"
            "支持 reply 文本回复和 shell 系统命令。"
        ),
        input_schema=_RUN_COMMAND_SCHEMA,
        llm_result_fields=["result"],
    )
    async def run_command(self, text: str = "", **kwargs):
        await self._ensure_config_loaded()
        if not self.admin_password:
            return Err(SdkError("管理员密码未设置，自然语言命令插件已禁用。请在 plugin.toml 中配置 admin_password。"))

        had_prefix = text.strip().startswith("/")
        user_input = parse_user_input(text)
        if not user_input:
            return Err(SdkError("命令内容为空"))

        # 降权出口：/ + 语义为“退出管理员权限”的自然语言
        if is_exit_admin(user_input):
            return Ok(self._exit_admin())

        # admin 权限只在带 / 前缀时生效；不带 / 的自然语言一律按 user 级别处理（与 user 同级），
        # 因此提权不会让普通聊天/自然语言获得管理员能力。
        allow_admin = had_prefix
        effective = self.registry.user_permission if allow_admin else "user"

        # 内置管理命令走快捷路径
        builtin = user_input.split()[0].lower()
        if builtin == "cmdlist":
            return Ok(self._list_commands())
        if builtin == "findcmd":
            rest = user_input[len("findcmd"):].strip()
            return Ok(self._list_commands(keyword=rest))
        if builtin == "helpcmd":
            return Ok(_HELP_TEXT)
        if builtin in ("delcmd",):
            rest = user_input[len("delcmd"):].strip()
            return Ok(self._delete_command(rest, permission=effective))
        if builtin == "su":
            rest = user_input[len("su"):].strip()
            return Ok(self._switch_permission(rest))
        if builtin == "reloadcmd":
            self.registry.reload()
            self._refresh_capabilities()
            return Ok("已刷新命令配置（含插件能力表）")
        # 插件互联：查看能力 / 直调其它插件
        if builtin in ("plugin", "插件", "pluginlist", "插件列表"):
            return Ok(self._plugin_overview())
        if builtin in ("调用", "call"):
            return await self._direct_plugin_call(user_input[len(builtin):].strip(), effective)
        if builtin in ("深搜", "deepsearch", "deep_search"):
            query = user_input[len(builtin):].strip()
            try:
                return Ok(await self._run_deep_search(query))
            except SdkError as exc:
                return Err(exc)
            except Exception as exc:
                self.logger.exception("深搜异常: %s", exc)
                return Err(SdkError(f"深搜出错了喵：{exc}"))

        # AI 语义匹配或自动创建（威胁审查 risk 与匹配/创建并入同一轮，不额外调用模型）
        try:
            result = await self._match_command(user_input, effective)
        except SdkError as exc:
            return Err(exc)
        except Exception as exc:
            self.logger.exception("命令匹配异常: %s", exc)
            return Err(SdkError(f"命令匹配失败：{exc}"))

        action = normalize_action(result)
        self.logger.info("[natural_command] 模型判定 action=%s result=%s", action, result)

        if action == "execute":
            cmd_id = _safe_str(result.get("command_id"))
            if not cmd_id:
                return Err(SdkError("AI 未返回 command_id"))
            return await self._execute_command(
                cmd_id, risk=result.get("risk"), allow_admin=allow_admin,
                args=result.get("args"),
            )

        if action == "need_args":
            return Ok(self._need_args_message(result))

        if action == "deep_search":
            query = _safe_str(result.get("query")) or user_input
            try:
                return Ok(await self._run_deep_search(query))
            except SdkError as exc:
                return Err(exc)
            except Exception as exc:
                self.logger.exception("深搜异常: %s", exc)
                return Err(SdkError(f"深搜出错了喵：{exc}"))

        new_cmd = extract_new_command(result)

        # 未给出可用的新命令（含 not_found / 格式错误）：只要允许就补一次生成
        if not isinstance(new_cmd, dict) or not new_cmd.get("id"):
            if not self.auto_create:
                return Err(SdkError("未识别该命令，且未开启自动创建。"))
            new_cmd = await self._generate_new_command(user_input)

        if not isinstance(new_cmd, dict) or not new_cmd.get("id"):
            return Err(SdkError("未能生成有效的命令配置。"))

        # 创建时模型同时给出本次执行用的参数值（顶层 args）
        return await self._create_and_run(
            new_cmd, allow_admin=allow_admin, args=result.get("args") if result else None
        )

    def _need_args_message(self, result: dict[str, Any]) -> str:
        """need_args：用模型给的 ask 文案向用户要参数；没有就用缺参列表兜底。"""
        ask = _safe_str(result.get("ask"))
        if ask:
            return ask
        missing = result.get("missing")
        if isinstance(missing, list) and missing:
            return "喵？还差一些信息呢：" + "、".join(str(m) for m in missing) + "。告诉本喵就好～"
        return "喵？还差一些信息呢，请补充完整再试～"

    async def _execute_command(
        self,
        cmd_id: str,
        risk: Any = None,
        allow_admin: bool = True,
        args: Any = None,
    ):
        effective = self.registry.user_permission if allow_admin else "user"
        exec_result = await self._dispatch_command(cmd_id, effective, risk=risk, args=args)
        if exec_result["success"]:
            return Ok(exec_result["output"])
        return Err(SdkError(exec_result["output"]))

    async def _dispatch_command(
        self,
        cmd_id: str,
        effective: str,
        risk: Any = None,
        args: Any = None,
    ) -> dict[str, Any]:
        """权限预检 + 按类型分发执行，统一返回 {"success","output"} 字典。"""
        allowed, deny_reason, cmd = self.registry.authorize_command(cmd_id, effective, risk)
        if not allowed or cmd is None:
            return {"success": False, "output": deny_reason or f"未找到命令：{cmd_id}"}
        if str(cmd.get("type", "")).lower() == "plugin":
            return await self._run_plugin_command(cmd, args)
        return self.registry.execute_command(cmd_id, user_permission=effective, risk=risk, args=args)

    async def _run_plugin_command(self, cmd: dict[str, Any], args: Any) -> dict[str, Any]:
        """执行 type=plugin 命令：调用其他 N.E.K.O 插件暴露的入口。"""
        target = _safe_str(cmd.get("content"))
        if ":" not in target:
            return {"success": False, "output": (
                f"插件命令的 content 应写成 '插件id:入口id'，当前是：{target}。"
                "请修正这条命令或重新创建。"
            )}
        call_args = {
            k: v for k, v in (args or {}).items()
            if isinstance(k, str) and isinstance(v, (str, int, float, bool, list, dict))
        }
        self.logger.info("[natural_command] 调用插件能力 target=%s args=%s", target, call_args)
        try:
            result = await self._call_entry(target, call_args, timeout=self.shell_timeout)
        except asyncio.TimeoutError:
            return {"success": False, "output": f"喵呜…插件能力 [{target}] 响应超时（{int(self.shell_timeout)} 秒）。"}
        except Exception as exc:
            self.logger.exception("call_entry 失败 target=%s: %s", target, exc)
            return {"success": False, "output": (
                f"调用插件能力 [{target}] 失败：{exc}。"
                "可能该插件未安装或未启用，可以先 /cmdlist 看看本机有哪些命令。"
            )}
        return {"success": True, "output": format_plugin_result(target, result)}

    # ── 深度搜索代理（v0.5）：搜索 → 筛选 → 进页面 → 核实 → 作答 ──
    async def _deep_fetch_text(self, url: str) -> str:
        """抓取网页正文；B站视频页走 API 拿简介+热评（HTML 是 JS 渲染的拿不到）。"""
        if "bilibili.com/video/" in url or url.startswith("BV"):
            return await self._bilibili_video_text(url)

        def _get() -> tuple[int, bytes, str]:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
                method="GET",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.request_timeout) as resp:
                    return resp.status, resp.read(), resp.headers.get("Content-Type", "")
            except Exception:
                return 0, b"", ""

        status, body, ctype = await asyncio.to_thread(_get)
        text = ""
        if status == 200 and body:
            text = html_to_text(decode_page_body(body, ctype))
        if len(text.strip()) >= 500:
            return text
        # 静态不足（JS 渲染 / 反爬 403 / 空壳）→ 交给深读卫星用真浏览器渲染
        satellite_text = await self._satellite_read(url)
        if len(satellite_text.strip()) > len(text.strip()):
            return satellite_text
        return text

    async def _satellite_read(self, url: str) -> str:
        """通过深读卫星插件用真浏览器读取页面（附属插件未装/失败时静默回退）。"""
        try:
            raw = await self._call_entry(
                "neko_deep_fetch:read_page", {"url": url, "force_browser": True}, timeout=self.request_timeout
            )
        except Exception as exc:
            self.logger.info("[deep_search] 卫星读页不可用: %s", exc)
            return ""
        report = raw
        if isinstance(report, dict) and isinstance(report.get("result"), dict):
            report = report["result"]
        if isinstance(report, dict):
            return str(report.get("text") or "")
        return ""

    async def _satellite_bing_cards(self, query: str) -> list[dict[str, Any]]:
        """通过深读卫星用真浏览器做 Bing 搜索，返回深搜卡片。"""
        try:
            raw = await self._call_entry(
                "neko_deep_fetch:bing_search", {"query": query, "max_results": 8}, timeout=self.request_timeout
            )
        except Exception as exc:
            self.logger.info("[deep_search] 卫星 Bing 不可用: %s", exc)
            return []
        payload = raw
        if isinstance(payload, dict) and "result" in payload:
            payload = payload["result"]
        if not isinstance(payload, dict):
            return []
        cards: list[dict[str, Any]] = []
        for item in payload.get("results") or []:
            if isinstance(item, dict) and _safe_str(item.get("url")):
                cards.append(
                    {
                        "title": _safe_str(item.get("title")),
                        "url": _safe_str(item.get("url")),
                        "desc": "",
                    }
                )
        return cards

    async def _bili_search_cards(self, query: str) -> list[dict[str, Any]]:
        """B站搜索（匿名）：把视频结果转成深搜卡片（兑换码视频是重要来源）。"""
        from urllib.parse import quote

        cookie = await self._get_bili_cookie()
        url = (
            "https://api.bilibili.com/x/web-interface/search/type"
            f"?search_type=video&keyword={quote(query)}&page_size=8"
        )
        data = await asyncio.to_thread(self._bili_get_json, url, cookie)
        if not data or data.get("code") != 0:
            self.logger.warning("[deep_search] B站搜索 code=%s", (data or {}).get("code"))
            return []
        cards: list[dict[str, Any]] = []
        for item in (data.get("data") or {}).get("result") or []:
            if not isinstance(item, dict) or not item.get("bvid"):
                continue
            title = _safe_str(item.get("title"))
            description = _safe_str(item.get("description"))
            cards.append({
                "title": re.sub(r"<[^>]+>", "", title),
                "url": f"https://www.bilibili.com/video/{item['bvid']}",
                "desc": re.sub(r"<[^>]+>", "", description)[:200],
            })
        return cards

    async def _bilibili_video_text(self, url: str) -> str:
        """B站视频页：用 view API 拿简介、reply API 拿热评（兑换码常藏在里面）。"""
        video_id = parse_video_id(url)
        if not video_id or "bvid" not in video_id:
            return ""
        try:
            info = await self._fetch_bili_video(video_id["bvid"])
        except Exception:
            return ""
        parts = [str(info.get("title") or ""), str(info.get("desc") or "")]
        aid = int(info.get("aid") or 0)
        if aid:
            try:
                cookie = await self._get_bili_cookie()
                data = await asyncio.to_thread(
                    self._bili_get_json,
                    f"https://api.bilibili.com/x/v2/reply/main?type=1&oid={aid}&mode=3",
                    cookie,
                )
            except Exception:
                data = None
            if data and data.get("code") == 0:
                for reply in (data.get("data") or {}).get("replies") or []:
                    if isinstance(reply, dict):
                        msg = _safe_str((reply.get("content") or {}).get("message"))
                        if msg:
                            parts.append(msg)
        return html_to_text("\n".join(p for p in parts if p))

    # B站匿名访问层（自包含，不依赖其它插件）
    _BILI_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    def _bili_get(self, url: str, cookie: str = "") -> tuple[int, bytes]:
        headers = dict(self._BILI_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            self.logger.warning("[natural_command] B站请求失败 status={} url={}", exc.code, url[:90])
            return exc.code, b""
        except Exception as exc:
            self.logger.warning("[natural_command] B站请求异常: {} url={}", exc, url[:90])
            return 0, b""

    def _bili_get_json(self, url: str, cookie: str = "") -> Optional[dict[str, Any]]:
        status, body = self._bili_get(url, cookie)
        if status != 200 or not body:
            return None
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    async def _get_bili_cookie(self) -> str:
        """匿名获取 buvid3（B站风控要求的最低限度 cookie）。"""
        try:
            data = await asyncio.to_thread(
                self._bili_get_json, "https://api.bilibili.com/x/frontend/finger/spi"
            )
        except Exception:
            data = None
        if data and data.get("code") == 0:
            b3 = _safe_str((data.get("data") or {}).get("b_3"))
            if b3:
                return f"buvid3={b3}"
        return ""

    async def _fetch_bili_video(self, bvid: str) -> dict[str, Any]:
        """读取视频信息（标题/简介/aid/时长）。失败抛 SdkError。"""
        cookie = await self._get_bili_cookie()
        data = await asyncio.to_thread(
            self._bili_get_json,
            f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
            cookie,
        )
        if not data or data.get("code") != 0:
            raise SdkError(f"视频信息没拿到喵（接口码 {(data or {}).get('code')}）。")
        v = data.get("data") or {}
        return {
            "bvid": _safe_str(v.get("bvid")),
            "aid": _safe_int(v.get("aid"), 0),
            "title": _safe_str(v.get("title"), "未知标题"),
            "desc": _safe_str(v.get("desc")),
            "duration": _safe_int(v.get("duration"), 0),
        }

    async def _run_deep_search(self, query: str, max_pages: Optional[int] = None) -> str:
        await self._ensure_config_loaded()
        query = _safe_str(query)
        if not query:
            return "想让我深搜什么喵？/深搜 <问题>，比如：/深搜 原神最新直播兑换码"
        limit = max(1, min(int(max_pages or self.deep_search_max_pages), 6))

        # 1) 搜索：anysearch + B站搜索 双来源（互为备份，合并去重）
        search_notes: list[str] = []
        cards: list[dict[str, Any]] = []
        try:
            raw = await self._call_entry(
                "anysearch:search", {"query": query, "max_results": 10}, timeout=self.request_timeout
            )
            search_text = raw.get("result") if isinstance(raw, dict) else str(raw)
            cards = parse_search_cards(search_text or "")
        except Exception as exc:
            self.logger.warning("[deep_search] anysearch 失败: %s", exc)
            search_notes.append("anysearch 调用失败")
        try:
            bili_cards = await self._bili_search_cards(query)
        except Exception as exc:
            self.logger.warning("[deep_search] B站搜索失败: %s", exc)
            bili_cards = []
            search_notes.append("B站搜索失败")
        # 合并去重（按 URL），统一重编号——筛选解析按编号回查，必须连续
        seen_urls: set[str] = set()
        merged: list[dict[str, Any]] = []
        for card in cards + bili_cards:
            url = str(card.get("url") or "").rstrip("/")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            merged.append(card)
        cards = [{**card, "index": i} for i, card in enumerate(merged, 1)]
        if not cards:
            # 常规双源全挂（anysearch 异常 / B站风控）→ 先让卫星用真浏览器 Bing 补一轮
            try:
                cards = await self._satellite_bing_cards(query)
                if cards:
                    search_notes.append("已切换真浏览器搜索")
            except Exception as exc:
                self.logger.warning("[deep_search] 卫星 Bing 补源失败: %s", exc)
        if not cards:
            note_text = f"（{'；'.join(search_notes)}）" if search_notes else ""
            return (
                f"呜…搜索这一步就没走通喵{note_text}。"
                "本喵拿不到结果列表，没法帮你进页面核实。请检查网络后稍后再试。"
            )

        # 2) 筛选：按标题/摘要挑最可能藏答案的页面
        select_prompt = build_select_prompt(query, cards)
        selected, direct = [], ""
        try:
            select_raw = await self._call_llm_json("你是搜索代理的筛选引擎。", select_prompt)
            selected, direct = parse_selection(select_raw, cards)
        except SdkError as exc:
            self.logger.warning("[deep_search] 筛选失败: %s", exc)
            return str(exc)
        if direct:
            return f"不用翻页面喵，搜索摘要里就有答案：\n{direct}\n（来源：{cards[0].get('url', '')}）"
        if not selected:
            return "搜了一圈，标题和摘要看起来都不对劲喵，本喵没敢乱翻。换个更具体的关键词试试？"

        # 2.5) 进度反馈：深搜要翻好几个页面，先让猫娘报个幕
        if self.deep_search_progress:
            try:
                self.ctx.push_message(
                    source=_PLUGIN_ID,
                    visibility=[],
                    ai_behavior="respond",
                    parts=[{"type": "text", "text": (
                        f"🔍 本喵拿到 {len(cards)} 条搜索结果，挑出了 {min(len(selected), limit)} 个最像的页面，"
                        "现在开始逐个翻页核实喵，大概要等一小会儿…"
                    )}],
                    priority=3,
                    metadata={"description": "🐱 深搜进行中"},
                )
            except Exception:
                self.logger.warning("[deep_search] 进度推送失败")

        # 3) 逐页抓取 + 分析 + 交叉核对
        analyses: list[dict[str, Any]] = []
        for card in selected[:limit]:
            page_text = await self._deep_fetch_text(card.get("url", ""))
            if not page_text:
                analyses.append({"card": card, "analysis": {"relevant": False, "answer": "", "codes": [], "summary": ""}, "verified_codes": []})
                continue
            try:
                analysis = parse_page_analysis(
                    await self._call_llm_json(
                        "你是搜索代理的阅读引擎。",
                        build_page_prompt(query, card, page_text),
                    )
                )
            except SdkError as exc:
                self.logger.warning("[deep_search] 页面分析失败: %s", exc)
                analysis = {"relevant": False, "answer": "", "codes": [], "summary": f"分析失败：{exc}"}
            verified = cross_check_codes(analysis.get("codes", []), page_text)
            analyses.append({"card": card, "analysis": analysis, "verified_codes": verified})
            await asyncio.sleep(0.3)

        # 4) 汇总（本地组装，只引用核实过的内容）
        report = build_final_report(query, analyses, self.catgirl_name)
        self.logger.info("[deep_search] 完成：%s（读了 %d 页）", query, len(analyses))
        return report

    @plugin_entry(
        id="deep_search",
        name="深度搜索",
        description="搜索代理：搜索 → 按摘要筛选候选网页 → 逐页读取正文 → 交叉核实答案（如兑换码）→ 汇总报告。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要核实的问题"},
                "max_pages": {"type": "integer", "description": "最多读几个页面（1-6，默认配置值）"},
            },
            "required": ["query"],
        },
    )
    async def deep_search_entry(self, query: str = "", max_pages: int = 0, **_):
        try:
            return Ok(await self._run_deep_search(query, max_pages or None))
        except Exception as exc:
            self.logger.exception("深搜失败: %s", exc)
            return Err(SdkError(f"深搜出错了喵：{exc}"))

    async def _direct_plugin_call(self, rest: str, effective: str):
        """`/调用 插件id:入口id k=v|JSON`：跳过 AI 匹配，直连其它插件的能力。"""
        if not rest:
            return Ok(
                "用法喵：/调用 插件id:入口id 参数=值 …\n"
                "例：/调用 anysearch:search query=原神\n"
                "也支持 JSON：/调用 neko_watch_party:start_watch {\"video\": \"BV...\", \"begin_now\": true}\n"
                "有哪些能力可用：/插件"
            )
        parts = rest.split(None, 1)
        target = parts[0]
        if ":" not in target:
            return Ok(f"「{target}」不是有效的 插件id:入口id 喵。用 /插件 看看有哪些能力可用～")
        # 权限门控：直调是在越过 AI 审查直接指挥其它插件，可配置要求提权
        if self.direct_call_permission == "admin" and effective != "admin":
            return Ok("直调插件能力需要管理员权限喵：请先 /su <密码> 提权。")
        args, note = parse_direct_call_args(parts[1] if len(parts) > 1 else "")
        self.logger.info("[natural_command] 直调插件能力 target=%s args=%s", target, args)
        result = await self._run_plugin_command({"type": "plugin", "content": target, "name": target}, args)
        output = result["output"]
        if note:
            output += note
        if result["success"]:
            return Ok(output)
        return Err(SdkError(output))

    async def _create_and_run(
        self,
        new_cmd: dict[str, Any],
        allow_admin: bool = True,
        args: Any = None,
    ):
        created = self.registry.add_command(new_cmd)
        cmd_id = created.get("id", new_cmd.get("id"))
        if str(created.get("type", "")).lower() == "plugin":
            # 新学会的插件能力自动进能力表，下次匹配提示词就能看到
            self._refresh_capabilities()
        effective = self.registry.user_permission if allow_admin else "user"
        exec_result = await self._dispatch_command(cmd_id, effective, args=args)
        head = (
            f"✅ 已自动创建命令 [{created.get('name', cmd_id)}]\n"
            f"描述：{created.get('description', '无')}\n"
            f"类型：{created.get('type', 'reply')}｜权限：{created.get('permission', 'user')}"
            f"｜风险：{created.get('risk', '未知')}\n"
        )
        if exec_result["success"]:
            return Ok(f"{head}结果：{exec_result['output']}")
        return Err(SdkError(exec_result["output"]))

    async def _generate_new_command(self, user_input: str) -> Optional[dict[str, Any]]:
        prompt = build_create_prompt(
            user_input=user_input,
            default_permission=self.default_permission,
            default_type=self.default_type,
            capability_text=self._capability_text,
        )
        try:
            result = await self._call_llm_json("你是一个命令生成引擎。", prompt)
        except SdkError as exc:
            self.logger.warning("[natural_command] 生成新命令失败：%s", exc)
            return None
        if normalize_action(result) == "not_found":
            return None
        return extract_new_command(result)

    # ── 内置管理功能 ────────────────────────────────────────────
    def _list_commands(self, keyword: str = "") -> str:
        if not self.registry.commands:
            return "暂无命令"
        return format_command_lines(self.registry.commands, keyword=keyword)

    def _delete_command(self, args: str, permission: str = "user") -> str:
        cmd_id = args.strip()
        if not cmd_id:
            return "用法：/delcmd <命令ID>"
        target = self.registry.commands.get(cmd_id)
        if target is None:
            return f"命令不存在：{cmd_id}"
        # admin 级（harmful）命令的删除本身就在改安全边界，必须管理员来操作
        if str(target.get("permission", "user")).lower() == "admin" and permission != "admin":
            return "该命令是 admin 级，删除需要管理员权限：请先 /su <密码> 提权。"
        if self.registry.delete_command(cmd_id):
            return f"已删除命令：{cmd_id}"
        return f"命令不存在：{cmd_id}"

    def _switch_permission(self, password: str) -> str:
        if not self.admin_password:
            return "未设置管理员密码"
        if self.registry.switch_permission(password):
            return "已切换为管理员权限（仅 /命令 下的高危命令会用到）"
        return "密码错误"

    def _exit_admin(self) -> str:
        if self.registry.reset_permission():
            return "已解除管理员权限，回到 user 状态喵～"
        return "当前已经是 user 权限喵～"

    # ── 显式管理入口（供 AI / 面板调用）──────────────────────────
    @plugin_entry(
        id="list_commands",
        name="命令列表",
        description="查看所有已配置的命令",
        input_schema={"type": "object", "properties": {}},
    )
    async def list_commands_entry(self, **_):
        return Ok(self._list_commands())

    @plugin_entry(
        id="add_command",
        name="手动添加命令",
        description="管理员手动添加一条命令配置",
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "risk": {"type": "string", "enum": ["harmless", "indeterminate", "harmful"]},
                "permission": {"type": "string", "enum": ["user", "admin"]},
                "type": {"type": "string", "enum": ["reply", "shell"]},
                "content": {"type": "string"},
            },
            "required": ["id", "name", "content"],
        },
    )
    async def add_command_entry(self, **kwargs):
        if self.registry.user_permission != "admin":
            return Err(SdkError("需要管理员权限"))
        self.registry.add_command(kwargs)
        return Ok(f"已添加命令：{kwargs.get('name', kwargs.get('id'))}")

    @plugin_entry(
        id="delete_command",
        name="删除命令",
        description="删除指定命令",
        input_schema={
            "type": "object",
            "properties": {"command_id": {"type": "string"}},
            "required": ["command_id"],
        },
    )
    async def delete_command_entry(self, command_id: str = "", **_):
        return Ok(self._delete_command(command_id, permission=self.registry.user_permission))

    @plugin_entry(
        id="switch_permission",
        name="切换权限",
        description="输入管理员密码切换为 admin 权限",
        input_schema={
            "type": "object",
            "properties": {"password": {"type": "string"}},
            "required": ["password"],
        },
    )
    async def switch_permission_entry(self, password: str = "", **_):
        await self._ensure_config_loaded()
        return Ok(self._switch_permission(password))

    @plugin_entry(
        id="reload_commands",
        name="刷新命令配置",
        description="重新加载 commands.json",
        input_schema={"type": "object", "properties": {}},
    )
    async def reload_commands_entry(self, **_):
        self.registry.reload()
        return Ok("已刷新命令配置")
