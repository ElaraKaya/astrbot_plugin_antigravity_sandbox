# Google Antigravity / Gemini 托管智能体用法笔记

整理日期：2026-09-08（UTC+8）。只记录实际抓取到的官方文档内容，**不编造接口**。

**本插件新功能推出日期：** 环境回收（Environments list/delete + 24h TTL + 存储配额 429 就地重试）于 **2026-09-08** 随插件 1.4.0 上线。

HTML 文档站 `https://ai.google.dev/gemini-api/docs/*.md` 返回的是 DevSite JS 外壳。有效正文来自对应的 `.md.txt` 以及 REST 参考。

## 已抓取的官方页面

| 文档 | URL | 抓取形态 |
| --- | --- | --- |
| Antigravity Agent | https://ai.google.dev/gemini-api/docs/antigravity-agent | `.md.txt` 成功（2026-09-08 再抓） |
| Environments | https://ai.google.dev/gemini-api/docs/agent-environment | `.md.txt` 成功（2026-09-08 再抓） |
| Environments REST | https://ai.google.dev/api/environments | `.md.txt` 成功（2026-09-08） |
| Managed Agents Quickstart | https://ai.google.dev/gemini-api/docs/managed-agents-quickstart | `.md.txt` 成功 |
| Interactions overview | https://ai.google.dev/gemini-api/docs/interactions-overview | `.md.txt` 成功 |
| Building Managed Agents | https://ai.google.dev/gemini-api/docs/custom-agents | `.md.txt` 成功 |
| Background execution | https://ai.google.dev/gemini-api/docs/background-execution | `.md.txt` 成功 |
| Agents overview | https://ai.google.dev/gemini-api/docs/agents | `.md.txt` 成功 |
| Interactions REST | https://ai.google.dev/api/interactions | `.md.txt` 成功 |

未克隆任何 GitHub 仓库。本插件只直连 Gemini REST，不实现参考项目的本地控制台或协议中转/TPM/多 Key 网关。官方 Triggers（cron 调度）本插件不调用。

## Agent 与模型

- **Agent id（当前预览）：** `antigravity-preview-05-2026`
- **默认底层模型（官方 antigravity-agent 正文，2026-09-08）：** `gemini-3.8-flash`  
  原文：It is built with Gemini 3.8 Flash。If you omit `agent_config`, the agent defaults to `gemini-3.8-flash`。
- **`agent_config.model` 官方列出的取值（2026-09-08）：**

  | 模型 | `agent_config.model` |
  | --- | --- |
  | Gemini 3.8 Flash（默认） | `gemini-3.8-flash` |
  | Gemini 3.7 Flash | `gemini-3.7-flash` |
  | Gemini 3.6 Flash | `gemini-3.6-flash` |
  | Gemini 3.5 Flash | `gemini-3.5-flash` |
  | Gemini 3.5 Flash-Lite | `gemini-3.5-flash-lite` |

- 配置形态：

  ```json
  "agent": "antigravity-preview-05-2026",
  "agent_config": { "type": "antigravity", "model": "gemini-3.8-flash" }
  ```

- 用 `agents.create` 保存的**具名**托管智能体：创建时锁定模型，**交互时不能再覆盖** `agent_config.model`。本插件只调用预置 agent。
- 插件设置 `default_model=auto`（或留空）时不发送 `model`；若同时没有 `max_total_tokens`，则整段省略 `agent_config`，由服务端默认/路由。指定上表之一则强制写入。

注意：2026-08-30 抓取时默认还是 Gemini 3.7 Flash；**2026-09-08 官方正文已改为 Gemini 3.8 Flash**。

## Interactions API

- 端点：`POST https://generativelanguage.googleapis.com/v1beta/interactions`
- 鉴权：请求头 `x-goog-api-key`
- 查询：`GET https://generativelanguage.googleapis.com/v1beta/interactions/{id}`
- 后台任务：创建时 `"background": true`，立即返回 id，再 GET 轮询 `status`
- 部分 REST 示例带 `Api-Revision: 2026-05-20`
- `output_text`：SDK 便捷字段。REST 响应示例里常见的是 `steps[].type == "model_output"` 的 `content[].text`。插件两者都读。

### 状态（REST Interaction.status）

`in_progress`、`queued`、`requires_action`、`completed`、`failed`、`cancelled`、`incomplete`、`budget_exceeded`

后台文档强调轮询直到 `completed` / `failed`。`incomplete` 出现在 `max_total_tokens` 预算耗尽时（best-effort，可能略超）。

### 对话 vs 沙盒（两个独立维度）

| 维度 | 字段 | 作用 |
| --- | --- | --- |
| 对话 | `previous_interaction_id` | 继承聊天 / 推理 / 工具轨迹 |
| 环境 | `environment` | 继承文件、已装包、沙盒状态 |

- 省略 `previous_interaction_id`、只传 `environment=<environment_id>`：同沙盒**新会话**（文件还在，对话不继承）。
- 传 `previous_interaction_id` 且 `environment="remote"`：新沙盒、旧对话。
- 后台延续：前序必须离开 `in_progress`，否则 `400`。托管智能体延续时必须**同时**带 `previous_interaction_id` 和 `environment`。
- `store=false` 与 `background=true` / 后续 `previous_interaction_id` 不兼容。后台要求 `store=true`（默认）。
- 数据保留（overview）：付费档交互约 55 天，免费档约 1 天。

## environment

三种形态：

1. `"remote"`：新建沙盒
2. `"env_abc123"`：复用已有 `environment_id`
3. 对象：`{"type":"remote", "sources":[...], "network":...}` 新建并挂源 / 网络规则

REST `EnvironmentConfig` 另有 `environment_id`：指定则**更新已有环境**而不是新建。插件在「复用沙盒 + 再挂 inline 文件」时使用该字段。延续会话（`previous_interaction_id`）时官方不允许再带 `sources`。

响应里的 `environment_id` 即沙盒 id。

### sources（官方三种 + REST 第四种）

agent-environment 教程表：

| type | 含义 | 限制 |
| --- | --- | --- |
| `repository` | 按 URL clone 到 `target` | 500 MB |
| `gcs` | 从 Cloud Storage 拷到 `target` | 2 GB |
| `inline` | 把**原始文本**写到 `target` | 每文件 1 MB，合计 2 MB |

REST `EnvironmentConfig.Source` 额外字段：

- `content`：`type=inline` 时的内容（string）
- `encoding`：可选，例如 `base64`
- `source` / `target` / `type`
- `type` 取值：`gcs`、`inline`、`repository`、`skill_registry`

本插件附件只用 **inline**。对非 UTF-8 文件发 `encoding=base64`。`target` 不能是 `/`。

### 生命周期与资源

| 项 | 官方描述 |
| --- | --- |
| Created | `environment:"remote"` 或 config 对象时创建 |
| Active | 交互进行中 |
| Idle | 闲置约 15 分钟后自动快照并停机 |
| Offline | 自上次活跃起保留 7 天，可凭 id 恢复（冷启动） |
| Deleted | 7 天 TTL 或**手动删除**。过期 id → 404 |
| CPU / 内存 | 4 核 / 16 GB |
| 预览期算力 | CPU/内存/沙盒执行**不计费** |
| 预装 | Ubuntu，Python 3.12（numpy/pandas/requests/google-genai 等），Node.js 22，以及 curl/git/jq 等 |

官方明确：不要干等 7 天 TTL，工作流结束时应调用 Environments API **显式删除**。项目级环境存储配额耗尽时，创建新环境会返回 HTTP 429，`message=Project environment storage quota exceeded`，`code=too_many_requests`。

## Environments API（2026-09-08 抓取）

官方 REST：https://ai.google.dev/api/environments （Beta，`/v1beta/`）

| 操作 | 方法 | URL |
| --- | --- | --- |
| List | `GET` | `https://generativelanguage.googleapis.com/v1beta/environments?pageSize=1000` |
| Get | `GET` | `https://generativelanguage.googleapis.com/v1beta/environments/{id}` |
| Delete | `DELETE` | `https://generativelanguage.googleapis.com/v1beta/environments/{id}` |
| Create | `POST` | `https://generativelanguage.googleapis.com/v1beta/environments`（本插件不直接调用；提交任务时由 Interactions 创建） |

鉴权：请求头 `x-goog-api-key`。

List 查询参数：`page_size` / `pageSize`（默认 50，最大 1000）、`page_token` / `pageToken`。响应含 `environments[]` 与 `next_page_token`。

List 示例：

```text
GET https://generativelanguage.googleapis.com/v1beta/environments?pageSize=10
Header: x-goog-api-key
```

Delete 示例：

```text
DELETE https://generativelanguage.googleapis.com/v1beta/environments/YOUR_ENVIRONMENT_ID
Header: x-goog-api-key
```

成功时 Delete 响应体为空。SDK 写法是 `client.environments.delete(name="environments/YOUR_ENVIRONMENT_ID")`。

Environment 资源字段（REST，output only 居多）：`id` / `environment_id`、`created`、`last_accessed`、`updated`、`file_count`、`size_bytes`、`status`（`active` / `expired`）、`sources`、`network`。

教程 List 示例用 `environment_id`；REST 资源定义用 `id`。本插件读取时两者都认，并去掉 `environments/` 前缀。

## 插件环境回收（推出日期：2026-09-08）

插件 1.4.0 起实现官方建议的显式删除，而不是只建不删。

| 项 | 行为 |
| --- | --- |
| 新建任务 | 仍 `environment: "remote"`，每次新沙盒 |
| 续接任务 | 复用 `sandbox_id`，刷新本地 `last_used_at` |
| 取回任务 | 不立刻删除（要给 `/agcontinue` 窗口） |
| 自动回收 | 启动时、下次 submit 前：删除闲置超过 **24 小时** 的环境；每个 Key 至少留最近 **2** 个 |
| 存储 429 | 识别 `environment storage quota`，在**当前 Key** 紧急回收后用同一把 Key 重试一次，再考虑切换备用 Key |
| 范围 | 默认 `tracked`（本插件记录过的 sandbox_id）；`/agencleanup all` 或配置 `env_cleanup_scope=all` 才动整个项目 |
| 指令 | `/agenvlist`、`/agencleanup`（管理员） |

本地 `env_meta.json` 只记 `sandbox_id → last_used_at/status`，不写 API Key。

## 下载沙盒快照

尚无 SDK 方法。官方示例：

```text
GET https://generativelanguage.googleapis.com/v1beta/files/environment-{env_id}:download?alt=media
Header: x-goog-api-key
```

得到整个沙盒的 **TAR**。本插件不下载快照。

## 默认工具

不传 `tools` 时默认：`code_execution`、`google_search`、`url_context`。指定 `environment` 后自动启用文件系统工具。本插件提交任务不传 `tools`。

自动上下文压缩约在 135k tokens 触发。

## background

长任务用 `"background": true`。轮询 `GET /v1beta/interactions/{id}` 直到离开 `in_progress`。

取消示例不完全一致（缺口）：

- background-execution：`POST /v1beta/interactions/{id}/cancel`
- antigravity-agent（2026-09-08）：`POST /v1beta/interactions/{id}:cancel`

本插件的工具不调用 cancel。

## 配额与费用

- 免费档与付费档都可用（preview）
- 免费档有 rate limit 与 usage quota
- 一次托管智能体交互是多轮工具循环，**典型消耗 100k–3M tokens**
- 环境算力预览期不计费；按底层 Gemini token + 工具用量付费
- HTTP 429 可能是 RPM/TPM，也可能是**项目环境存储配额**（本插件 2026-09-08 起对后者做回收重试）
- `max_total_tokens` 写在 `agent_config`（`type=antigravity`）

交互对象保留：免费约 1 天，付费约 55 天。沙盒闲置 7 天删除（官方 TTL）；插件 24 小时后主动删。

## 预览限制（摘录）

- 功能与 schema 可能变化
- Antigravity **不支持** `temperature` / `top_p` / `top_k` / `stop_sequences` / `max_output_tokens`（400）
- 不支持 structured output
- 不可用：`file_search`、`computer_use`、`google_maps`
- 多模态输入目前仅 text + image（image 为 inline base64 `data`）；audio/video/document 不行
- MCP：需 Streamable HTTP，SSE 不行；server `name` 必须小写字母数字
- function calling 只支持 stateful（必须 `previous_interaction_id` 续轮）

## 本插件实际调用

只打 Google 官方 REST，不经过任何本地控制台或协议网关：

- `POST /v1beta/interactions`
- `GET /v1beta/interactions/{id}`
- `GET /v1beta/environments`
- `DELETE /v1beta/environments/{id}`
- （未使用）`GET /v1beta/files/environment-{id}:download?alt=media`

字段含义：`id` = 任务 / interaction；`environment_id` = 沙盒；省略 `previous_interaction_id` = 同沙盒新会话。

## 缺口 / 未在官方正文确认的点

1. 免费档精确 TPM 未出现在本次抓取的 agent/environment/interactions 正文中。
2. REST `output_text` 标注为 SDK 添加；原始 JSON 可能只有 `steps`。
3. 任务进行中下载 TAR 是否始终成功：文档未明确保证。
4. `POST .../cancel` vs `POST ...:cancel` 两处写法并存。
5. inline `encoding=base64` 只在 REST EnvironmentConfig 出现，教程示例全是纯文本。
6. `skill_registry` source 类型仅 REST 列出，教程三种 source 表未包含；本插件不使用。
7. 环境存储配额的具体字节上限官方页面未写死；实测 429 正文为 `Project environment storage quota exceeded`。

## 给机器人的实用建议

1. 提交后立刻把 `task_id`、`sandbox_id` 给用户存好（免费档交互可能只留 1 天）。
2. 长任务用 background，不要同步死等。
3. 续聊必须等上一轮结束，并同时传 sandbox id；续聊请在 24 小时 TTL 内进行。
4. 只要文件、不要旧对话：复用 `sandbox_id`，`new_session=true`。
5. 附件尽量用小文本；大仓库用智能体在沙盒内 `git clone`，不要走 inline。克隆整仓会把单个环境撑到数百 MB～数 GB，加速打满项目存储配额。
6. 历史环境堆积时，管理员执行 `/agencleanup all`。
