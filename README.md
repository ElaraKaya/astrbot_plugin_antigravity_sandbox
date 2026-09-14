# Antigravity 沙盒任务

AstrBot 插件：把 Google **Antigravity** 托管智能体当成可提交 / 可取回的沙盒任务。每次提交都新建沙盒，立即返回 `task_id`、`sandbox_id` 和预期公网地址；需要时再查询文本回执。

- 插件目录 / 元数据名：`astrbot_plugin_antigravity_sandbox`
- 作者：珂夜
- 版本：1.5.0
- 需要 AstrBot `>=4.5.7,<5`
- **只直连 Gemini Interactions / Environments / Files API**。不跑本地控制台，也不做 OpenAI/Gemini 协议中转。
- Key 写在本插件设置（`_conf_schema.json`）里的 `gemini_api_keys`
- 模型与 API 说明见 [MODEL.md](MODEL.md)

沙盒语义（新建沙盒、同沙盒新会话、`interaction_id` 与 `environment_id` 分开）对齐官方 Interactions，并参考 OnsWayn/antigravity-agent-webui 对这两类 id 的划分；**不实现**该项目的网关、TPM、多 Key、会话中转。

**环境回收（2026-09-08 起）：** 任务仍每次新建沙盒，但会在启动时、下次提交前，以及存储配额 429 时，按 24 小时闲置 TTL 删除可回收的 `environment`，避免 `Project environment storage quota exceeded`。管理员可用 `/agenvlist`、`/agencleanup`。

## 安装

1. 将整个文件夹复制到：

   ```text
   AstrBot/data/plugins/astrbot_plugin_antigravity_sandbox/
   ```

2. 确认有 `main.py`、`metadata.yaml`、`_conf_schema.json`。
3. 依赖：`pip install -r requirements.txt`（`httpx`）。
4. 启用 / 重载插件后，在**本插件设置**填写 `gemini_api_keys`。

插件不下载或解压沙盒环境快照。任务产物应由 Agent 按提交时的上传要求直接上传到图床。

## 插件设置（`_conf_schema.json`）

| 字段 | 说明 |
| --- | --- |
| `gemini_api_keys` | Gemini API Key 列表；请求按顺序轮询。401/403 换备用 Key；普通 429 也换 Key。环境存储配额 429 会先在当前 Key 回收再重试。密钥不写日志。 |
| `gemini_api_key` | 旧版单 Key 兼容项，建议迁移到列表。 |
| `default_model` | `agent_config.model`。`auto` 或留空：不发送 `model`，无 token 上限时整段省略 `agent_config`，交给服务端默认/路由（官方 2026-09-08 默认 `gemini-3.8-flash`）。也可强制 `gemini-3.8-flash` / `gemini-3.7-flash` / `gemini-3.6-flash` / `gemini-3.5-flash` / `gemini-3.5-flash-lite`。 |
| `submit_background` | 默认 `true`：`background=true`，提交后马上返回 id。 |
| `max_total_tokens` | `0` 表示不发送。大于 0 时写入 `agent_config.max_total_tokens`。 |
| `upload_webhook_url` | 完整上传接口，例如 `https://example.com/Webhook/upload`。留空不启用。 |
| `upload_public_base_url` | 公网访问基础地址，例如 `https://example.com`。留空不启用。 |
| `upload_token` | 图床 Bearer Token，secret 配置，不写日志；任务提交时挂载为 `/workspace/upload.token`。 |
| `upload_prefix` | 默认上传目录，默认 `agysb`。 |
| `env_auto_cleanup` | **2026-09-08 起。** 默认 `true`：启动时以及每次新建任务提交前，按 TTL 回收闲置环境。 |
| `env_idle_ttl_hours` | **2026-09-08 起。** 默认 `24`。从本插件最后一次 submit/retrieve/continue 或 Google `last_accessed` 起算。 |
| `env_keep_recent` | **2026-09-08 起。** 默认 `2`。每个 Key 至少保留最近用过的沙盒，方便当天 `/agcontinue`。 |
| `env_cleanup_scope` | **2026-09-08 起。** `tracked`（默认，只删本插件记录过的）或 `all`（该 Gemini 项目里可回收的都删）。 |
| `env_cleanup_on_quota` | **2026-09-08 起。** 默认 `true`。遇到环境存储配额 429 时，先在当前 Key 紧急回收再用同一把 Key 重试，而不是只换备用 Key。 |

## LLM 工具

`context.add_llm_tools(...)` 注册。另有指令 `/agsubmit`、`/agretrieve`、`/agcontinue`。

### 1. `submit_sandbox_task`

| 参数 | 类型 | 默认 | 含义 |
| --- | --- | --- | --- |
| `prompt` | string | 必填 | 任务文本。请严格聚焦当前任务，不要加入和用户需求无关的信息，不要强行关联记忆上下文与历史聊天 |
| `output_files` | string | 空 | 产出原始文件名，逗号分隔。插件提交时自动加 `YYMMDDHHMMSS_` 前缀（如 `260901131456_new.docx`），用于上传路径和预期公网地址，避免多次任务覆盖同一文件 |
| `file_paths` | string | 空 | 逗号分隔的本地路径，读入后作为 `environment.sources` inline |
| `file_contents` | string | 空 | JSON 列表 `[{target, content}]` |

每次强制新建沙盒并以后台模式提交。立即返回 `task_id`、`sandbox_id`、`status` 和按加时间戳后的文件名拼出的预期公网地址；不轮询。调用方只需给 `new.docx` 这类原始名，时间戳由插件统一生成，上传说明和预期地址共用同一份，避免模型漏拼或两次任务互相覆盖。LLM 根据产物类型决定稍后下载发送，或让用户自行访问地址。提交任务时请保持指令独立纯净，严禁夹带无关历史信息或强行关联记忆上下文。

请求：`POST https://generativelanguage.googleapis.com/v1beta/interactions`

### 2. `retrieve_sandbox_task`

| 参数 | 类型 | 含义 |
| --- | --- | --- |
| `task_id` | string | interaction id |
| `sandbox_id` | string | environment_id |

1. 单次 `GET /v1beta/interactions/{task_id}` 查询当前状态与回执，不轮询。
2. 返回回执文本、状态、步骤摘要和用量。
3. 不下载、不解压环境快照，避免环境包网络问题。

### 3. `continue_sandbox_task`

在已有 `sandbox_id` + 上一轮 `task_id` 上续接。请求不带 `sources`（官方：延续会话时不允许增量挂载）。刷新该沙盒的 `last_used_at`，使其在 TTL 内不会被回收。

## 环境回收（2026-09-08 起）

官方沙盒闲置约 7 天才自动删除，项目环境存储配额往往更早耗尽（HTTP 429，`Project environment storage quota exceeded`）。

插件策略：

1. **submit 仍然每次新建沙盒**，不复用旧环境（避免串文件和 `/workspace/upload.token`）。
2. retrieve 成功**不会立刻删除**，以便 `/agcontinue`。
3. 启动时、下次新建提交前：删除已超过 `env_idle_ttl_hours`（默认 24 小时）且不在「最近 N 个」保护名单里的环境。
4. 存储配额 429：对该 Key 的项目做紧急回收（只保护 15 分钟内用过的、仍在跑且 2 小时内用过的、以及最近 N 个），然后用**同一把 Key** 重试一次。
5. 默认 `tracked` 只动本插件记录过的 `sandbox_id`。历史堆积或其它工具留下的环境，用管理员指令 `/agencleanup all`。

官方接口：

```text
GET    https://generativelanguage.googleapis.com/v1beta/environments
DELETE https://generativelanguage.googleapis.com/v1beta/environments/{id}
```

## 指令

- `/agsubmit <任务文本> [类型...]`
- `/agretrieve <taskid>`
- `/agcontinue <taskid> <任务文本> [类型...]`
- `/agenvlist`（管理员）列出各 Key 的环境数量与占用
- `/agencleanup` / `/agencleanup all`（管理员）回收闲置环境
- `/aghelp`
