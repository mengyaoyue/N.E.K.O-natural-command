"""自然语言命令插件冒烟测试：结构契约 + 核心逻辑（不依赖 N.E.K.O SDK）"""

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_logic_module():
    """加载 _command_logic.py，避开 SDK 导入"""
    spec = importlib.util.spec_from_file_location(
        "neko_natural_command_logic", ROOT / "_command_logic.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["neko_natural_command_logic"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestPluginManifest:
    def test_plugin_toml_exists(self):
        assert (ROOT / "plugin.toml").is_file()

    def test_entry_declared(self):
        text = (ROOT / "plugin.toml").read_text(encoding="utf-8")
        assert 'id = "neko_natural_command"' in text
        assert 'entry = "plugin.plugins.neko_natural_command:NaturalCommandPlugin"' in text


class TestCommandLogic:
    def test_default_commands_loaded(self):
        mod = _load_logic_module()
        registry = mod.CommandRegistry(commands_path=ROOT / "commands.json.example")
        assert "greeting" in registry.commands
        assert registry.commands["greeting"]["type"] == "reply"

    def test_admin_password_required(self):
        mod = _load_logic_module()
        # 空密码时权限校验必须失败
        registry = mod.CommandRegistry(
            commands_path=ROOT / "commands.json.example",
            admin_password="",
        )
        assert registry.admin_password_valid is False
        assert registry.check_permission("admin", user_permission="user") is False

    def test_permission_check(self):
        mod = _load_logic_module()
        registry = mod.CommandRegistry(
            commands_path=ROOT / "commands.json.example",
            admin_password="CHANGE_ME",
        )
        assert registry.check_permission("user", "user") is True
        assert registry.check_permission("admin", "user") is False
        assert registry.check_permission("admin", "admin") is True

    def test_execute_reply(self):
        mod = _load_logic_module()
        registry = mod.CommandRegistry(
            commands_path=ROOT / "commands.json.example",
            admin_password="CHANGE_ME",
            user_permission="user",
        )
        result = registry.execute_command("greeting")
        assert result["success"] is True
        assert "你好" in result["output"]

    def test_execute_admin_requires_su(self, tmp_path):
        mod = _load_logic_module()
        registry = mod.CommandRegistry(
            commands_path=tmp_path / "commands.json",
            admin_password="CHANGE_ME",
            user_permission="user",
        )
        registry.add_command(
            {
                "id": "danger_cmd",
                "name": "危险命令",
                "description": "可能影响设备资料",
                "risk": "harmful",
                "type": "reply",
                "content": "危险",
            }
        )
        result = registry.execute_command("danger_cmd")
        assert result["success"] is False
        assert "权限" in result["output"]

    def test_su_password(self):
        mod = _load_logic_module()
        registry = mod.CommandRegistry(
            commands_path=ROOT / "commands.json.example",
            admin_password="CHANGE_ME",
            user_permission="user",
        )
        assert registry.switch_permission("CHANGE_ME") is True
        assert registry.user_permission == "admin"
        assert registry.switch_permission("wrong") is False

    def test_add_and_save_command(self, tmp_path):
        mod = _load_logic_module()
        commands_path = tmp_path / "commands.json"
        registry = mod.CommandRegistry(
            commands_path=commands_path,
            admin_password="CHANGE_ME",
        )
        registry.add_command(
            {
                "id": "test_cmd",
                "name": "测试命令",
                "description": "仅用于测试",
                "permission": "user",
                "type": "reply",
                "content": "测试通过",
            }
        )
        assert "test_cmd" in registry.commands
        data = json.loads(commands_path.read_text(encoding="utf-8"))
        assert data["test_cmd"]["content"] == "测试通过"

    def test_delete_command(self, tmp_path):
        mod = _load_logic_module()
        commands_path = tmp_path / "commands.json"
        registry = mod.CommandRegistry(
            commands_path=commands_path,
            admin_password="CHANGE_ME",
        )
        registry.add_command({"id": "del_me", "name": "删除我"})
        registry.delete_command("del_me")
        assert "del_me" not in registry.commands

    def test_parse_user_input_splits_prefix(self):
        mod = _load_logic_module()
        assert mod.parse_user_input("/ 问候") == "问候"
        assert mod.parse_user_input("/问候") == "问候"
        assert mod.parse_user_input("hello") == "hello"

    def test_normalize_risk(self):
        mod = _load_logic_module()
        assert mod.normalize_risk("无危害") == "harmless"
        assert mod.normalize_risk("HARMFUL") == "harmful"
        assert mod.normalize_risk("难以分辨") == "indeterminate"
        assert mod.normalize_risk("随便") == ""

    def test_permission_for_risk(self):
        mod = _load_logic_module()
        assert mod.permission_for_risk("harmless") == "user"
        assert mod.permission_for_risk("indeterminate") == "user"
        assert mod.permission_for_risk("harmful") == "admin"

    def test_new_command_permission_derives_from_risk(self, tmp_path):
        mod = _load_logic_module()
        registry = mod.CommandRegistry(
            commands_path=tmp_path / "commands.json",
            admin_password="CHANGE_ME",
        )
        harmless = registry.add_command(
            {"id": "h", "name": "无害", "risk": "harmless", "type": "reply", "content": "hi"}
        )
        harmful = registry.add_command(
            {"id": "d", "name": "有害", "risk": "harmful", "type": "reply", "content": "no"}
        )
        assert harmless["permission"] == "user"
        assert harmful["permission"] == "admin"

    def test_auto_audit_downgrades_admin_command(self, tmp_path):
        mod = _load_logic_module()
        registry = mod.CommandRegistry(
            commands_path=tmp_path / "commands.json",
            admin_password="CHANGE_ME",
            user_permission="user",
        )
        registry.add_command(
            {"id": "legacy", "name": "旧命令", "permission": "admin", "type": "reply", "content": "ok"}
        )
        assert registry.commands["legacy"]["permission"] == "admin"
        result = registry.execute_command("legacy", risk="harmless")
        assert result["success"] is True
        assert registry.commands["legacy"]["permission"] == "user"
        assert registry.commands["legacy"]["risk"] == "harmless"

    def test_harmful_blocked_then_allowed_after_su(self, tmp_path):
        mod = _load_logic_module()
        registry = mod.CommandRegistry(
            commands_path=tmp_path / "commands.json",
            admin_password="CHANGE_ME",
            user_permission="user",
        )
        registry.add_command(
            {"id": "danger", "name": "危险", "risk": "harmful", "type": "reply", "content": "boom"}
        )
        blocked = registry.execute_command("danger", risk="harmful")
        assert blocked["success"] is False
        registry.switch_permission("CHANGE_ME")
        allowed = registry.execute_command("danger", risk="harmful")
        assert allowed["success"] is True

    def test_reset_permission_returns_to_user(self):
        mod = _load_logic_module()
        registry = mod.CommandRegistry(
            commands_path=ROOT / "commands.json.example",
            admin_password="CHANGE_ME",
            user_permission="admin",
        )
        assert registry.reset_permission() is True
        assert registry.user_permission == "user"
        assert registry.reset_permission() is False

    def test_is_exit_admin(self):
        mod = _load_logic_module()
        assert mod.is_exit_admin("退出admin权限") is True
        assert mod.is_exit_admin("exit admin") is True
        assert mod.is_exit_admin("取消管理员") is True
        assert mod.is_exit_admin("打开记事本") is False
        assert mod.is_exit_admin("关闭显示器") is False

    def test_is_launch_command(self):
        mod = _load_logic_module()
        assert mod.is_launch_command('start "" "https://example.com"') is True
        assert mod.is_launch_command("notepad") is True
        assert mod.is_launch_command("cmd") is True
        assert mod.is_launch_command('powershell -Command "Get-Date"') is False
        assert mod.is_launch_command("cmd /c echo hi") is False

    def test_shell_query_captures_output(self, tmp_path):
        mod = _load_logic_module()
        registry = mod.CommandRegistry(
            commands_path=tmp_path / "commands.json",
            admin_password="CHANGE_ME",
            app_index=mod.AppIndex.from_entries([]),
        )
        registry.add_command(
            {
                "id": "echo_test",
                "name": "输出测试",
                "risk": "harmless",
                "type": "shell",
                "content": "cmd /c echo neko_output_ok",
            }
        )
        result = registry.execute_command("echo_test")
        assert result["success"] is True
        assert "neko_output_ok" in result["output"]

    def test_compact_ignores_spaces_and_punctuation(self):
        mod = _load_logic_module()
        assert mod._compact("TRAE Work CN") == "traeworkcn"
        assert mod._compact("TRAE-Work_CN") == "traeworkcn"

    def test_match_key_ignores_spaces(self):
        mod = _load_logic_module()
        # 桌面快捷方式叫 “TRAE Work CN”，用户只打 “TRAEWORKCN” 也要能命中
        assert mod._match_key("TRAE Work CN", ["TRAEWORKCN"]) is not None
        assert mod._match_key("TRAE Work CN", ["trae work cn"]) is not None

    def test_resolve_app_name_without_spaces(self):
        mod = _load_logic_module()
        trae_lnk = r"C:\Users\11\Desktop\TRAE Work CN.lnk"
        index = mod.AppIndex.from_entries([("TRAE Work CN", trae_lnk, mod._SOURCE_SHORTCUT)])
        assert index.find(["TRAEWORKCN"]) == trae_lnk
        resolved = mod.resolve_shell_target(
            'start "" "TRAEWORKCN"',
            hint="打开桌面上的TRAEWORKCN",
            app_index=index,
        )
        assert resolved == f'start "" "{trae_lnk}"'

    def test_load_settings_parses_auto_clean(self):
        mod = _load_logic_module()
        assert mod.load_settings({"auto_clean": False})["auto_clean"] is False
        assert mod.load_settings(None)["auto_clean"] is True

    def test_prune_removes_placeholder_duplicate_and_invalid(self, tmp_path):
        mod = _load_logic_module()
        commands_path = tmp_path / "commands.json"
        commands_path.write_text(
            json.dumps(
                {
                    "greeting": {
                        "id": "greeting",
                        "name": "问候",
                        "type": "reply",
                        "content": "你好喵～",
                    },
                    # 占位命令：AI 当时没真完成任务，只存了推脱话术
                    "trap": {
                        "id": "trap",
                        "name": "打开x",
                        "type": "reply",
                        "content": "请提供完整的网址或软件名称，我才能帮你打开喵～",
                    },
                    # 与 greeting 内容重复
                    "dup": {
                        "id": "dup",
                        "name": "问候二",
                        "type": "reply",
                        "content": "你好喵～",
                    },
                    # 损坏：缺少 content
                    "broken": {"id": "broken", "name": "坏命令", "type": "reply"},
                    # 损坏：类型非法
                    "badtype": {
                        "id": "badtype",
                        "name": "坏类型",
                        "type": "magic",
                        "content": "x",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        registry = mod.CommandRegistry(commands_path=commands_path, admin_password="CHANGE_ME")
        report = registry.prune_dirty_commands()

        removed = {item["id"] for item in report["removed"]}
        assert {"trap", "dup", "broken", "badtype"} <= removed
        assert set(registry.commands) == {"greeting"}
        assert report["removed_count"] == 4
        assert report["kept"] == 1

    def test_prune_keeps_clean_registry(self, tmp_path):
        mod = _load_logic_module()
        registry = mod.CommandRegistry(
            commands_path=tmp_path / "commands.json", admin_password="CHANGE_ME"
        )
        before = len(registry.commands)
        report = registry.prune_dirty_commands()
        assert report["removed_count"] == 0
        assert len(registry.commands) == before

    def test_load_corrupt_top_level_resets_to_defaults(self, tmp_path):
        mod = _load_logic_module()
        commands_path = tmp_path / "commands.json"
        commands_path.write_text("[1, 2, 3]", encoding="utf-8")
        registry = mod.CommandRegistry(commands_path=commands_path, admin_password="CHANGE_ME")
        assert isinstance(registry.commands, dict)
        assert "greeting" in registry.commands

