# Antigravity 沙盒任务

AstrBot 插件：用 Google **Antigravity** 托管智能体在云端沙盒里跑任务。提交后马上拿到任务 ID，需要时再取回结果。

- 插件名：`astrbot_plugin_antigravity_sandbox`
- 作者：珂夜
- 版本：1.5.3
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

本插件只直连 Gemini Interactions / Environments API，**不做** WebUI，也**不做** OpenAI 协议中转。那些请用上面的 webui 项目。

## Key 说明（必读）

填写的是 **Google AI Studio** 的 Gemini API Key（不是 OpenAI Key）。

- 免费层级大约每天 **100 次**调用，适合偶尔提交沙盒任务，不适合高频刷。
- Key 只写在本插件设置里，不要发到聊天。
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

最少只要填 Key：

| 配置 | 说明 |
| --- | --- |
| `gemini_api_keys` | **必填。** AI Studio 的 Gemini Key，可多个 |
| `default_model` | 默认 `auto`，一般不用改 |
| `submit_background` | 默认开启：提交后立刻回 ID，稍后取回 |
| `upload_webhook_url` | 图床上传地址（可选） |
| `upload_public_base_url` | 图床公网基础地址（可选） |
| `upload_token` | 图床 Token（可选，会挂到沙盒 `/workspace/upload.token`） |
| `upload_prefix` | 默认上传目录，默认 `agysb` |
| `env_auto_cleanup` | 默认开启：自动回收闲置沙盒，避免存储配额打满 |
| `env_idle_ttl_hours` | 默认 24：闲置多久可回收 |
| `env_cleanup_scope` | 建议保持 `tracked`（只删本插件建过的）。`all` 可能影响同一 Key 下其它工具 |



## 使用

### 指令

| 指令 | 作用 |
| --- | --- |
| `/aghelp` | 帮助 |
| `/agsubmit 任务内容` | 提交新沙盒任务 |
| `/agretrieve <task_id> <sandbox_id>` | 取回执 |
| `/agcontinue <sandbox_id> <上一轮 task_id> 续写内容` | 同沙盒续跑 |
| `/agenvlist` | 查看本插件记录的环境 |
| `/agencleanup` | 回收闲置环境（慎用 `all`） |

短号是四位数字（如 `0001`）。续接不换号，覆盖为该沙盒最新一轮；不能从更早的祖先 id 分叉。  
续接前若未取回会先自动取回上一轮；`status` 不是 `completed` 则只返回当前状态、不续接。

### 给大模型用的工具

插件会注册三个工具，对话里直接说「去沙盒做某某事」即可：

- `submit_sandbox_task`：提交任务
- `retrieve_sandbox_task`：按 ID 取回
- `continue_sandbox_task`：同沙盒续跑

提交时把任务写清楚、独立，不要把无关聊天记忆硬塞进提示词。  
需要产出文件时，写原始文件名即可（如 `report.docx`），插件会自动加时间戳前缀，避免互相覆盖。

## 环境回收

Google 侧闲置环境不一定马上删，但项目**环境存储配额**容易先满，出现：

```text
Project environment storage quota exceeded
```

所以插件会在启动时、下次新建任务前，按默认 24 小时 TTL 回收闲置环境；配额 429 时也会先清再建。

默认只动本插件跟踪过的沙盒。

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

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

