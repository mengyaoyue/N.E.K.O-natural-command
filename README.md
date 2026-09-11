# 自然语言命令插件（neko_natural_command）

> 说人话就能指挥电脑喵～ 用 `/自然语言` 触发动作：AI 先在命令库里语义匹配，命中即执行；没命中就当场自学会，自动生成新命令并长期保存，越用越顺手。

- 版本：`0.3.0`
- 作者：MENGYAOYUE
- 适用：N.E.K.O 插件运行时（SDK `>=0.1.0, <0.3.0`）
- 依赖：**零第三方库**——LLM 调用已改用 Python 标准库（v0.1 依赖 httpx，v0.2 起不再需要）

## v0.3.0 更新内容：像 agent 一样干活

- **参数化命令（agent 核心）**：命令模板支持 `{占位符}` 参数。AI 自动创建命令时会把意图里的可变部分（搜索词、名字、网址等）做成参数，执行时从你的话里提取值填入——"B站搜索 原神"学一次，以后任何关键词都直接复用；全新安装还内置了 `web_search`（联网搜索）和 `bilibili_search`（B站搜索）两条参数化示例。
- **跨插件调用（新命令类型 `plugin`）**：新增 `plugin` 类型命令，content 写 `插件id:入口id`，执行时通过 SDK `ctx.plugins.call_entry` 调用其他插件的能力。已接入的典型能力：
  - `anysearch:search` —— 联网搜索（"查一查原神建模"→ 猫娘直接搜给你）
  - `sys_monitor:a_status` —— 查本机 CPU/内存/磁盘/电量
  - 权限模型对 plugin 命令同样生效（risk → user/admin）。
- **缺参追问（`need_args`）**：参数不够时不再瞎猜，AI 会用猫娘口吻向你追问缺少的信息。
- **猫娘人设保持**：匹配/创建提示词要求 reply 与命令命名都用猫娘口吻；执行成功、超时、失败、缺参等全部反馈文案带人设（"已帮你打开…喵～"）。
- 参数值安全清洗：压平换行、shell 场景剥离双引号，防止参数注入破坏命令结构；插件结果超长自动截断。

## v0.2.0 更新内容

- **防"无效命令"（重点改进）**：执行前逐类校验 shell 命令，杜绝"命令跑了但什么都没发生"：
  - 裸软件名（`start 哔哩哔哩`）自动在本机快捷方式 / 商店应用 / 注册表 / 安装目录中解析成真实程序路径；
  - 不存在的文件路径（`.lnk` / `.exe`）执行前拦截，回退按名称找真实程序，仍找不到则明确报错而不是无声失败；
  - 漏写协议的网址（`start www.bilibili.com`）自动补全 `https://`；
  - 不带 `start` 的裸应用名 / 裸协议 / 裸域名自动改写为 `start` 形态；
  - `start` 的开关（`/max`、`/min`、`/D <目录>`）先剥离再验证，不会被当成应用名；
  - cmd 内置命令与查询命令（`dir`、`tasklist`、`ping x.x.x.x`）有白名单，绝不被误改写；
  - 提示词同步加固：禁止 AI 编造协议与文件路径，从源头减少无效命令。
- **零第三方依赖**：模型调用从 `httpx` 迁移到标准库 `urllib`，安装更省心，不再有依赖冲突。
- **匹配提示词瘦身**：命令库变大后，本地先预筛出与输入最相关的候选（最多 30 条）再交给 AI，响应更快、token 更省。
- **shell 超时可配置**：新增 `shell_timeout`（默认 30 秒），查询类命令卡死时能按时止损；输出超过 4000 字符自动截断，防止撑爆 AI 上下文。
- **命令 id 清洗**：AI 自动创建的命令 id 落库前统一清洗为字母/数字/下划线，中文或空 id 自动回退命名，撞名自动加序号，不再产生脏键。
- **新增内置命令**：`/helpcmd` 查看速查帮助；`/findcmd <关键词>` 按关键词筛选命令列表。
- **删除命令加权限门控**：`/delcmd` 删除 admin 级（harmful）命令现在需要先 `/su` 提权。
- **密码比较加固**：管理员密码改为常数时间比较（`hmac.compare_digest`）。

---

## 一、如何安装

> ⚠️ 本插件可以执行 shell 系统命令，请只安装可信来源的副本。

N.E.K.O 装插件一共有 **三条通道**，本插件支持其中两条。先弄懂它们的关系，再看具体步骤，就不会被 `.neko-plugin-packages` 绕晕。

| 通道 | 怎么装 | 装完在哪 | `.neko-plugin-packages` 里会有东西吗 |
|------|--------|----------|--------------------------------------|
| ① 导入 `.neko-plugin` 包（推荐） | 在「插件管理」里导入 `.neko-plugin` 文件 | `plugins/neko_natural_command/` | **有**：包文件会收进这里存档 |
| ② 手动放文件夹 | 直接把 `neko_natural_command` 文件夹拷进 `plugins/` | `plugins/neko_natural_command/` | **没有**：这条路既不用包、也不产生包 |
| ③ 插件市场 | 在插件市场点安装 | `plugins/<插件名>/` | 有（市场下载的 `.neko-plugin`） |

不管走哪一条，**插件最终都落在 `plugins/neko_natural_command/`**；N.E.K.O 运行的是这个文件夹，而不是包文件本身。

### `.neko-plugin-packages` 到底是什么？

它是 N.E.K.O 安装目录下的一个**包仓库 / 存档目录**，只用来存放 `.neko-plugin` 打包文件本身：

```text
<N.E.K.O 安装目录>/
├── plugins/                       ← 插件真正运行的地方（三条通道最终都装到这里）
│   └── neko_natural_command/
└── .neko-plugin-packages/         ← 只存 .neko-plugin 打包文件（导入源 / 市场下载存档）
    ├── neko_natural_command.neko-plugin
    ├── anysearch.neko-plugin
    └── ...（其他插件的包）
```

几个要点：

- 它只放**打包文件（.neko-plugin）**，不放解压后的插件；`plugins/` 里才是解压后真正运行的插件。
- **什么时候会出现文件**：用通道 ① **导入包**、或通道 ③ **从市场安装**时，包会被收进这里；用通道 ② **手动拷文件夹**时，完全不会用到它。
- 所以：**如果你在 `.neko-plugin-packages` 里没看到本插件，是正常的** —— 说明当初是用「方式 ② 手动拷文件夹」装的，不是包安装。
- 可以把它理解成「N.E.K.O 的包存档区」。里面的包只是存档，一般删掉也不影响已经装好的插件运行。

> 想确认自己当初是怎么装的？打开安装目录下的 `plugins.lock.json`，看本插件的 `channel` 字段：`manual` = 手动放文件夹，`market` = 市场安装。

### 方式 ①：导入 `.neko-plugin` 包（推荐）

1. 到本仓库的 **Releases** 页面，下载 `neko_natural_command.neko-plugin`。
2. 打开 N.E.K.O → **插件管理**，找到**导入 / 安装本地包**（导入 `.neko-plugin`）的入口。
3. 选中刚下载的 `neko_natural_command.neko-plugin`，确认安装。
4. 装好后，N.E.K.O 会把它**解压到 `plugins/neko_natural_command/`**，同时把包文件收进 `.neko-plugin-packages/` 存档。
5. 在**插件管理**里启用「自然语言命令」。
6. **必做：设置管理员密码**（见下一节），否则插件会被安全地禁用。

### 方式 ②：手动放入 `plugins` 目录

1. **关闭 N.E.K.O**，确认插件没有在运行。
2. 获取插件文件夹（二选一），文件夹名必须是 **`neko_natural_command`**：

```bash
# git 克隆（仓库文件夹默认叫 N.E.K.O-natural-command，克隆后请改名）
git clone https://github.com/mengyaoyue/N.E.K.O-natural-command.git
ren N.E.K.O-natural-command neko_natural_command
```

或者：在仓库页面点 **Code → Download ZIP**，解压后把得到的文件夹改名为 `neko_natural_command`。

3. 把**整个 `neko_natural_command` 文件夹**放进 N.E.K.O 安装目录下的 `plugins` 目录里，最终结构如下：

```
<N.E.K.O 安装目录>/
└── plugins/
    └── neko_natural_command/          ← 本插件放在这里
        ├── plugin.toml                ← 插件清单
        ├── __init__.py                ← 入口代码
        ├── _command_logic.py          ← 核心逻辑
        ├── config.example.toml        ← 配置模板
        ├── commands.json.example      ← 命令库示例
        ├── profiles.toml
        ├── profiles/default.toml
        ├── README.md
        └── tests/
            ├── test_basic.py
            └── test_smoke.py
```

> 注意层级：是 `plugins/neko_natural_command/...`。
> **不要把 `neko_natural_command` 里面的文件直接倒进 `plugins/`。**
> 这条通道**不会**在 `.neko-plugin-packages` 里生成任何文件，这是正常的。

4. 启动 N.E.K.O，在**插件管理**里找到「自然语言命令」，启用它。
5. **必做：设置管理员密码**（见下一节），否则插件会被安全地禁用。

### 首次安装后必做：设置管理员密码

插件**只有在配置了 `admin_password` 后才会工作**；未设置时 `run_command` 会直接拒绝执行（安全设计，避免无口令的 shell 执行）。

- 配置项位置：插件配置的 `[neko_natural_command]` 段（通常对应插件目录下的 `config/plugin.toml`，可在 N.E.K.O 的插件配置界面修改，或参考 `config.example.toml`）。
- 本仓库 / 分发包为了开箱即用，填写了占位密码 `CHANGE_ME`；**请务必改成你自己的密码**。

```toml
[neko_natural_command]
admin_password = "CHANGE_ME"   # ← 改成你自己的密码，不要留默认值
```

改完保存后重启插件（或用 `/reloadcmd` 刷新命令库）即可生效。

---

## 二、快速开始

1. 按上一节设置好 `admin_password` 并启用插件。
2. 在聊天里发送 `/问候` → 返回 `你好喵～今天也要开心呀！`
3. 发送 `/ 打开记事本` → 系统记事本被打开（这条来自 `commands.json.example` 示例）。
4. 发送一条命令库里没有的，例如 `/ 查询一下当前系统时间` → AI 会**现场创建**新命令、执行、并保存到 `commands.json`，下次直接可用。

---

## 三、如何使用（内置命令）

所有命令都以 `/` 开头，`/` 后面可跟自然语言：

| 输入 | 说明 |
|------|------|
| `/ 问候` 或 `/问候` | 触发已配置的 `greeting` 命令（示例命令库内置） |
| `/ 打开记事本` | 触发 `open_notepad`（无害命令，user 权限即可） |
| `/ <任意自然语言>` | AI 语义匹配已有命令；未命中时（若允许）自动创建新命令并执行 |
| `/ B站搜索 原神` | 参数化命令：搜索词自动提取；命令库会用 `{参数}` 模板记住这类意图 |
| `/ 查一查 原神4.8 新角色` | 调用其他插件能力（如 anysearch 联网搜索）并让猫娘汇总 |
| `/退出管理员`、`/取消管理员`、`/exit admin` | 语义为“退出管理员权限”时，回到 user 状态 |
| `/cmdlist` | 列出所有命令（含类型、权限、风险等级） |
| `/findcmd <关键词>` | 按关键词筛选命令列表，例如 `/findcmd 打开` |
| `/helpcmd` | 查看命令速查帮助 |
| `/delcmd <命令ID>` | 删除指定命令（admin 级命令需先 `/su` 提权） |
| `/su <密码>` | 输入管理员密码，切换为 admin 权限 |
| `/reloadcmd` | 重新加载 `commands.json` |

> 触发方式：N.E.K.O 主 AI 识别到 `/` 开头的自然语言后，会调用本插件的 `neko_run_command` 工具（入口 id：`run_command`）来完成匹配 / 创建 / 执行。

---

## 四、权限模型（三级威胁审查）

AI 会为每条命令判定一个风险等级 `risk`，再映射到执行所需的权限 `permission`：

| 风险 `risk` | 含义 | 所需权限 |
|-------------|------|----------|
| `harmless` 无害 | 只读 / 打开应用 / 文本回复等，不影响设备资料 | `user` |
| `indeterminate` 无法判定 | 难以分辨是否有害（AI 拿不准） | `user` |
| `harmful` 有害 | 可能对设备或资料造成危害 | `admin` |

使用规则：

1. **默认 `user` 身份**：可执行 `harmless` 与 `indeterminate` 的命令；命中 `harmful` 命令会被拒绝并提示“权限不足”。
2. **提权**：发送 `/su <你的密码>`，成功后本次运行期间切换为 `admin`，之后 `harmful` 命令才可执行。
3. **降权**：在 `admin` 状态下发送语义为“退出管理员权限”的 `/命令`（如 `/退出管理员`、`/exit admin`、`/取消管理员`），即回到 `user`。
4. **只有 `/` 前缀受身份影响**：`admin` 状态仅对带 `/` 前缀的命令生效；不带 `/` 的普通自然语言一律按 `user` 处理，所以提权不会让日常聊天获得系统能力。
5. **密码错误**：返回“密码错误”，权限不变。
6. **AI 无法自行提权**：提权只认 `/su <密码>`，AI 自动创建命令时也不会绕过权限。
7. **未设置密码即禁用**：`admin_password` 为空时，插件拒绝执行任何命令。

### 自动重审降级

历史命令若被记为 `admin` 但实际无害，AI 在执行匹配时会重新审查其风险；复审为 `harmless` / `indeterminate` 时会把该命令**自动降级为 `user`** 并写回 `commands.json`，因此不必手动改历史配置。

### 新建命令的权限

AI 自动创建的新命令，权限由审查出的 `risk` 决定：

- `harmless` / `indeterminate` → `user`
- `harmful` → `admin`（需先 `/su` 提权）

> 提权状态保存在插件进程内，**重启插件 / 重启 N.E.K.O 后会回到默认 `user`**。

---

## 五、配置

在插件配置的 `[neko_natural_command]` 段（`config.example.toml` 是模板，安装后作为运行时配置的默认值）：

```toml
[neko_natural_command]
admin_password = "CHANGE_ME"   # 管理员密码，必填；为空则插件禁用
auto_create = true             # 未匹配时是否允许 AI 自动创建新命令（含 shell 类型）
auto_clean = true              # 启动时是否自动清理本插件命令库里的脏数据
default_permission = "user"    # 无法判定风险时的兜底权限：user / admin
default_type = "reply"         # 自动创建命令默认类型：reply / shell
llm_timeout = 20               # 调用大模型超时（秒）
shell_timeout = 30             # shell 查询类命令超时（秒），输出超 4000 字符自动截断
```

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `admin_password` | 无（必填） | 管理员密码。为空时插件拒绝执行所有命令 |
| `auto_create` | `true` | 未匹到命令时是否允许 AI 自动创建并保存新命令 |
| `auto_clean` | `true` | 插件启动时自动清理命令库里的脏数据（见下） |
| `default_permission` | `user` | 风险无法判定时的兜底权限 |
| `default_type` | `reply` | 自动创建命令的默认类型 |
| `llm_timeout` | `20` | AI 匹配 / 生成时调用大模型的超时时间（秒） |
| `shell_timeout` | `30` | shell 查询类命令最长等待时间（秒），最小 1 秒 |

### 启动自动清理（auto_clean）

插件启动时会自动清理**本插件自己命令库**里的垃圾脏数据，严格限定在本插件范围内，不会读写插件目录以外的任何文件；清理失败也不会影响插件启动。清理对象：

- **占位 / 空壳命令**：内容是“请提供…”“找不到…”“抱歉…”之类的无效回复；
- **重复命令**：ID 或内容重复的条目；
- **无效 / 损坏条目**：类型不是 `reply` / `shell`、结构损坏的条目。

---

## 六、命令类型

- `reply`：返回文本内容（猫娘口吻）。
- `shell`：执行系统命令（是否可用由 `risk` 判定，`harmful` 时仅管理员可用）。
  - 「打开类」命令（如启动软件）不等待输出，立即返回；
  - 「查询类」命令会捕获命令输出并回传结果（超时受 `shell_timeout` 限制）。
  - 支持 `{参数}` 占位符：执行时由 AI 从用户输入提取的值填充（如 `start "" "https://search.bilibili.com/all?keyword={query}"`）。
- `plugin`：调用其他 N.E.K.O 插件的能力，content 写 `插件id:入口id`（如 `anysearch:search`），参数直接透传；同样受三级权限审查约束。

---

## 七、数据存放位置

- **命令库**：持久化在插件数据目录下的 `commands.json`（AI 自动创建的命令都保存在这里）。
- **配置**：运行时配置位于插件配置段 `[neko_natural_command]`。
- 卸载插件不会自动删除命令库，如需彻底清理请一并删除插件数据目录。

---

## 八、卸载

1. 在 N.E.K.O 插件管理中停用插件。
2. 删除 `plugins/neko_natural_command` 整个文件夹。
3. （可选）删除插件数据目录下的 `commands.json` 以清除已创建的命令。

---

## 九、注意事项

- 本插件可让 AI 依据自然语言**创建并执行系统命令**。请确保管理员密码只告知可信用户。
- AI 的威胁审查是辅助手段，**不能替代人工判断**，请谨慎授权 `shell` 与 `admin`。
- 若不需要 AI 自动创建命令，可将 `auto_create` 设为 `false`。
- 若担心 AI 自动生成 `shell` 命令，可将 `default_type` 设为 `reply`（默认即为 `reply`）并关闭 `auto_create`。
