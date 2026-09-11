"""不依赖 pytest 的核心逻辑独立测试"""

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_logic():
    spec = importlib.util.spec_from_file_location(
        "neko_natural_command_logic", ROOT / "_command_logic.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["neko_natural_command_logic"] = mod
    spec.loader.exec_module(mod)
    return mod


def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")


def main():
    print("加载 _command_logic ...")
    mod = load_logic()

    # 1. 默认示例命令加载
    registry = mod.CommandRegistry(commands_path=ROOT / "commands.json.example")
    assert_eq("greeting" in registry.commands, True, "greeting 应存在")
    assert_eq(registry.commands["greeting"]["type"], "reply", "greeting 类型应为 reply")

    # 2. 空密码必须禁用权限功能
    empty = mod.CommandRegistry(
        commands_path=ROOT / "commands.json.example",
        admin_password="",
    )
    assert_eq(empty.admin_password_valid, False, "空密码应当无效")
    assert_eq(empty.check_permission("admin", "user"), False, "空密码时应拒绝 admin")

    # 3. 权限校验
    reg = mod.CommandRegistry(
        commands_path=ROOT / "commands.json.example",
        admin_password="CHANGE_ME",
    )
    assert_eq(reg.check_permission("user", "user"), True, "user 权限可执行 user 命令")
    assert_eq(reg.check_permission("admin", "user"), False, "user 权限不可执行 admin 命令")
    assert_eq(reg.check_permission("admin", "admin"), True, "admin 权限可执行 admin 命令")

    # 4. 执行 reply 命令
    reg_user = mod.CommandRegistry(
        commands_path=ROOT / "commands.json.example",
        admin_password="CHANGE_ME",
        user_permission="user",
    )
    result = reg_user.execute_command("greeting")
    assert_eq(result["success"], True, "greeting 应成功")
    assert "你好" in result["output"], f"greeting 输出应包含问好: {result['output']!r}"

    # 5. 权限由 risk 审查推导：无害命令 user 可执行，有害命令 user 被拒绝
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "commands.json"
        reg5 = mod.CommandRegistry(
            commands_path=tmp_path,
            admin_password="CHANGE_ME",
            user_permission="user",
        )
        reg5.add_command(
            {"id": "safe_cmd", "name": "无害命令", "risk": "harmless", "type": "reply", "content": "ok"}
        )
        assert_eq(reg5.execute_command("safe_cmd")["success"], True, "无害命令 user 状态应可执行")
        reg5.add_command(
            {
                "id": "danger_cmd",
                "name": "危险命令",
                "description": "可能影响设备资料",
                "risk": "harmful",
                "type": "reply",
                "content": "危险",
            }
        )
        result = reg5.execute_command("danger_cmd")
        assert_eq(result["success"], False, "user 执行有害命令应失败")
        assert "权限" in result["output"], "拒绝信息应包含权限字样"

    # 6. 切换权限
    assert_eq(reg_user.switch_permission("CHANGE_ME"), True, "正确密码应切换成功")
    assert_eq(reg_user.user_permission, "admin", "切换后应为 admin")
    assert_eq(reg_user.switch_permission("wrong"), False, "错误密码应失败")

    # 7. 添加并保存命令
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "commands.json"
        reg2 = mod.CommandRegistry(
            commands_path=tmp_path,
            admin_password="CHANGE_ME",
        )
        reg2.add_command(
            {
                "id": "test_cmd",
                "name": "测试命令",
                "description": "仅用于测试",
                "permission": "user",
                "type": "reply",
                "content": "测试通过",
            }
        )
        assert_eq("test_cmd" in reg2.commands, True, "添加后命令应存在")
        data = json.loads(tmp_path.read_text(encoding="utf-8"))
        assert_eq(data["test_cmd"]["content"], "测试通过", "保存后 content 应对齐")

    # 8. 删除命令
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "commands.json"
        reg3 = mod.CommandRegistry(commands_path=tmp_path, admin_password="CHANGE_ME")
        reg3.add_command({"id": "del_me", "name": "删除我"})
        reg3.delete_command("del_me")
        assert_eq("del_me" not in reg3.commands, True, "删除后命令应不存在")

    # 9. 解析用户输入
    assert_eq(mod.parse_user_input("/ 问候"), "问候", "空格分隔应去除")
    assert_eq(mod.parse_user_input("/问候"), "问候", "无空格应去除")
    assert_eq(mod.parse_user_input("hello"), "hello", "无前缀应原样返回")

    # 10. 配置段解析：SDK 的 self.config.dump() 返回整个 plugin.toml 字典，
    #     插件配置位于 cfg["neko_natural_command"] 段（回归：曾用同步 ctx.config
    #     属性读取，导致读不到密码、run_command 被禁用）
    settings = mod.load_settings(
        {
            "admin_password": "CHANGE_ME",
            "auto_create": False,
            "auto_clean": False,
            "default_permission": "admin",
            "default_type": "shell",
            "llm_timeout": "30",
        }
    )
    assert_eq(settings["admin_password"], "CHANGE_ME", "应解析出 admin_password")
    assert_eq(settings["auto_create"], False, "应解析出 auto_create")
    assert_eq(settings["auto_clean"], False, "应解析出 auto_clean")
    assert_eq(settings["default_permission"], "admin", "应解析出 default_permission")
    assert_eq(settings["default_type"], "shell", "应解析出 default_type")
    assert_eq(settings["llm_timeout"], 30.0, "llm_timeout 应转为 float")

    defaults = mod.load_settings(None)
    assert_eq(defaults["admin_password"], "", "缺失配置段时密码应为空")
    assert_eq(defaults["auto_create"], True, "缺失配置段时 auto_create 默认 True")
    assert_eq(defaults["auto_clean"], True, "缺失配置段时 auto_clean 默认 True")
    assert_eq(defaults["default_permission"], "user", "缺失配置段时默认 user")
    assert_eq(defaults["default_type"], "reply", "缺失配置段时默认 reply")
    assert_eq(defaults["llm_timeout"], 20.0, "缺失配置段时 llm_timeout 默认 20")
    assert_eq(
        mod.load_settings({"llm_timeout": "abc"})["llm_timeout"],
        20.0,
        "非法 llm_timeout 应回退默认值",
    )

    # 11. shell 目标解析：伪协议 / 网址 / 软件名
    #     修复两类问题：
    #     (a) AI 编造未注册协议（start bilibili://）→ 弹“没有可打开此链接的应用”
    #     (b) 未注册协议且找不到程序 → 返回空串，由上层给友好提示（不弹系统对话框）
    fake_lnk = Path("C:/Users/Public/Desktop/哔哩哔哩.lnk")
    uninstall_lnk = Path(
        "C:/ProgramData/Microsoft/Windows/Start Menu/Programs/哔哩哔哩/卸载哔哩哔哩.lnk"
    )
    shortcuts = [uninstall_lnk, fake_lnk]

    def no_protocol(_scheme):
        return False

    # 11.1 未注册伪协议 → 靠快捷方式解析，并排除“卸载”
    resolved = mod.resolve_shell_target(
        "start bilibili://",
        hint="打开桌面上面的哔哩哔哩快捷键",
        shortcuts=shortcuts,
        app_paths=[],
        protocol_checker=no_protocol,
    )
    assert "哔哩哔哩.lnk" in resolved, f"伪协议应解析到真实快捷方式: {resolved!r}"
    assert "卸载" not in resolved, f"不应命中卸载快捷方式: {resolved!r}"
    assert resolved.startswith('start ""'), f"应使用 start 加引号: {resolved!r}"

    # 11.2 已注册协议 → 原样保留，交给系统处理
    resolved = mod.resolve_shell_target(
        "start steam://open/main",
        shortcuts=shortcuts,
        app_paths=[],
        protocol_checker=lambda scheme: scheme == "steam",
    )
    assert_eq(resolved, "start steam://open/main", "已注册协议应原样保留")

    # 11.3 未注册协议 + 无快捷方式 → 回退注册表 App Paths
    resolved = mod.resolve_shell_target(
        "start spotify://",
        hint="打开 Spotify",
        shortcuts=[],
        app_paths=[("Spotify.exe", r"C:\Apps\Spotify\Spotify.exe")],
        protocol_checker=no_protocol,
    )
    assert_eq(
        resolved,
        'start "" "C:\\Apps\\Spotify\\Spotify.exe"',
        "应回退到注册表 App Paths",
    )

    # 11.4 未注册协议 + 完全找不到 → 返回空串（上层给友好提示，而非弹系统对话框）
    resolved = mod.resolve_shell_target(
        "start bilibili://",
        hint="",
        shortcuts=[],
        app_paths=[],
        protocol_checker=no_protocol,
    )
    assert_eq(resolved, "", "找不到程序时应返回空串")

    # 11.5 网页地址 → 补全引号，避免被当成可执行文件路径
    resolved = mod.resolve_shell_target("start https://www.bilibili.com")
    assert_eq(resolved, 'start "" "https://www.bilibili.com"', "网页应补全引号")

    resolved = mod.resolve_shell_target('start "" "https://www.bilibili.com"')
    assert_eq(
        resolved,
        'start "" "https://www.bilibili.com"',
        "已规范的网页命令不应重复包装",
    )

    # 11.6 系统内置命令 → 原样返回
    resolved = mod.resolve_shell_target("notepad")
    assert_eq(resolved, "notepad", "内置命令应原样返回")

    # 11.7 纯软件名 → 快捷方式
    resolved = mod.resolve_shell_target(
        'start "" "哔哩哔哩"',
        hint="打开哔哩哔哩",
        shortcuts=shortcuts,
        app_paths=[],
    )
    assert "哔哩哔哩.lnk" in resolved, f"软件名应解析到快捷方式: {resolved!r}"

    # 11.7b 纯软件名但完全找不到 → 同样返回空串，走友好提示
    resolved = mod.resolve_shell_target(
        'start "" "钉钉"',
        hint="打开钉钉",
        shortcuts=[],
        app_paths=[],
    )
    assert_eq(resolved, "", "软件名找不到时应返回空串")

    # 11.8 执行期兜底：无法解析的 shell 命令返回友好提示，而不是抛给系统
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "commands.json"
        reg_shell = mod.CommandRegistry(
            commands_path=tmp_path,
            admin_password="CHANGE_ME",
            app_index=mod.AppIndex.from_entries([]),
        )
        reg_shell.add_command(
            {
                "id": "open_missing_app",
                "name": "打开不存在的软件",
                "description": "用于验证兜底提示",
                "permission": "user",
                "type": "shell",
                "content": "start zzzznotexist_app://",
            }
        )
        result = reg_shell.execute_command("open_missing_app")
        assert_eq(result["success"], False, "无法解析的软件应返回失败")
        assert "没找到" in result["output"], f"应给出友好提示: {result['output']!r}"

    # 11.9 系统级应用索引：多来源合并 + 商店应用（AUMID）也能解析
    index = mod.AppIndex.from_entries(
        [
            ("哔哩哔哩", r"C:\Users\Public\Desktop\哔哩哔哩.lnk", mod._SOURCE_SHORTCUT),
            (
                "哔哩哔哩",
                "shell:AppsFolder\\BiliBili.BiliBili_8wekyb3d8bbwe!App",
                mod._SOURCE_START_MENU,
            ),
            ("计算器", "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", mod._SOURCE_START_MENU),
            ("哔哩哔哩", r"C:\Program Files\BiliBili\BiliBili.exe", mod._SOURCE_INSTALL_DIR),
        ]
    )
    # 同一名称命中多个来源时，优先级最高的（快捷方式）胜出
    resolved = mod.resolve_shell_target(
        "start bilibili://",
        hint="打开哔哩哔哩",
        app_index=index,
        protocol_checker=no_protocol,
    )
    assert "哔哩哔哩.lnk" in resolved, f"应优先命中快捷方式: {resolved!r}"

    # 商店应用（UWP）只有 AUMID 时，也能解析成 shell:AppsFolder 目标
    resolved = mod.resolve_shell_target(
        "start calc://",
        hint="打开计算器",
        app_index=index,
        protocol_checker=no_protocol,
    )
    assert resolved.startswith('start "" "shell:AppsFolder\\'), f"商店应用应解析为 AUMID: {resolved!r}"
    assert "WindowsCalculator" in resolved, f"应命中计算器 AUMID: {resolved!r}"

    # 11.10 来源优先级：快捷方式(0) 优先于 安装目录(4)
    index2 = mod.AppIndex.from_entries(
        [
            ("网易云音乐", r"C:\Program Files\Netease\CloudMusic\cloudmusic.exe", mod._SOURCE_INSTALL_DIR),
            ("网易云音乐", r"C:\Users\Public\Desktop\网易云音乐.lnk", mod._SOURCE_SHORTCUT),
        ]
    )
    resolved = mod.resolve_shell_target(
        'start "" "网易云音乐"',
        hint="打开网易云音乐",
        app_index=index2,
    )
    assert "网易云音乐.lnk" in resolved, f"快捷方式应优先于安装目录: {resolved!r}"

    # 11.11 默认索引是运行时按机器采集的，不依赖任何写死的本机路径
    default_index = mod.AppIndex()
    assert_eq(default_index.entries() == [] or isinstance(default_index.entries(), list), True,
              "默认索引应能返回条目列表")

    # 12. 三档威胁审查（AI 自动审核）→ 权限映射 & 自动重审降级
    # 12.1 风险等级归一化（兼容中英文）
    assert_eq(mod.normalize_risk("无危害"), "harmless", "中文无害应归一化")
    assert_eq(mod.normalize_risk("HARMFUL"), "harmful", "英文有害应归一化")
    assert_eq(mod.normalize_risk("难以分辨"), "indeterminate", "无法判定应归一化")
    assert_eq(mod.normalize_risk("随便"), "", "无法识别应返回空串（回退 permission）")

    # 12.2 风险 → 权限：只有 harmful 需要 admin，无害/无法判定都归 user
    assert_eq(mod.permission_for_risk("harmless"), "user", "无害应是 user")
    assert_eq(mod.permission_for_risk("indeterminate"), "user", "无法判定应是 user")
    assert_eq(mod.permission_for_risk("harmful"), "admin", "有害应是 admin")

    # 12.3 自动新建命令按 risk 自动定权限（审查并入创建流程）
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "commands.json"
        reg_risk = mod.CommandRegistry(commands_path=tmp_path, admin_password="CHANGE_ME")
        reg_risk.add_command({"id": "risky", "name": "危险命令", "risk": "harmful", "type": "reply", "content": "x"})
        assert_eq(reg_risk.commands["risky"]["permission"], "admin", "harmful 新命令应为 admin")
        reg_risk.add_command({"id": "safe_cmd", "name": "安全命令", "risk": "harmless", "type": "reply", "content": "x"})
        assert_eq(reg_risk.commands["safe_cmd"]["permission"], "user", "harmless 新命令应为 user")

    # 12.4 自动重审降级：原本 admin 的无害命令，AI 复审后 user 状态即可执行并降级
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "commands.json"
        reg_audit = mod.CommandRegistry(
            commands_path=tmp_path,
            admin_password="CHANGE_ME",
            user_permission="user",
        )
        reg_audit.add_command(
            {
                "id": "legacy_admin_msg",
                "name": "旧管理员命令",
                "permission": "admin",
                "type": "reply",
                "content": "hello",
            }
        )
        assert_eq(reg_audit.commands["legacy_admin_msg"]["permission"], "admin", "初始应为 admin")
        result = reg_audit.execute_command("legacy_admin_msg", risk="harmless")
        assert_eq(result["success"], True, "AI 复审无害后 user 状态应可直接执行")
        assert_eq(reg_audit.commands["legacy_admin_msg"]["permission"], "user", "应自动降级为 user")
        assert_eq(reg_audit.commands["legacy_admin_msg"]["risk"], "harmless", "应把 risk 写回命令")

    # 12.5 harmful 复审：user 状态仍被拒绝，提权后可执行
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "commands.json"
        reg_harm = mod.CommandRegistry(
            commands_path=tmp_path,
            admin_password="CHANGE_ME",
            user_permission="user",
        )
        reg_harm.add_command({"id": "wipe", "name": "清空", "risk": "harmful", "type": "reply", "content": "x"})
        result = reg_harm.execute_command("wipe")
        assert_eq(result["success"], False, "harmful 命令 user 状态应被拒绝")
        reg_harm.switch_permission("CHANGE_ME")
        assert_eq(reg_harm.execute_command("wipe")["success"], True, "提权后 harmful 命令应可执行")

    # 12.6 不带 / 前缀时即使处于 admin 也按 user 级处理（execute_command 可显式指定权限）
    assert_eq(
        reg_harm.execute_command("wipe", user_permission="user")["success"],
        False,
        "显式 user 权限应拒绝 harmful 命令",
    )

    # 12.7 降权出口：/ + 语义为“退出管理员权限”
    assert_eq(mod.is_exit_admin("退出admin权限"), True, "应识别退出管理员")
    assert_eq(mod.is_exit_admin("exit admin"), True, "英文也应识别")
    assert_eq(mod.is_exit_admin("取消管理员"), True, "取消管理员也应识别")
    assert_eq(mod.is_exit_admin("打开记事本"), False, "普通命令不应误判")
    assert_eq(mod.is_exit_admin("关闭显示器"), False, "关闭普通程序不应误判")
    assert_eq(reg_harm.reset_permission(), True, "处于 admin 时应可退出")
    assert_eq(reg_harm.user_permission, "user", "退出后应回到 user")
    assert_eq(reg_harm.reset_permission(), False, "已是 user 时再退出返回 False")

    # 13. shell 输出回传：查询类命令必须捕获结果，打开类命令仍为 fire-and-forget
    #     回归：/查看内存占用 能创建命令并“执行成功”，但结果因 stdout 被丢弃而拿不到数字
    # 13.1 命令分类
    assert_eq(mod.is_launch_command('start "" "https://www.bilibili.com"'), True, "打开网页应视为启动类")
    assert_eq(mod.is_launch_command('start "" "C:\\Apps\\Spotify\\Spotify.exe"'), True, "打开软件应视为启动类")
    assert_eq(mod.is_launch_command("notepad"), True, "内置程序应视为启动类")
    assert_eq(mod.is_launch_command("calc.exe"), True, "带后缀的内置程序应视为启动类")
    assert_eq(mod.is_launch_command(r"C:\Users\Public\Desktop\哔哩哔哩.lnk"), True, "快捷方式应视为启动类")
    assert_eq(mod.is_launch_command("cmd"), True, "单独出现的交互式外壳应视为启动类")
    assert_eq(
        mod.is_launch_command('powershell -NoProfile -Command "Get-CimInstance Win32_OperatingSystem"'),
        False,
        "查询命令不应视为启动类",
    )
    assert_eq(mod.is_launch_command("cmd /c echo hi"), False, "带参数的外壳命令不应视为启动类")

    # 13.2 查询类命令会捕获 stdout 并回传给上层
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "commands.json"
        reg_out = mod.CommandRegistry(
            commands_path=tmp_path,
            admin_password="CHANGE_ME",
            app_index=mod.AppIndex.from_entries([]),
        )
        reg_out.add_command(
            {
                "id": "echo_test",
                "name": "输出测试",
                "risk": "harmless",
                "type": "shell",
                "content": "cmd /c echo neko_output_ok",
            }
        )
        result = reg_out.execute_command("echo_test")
        assert_eq(result["success"], True, f"查询命令应成功: {result!r}")
        assert "neko_output_ok" in result["output"], f"应回传命令输出: {result['output']!r}"

    # 14. 忽略空格 / 分隔符的模糊匹配
    #     回归：桌面快捷方式叫 “TRAE Work CN”，用户输入 “TRAEWORKCN” 却匹配不到
    # 14.1 紧凑化：去掉空白与常见分隔/标点
    assert_eq(mod._compact("TRAE Work CN"), "traeworkcn", "空格应被忽略")
    assert_eq(mod._compact("TRAE-Work_CN"), "traeworkcn", "连字符/下划线应被忽略")
    assert_eq(mod._compact("哔哩哔哩"), "哔哩哔哩", "纯中文不应被破坏")

    # 14.2 直接匹配：漏打空格也能命中
    assert mod._match_key("TRAE Work CN", ["TRAEWORKCN"]) is not None, "漏打空格应能匹配"
    assert mod._match_key("TRAE Work CN", ["trae work cn"]) is not None, "大小写不同应能匹配"
    assert mod._match_key("TRAE Work CN", ["TRAEWORKCN", "别的软件"]) is not None, "多个候选中应有命中"

    # 14.3 端到端：把 “TRAE Work CN” 快捷方式解析出来
    trae_lnk = r"C:\Users\11\Desktop\TRAE Work CN.lnk"
    index_trae = mod.AppIndex.from_entries([("TRAE Work CN", trae_lnk, mod._SOURCE_SHORTCUT)])
    assert_eq(index_trae.find(["TRAEWORKCN"]), trae_lnk, "索引应能按紧凑串命中")
    resolved = mod.resolve_shell_target(
        'start "" "TRAEWORKCN"',
        hint="打开桌面上的TRAEWORKCN",
        app_index=index_trae,
    )
    assert_eq(resolved, f'start "" "{trae_lnk}"', f"应解析到 TRAE Work CN 快捷方式: {resolved!r}")

    # 14.4 不能因为忽略空格而误伤：完全不相关的名字仍应匹配失败
    assert_eq(mod._match_key("TRAE Work CN", ["钉钉"]), None, "无关名称不应误匹配")
    assert_eq(mod._match_key("TRAE Work CN", ["xy"]), None, "过短候选不应误匹配")

    # 15. 启动清理垃圾脏数据（仅限本插件范围：占位/空壳、重复、无效/损坏）
    #     回归：历史自动创建失败时会存下 “请提供…/我找不到…” 之类占位 reply 命令
    with tempfile.TemporaryDirectory() as tmpdir:
        dirty_path = Path(tmpdir) / "commands.json"
        dirty = {
            "greeting": {
                "id": "greeting",
                "name": "问候",
                "type": "reply",
                "content": "你好喵～今天也要开心呀！",
            },
            # 占位 / 空壳命令：内容是推脱话术，属于典型的脏数据
            "open_treaworkcn": {
                "id": "open_treaworkcn",
                "name": "打开treaworkcn",
                "type": "reply",
                "content": "请提供完整的网址或软件名称，我才能帮你打开喵～",
            },
            "open_browser_first_link": {
                "id": "open_browser_first_link",
                "name": "打开浏览器第一个链接",
                "type": "reply",
                "content": "请提供具体链接网址，我才能帮你打开喵～",
            },
            "empty_shell": {
                "id": "empty_shell",
                "name": "空壳命令",
                "type": "shell",
                "content": "   ",
            },
            # 重复命令：与 greeting 内容完全一致
            "greeting_copy": {
                "id": "greeting_copy",
                "name": "问候副本",
                "type": "reply",
                "content": "你好喵～今天也要开心呀！",
            },
            # 无效 / 损坏条目：缺内容、类型非法、内容不是文本、缺少 id
            "broken_no_content": {"id": "broken_no_content", "name": "坏命令", "type": "reply"},
            "broken_bad_type": {
                "id": "broken_bad_type",
                "name": "坏类型",
                "type": "magic",
                "content": "x",
            },
            "broken_bad_content": {
                "id": "broken_bad_content",
                "name": "坏内容",
                "type": "reply",
                "content": 123,
            },
            "": {"name": "没id", "type": "reply", "content": "hi"},
            # 正常命令：必须保留
            "open_notepad": {
                "id": "open_notepad",
                "name": "打开记事本",
                "type": "shell",
                "content": "notepad",
            },
        }
        dirty_path.write_text(json.dumps(dirty, ensure_ascii=False), encoding="utf-8")
        reg_clean = mod.CommandRegistry(commands_path=dirty_path, admin_password="CHANGE_ME")
        report = reg_clean.prune_dirty_commands()

        removed_ids = {item["id"] for item in report["removed"]}
        for gone in (
            "open_treaworkcn",
            "open_browser_first_link",
            "empty_shell",
            "greeting_copy",
            "broken_no_content",
            "broken_bad_type",
            "broken_bad_content",
            "",
        ):
            assert gone in removed_ids, f"{gone!r} 应被清理: {report}"
        assert "greeting" in reg_clean.commands, "正常 reply 命令不应被误删"
        assert "open_notepad" in reg_clean.commands, "正常 shell 命令不应被误删"
        assert_eq(report["kept"], 2, "应只剩 2 条正常命令")
        assert_eq(report["removed_count"], 8, "应移除 8 条脏数据")

        # 清理结果必须落盘：重新加载后脏数据不会复活
        reg_reload = mod.CommandRegistry(commands_path=dirty_path, admin_password="CHANGE_ME")
        assert "open_treaworkcn" not in reg_reload.commands, "清理结果应已持久化"

    # 15.2 干净的命令库不会误伤：内置默认命令清理后保持不变
    with tempfile.TemporaryDirectory() as tmpdir:
        clean_path = Path(tmpdir) / "commands.json"
        reg_ok = mod.CommandRegistry(commands_path=clean_path, admin_password="CHANGE_ME")
        before = len(reg_ok.commands)
        clean_report = reg_ok.prune_dirty_commands()
        assert_eq(clean_report["removed_count"], 0, "干净库不应有清理项")
        assert_eq(len(reg_ok.commands), before, "干净库命令数不应变化")

    print("全部测试通过 ✅")


if __name__ == "__main__":
    main()
