# 插件打包 / 发布速查

本目录保存 **N.E.K.O 插件打包与发布** 的可复用工具和要点，下次直接调用，不用再重新摸索。

## 一、打包（生成 .neko-plugin）

```powershell
# 在本仓库根目录（含 plugin.toml）执行
py packaging/build_plugin.py

# 或指定任意插件目录 / 输出路径
py packaging/build_plugin.py --src <插件源码目录> --out <输出.neko-plugin>
```

- 默认把「脚本上一级目录」当插件源码，产物写到该目录的上一级：`<插件id>-<版本>.neko-plugin`。
- 脚本结束会打印 `payload hash` 与 `校验: OK`，只有包内 payload 与 `metadata.toml` 记录一致才算成功。

### 打包时会自动排除

| 排除项 | 原因 |
| --- | --- |
| `tests/` `packaging/` | 测试与打包工具本身，不随包发布 |
| `config/` `data/` | 运行时配置与数据，**可能含真实密码，绝不进包** |
| `__pycache__` `.git` `.idea` `.vscode` `.pytest_cache` `.mypy_cache` `.ruff_cache` `.venv` | 缓存/IDE 目录 |
| 根目录 `dist/` `build/`、文件 `.DS_Store`、后缀 `.pyc`/`.pyo` | 构建产物/垃圾文件 |

> ⚠️ 不要直接对 N.E.K.O 运行目录里的插件实例（`N.E.K.O\plugins\<id>`）打包——它带有 `config/`（真实密码）和 `data/`。用仓库里的干净源码打包。

## 二、包结构（与官方 neko_plugin_cli 对齐）

```
manifest.toml                  schema_version / package_type / id / package_name / version / package_description
metadata.toml                  [payload] sha256 + [source]
payload/dependencies.toml      依赖清单（纯 Python 插件全为空数组）
payload/plugins/<id>/**         插件源码（排除上表项后）
payload/profiles/default.toml   默认 profile：enabled_plugins + [plugin.<id>] 运行配置
```

- ZIP，`ZIP_DEFLATED`，条目按 **NFC 规范化后的 posix 路径** 排序。
- **payload hash** = 对 `payload/` 下所有文件，按「相对 `payload/` 的路径」排序后，`路径utf8 + \0 + 文件字节 + \0` 依次喂给 SHA-256。
- `payload/profiles/default.toml` 由 `config.example.toml` 的 `[plugin_runtime]` 和 `[<插件id>]` 合并生成。
- `plugin.meta.json` 只有在能通过 N.E.K.O 运行时导入插件时才会生成；本地打包通常**没有**它，属正常（宿主会回退读 manifest）。

## 三、发布到 GitHub

```powershell
$env:GH_CONFIG_DIR="d:\neko\_ghcfg"
$gh="C:\Program Files\GitHub CLI\gh.exe"

# 首次：创建 Release 并附带包文件
& $gh release create v<版本> "<包路径>.neko-plugin" `
    --repo mengyaoyue/N.E.K.O-natural-command --target main `
    --title "v<版本> - <标题>" --notes "<发布说明>"

# 更新：覆盖同名附件
& $gh release upload v<版本> "<包路径>.neko-plugin" `
    --repo mengyaoyue/N.E.K.O-natural-command --clobber
```

`gh` 不在 PATH 时用上面的绝对路径；登录状态存在 `d:\neko\_ghcfg`（`gh auth status` 查看）。

## 四、安装方式（写进 README / Release 说明）

1. 下载 `.neko-plugin` 包，在 N.E.K.O 中导入；
2. 或解压后把 `plugins/<id>/` 放进 `N.E.K.O\plugins\` 下手动安装。

包仓库地址：https://github.com/mengyaoyue/N.E.K.O-natural-command

## 五、发版前自查

- [ ] `plugin.toml` 里 `version` 已升位，`description` 是要展示的文案。
- [ ] 敏感项为占位符（如 `admin_password = "CHANGE_ME"`），仓库里**不能有真实密码**。
- [ ] 运行打包，确认打印 `校验: OK`。
- [ ] 确认包里没有 `config/`、`data/`、`tests/`。
- [ ] 上传 Release 附件后，用 `gh release view <tag> --json assets` 确认附件状态为 `uploaded`。
