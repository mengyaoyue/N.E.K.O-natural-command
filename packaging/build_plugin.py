#!/usr/bin/env python3
"""N.E.K.O 插件打包工具 —— 生成可导入的 .neko-plugin 包。

用法：
    py packaging/build_plugin.py                       # 打包本仓库（脚本上一级目录）
    py packaging/build_plugin.py --src <插件源码目录>   # 打包指定目录
    py packaging/build_plugin.py --src <目录> --out <输出文件.neko-plugin>

产物：
    <插件id>-<版本>.neko-plugin  （ZIP 格式，可在 N.E.K.O 中直接导入）

产物结构与 N.E.K.O 官方 neko_plugin_cli 的 build_plugin 保持一致：
    manifest.toml                 包元信息（id / 名称 / 版本 / 描述）
    metadata.toml                 payload 的 sha256，以及 source 信息
    payload/dependencies.toml     依赖清单（本项目为纯 Python，均为空）
    payload/plugins/<id>/**       插件源码
    payload/profiles/default.toml 默认 profile（含插件运行配置）
"""

import argparse
import hashlib
import shutil
import tempfile
import unicodedata
import zipfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

EXCLUDE_DIR_NAMES = {
    "__pycache__", ".github", ".vscode", ".idea", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".venv", ".git",
    "tests",       # 测试只留在仓库，不进包
    "packaging",   # 本打包工具自身，不进包
    "config",      # 运行时配置（可能含真实密码），绝不进包
    "data",        # 运行时数据，绝不进包
}
ROOT_EXCLUDE_DIR_NAMES = {"dist", "build"}
EXCLUDE_FILE_NAMES = {".DS_Store"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}

_ESC = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
_BARE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def norm(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def norm_rel(path: Path, root: Path) -> str:
    return norm(path.relative_to(root).as_posix())


def should_skip(rel: Path, is_dir: bool) -> bool:
    dir_parts = rel.parts if is_dir else rel.parts[:-1]
    if dir_parts and dir_parts[0] in ROOT_EXCLUDE_DIR_NAMES:
        return True
    if any(part in EXCLUDE_DIR_NAMES for part in dir_parts):
        return True
    if not is_dir:
        if rel.name in EXCLUDE_FILE_NAMES:
            return True
        if rel.suffix in EXCLUDE_SUFFIXES:
            return True
    return False


def escape_string(value: str) -> str:
    out = []
    for ch in value:
        esc = _ESC.get(ch)
        if esc is not None:
            out.append(esc)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\u%04X" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def bare_or_quoted(key: str) -> str:
    if key and all(ch in _BARE for ch in key):
        return key
    return '"%s"' % escape_string(key)


def render(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return '"%s"' % escape_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(render(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = ["%s = %s" % (bare_or_quoted(str(k)), render(v)) for k, v in value.items()]
        return "{ " + ", ".join(pairs) + " }"
    if value is None:
        return '""'
    return '"%s"' % escape_string(str(value))


def dump_mapping(mapping) -> list:
    return ["%s = %s" % (bare_or_quoted(k), render(v)) for k, v in mapping.items()]


def payload_hash(payload_dir: Path) -> str:
    files = [(norm_rel(p, payload_dir), p) for p in payload_dir.rglob("*") if p.is_file()]
    digest = hashlib.sha256()
    for rel, path in sorted(files, key=lambda item: item[0]):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def archive_payload_hash(archive_path: Path) -> str:
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        items = sorted(
            [(norm(n[len("payload/"):]), n) for n in names if n.startswith("payload/") and not n.endswith("/")],
            key=lambda item: item[0],
        )
        digest = hashlib.sha256()
        for rel, arcname in items:
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(archive.read(arcname))
            digest.update(b"\0")
    return digest.hexdigest()


def build(src: Path, out: Path) -> Path:
    plugin_toml = tomllib.loads((src / "plugin.toml").read_text(encoding="utf-8"))
    pt = plugin_toml["plugin"]
    plugin_id = pt["id"]
    name = pt.get("name") or plugin_id
    version = pt.get("version") or "0.1.0"
    package_type = pt.get("type") or "plugin"
    description = pt.get("description") or ""

    config_example_path = src / "config.example.toml"
    defaults = (
        tomllib.loads(config_example_path.read_text(encoding="utf-8"))
        if config_example_path.is_file() else plugin_toml
    )
    plugin_runtime = defaults.get("plugin_runtime")
    runtime_config = defaults.get(plugin_id)
    if not isinstance(runtime_config, dict):
        runtime_config = {}

    with tempfile.TemporaryDirectory(prefix="neko_pkg_") as tmp:
        staging = Path(tmp)
        payload_dir = staging / "payload"
        plugin_dir = payload_dir / "plugins" / plugin_id
        profiles_dir = payload_dir / "profiles"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        profiles_dir.mkdir(parents=True, exist_ok=True)

        for path in sorted(src.rglob("*")):
            rel = path.relative_to(src)
            if should_skip(rel, path.is_dir()):
                continue
            dest = plugin_dir / rel
            if path.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)

        profile_lines = [
            'name = "default"',
            'enabled_plugins = ["%s"]' % escape_string(plugin_id),
            "",
            "[plugin.%s]" % bare_or_quoted(plugin_id),
            "enabled = true",
        ]
        if isinstance(plugin_runtime, dict):
            auto_start = plugin_runtime.get("auto_start")
            if isinstance(auto_start, bool):
                profile_lines.append("auto_start = %s" % render(auto_start))
        if runtime_config:
            profile_lines.extend(dump_mapping(runtime_config))
        (profiles_dir / "default.toml").write_text(
            "\n".join(profile_lines).rstrip() + "\n", encoding="utf-8", newline="\n"
        )

        dep_lines = [
            'schema_version = "1.0"',
            "",
            "[plugins.%s]" % bare_or_quoted(plugin_id),
            "python_requirements = %s" % render([]),
            "host_python_requirements = %s" % render([]),
            "plugin_dependencies = %s" % render([]),
            "advanced_plugin_dependencies = %s" % render([]),
            'vendor_path = "plugins/%s/vendor"' % escape_string(plugin_id),
            "vendor_present = %s" % render(False),
            "",
        ]
        (payload_dir / "dependencies.toml").write_text(
            "\n".join(dep_lines).rstrip() + "\n", encoding="utf-8", newline="\n"
        )

        hash_value = payload_hash(payload_dir)

        manifest_lines = [
            'schema_version = "1.0"',
            'package_type = "%s"' % escape_string(package_type),
            "",
            'id = "%s"' % escape_string(plugin_id),
            'package_name = "%s"' % escape_string(name),
            'version = "%s"' % escape_string(version),
        ]
        if description:
            manifest_lines.append('package_description = "%s"' % escape_string(description))
        manifest_lines.append("")
        (staging / "manifest.toml").write_text(
            "\n".join(manifest_lines), encoding="utf-8", newline="\n"
        )

        (staging / "metadata.toml").write_text(
            "\n".join([
                "[payload]",
                'hash_algorithm = "sha256"',
                'hash = "%s"' % hash_value,
                "",
                "[source]",
                'kind = "local"',
                'paths = ["%s"]' % escape_string(plugin_id),
                "",
            ]),
            encoding="utf-8", newline="\n",
        )

        if out.exists():
            out.unlink()
        out.parent.mkdir(parents=True, exist_ok=True)
        entries = [(norm_rel(p, staging), p) for p in staging.rglob("*") if p.is_file()]
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for arcname, path in sorted(entries, key=lambda item: item[0]):
                archive.write(path, arcname=arcname)

        zip_hash = archive_payload_hash(out)

    print("输出        :", out, "(%d bytes)" % out.stat().st_size)
    print("payload hash:", hash_value)
    print("校验        :", "OK" if hash_value == zip_hash else "MISMATCH (%s)" % zip_hash)
    if hash_value != zip_hash:
        raise SystemExit("包内 payload 与 metadata.toml 记录不一致，已中止")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 N.E.K.O 插件为 .neko-plugin")
    parser.add_argument("--src", type=Path, default=Path(__file__).resolve().parent.parent,
                        help="插件源码目录（含 plugin.toml），默认脚本上一级")
    parser.add_argument("--out", type=Path, default=None,
                        help="输出文件路径，默认 <源码上一级>/<id>-<版本>.neko-plugin")
    args = parser.parse_args()

    src = args.src.resolve()
    if not (src / "plugin.toml").is_file():
        raise SystemExit("找不到 %s，请用 --src 指定包含 plugin.toml 的插件目录" % (src / "plugin.toml"))

    if args.out is None:
        pt = tomllib.loads((src / "plugin.toml").read_text(encoding="utf-8"))["plugin"]
        out = src.parent / ("%s-%s.neko-plugin" % (pt["id"], pt.get("version") or "0.1.0"))
    else:
        out = args.out.resolve()

    build(src, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
