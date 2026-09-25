# Antigravity 沙盒任务

目前 aistudio.google.com 对该沙盒 API 的免费层级，每日有 100 次免费调用额度。

AstrBot 插件：用 Google **Antigravity** 托管智能体在云端沙盒里跑任务。提交后马上拿到任务 ID，需要时再取回结果。

- 插件名：`astrbot_plugin_antigravity_sandbox`
- 作者：珂夜
- 版本：1.6.1
- 需要 AstrBot `>=4.5.7,<5`

## 介绍

Google 提供了云端 Linux 沙盒智能体（Antigravity）。装好本插件后，AstrBot 可以：

1. **提交任务** → 立刻返回 `task_id`、`sandbox_id`
2. **查询进度 / 取回文字回执**
3. **在同一个沙盒里续跑**（不丢文件）

产物建议让智能体直接传到你配置的图床；插件**不会**下载、解压整份环境快照。
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

- 查看本地短号索引（及远端补全）的沙盒列表
- 浏览环境 `workspace` 文件（名称 / 路径 / 类型 / 大小）
- 勾选或单行下载。服务端同时最多拉取 2 个文件，进度约每 500ms 更新一次，可以取消
- 拖拽或按钮上传到 `/workspace/<文件名>`
- **删除沙盒**（二次确认；会调远端删除并清理本地短号 / key 映射）

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

最少只要填 Key。配置分成接入、代理、回执、图床和环境回收几组：

| 分组 | 配置 | 说明 |
| --- | --- | --- |
| 接入与模型 | `gemini_api_keys` | **必填。** AI Studio 的 Gemini Key，可多个 |
| 接入与模型 | `default_model` | 底层模型。默认 `auto`，一般不用改 |
| 接入与模型 | `sandbox_agent` | 默认 `antigravity-preview-05-2026`。Google 发布新沙盒版本后只改这里，不要填 `gemini-*` 模型名 |
| 接入与模型 | `submit_background` | 默认开启：提交后立刻回 ID，稍后取回 |
| 接入与模型 | `max_in_progress_per_key` | 每个 Key 同时处于 `in_progress` 的任务上限，默认 1。新建任务换到空闲 Key；续接不换 Key |
| 网络代理 | `proxy` | 插件访问 Gemini 的代理。留空直连。图床 Webhook 不走这里 |
| 取回回执 | `image_receipt` | 图片回执。默认开启，仅对 status 为 `completed` 且达到字数阈值的取回生效；其它状态或字数不足时发纯文本 |
| 取回回执 | `image_receipt_min_length` | 触发图片回执的最少字符数。默认 200。开启图片回执且 status 为 completed 时，仅当回执内容长度大于或等于此阈值时才会渲染成图片，否则保持纯文本发送。配置为 0 或负数时视为不限制长度（任意长度均转图） |
| 取回回执 | `truncate_chars` | 文本回执截断字符数，默认 2000。只有确定会发图片回执时不截断 |
| 取回回执 | `receipt_template` | 渲染模板。默认留空表示沿用 AstrBot 当前全局选中的模板，可指定 `base`、`astrbot_vitepress`、`astrbot_powershell` 或自定义模板名 |
| 取回回执 | `auto_retrieve` | 默认开启：提交或续接满 1 小时后后台查询一次，只写日志 |
| 图床上传 | `enabled` | 启用上传图床。关闭后不挂载 Token、不上传文件，也不上传 md |
| 图床上传 | `webhook_url` | 图床上传地址（可选） |
| 图床上传 | `public_base_url` | 图床公网基础地址（可选） |
| 图床上传 | `token` | 图床 Token（可选，会挂到沙盒 `/workspace/upload.token`） |
| 图床上传 | `prefix` | 默认上传目录，默认 `agysb` |
| 沙盒环境回收 | `auto_cleanup` | 默认开启：自动回收闲置沙盒，避免存储配额打满 |
| 沙盒环境回收 | `idle_ttl_hours` | 默认 24：闲置多久可回收 |
| 沙盒环境回收 | `scope` | 建议保持 `tracked`（只删本插件建过的）。`all` 可能影响同一 Key 下其它工具 |

运行时状态（短号索引、环境记录、自动取回队列）保存在 AstrBot 的 `data/plugin_data/astrbot_plugin_antigravity_sandbox/`。升级或重装插件不会覆盖这些数据。旧版本写在插件目录里的 `task_keys.json` / `task_index.json` / `env_meta.json` / `auto_retrieve.json` 会在启动时自动迁过去，并去掉明文 Key。



## 使用

### 指令

| 指令 | 作用 |
| --- | --- |
| `/aghelp` | 帮助 |
| `/agsubmit` 或 `/ags` | 提交新沙盒任务。默认产出 `result.md`。进行中任务满了会换空闲 Key |
| `/agretrieve` 或 `/agr` | 按任务编号取回执。确定会发图片回执时不截断，其余按 `truncate_chars` |
| `/agcontinue` 或 `/agc` | 同沙盒续跑，不换 Key。不写类型时默认 md 并返回预期网址。附件 PUT 进已有沙盒 |
| `/agls <任务编号>` | 列出该沙盒 workspace 文件，渲染成表格图片发送 |
| `/agget <任务编号> <完整路径>` | 拉取文件发到聊天。超过 20MB 或 90 秒则中止 |
| `/agenvlist` 或 `/agels` | 管理员：查看当前项目沙盒占用 |
| `/agenvcleanup` 或 `/agecl` | 管理员：回收闲置环境。`all` 扫整个项目；短号立即删指定沙盒 |

类型写在任务文本**前面**，任务文本可含空格，整段原样交给沙盒。例如：

```text
/agsubmit png 查询今日新闻
/agsubmit svg png 画一张示意图
/agcontinue 0001 html 把上一轮结果改成网页
```

不要把类型写在最后：解析器从前往后吃类型，后面全部当任务文本，避免空格截断或把任务末尾单词误当成类型丢掉。

短号是四位数字（如 `0001`）。续接不换号，覆盖为该沙盒最新一轮；不能从更早的祖先 id 分叉。  
续接前若未取回会先自动取回上一轮。上一轮已 `completed`、开启了图片回执且正文字数达标时，把回执图片直接发到聊天，否则发纯文本；`status` 不是 `completed` 则只返回当前状态、不续接。

不写类型的续接不会再要求沙盒上传 `result.md`（避免复制旧报告交差），但会按 md 给出预期网址；取回 `completed` 且图床开启时，插件把本轮 `output_text` 做成带时间戳的 md 传到图床。续接附件用 PUT 写入已有环境，不走 interaction sources；没有后缀时按 `.md` 写入，某个文件失败不影响续接。聊天取回时，只有确定会发图片回执才不截断正文；否则按 `truncate_chars`（默认 2000）截断。渲染或发送失败时回退成截断后的纯文本。指定了 `html` / `png` 等类型时，仍由沙盒按该类型上传。

`/agls` 会把文件列表整理成表格图片发出来（目录在前、文件按大小降序），渲染失败时回退成原来的制表符文本。LLM 工具 `list_sandbox_task` 仍是文本列表，只保留 name / path / type / size_bytes。

`/agget` 按「任务编号 + 完整路径」拉取文件，只有一个沙盒时可省略编号只填路径：

```text
/agget 0003 workspace/dist/index.html
/agget workspace/index.html          # 只有一个沙盒时可省略编号
/agget 0003:workspace/index.html     # 早期冒号写法仍兼容
```

`/agget` 会先看列表里的 `size_bytes`，再用 HEAD 的 Content-Length 卡住 20MB，然后流式下载。沙盒网络不一定能通，失败时请改用图床或 WebUI。临时文件会定时清理。`get_sandbox_task` 工具同样按 任务编号 + 文件名 拉取，语义与前者一致。

### 给大模型用的工具

插件会注册五个工具，对话里直接说「去沙盒做某某事」即可：

- `submit_sandbox_task`：提交任务。Key 的进行中任务满了会换空闲 Key
- `retrieve_sandbox_task`：按短号取回。确定会发图片回执时插件直接把图片发给用户，工具只回简报
- `continue_sandbox_task`：同沙盒续跑，不换 Key。附件 PUT 进 workspace，不走 interaction sources
- `list_sandbox_task`：列出 workspace 文件
- `get_sandbox_task`：拉取单个文件。沙盒网络存疑，不一定能成功；超过 20MB 或 90 秒会中止

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

