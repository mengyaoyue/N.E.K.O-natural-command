"""自然语言命令插件 v0.1（作者：MENGYAOYUE）

用 /自然语言 触发动作：AI 先在命令库里语义匹配，命中即执行；未命中时自动生成
新命令写回 commands.json 长期保存，越用越顺手。支持 reply 文本回复与 shell
系统命令；shell 又区分「打开类」（fire-and-forget）与「查询类」（捕获 stdout
回传结果）。内置无害 / 无法判定 / 有害三档 AI 安全审查，由风险决定所需权限。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import httpx

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

from ._command_logic import (
    CommandRegistry,
    build_create_prompt,
    build_endpoint_candidates,
    build_match_prompt,
    extract_json_object,
    extract_new_command,
    is_exit_admin,
    load_settings,
    normalize_action,
    parse_user_input,
    safe_str as _safe_str,
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


def _build_endpoint_candidates(base_url: str, model: str) -> list[str]:
    return build_endpoint_candidates(base_url, model)


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
        self._config_loaded: bool = False

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

        self.registry.admin_password = self.admin_password
        self.registry.auto_create = self.auto_create
        self.registry.default_permission = self.default_permission
        self.registry.default_type = self.default_type
        self._config_loaded = True

    async def _ensure_config_loaded(self) -> None:
        if not self._config_loaded:
            await self._load_config()

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
        last_text = ""
        async with httpx.AsyncClient(timeout=self.llm_timeout, follow_redirects=True) as client:
            for endpoint in endpoints:
                body = payload
                if ":generateContent" in endpoint:
                    body = {
                        "contents": [
                            {"role": "user", "parts": [{"text": system + "\n" + user}]}
                        ],
                    }
                resp = await client.post(
                    endpoint,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {api_key}",
                        "x-api-key": api_key,
                    },
                    json=body,
                )
                last_text = resp.text
                self.logger.info(
                    "[natural_command] endpoint=%s status=%s body=%s",
                    endpoint,
                    resp.status_code,
                    last_text[:500],
                )
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if ":generateContent" in endpoint:
                            text = data["candidates"][0]["content"]["parts"][0]["text"]
                        else:
                            text = data["choices"][0]["message"]["content"]
                    except (KeyError, IndexError, ValueError):
                        continue

                    self.logger.info("[natural_command] 模型原始返回：%s", text[:500])
                    raw_json = extract_json_object(text)
                    if not raw_json:
                        continue
                    try:
                        return json.loads(raw_json)
                    except json.JSONDecodeError:
                        continue
        raise SdkError(f"模型返回无法解析为 JSON：{last_text[:200]}")

    async def _match_command(self, user_input: str, user_permission: str) -> dict[str, Any]:
        prompt = build_match_prompt(
            commands=list(self.registry.commands.values()),
            user_input=user_input,
            user_permission=user_permission,
            auto_create=self.auto_create,
            default_permission=self.default_permission,
            default_type=self.default_type,
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

        # 内置管理命令走快捷路径
        builtin = user_input.split()[0].lower()
        if builtin == "cmdlist":
            return Ok(self._list_commands())
        if builtin == "delcmd":
            rest = user_input[len("delcmd"):].strip()
            return Ok(self._delete_command(rest))
        if builtin == "su":
            rest = user_input[len("su"):].strip()
            return Ok(self._switch_permission(rest))
        if builtin == "reloadcmd":
            self.registry.reload()
            return Ok("已刷新命令配置")

        # admin 权限只在带 / 前缀时生效；不带 / 的自然语言一律按 user 级别处理（与 user 同级），
        # 因此提权不会让普通聊天/自然语言获得管理员能力。
        allow_admin = had_prefix
        effective = self.registry.user_permission if allow_admin else "user"

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
            return self._execute_command(cmd_id, risk=result.get("risk"), allow_admin=allow_admin)

        new_cmd = extract_new_command(result)

        # 未给出可用的新命令（含 not_found / 格式错误）：只要允许就补一次生成
        if not isinstance(new_cmd, dict) or not new_cmd.get("id"):
            if not self.auto_create:
                return Err(SdkError("未识别该命令，且未开启自动创建。"))
            new_cmd = await self._generate_new_command(user_input)

        if not isinstance(new_cmd, dict) or not new_cmd.get("id"):
            return Err(SdkError("未能生成有效的命令配置。"))

        return self._create_and_run(new_cmd, allow_admin=allow_admin)

    def _execute_command(self, cmd_id: str, risk: Any = None, allow_admin: bool = True):
        effective = self.registry.user_permission if allow_admin else "user"
        exec_result = self.registry.execute_command(cmd_id, user_permission=effective, risk=risk)
        if exec_result["success"]:
            return Ok(exec_result["output"])
        return Err(SdkError(exec_result["output"]))

    def _create_and_run(self, new_cmd: dict[str, Any], allow_admin: bool = True):
        created = self.registry.add_command(new_cmd)
        cmd_id = created.get("id", new_cmd.get("id"))
        effective = self.registry.user_permission if allow_admin else "user"
        exec_result = self.registry.execute_command(cmd_id, user_permission=effective)
        if exec_result["success"]:
            return Ok(
                f"✅ 已自动创建命令 [{created.get('name', cmd_id)}]\n"
                f"描述：{created.get('description', '无')}\n"
                f"类型：{created.get('type', 'reply')}｜权限：{created.get('permission', 'user')}"
                f"｜风险：{created.get('risk', '未知')}\n"
                f"结果：{exec_result['output']}"
            )
        return Err(SdkError(exec_result["output"]))

    async def _generate_new_command(self, user_input: str) -> Optional[dict[str, Any]]:
        prompt = build_create_prompt(
            user_input=user_input,
            default_permission=self.default_permission,
            default_type=self.default_type,
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
    def _list_commands(self) -> str:
        if not self.registry.commands:
            return "暂无命令"
        lines = ["📋 已配置命令："]
        for cmd_id, cmd in self.registry.commands.items():
            perm = "🔒 admin" if cmd.get("permission") == "admin" else "👤 user"
            risk = _safe_str(cmd.get("risk"))
            risk_text = f" 风险:{risk}" if risk else ""
            t = cmd.get("type", "reply")
            lines.append(f"- {cmd.get('name', cmd_id)} ({cmd_id}) [{t}] {perm}{risk_text}")
        return "\n".join(lines)

    def _delete_command(self, args: str) -> str:
        cmd_id = args.strip()
        if not cmd_id:
            return "用法：/delcmd <命令ID>"
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
        return Ok(self._delete_command(command_id))

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
