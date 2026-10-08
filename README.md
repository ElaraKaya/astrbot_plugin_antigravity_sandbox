# Antigravity 沙盒任务

目前 aistudio.google.com 对该沙盒 API 的免费层级，每日有 100 次免费调用额度。

AstrBot 插件：用 Google **Antigravity** 托管智能体在云端沙盒里跑任务。提交后马上拿到任务 ID，需要时再取回结果。

- 插件名：`astrbot_plugin_antigravity_sandbox`
- 作者：珂夜
- 版本：1.6.4
- 需要 AstrBot `>=4.5.7,<5`

## 介绍

Google 提供了云端 Linux 沙盒智能体（Antigravity）。装好本插件后，AstrBot 可以：

1. **提交任务** → 立刻返回 `task_id`、`sandbox_id`
2. **查询进度 / 取回文字回执**
3. **在同一个沙盒里续跑**（不丢文件）

产物要求沙盒保存在工作空间，需要时用 `/agget` 拉取；图床开启时也可以让沙盒上传。插件**不会**下载、解压整份环境快照。
无图床时也能够取回文本消息。

相关链接：

- 官方说明：[Antigravity Agent](https://ai.google.dev/gemini-api/docs/antigravity-agent)
- 本地控制台 / 协议网关（功能不同）：[OnsWayn/antigravity-agent-webui](https://github.com/OnsWayn/antigravity-agent-webui)

本插件只直连 Gemini Interactions / Environments API，也**不做** OpenAI 协议中转。本地独立控制台请用上面的 webui 项目；本插件在 AstrBot Dashboard 插件页提供「沙盒文件」Pages。



## Dashboard 沙盒文件（WebUI）

在 AstrBot 管理面板：

1. 打开 **插件** → **Antigravity 沙盒任务**
2. 进入插件详情里的 Pages：**沙盒文件**

可在该页：

- 查看本地短号索引（及远端补全）；左侧按沙盒分组，沙盒内是标记 task_id 的会话小卡
- 会话小卡显示完成状态（运行中 / 已完成 / 失败 / 已取消 / 未获取），可「获取状态」：查询远端状态、写入记录并在后台日志打印
- 浏览环境 `workspace` 文件（名称 / 路径 / 类型 / 大小 / 修改时间，修改时间取文件元数据的 `modified`，本地时区显示）
- 勾选或单行下载。服务端同时最多拉取 2 个文件，进度约每 500ms 更新一次，可以取消
- 拖拽或按钮上传到 `/workspace/<文件名>`
- **删除会话**：删除该交互与本地短号记录，不删文件、不影响其他会话；任何状态都能删，运行中的直接删
- **删除沙盒**（两步确认）：删除整个环境、共享文件与该沙盒全部会话短号，不可恢复
- **清理会话**：立即清理终态且闲置的会话，每个沙盒至少保留最近 1 条

API Key 不会下发到浏览器；服务端按沙盒绑定的 Key 访问 Gemini Environments Files API。

## Key 说明（必读）

填写的是 **Google AI Studio** 的 Gemini API Key（不是 OpenAI Key）。

- 免费层级每日 **100 次**调用，适合偶尔提交沙盒任务，不适合高频刷。
- Key 只写在本插件设置里，不要发到聊天。任务绑定只保存指纹，不会把明文 Key 写进状态文件。
- 可填多个 Key：按顺序轮询；遇到 401 / 403 / 普通 429 会换下一个。
- Antigravity 属于预览能力，接口和配额可能变化。

申请地址：[Google AI Studio API Key](https://aistudio.google.com/apikey)

## 安装

### 方式一：插件市场（推荐）

在 AstrBot 管理面板打开 **插件市场**，搜索 `astrbot_plugin_antigravity_sandbox` 或「Antigravity 沙盒」，点击安装即可。

### 方式二：用仓库地址安装

在 AstrBot 管理面板的插件管理里，通过 GitHub 仓库地址安装：

```text
https://github.com/ElaraKaya/astrbot_plugin_antigravity_sandbox
```

或在聊天里（需管理员）执行：

```text
plugin i https://github.com/ElaraKaya/astrbot_plugin_antigravity_sandbox
```

安装后在插件列表里启用 / 重载，再打开本插件配置填写 Key。

## 配置

最少只要填 Key。配置分成接入、代理、聊天拉取、回执、测试功能、图床和环境回收几组：

| 分组 | 配置 | 说明 |
| --- | --- | --- |
| 接入与模型 | `gemini_api_keys` | **必填。** AI Studio 的 Gemini Key，可多个 |
| 接入与模型 | `default_model` | 底层模型。默认 `auto`，一般不用改 |
| 接入与模型 | `sandbox_agent` | 默认 `antigravity-preview-09-2026`。Google 发布新沙盒版本后只改这里，不要填 `gemini-*` 模型名 |
| 接入与模型 | `submit_background` | 默认开启：提交后立刻回 ID，稍后取回 |
| 接入与模型 | `max_in_progress_per_key` | 每个 Key 同时处于 `in_progress` 的任务上限，默认 4。新建任务优先给更空闲的 Key；额度相同则轮到上一把的下一把。续接不换 Key |
| 网络代理 | `proxy` | 插件访问 Gemini 的代理。留空直连。图床 Webhook 不走这里 |
| 聊天拉取 | `max_mb` | `/agget` 的大小上限，单位 MB。默认 0，不限制。`/agget` 不设总超时，进度停 10 分钟会中止 |
| 聊天拉取 | `tool_max_mb` | `get_sandbox_task` 的大小上限，单位 MB。默认 0，不限制。该工具固定 90 秒超时 |
| 聊天拉取 | `progress` | 拉取进度反馈。默认开启。下载每跨过一次阈值就发一条进度。文件小于阈值时不发 |
| 聊天拉取 | `progress_mb` | 进度反馈阈值，单位 MB。默认 40。小于 1 时按 40 处理 |
| 取回回执 | `image_receipt` | 图片回执。默认开启，仅对 status 为 `completed` 且达到字数阈值的取回生效；其它状态或字数不足时发纯文本 |
| 取回回执 | `image_receipt_min_length` | 触发图片回执的最少字符数。默认 200。开启图片回执且 status 为 completed 时，仅当回执内容长度大于或等于此阈值时才会渲染成图片，否则保持纯文本发送。配置为 0 或负数时视为不限制长度（任意长度均转图） |
| 取回回执 | `truncate_chars` | 文本回执截断字符数，默认 2000。只有确定会发图片回执时不截断 |
| 取回回执 | `receipt_template` | 渲染模板。默认留空表示沿用 AstrBot 当前全局选中的模板，可指定 `base`、`astrbot_vitepress`、`astrbot_powershell` 或自定义模板名 |
| 取回回执 | `sandbox_path_receipt` | 沙盒路径回执。默认开启。只决定提交、续接和取回的回执里写不写工作空间路径，以及 `/agget` 或 `get_sandbox_task` 提示。沙盒始终被要求把产物存到 `/workspace/` |
| 取回回执 | `auto_retrieve` | 默认开启：提交或续接满 1 小时后后台查询一次，只写日志 |
| 取回回执 | `file_status_poll` | 完成标记提醒。默认开启。沙盒做完会写一个空的完成标记，插件按间隔去看，最多 1 小时。看到了就提示用 `/agr` 取回。`/agr` 问到还在跑会继续看，问到已结束才停 |
| 取回回执 | `file_poll_interval` | 完成标记查询间隔，默认 30 秒，最小 5 秒 |
| 测试功能 | `completed_notify` | 工具提交也提醒。默认关闭。`/ags`、`/agc` 照常提示；模型用工具提交的默认只写日志。打开后工具提交也提示 |
| 图床上传 | `enabled` | 启用上传图床。关闭后不再要求沙盒上传。产物仍要求存到工作空间 |
| 图床上传 | `webhook_url` | 图床上传地址（可选） |
| 图床上传 | `public_base_url` | 图床公网基础地址（可选）。发给沙盒的地址用这个 |
| 图床上传 | `receipt_base_url` | 回执基础地址。默认空。只替换回执链接的域名，空则用公网访问基础地址。QQ 群里频繁发送未备案域名，域名容易被标记 |
| 图床上传 | `receipt_url` | 回执图床 URL。默认开启。关闭后回执不写图床链接。是否上传仍由总开关决定 |
| 图床上传 | `token` | 图床 Token（可选）。存为项目内独占的 Gemini bearer 凭据，由出站代理注入到 Webhook 主机 |
| 图床上传 | `prefix` | 默认上传目录，默认 `agysb` |
| 沙盒环境回收 | `auto_cleanup` | 默认开启：自动回收闲置沙盒，避免存储配额打满 |
| 沙盒环境回收 | `idle_ttl_hours` | 默认 72（3 天）：沙盒闲置多久可回收 |
| 沙盒环境回收 | `cleanup_sessions` | 默认开启：自动清理终态且闲置的会话。每个沙盒至少保留最近 1 条，不删共享文件 |
| 沙盒环境回收 | `session_idle_hours` | 默认 24：会话闲置多久可清理 |
| 沙盒环境回收 | `session_keep_recent` | 默认 1：每个沙盒至少保留的最近会话数，填 0 也按 1 处理 |
| 沙盒环境回收 | `scope` | 建议保持 `tracked`（只删本插件建过的）。`all` 可能影响同一 Key 下其它工具 |

运行时状态（短号索引、环境记录、自动取回队列）保存在 AstrBot 的 `data/plugin_data/astrbot_plugin_antigravity_sandbox/`。升级或重装插件不会覆盖这些数据。旧版本写在插件目录里的 `task_keys.json` / `task_index.json` / `env_meta.json` / `auto_retrieve.json` 会在启动时自动迁过去，并去掉明文 Key。



## 使用

### 指令

| 指令 | 作用 |
| --- | --- |
| `/aghelp` | 帮助 |
| `/agsubmit` 或 `/ags` | 提交新沙盒任务。默认产出 `result.md`。优先用更空闲的 Key；进行中数量一样则换到上一把的下一把 |
| `/agretrieve` 或 `/agr` | 按任务编号取回执。确定会发图片回执时不截断，其余按 `truncate_chars` |
| `/agcontinue` 或 `/agc` | 在选中会话里续跑，不换 Key、短号不变，不跳到同沙盒的另一独立会话。不写类型时默认 md。聊天里引用/附带的**图片**直接作为多模态输入发给 agent；其它文件 PUT 进已有沙盒并回读校验 |
| `/agnew <任务编号\|sandbox_id> [类型...] <任务文本>` | 使用旧任务绑定的 Key，在原沙盒开启独立新会话，成功后分配新短号，旧短号不变。共享文件，不继承旧对话。聊天里引用/附带的图片直接发给 agent 看。也可直接传 sandbox_id 收养只在远端存在的沙盒 |
| `/agls <任务编号>` | 列出该沙盒 workspace 文件，渲染成表格图片发送 |
| `/agget <任务编号> <完整路径>` | 后台拉取文件，马上返回，好了再发到聊天。大小上限见配置，默认不限制。进度默认每 40MB 一次。不设总超时，进度停 10 分钟会中止 |
| `/agenvlist` 或 `/agels` | 管理员：查看当前项目沙盒占用 |
| `/agenvcleanup` 或 `/agecl` | 管理员：回收闲置环境。`all` 扫整个项目；短号立即删指定沙盒 |

类型写在任务文本**前面**，任务文本可含空格，整段原样交给沙盒。例如：

```text
/agsubmit png 查询今日新闻
/agsubmit svg png 画一张示意图
/agcontinue 0001 html 把上一轮结果改成网页
```

不要把类型写在最后：解析器从前往后吃类型，后面全部当任务文本，避免空格截断或把任务末尾单词误当成类型丢掉。

短号是四位数字（如 `0001`）。`/agcontinue` 续接不换号，更新为选中会话的最新一轮；不能从该会话更早的祖先 ID 分叉，也不会切到同沙盒的另一独立会话。

`/agnew` 复用环境但开启独立上下文，例如 `/agnew 0001 html 使用现有文件重新生成首页`。提交前使用旧任务绑定的 Key 查询沙盒是否存在；编号不存在、沙盒已删除、绑定 Key 已移除或其进行中任务数达到上限时拦截，不轮换其它 Key。成功后得到新编号（例如 `0002`），`0001` 的任务绑定与取回信息保留。新会话不要求旧任务已经 `completed`，两者各自占用任务名额；默认每 Key 上限为 4，用户显式配置的值不被覆盖。

这些会话共享整个沙盒文件系统，并非文件隔离。多个会话同时改同一文件可能互相覆盖；需要文件隔离时使用 `/ags` 新建沙盒。`/agc` 和 `/agnew` 会自动收集聊天里引用/附带的图片（按文件头识别，JPEG/PNG/GIF/WebP/BMP，单张上限 10MB），作为多模态输入直接发送，agent 可立即查看；其它附件仍走 Files PUT 写入沙盒（上传后会回读校验字节数）。删除任一关联短号的沙盒会影响该沙盒全部会话。

`/agnew` 也可以直接传 `sandbox_id`（32 位十六进制），用于只在远端存在、本地没有会话的沙盒：插件会依次用配置里的 Key 探测该沙盒，用第一个能访问的 Key 在里面开启首个会话。之后这个沙盒就和其他沙盒一样有会话卡、可续接、可清理。注意这会在沙盒里**真实产生一次交互**，消耗一次调用额度。

会话可以被单独删除（WebUI 的「删除会话」或沙盒文件页操作），删除的是该交互与本地短号记录，共享文件与其他会话不受影响；**任何状态都能删**——运行中（in_progress / queued / requires_action）的会话直接删，专门对付卡死永远不会自己跑完的会话（真机实测 DELETE 对运行中的交互直接成功）；远端删除失败也会清掉本地记录，但会在回执里说明该交互可能仍在沙盒中运行。自动清理只处理已终态且闲置超过「会话闲置小时」的会话，且每个沙盒至少保留最近 1 条。
续接前若未取回会先自动取回上一轮。上一轮已 `completed`、开启了图片回执且正文字数达标时，把回执图片直接发到聊天，否则发纯文本；`status` 不是 `completed` 则只返回当前状态、不续接。

不写类型时，提交和续接都默认产出 `result.md`。插件按这一轮的提交时间加上 `YYMMDDHHMMSS_` 前缀，并要求沙盒把产物保存到 `/workspace/` 下的这个文件名；md 写入本轮回复。图床总开关开启且 Webhook、公网地址、Token 齐全时，同时要求沙盒上传该文件。沙盒路径回执、回执图床 URL 两个开关只决定提交、续接和取回的回执里写不写路径或链接。回执里的图床链接可用「回执基础地址」换域名，发给沙盒的仍是公网访问基础地址。聊天里引用/附带的图片作为多模态 input 分节直接发送（`/ags` 的附件则作为 inline sources 挂载，上限 1MB/文件、2MB 合计）；其它续接附件用 PUT 写入已有环境，上传后会回读校验字节数，不一致时回执明示未生效；没有后缀时按 `.md` 写入，某个文件失败不影响续接。聊天取回时，只有确定会发图片回执才不截断正文；否则按 `truncate_chars`（默认 2000）截断。渲染或发送失败时回退成截断后的纯文本。写了 `html` / `png` 等类型时，沙盒仍按这些文件名保存，并额外要求一份带时间戳的 `result.md`。调用方已经要了 `result.md` 时不加第二份。

`/agls` 会把文件列表整理成表格图片发出来（目录在前、文件按大小降序），渲染失败时回退成原来的制表符文本。LLM 工具 `list_sandbox_task` 仍是文本列表，只保留 name / path / type / size_bytes。

「取回回执 / 完成标记提醒」开启时，插件还会在给沙盒的提示词末尾追加完成标记要求：用本轮提交时间戳拼一个 `<时间戳>.completed` 空文件（例如 `260928153045.completed`），沙盒建好后不需要在回执里提它。插件按「完成标记查询间隔」（默认 30 秒）去看这个文件在不在，最多 1 小时，看到了就在原会话提示 `/agr`。`/agr` 问到任务还在跑（`in_progress`）时继续看，问到已结束才停。这份空文件与 `result.md` 互不影响，也不要求上传图床。

看到完成标记后怎么提示，按提交来源区分：`/ags`、`/agc` 提交的照常在原会话提示并 @ 提交人；模型调用工具提交的默认只写日志、不提示，避免打断当前对话。需要工具提交也提示时，打开「取回回执 / 工具提交也提醒」。



`/agget` 按「任务编号 + 完整路径」拉取文件，只有一个沙盒时可省略编号只填路径：

```text
/agget 0003 workspace/dist/index.html
/agget workspace/index.html          # 只有一个沙盒时可省略编号
/agget 0003:workspace/index.html     # 早期冒号写法仍兼容
```

`/agget` 会先回一句「开始拉取」，再在后台看列表里的 `size_bytes` 和 HEAD 的 Content-Length，对照「/agget 大小上限」（默认不限制）后流式下载。进度反馈开启时，每跨过一次阈值（默认 40MB）发一条进度；文件小于该阈值时不发。不设总超时。连续 10 分钟没有新的下载数据会中止，并建议改用图床或 WebUI。沙盒网络不一定能通。临时文件会定时清理。`get_sandbox_task` 用另一套「工具拉取大小上限」，并在 90 秒时中止。

### 给大模型用的工具

插件会注册五个工具，对话里直接说「去沙盒做某某事」即可：

- `submit_sandbox_task`：提交任务。优先用更空闲的 Key；进行中数量一样则换到上一把的下一把
- `retrieve_sandbox_task`：按短号取回。确定会发图片回执时插件直接把图片发给用户，工具只回简报
- `continue_sandbox_task`：同沙盒续跑，不换 Key。不写产出文件时默认 `result.md`。附件 PUT 进 workspace，不走 interaction sources
- `list_sandbox_task`：列出 workspace 文件
- `get_sandbox_task`：拉取单个文件。沙盒网络存疑，不一定能成功。大小上限见「工具拉取大小上限」，默认不限制；超过上限或 90 秒会中止

提交时把任务写清楚、独立，不要把无关聊天记忆硬塞进提示词。  
需要产出文件时，写原始文件名即可（如 `report.docx`），插件会自动加时间戳前缀，避免互相覆盖。文件名里的中文等非 ASCII 字符会变成下划线（`文转图模板.tar` → `260922123022_file.tar`），扩展名始终保留；想让图床链接带中文原名，需要把 `_safe_upload_name` 的正则放宽成支持 Unicode。

## 环境回收

Google 侧闲置环境不一定马上删，但项目**环境存储配额**容易先满，出现：

```text
Project environment storage quota exceeded
```

所以插件会在启动时、下次新建任务前，按默认 24 小时 TTL 回收闲置环境；配额 429 时也会先清再建。每个 Key 还会先留最近 `keep_recent` 个（默认 2），方便续接。

`/agenvcleanup` 不带参数：只扫本插件记录过、且已过 TTL / 不在最近保留里的闲置沙盒。  
`/agenvcleanup all`：范围换成整个 Gemini 项目，TTL 和最近保留保护仍然生效，所以刚用过的也删不掉。  
`/agenvcleanup 0002`：按短号立即删除对应沙盒，不受 TTL / 最近保留限制。

## 更新日志

**1.6.4**：收拢 1.6.3beta1 到 beta12。完成标记轮询、图床凭据改由出站代理注入、沙盒文件 resumable 写入、图片改走多模态 input、`/agget` 后台拉取、`/agnew` 独立会话、WebUI 按沙盒分组，以及删除会话不再看状态。详见 [CHANGELOG.md](CHANGELOG.md)。

**1.6.3beta7**：`get_sandbox_task` 单独使用「工具拉取大小上限」，超时 90 秒。`/agget` 改为后台拉取，不设总超时，进度停 10 分钟会中止并建议换方式。详见 [CHANGELOG.md](CHANGELOG.md)。

**1.6.3beta6**：`/agget` 和 `get_sandbox_task` 的大小上限改到配置里，默认不限制。进度反馈默认每 40MB 一次，可以关闭或改阈值，比阈值小的文件不发进度。详见 [CHANGELOG.md](CHANGELOG.md)。

**1.6.3beta4**：图床 Token 改为项目内 bearer 凭据，由出站代理注入到 Webhook 主机，不再挂到沙盒文件。续接附件只 PUT 进沙盒，不再由插件再传一次图床。本版不删除历史沙盒里的 Token 文件，旧 Token 要在图床侧轮换后才算失效。详见 [CHANGELOG.md](CHANGELOG.md)。

**1.6.3beta3**：完成标记提醒挪到「取回回执」，默认开启。工具提交也提醒放回「测试功能」，默认关闭。单 Key 进行中上限默认 4。新增查询间隔，默认 30 秒。`/agr` 问到 `in_progress` 不再停止完成标记轮询，问到已结束才停。

**1.6.3beta2**：「取回文件查询状态」取到完成标记后的提示按提交来源区分。指令提交（`/ags`、`/agc`）照常在原会话提示并 @ 提交人；外层模型调用 `submit_sandbox_task`、`continue_sandbox_task` 提交的任务默认只写日志，不再往会话里弹「请使用 /agr 取回」。定时任务和模型自主提交因此不会再在群里插入与当前对话无关的消息。新增「测试功能 / 工具提交也提示完成」，默认关闭，打开后工具提交也照常提示。

**1.6.3beta1**：  修复续接任务部分 BUG 与编号逻;每轮任务强制生成带时间戳的 result.md;local 环境下调用 get_sandbox_task 会直接写入当前会话工作区;新增「取回文件查询状态」机制，改为轮询 .completed 空标记文件

**1.6.3**：产物始终要求沙盒存到工作空间。图床开着才要求上传。去掉插件自己把回执文字上传成 md。新增沙盒路径回执、回执基础地址、回执图床 URL。

**1.6.2**：新建任务按各 Key 的进行中数量负载均衡。更空闲的优先；额度相同则提交到上一把 Key 的下一把。续接不换 Key。

**1.6.1**：取回查询的 HTTP 500 并入超时 / 504 兜底，重试一次后仍失败则提示任务可能仍在跑。

**1.6**（接在 1.5.15 之后）：沙盒文件进度与并发、每 Key 进行中上限、续接 PUT、回执截断、出站代理、`/agls` 表格与 `/agget` 按路径拉取、短指令、帮助与回执文案、取回 7 秒超时并重试一次，以及后台任务自动 `store=true`。

详见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

本项目基于 [MIT License](LICENSE) 开源。

```text
MIT License

Copyright (c) 2026 Elara

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

