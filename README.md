# MaiBot 每日分析插件 📊

一款为 **MaiBot 1.0** 设计的智能聊天记录分析插件，能够生成精美的群聊总结和个人总结图片，帮你快速回顾群里发生了什么。

> 已适配 MaiBot 1.0 / maibot_sdk 2.x。图片渲染使用 **宿主内置的渲染能力**（`render.html2png`），
> 无需自行安装 Playwright / Chromium，开箱即用。

## ✨ 功能特点

- **📝 群聊总结** - AI 自动总结今天/昨天的群聊内容，像朋友讲故事一样自然
- **👤 个人总结** - 查看自己或他人的聊天记录总结，包含个性化分析
- **🎨 精美图片** - 手写便签风格，包含多种可视化模块
- **⏰ 定时自动总结** - 每天固定时间自动发送群聊总结
- **🎯 模块化显示** - 自由控制显示哪些模块、按什么顺序显示
- **🔐 权限控制** - 支持群聊黑白名单和独立的命令权限控制
- **🖥️ WebUI 配置** - 支持通过 MaiBot WebUI 可视化配置

### 图片包含的模块

**群聊总结模块：**
- **24H 活跃轨迹** - 24 小时发言分布柱状图
- **今日话题** - AI 提取的群聊热门话题，带参与者统计
- **群友画像** - 为活跃群友生成个性化称号 + MBTI 类型，带 QQ 头像
- **语出惊人** - 今日群聊金句摘录，AI 给出推荐理由
- **炫压抑评级** - 根据发言风格分析群友的"压抑指数"（娱乐向）

**个人总结模块：**
- **3H 活跃轨迹** - 显示用户最活跃时段前后 3 小时的发言分布
- **群友画像** - 个人专属称号和 MBTI 分析
- **炫压抑评级** - 个人压抑指数评估
- **语出惊人** - 个人金句摘录

## 🚀 快速开始

### 1️⃣ 安装

把整个插件目录放到 MaiBot 的 `plugins/` 下即可。本插件依赖：

- **宿主内置 HTML 渲染能力**（MaiBot 1.0 自带，会按需自动准备无头浏览器，无需手动安装）
- **jinja2**（已在 `_manifest.json` 的 `dependencies` 中声明为 Python 包依赖，宿主会自动安装）
- 手写字体已随插件打包在 `fonts/` 目录，渲染时以 base64 内嵌，**无需联网下载字体**

> 💡 无需运行任何安装脚本，也无需手动 `pip install playwright` 或下载 Chromium。

### 2️⃣ 启用插件

编辑 `config.toml`，或在 WebUI 中开启：

```toml
[plugin]
enabled = true   # 改为 true
```

### 3️⃣ 重启 / 重载

重启或在 WebUI 重载插件后即可使用。

## 📖 使用方法

### 群聊总结命令

```
/summary              # 查看今天的群聊总结
/summary 昨天          # 查看昨天的群聊总结
```

### 个人总结命令

```
/mysummary            # 查看自己今天的个人总结
/mysummary 昨天        # 查看自己昨天的个人总结
/mysummary @某人       # 查看他人今天的个人总结（需要权限）
/mysummary @某人 昨天   # 查看他人昨天的个人总结
/mysummary QQ号        # 通过 QQ 号查看他人总结（需要权限）
```

> 📝 查看他人总结需要在 `user_summary.allowed_users` 列表中，详见权限配置。
> @某人 既支持真实 @（从消息中解析对方 QQ），也支持直接写 QQ 号。

## ⚙️ 配置说明（config.toml）

```toml
# ========== 插件基本配置 ==========
[plugin]
enabled = false              # 是否启用插件
config_version = "2.3.0"     # 配置版本，请勿手动修改

# ========== 群聊总结配置 ==========
[summary]
# 5 个下拉槽位，按 slot_1..slot_5 顺序显示。每个可选：
# 无 / 24H活跃轨迹 / 今日话题 / 群友画像 / 语出惊人 / 炫压抑评级
slot_1 = "24H活跃轨迹"
slot_2 = "今日话题"
slot_3 = "群友画像"
slot_4 = "语出惊人"
slot_5 = "炫压抑评级"
max_depression_display = 6   # 炫压抑评级最多展示人数
depression_show_bottom = true # 是否展示倒数排名（开启：前N/2+后N/2；关闭：只前N）
# 图片顶部 Highlight Time 显示：消息时间跨度（最早→最晚）/ 最活跃时段（发言最多的一小时）
highlight_time_mode = "消息时间跨度"

# ========== 个人总结配置 ==========
[user_summary]
enabled = true               # 是否启用个人总结功能
# 查看他人名单模式：白名单=仅名单内可看他人；黑名单=名单内禁止看他人，其余人可看
view_others_mode = "白名单"
allowed_users = []           # 配合上面模式控制谁能看他人（为空=所有人可看他人）；所有人始终可看自己
# 4 个下拉槽位。每个可选：无 / 3H活跃轨迹 / 群友画像 / 炫压抑评级 / 语出惊人 / 群友画像+炫压抑评级(并排)
slot_1 = "3H活跃轨迹"
slot_2 = "群友画像+炫压抑评级(并排)"
slot_3 = "语出惊人"
slot_4 = "无"

# ========== 自动总结配置 ==========
[auto_summary]
enabled = false              # 是否启用每日自动总结
time = "23:00"               # 每日执行时间（HH:MM，24 小时制）
timezone = "Asia/Shanghai"   # 时区（IANA 名称，使用标准库 zoneinfo）
min_messages = 10            # 生成总结所需的最少消息数量
target_chats = []            # 目标群号（为空=所有活跃群）

# ========== 命令权限控制 ==========
[command_permission]
mode = "黑名单"              # "黑名单"=列表中的群禁用命令；"白名单"=只有列表中的群可用
target_chats = []            # 黑/白名单群号列表
admin_users = []             # /summary 管理员 QQ 号（为空=所有人可用）

# ========== 高级 ==========
[advanced]
# 分析使用的【模型任务名】(task)，不是模型名。如 utils / planner / replyer。
# 该任务内的模型按其 model_list 随机/轮询；填错会自动回退 utils。
# 想固定用某个具体模型：在 MaiBot 的 model_config.toml 把对应任务的 model_list 设成那一个。
model_task = "utils"
inject_memory = false        # 实验性：把总结注入麦麦记忆（群聊→该群；个人→对该用户的记忆）
llm_timeout_seconds = 60     # 单次 LLM 调用超时（秒），需 MaiBot 1.0.0-rc.4+ 生效
render_timeout_seconds = 25  # 单次图片渲染超时（秒）
```

> 📌 QQ 号、群号统一用 **字符串数组** 填写，例如 `["123456", "987654"]`。

### 权限说明

- **群聊黑/白名单**（`command_permission.mode` + `target_chats`）：
  - `黑名单`：列表中的群 **禁用** `/summary`、`/mysummary`；其余群可用。
  - `白名单`：列表为空时全部禁用；否则 **只有** 列表中的群可用。
- **`/summary` 管理员**（`command_permission.admin_users`）：为空时所有人可用；有值时仅列表内用户可用。
- **`/mysummary` 权限**（`user_summary.enabled` + `view_others_mode` + `allowed_users`）：
  - 关闭 `enabled` 后所有人都不能用。
  - **所有人始终可以查看自己**；查看他人受名单模式控制：
    - `allowed_users` 为空：所有人都能看他人。
    - `白名单`：仅名单内用户可看他人。
    - `黑名单`：名单内用户禁止看他人，其余人可看。
- `/summary` 与 `/mysummary` 的权限相互独立。

### 模块显示顺序（WebUI 下拉选择）

模块顺序用**固定的下拉槽位**控制，每个槽位在 WebUI 里是一个中文下拉框（和"权限模式"一样点开选）：

- **群聊**：`slot_1` ~ `slot_5` 共 5 个槽位，从上到下就是显示顺序。
  每个槽位可选：`无` / `24H活跃轨迹` / `今日话题` / `群友画像` / `语出惊人` / `炫压抑评级`。
- **个人**：`slot_1` ~ `slot_4` 共 4 个槽位。
  每个槽位可选：`无` / `3H活跃轨迹` / `群友画像` / `炫压抑评级` / `语出惊人` / `群友画像+炫压抑评级(并排)`。

规则：
- **想改顺序**：改各槽位选的模块即可。
- **想隐藏某模块**：把对应槽位选成 `无`（或不在任何槽位里选它）。
- 同一个模块在多个槽位重复选只显示一次。
- 个人的 `群友画像+炫压抑评级(并排)` 表示这两个模块横向并排显示。

示例（群聊只显示 24H 和话题）：

```toml
[summary]
slot_1 = "24H活跃轨迹"
slot_2 = "今日话题"
slot_3 = "无"
slot_4 = "无"
slot_5 = "无"
```

## 🎨 图片样式与字体

插件使用 **手写便签风格（Scrapbook）** 生成图片：纸张纹理、胶带贴纸、彩色便签、可视化图表、QQ 头像展示。

- **手写字体**（`ZCOOL KuaiLe` 标题体、`Patrick Hand` 英文手写体）已随插件打包在 `fonts/` 目录，
  渲染时以 `@font-face` base64 内嵌，**离线可用，不依赖 Google Fonts**。
- **正文**回退到系统中文字体（Windows 微软雅黑 / macOS 苹方 / Linux 文泉驿正黑等）。
- **QQ 头像**在渲染时在线拉取（`q1.qlogo.cn`），渲染开启 `allow_network`。
- 渲染由宿主 `render.html2png` 完成，直接返回图片 base64，不在本地落盘临时文件。

如需修改样式，编辑 `templates/scrapbook/image_template.html`（群聊）或
`user_summary_template.html`（个人）的 CSS。

## ❓ 常见问题

### 1. 图片生成失败？

- 检查 MaiBot 日志中本插件的报错（渲染失败时会在日志中记录原因，但不会向群里发文字）。
- 确认宿主的浏览器渲染能力可用（首次使用宿主可能需要自动准备无头浏览器，请耐心等待或查看宿主日志）。

### 2. 图片里中文是方块 / 字体不好看？

- 手写标题字体已内嵌，正常应能显示。
- 正文使用系统字体：若运行在 Linux 服务器且系统缺少中文字体，请安装中文字体（如
  `fonts-wqy-zenhei` / `fonts-noto-cjk`），否则正文会显示为方块。

### 3. 群友画像没有头像？

- 头像通过 QQ 官方服务器 `q1.qlogo.cn` 在线获取，需要渲染环境能访问该地址。
- 匿名用户或无 QQ 号的用户不会显示头像。

### 4. 自动总结不执行？

- 确认 `plugin.enabled = true` 且 `auto_summary.enabled = true`。
- 确认时间格式正确（`23:00`）。
- 确认群里消息数量达到 `min_messages`。
- 修改自动总结相关配置后，插件会在配置热更新时自动重启调度器。

### 5. /mysummary @某人 没反应？

- 确认 `user_summary.enabled = true`。
- 若 `allowed_users` 有值，确认你的 QQ 号在列表中。

## 📋 技术架构

```
用户命令 (/summary, /mysummary) → plugin.py（命令处理 + 权限）
            ↓
    ctx.message.get_by_time_in_chat（查询并归一化历史消息）
            ↓
    core/analysis.py（ctx.llm 分析：总结/话题/称号/金句/炫压抑/个人画像）
            ↓
    core/rendering.py（Jinja2 渲染 HTML + 内嵌字体）
            ↓
    ctx.render.html2png（宿主渲染为 PNG，返回 base64）
            ↓
    ctx.send.image（发送图片到聊天流）
```

**用到的宿主能力（capabilities）：**
`send.text`、`send.image`、`llm.generate`、`config.get`、
`message.get_by_time_in_chat`、`chat.get_group_streams`、`render.html2png`

**目录结构：**

```text
Maibot_daily_analysis-main/
  _manifest.json          # manifest_version 2，id=khiqwq.daily_analysis
  plugin.py               # 插件主类、命令、定时调度器
  config.toml             # 配置
  core/
    analysis.py           # AnalysisService：聊天分析（LLM）
    rendering.py          # SummaryRenderer：图片渲染
    constants.py          # AnalysisConfig：分析参数
  templates/scrapbook/    # Jinja2 HTML 模板
  fonts/                  # 打包的手写字体（woff2）
```

### 调整分析参数

编辑 `core/constants.py` 中的 `AnalysisConfig`：

```python
class AnalysisConfig:
    MIN_MESSAGES_FOR_TITLE = 5   # 参与称号/画像分析的最小发言数
    MAX_USERS_FOR_TITLE = 8      # 群友画像最多分析用户数
    MIN_QUOTE_LENGTH = 5         # 金句最小长度
    MAX_QUOTE_LENGTH = 100       # 金句最大长度
    MAX_DEPRESSION_DISPLAY = 6   # 炫压抑评级默认展示人数
```

## 📜 更新日志

### v2.3.0
- **WebUI 配置分页**：每个配置节渲染为独立标签页（插件/群聊总结/个人总结/自动总结/命令权限/高级）
- 模块顺序改为**中文下拉槽位**（slot_1.. + "无"隐藏）；个人支持"群友画像+炫压抑评级(并排)"
- 新增 **Highlight Time 显示**方式（消息时间跨度 / 最活跃时段）
- 个人总结新增**查看他人黑/白名单模式**（`view_others_mode`）
- 新增实验性 **总结注入麦麦记忆**（`maisaka.context.append`）
- 新增**高级**配置：模型任务（自由填任务名 + 加载校验回退）、LLM 调用超时、图片渲染超时（可在 WebUI 配置）
- 适配 **MaiBot 1.0.0-rc.4**：通过 `rpc_timeout_ms` 覆盖单次能力调用超时（防御式，旧版自动回退）；`plugin_type` 设为 `tool`
- 性能/健壮性：LLM 调用并发上限（信号量）、定时任务单群超时兜底、群聊总字数改用真实统计、群友画像头像匹配更稳健、QQ 头像渲染前预下载为 base64 离线渲染

### v2.0.0
- **适配 MaiBot 1.0 / maibot_sdk 2.x**（完整重写为新插件系统）
- 改用宿主内置 `render.html2png` 渲染，移除自带 Playwright/Chromium 与依赖安装脚本
- 手写字体改为随插件打包并 base64 内嵌，移除对 Google Fonts 的依赖，优化 Linux/离线渲染
- 图片渲染直接返回 base64，不再落盘临时文件
- 各项 LLM 分析并发执行，缩短整体耗时
- 群聊"表情统计"改用真实 emoji 计数
- 权限配置由 `use_blacklist` 改为更直观的 `mode = "黑名单"/"白名单"`
- 命令权限模式说明详见配置说明

### v1.2.x 及更早
- 见旧版历史：个人总结、群友画像/MBTI、金句、炫压抑评级、24H 活跃轨迹、定时自动总结等功能。

## 📜 许可证

GPL-3.0-or-later

## 👨‍💻 作者

风花叶 ([khiqwq](https://github.com/khiqwq))

## 🙏 致谢

- 原插件作者：久远 ([saberlights](https://github.com/saberlights))
- 图片风格灵感来自 astrbot 的 scrapbook 风格总结
- 感谢 MaiBot 项目提供的插件框架
