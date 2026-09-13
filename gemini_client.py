"""Gemini Interactions + Files API client for Antigravity sandboxes.

Direct Gemini REST only (no local WebUI, no protocol gateway):
  POST https://generativelanguage.googleapis.com/v1beta/interactions
  GET  https://generativelanguage.googleapis.com/v1beta/interactions/{id}
  GET  https://generativelanguage.googleapis.com/v1beta/environments
  DELETE https://generativelanguage.googleapis.com/v1beta/environments/{id}
  GET  https://generativelanguage.googleapis.com/v1beta/files/{resourceId}:download?alt=media
"""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

try:
    from astrbot.api import logger
except ImportError:  # local syntax tests without AstrBot installed

    class _NullLogger:
        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    logger = _NullLogger()

GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_ENVIRONMENTS_URL = "https://generativelanguage.googleapis.com/v1beta/environments"
GEMINI_FILES_BASE = "https://generativelanguage.googleapis.com/v1beta/files"
DEFAULT_AGENT = "antigravity-preview-05-2026"
ALLOWED_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
)
DEFAULT_MODEL = "gemini-3.8-flash"
AUTO_MODEL_ALIASES = frozenset({"", "auto"})
INLINE_PER_FILE_LIMIT = 1 * 1024 * 1024
INLINE_TOTAL_LIMIT = 2 * 1024 * 1024
SUBMIT_TIMEOUT = 30.0
RETRIEVE_GET_TIMEOUT = 60.0
DOWNLOAD_TIMEOUT = 15 * 60.0
ENV_LIST_TIMEOUT = 30.0
ENV_DELETE_TIMEOUT = 30.0
API_REVISION = "2026-05-20"
STORAGE_QUOTA_MARKERS = (
    "environment storage quota",
    "project environment storage quota exceeded",
)

TERMINAL_STATUS = frozenset(
    {
        "completed",
        "failed",
        "cancelled",
        "incomplete",
        "budget_exceeded",
        "requires_action",
    }
)
RUNNING_STATUS = frozenset({"in_progress", "queued"})

OnStorageQuota = Callable[[str], Awaitable[int]]


def environment_id_of(env: dict[str, Any] | None) -> str:
    if not isinstance(env, dict):
        return ""
    raw = env.get("environment_id") or env.get("id") or env.get("name") or ""
    text = str(raw).strip()
    if text.startswith("environments/"):
        text = text.split("/", 1)[1].strip()
    return text


def environment_size_bytes(env: dict[str, Any] | None) -> int:
    if not isinstance(env, dict):
        return 0
    raw = env.get("size_bytes") if env.get("size_bytes") is not None else env.get("sizeBytes")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def is_storage_quota_error(resp: httpx.Response | None) -> bool:
    if resp is None or resp.status_code != 429:
        return False
    text = (resp.text or "").lower()
    return any(marker in text for marker in STORAGE_QUOTA_MARKERS)


class GeminiClientError(Exception):
    """User-facing API/client error (message is Chinese)."""


class GeminiSubmitTimeoutError(GeminiClientError):
    """Submit timed out after the request may already have reached Google."""


def unwrap_interaction(payload: Any) -> dict[str, Any]:
    """Normalize {id,...} or {data:{id,...}} or {interaction:{id,...}}."""
    if not isinstance(payload, dict):
        return {}
    for key in ("data", "interaction"):
        inner = payload.get(key)
        if isinstance(inner, dict) and (
            inner.get("id") or inner.get("environment_id") or inner.get("status")
        ):
            return inner
    return payload


def extract_output_text(data: dict[str, Any]) -> str:
    """REST responses often omit SDK-only output_text; fall back to steps."""
    text = data.get("output_text")
    if isinstance(text, str) and text.strip():
        return text
    steps = data.get("steps") or []
    if not isinstance(steps, list):
        return ""
    for step in reversed(steps):
        if not isinstance(step, dict):
            continue
        if step.get("type") != "model_output":
            continue
        content = step.get("content")
        parts: list[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and item.get("text"):
                    parts.append(str(item["text"]))
        if parts:
            return "\n".join(parts)
    return ""


def summarize_steps(data: dict[str, Any], *, cap: int = 20) -> str:
    steps = data.get("steps") or []
    if not isinstance(steps, list) or not steps:
        return "(无 steps)"
    lines: list[str] = []
    for i, step in enumerate(steps):
        if i >= cap:
            lines.append(f"... 另有 {len(steps) - cap} 步未列出")
            break
        if not isinstance(step, dict):
            lines.append(f"- step[{i}]: {type(step).__name__}")
            continue
        stype = step.get("type") or "unknown"
        extra = ""
        if stype == "model_output":
            extra = " (模型输出)"
        elif "code" in stype:
            extra = " (代码执行)"
        elif "function" in stype or "tool" in stype:
            extra = " (工具)"
        lines.append(f"- [{i}] {stype}{extra}")
    return "\n".join(lines)


def sandbox_target_from_path(local_path: str) -> str:
    """target = /workspace/<basename> unless path already looks like a sandbox path."""
    raw = (local_path or "").strip().replace("\\", "/")
    if not raw:
        return "/workspace/unnamed"
    if raw.startswith(("/workspace/", ".agents/", "/home/", "/tmp/")):
        return raw
    name = Path(raw).name or "unnamed"
    return f"/workspace/{name}"


def resolve_model(raw: str | None) -> str:
    """Return a concrete model id, or empty string to omit agent_config.model."""
    model = (raw or "").strip()
    if model.lower() in AUTO_MODEL_ALIASES:
        return ""
    return model


class GeminiSandboxClient:
    def __init__(
        self,
        *,
        api_key: str = "",
        api_keys: list[str] | None = None,
        default_model: str = "auto",
        submit_background: bool = True,
        max_total_tokens: int = 0,
    ) -> None:
        raw_keys = list(api_keys or [])
        if api_key:
            raw_keys.append(api_key)
        self.api_keys = list(
            dict.fromkeys(str(key).strip() for key in raw_keys if str(key).strip())
        )
        self.api_key = self.api_keys[0] if self.api_keys else ""
        self.default_model = resolve_model(default_model)
        self.submit_background = bool(submit_background)
        try:
            self.max_total_tokens = int(max_total_tokens or 0)
        except (TypeError, ValueError):
            self.max_total_tokens = 0

    @property
    def model_label(self) -> str:
        if self.default_model:
            return self.default_model
        return "auto (omit agent_config.model)"

    def _headers(self, api_key: str | None = None) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key or self.api_key,
            "Api-Revision": API_REVISION,
        }

    def _client_kwargs(self, timeout: float, api_key: str | None = None) -> dict[str, Any]:
        return {
            "timeout": httpx.Timeout(timeout, connect=30.0),
            "follow_redirects": True,
            "headers": self._headers(api_key),
        }

    def require_api_key(self) -> None:
        if not self.api_keys:
            raise GeminiClientError("未配置 Gemini API Key。请在插件设置中填写 gemini_api_keys。")

    def _agent_config(self) -> dict[str, Any] | None:
        cfg: dict[str, Any] = {"type": "antigravity"}
        if self.default_model:
            cfg["model"] = self.default_model
        if self.max_total_tokens and self.max_total_tokens > 0:
            cfg["max_total_tokens"] = self.max_total_tokens
        # Official (2026-09-08): omit agent_config to default to gemini-3.8-flash.
        if "model" not in cfg and "max_total_tokens" not in cfg:
            return None
        return cfg

    def build_environment(
        self,
        *,
        new_sandbox: bool,
        sandbox_id: str | None,
        sources: list[dict[str, Any]] | None,
    ) -> str | dict[str, Any]:
        sources = sources or []
        if new_sandbox:
            if sources:
                return {"type": "remote", "sources": sources}
            return "remote"
        env_id = (sandbox_id or "").strip()
        if not env_id:
            raise GeminiClientError(
                "new_sandbox=false 时必须提供 sandbox_id（environment_id）以复用沙盒。"
            )
        if sources:
            # Official EnvironmentConfig.environment_id updates an existing env.
            return {
                "type": "remote",
                "environment_id": env_id,
                "sources": sources,
            }
        return env_id

    def build_sources_from_files(
        self,
        file_paths: str | None,
        file_contents: str | None,
    ) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        total = 0

        def _add_inline(target: str, content: str, encoding: str | None = None) -> None:
            nonlocal total
            raw_len = (
                len(content.encode("utf-8"))
                if encoding != "base64"
                else (len(base64.b64decode(content, validate=False)) if content else 0)
            )
            if encoding == "base64":
                try:
                    raw_len = len(base64.b64decode(content, validate=False))
                except Exception:
                    raw_len = len(content)
            if raw_len > INLINE_PER_FILE_LIMIT:
                raise GeminiClientError(
                    f"内联文件超过官方限制 1MB/文件: {target} ({raw_len} bytes)"
                )
            if total + raw_len > INLINE_TOTAL_LIMIT:
                raise GeminiClientError("内联文件合计超过官方限制 2MB。请减少附件。")
            total += raw_len
            item: dict[str, Any] = {
                "type": "inline",
                "target": target,
                "content": content,
            }
            if encoding:
                item["encoding"] = encoding
            sources.append(item)

        if file_paths:
            for piece in str(file_paths).split(","):
                local = piece.strip()
                if not local:
                    continue
                path = Path(local).expanduser()
                if not path.is_file():
                    raise GeminiClientError(f"本地文件不存在或不是文件: {local}")
                size = path.stat().st_size
                if size > INLINE_PER_FILE_LIMIT:
                    raise GeminiClientError(
                        f"本地文件超过官方 inline 1MB 限制: {local} ({size} bytes)"
                    )
                data = path.read_bytes()
                target = sandbox_target_from_path(local)
                try:
                    text = data.decode("utf-8")
                    _add_inline(target, text, encoding=None)
                except UnicodeDecodeError:
                    # REST EnvironmentConfig documents optional encoding=base64.
                    # agent-environment tutorial describes inline as raw text;
                    # binary is sent as base64 and documented in MODEL.md.
                    b64 = base64.b64encode(data).decode("ascii")
                    _add_inline(target, b64, encoding="base64")

        if file_contents:
            raw = str(file_contents).strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as e:
                    raise GeminiClientError(f"file_contents 不是合法 JSON 列表: {e}") from e
                if not isinstance(parsed, list):
                    raise GeminiClientError(
                        "file_contents 必须是 JSON 列表，例如 "
                        '[{"target":"/workspace/a.txt","content":"..."}]'
                    )
                for item in parsed:
                    if not isinstance(item, dict):
                        raise GeminiClientError("file_contents 列表项必须是对象")
                    target = str(item.get("target") or "").strip()
                    content = item.get("content")
                    if not target:
                        raise GeminiClientError("file_contents 项缺少 target")
                    if content is None:
                        raise GeminiClientError(f"file_contents 项缺少 content: {target}")
                    encoding = item.get("encoding")
                    enc = str(encoding).strip() if encoding else None
                    if not target.startswith("/") and not target.startswith("."):
                        target = f"/workspace/{Path(target).name}"
                    _add_inline(target, str(content), encoding=enc)

        return sources

    def build_create_payload(
        self,
        *,
        prompt: str,
        new_sandbox: bool,
        sandbox_id: str | None,
        new_session: bool,
        previous_task_id: str | None,
        sources: list[dict[str, Any]],
        background: bool | None = None,
    ) -> dict[str, Any]:
        prompt = (prompt or "").strip()
        if not prompt:
            raise GeminiClientError("prompt 不能为空。")
        env = self.build_environment(
            new_sandbox=new_sandbox,
            sandbox_id=sandbox_id,
            sources=sources,
        )
        payload: dict[str, Any] = {
            "agent": DEFAULT_AGENT,
            "input": prompt,
            "environment": env,
        }
        agent_config = self._agent_config()
        if agent_config:
            payload["agent_config"] = agent_config
        bg = self.submit_background if background is None else bool(background)
        if bg:
            payload["background"] = True
        if not new_session:
            prev = (previous_task_id or "").strip()
            if not prev:
                raise GeminiClientError(
                    "new_session=false 时必须提供 previous_task_id"
                    "（previous_interaction_id）以延续对话。"
                )
            if sources:
                raise GeminiClientError(
                    "延续会话（previous_interaction_id）时会话上下文与环境绑定，"
                    "不允许在请求中携带 sources 增量挂载文件，否则 Google 将返回 400 invalid_request。"
                )
            payload["previous_interaction_id"] = prev
        return payload

    async def _post_interaction(self, payload: dict[str, Any], api_key: str) -> httpx.Response:
        async with httpx.AsyncClient(**self._client_kwargs(SUBMIT_TIMEOUT, api_key)) as client:
            try:
                return await client.post(GEMINI_INTERACTIONS_URL, json=payload)
            except httpx.TimeoutException as e:
                raise GeminiSubmitTimeoutError(
                    f"提交等待超过 {int(SUBMIT_TIMEOUT)} 秒；请求可能已经被 Google 接收，"
                    "后台任务也可能已经创建并继续执行，但本次未取得 task_id 和 sandbox_id。"
                ) from e
            except httpx.HTTPError as e:
                raise GeminiClientError(f"提交网络错误: {e}") from e

    async def create_interaction(
        self,
        payload: dict[str, Any],
        *,
        api_key: str | None = None,
        on_storage_quota: OnStorageQuota | None = None,
    ) -> tuple[dict[str, Any], str]:
        self.require_api_key()
        logger.info("提交沙盒任务：Gemini Interactions API")
        keys_to_try = [api_key] if api_key else self.api_keys
        resp: httpx.Response | None = None
        used_key = keys_to_try[0]
        for index, key in enumerate(keys_to_try):
            used_key = key
            storage_retried = False
            while True:
                resp = await self._post_interaction(payload, key)
                if (
                    is_storage_quota_error(resp)
                    and on_storage_quota is not None
                    and not storage_retried
                ):
                    preview = self._http_error_body_preview(resp)
                    logger.warning(
                        f"Gemini Key #{index + 1} 环境存储配额耗尽，尝试回收后用同一把 Key 重试。"
                        f" 响应内容: {preview}"
                    )
                    storage_retried = True
                    deleted = 0
                    try:
                        deleted = int(await on_storage_quota(key) or 0)
                    except Exception as e:
                        logger.warning(f"环境存储配额回收失败: {e}")
                    if deleted > 0:
                        logger.warning(
                            f"Gemini Key #{index + 1} 已回收 {deleted} 个环境，重试提交"
                        )
                        continue
                    logger.warning(
                        f"Gemini Key #{index + 1} 回收了 0 个环境，无法靠回收解除存储配额"
                    )
                break
            if api_key:
                break
            if resp.status_code not in {401, 403, 429} or index == len(keys_to_try) - 1:
                break
            logger.warning(
                f"Gemini Key #{index + 1} 提交返回 HTTP {resp.status_code}，"
                f"切换下一个备用 Key。响应内容: {self._http_error_body_preview(resp)}"
            )
        if resp is None:
            raise GeminiClientError("没有可用的 Gemini API Key。")
        return self._parse_json_response(resp, action="提交任务"), used_key

    async def get_interaction(
        self, task_id: str, *, api_key: str | None = None
    ) -> dict[str, Any]:
        self.require_api_key()
        task_id = (task_id or "").strip()
        if not task_id:
            raise GeminiClientError("task_id 不能为空。")
        encoded = quote(task_id, safe="")
        url = f"{GEMINI_INTERACTIONS_URL}/{encoded}"
        target_key = api_key or (self.api_keys[0] if self.api_keys else None)
        if not target_key:
            raise GeminiClientError("没有可用的 Gemini API Key。")
        async with httpx.AsyncClient(
            **self._client_kwargs(RETRIEVE_GET_TIMEOUT, target_key)
        ) as client:
            try:
                resp = await client.get(url)
            except httpx.HTTPError as e:
                raise GeminiClientError(f"查询任务网络错误: {e}") from e
        return self._parse_json_response(resp, action="查询任务")

    async def list_environments(self, *, api_key: str | None = None) -> list[dict[str, Any]]:
        self.require_api_key()
        target_key = api_key or (self.api_keys[0] if self.api_keys else None)
        if not target_key:
            raise GeminiClientError("没有可用的 Gemini API Key。")
        environments: list[dict[str, Any]] = []
        page_token = ""
        async with httpx.AsyncClient(
            **self._client_kwargs(ENV_LIST_TIMEOUT, target_key)
        ) as client:
            while True:
                params: dict[str, Any] = {"pageSize": 1000}
                if page_token:
                    params["pageToken"] = page_token
                try:
                    resp = await client.get(GEMINI_ENVIRONMENTS_URL, params=params)
                except httpx.HTTPError as e:
                    raise GeminiClientError(f"列出沙盒环境网络错误: {e}") from e
                if resp.status_code >= 400:
                    raise GeminiClientError(self._format_http_error(resp, "列出沙盒环境"))
                try:
                    payload = resp.json()
                except Exception as e:
                    snippet = (resp.text or "")[:500]
                    raise GeminiClientError(
                        f"列出沙盒环境返回的不是 JSON: {e}; body={snippet}"
                    ) from e
                if not isinstance(payload, dict):
                    raise GeminiClientError("列出沙盒环境响应格式无法解析。")
                batch = payload.get("environments") or []
                if isinstance(batch, list):
                    environments.extend(item for item in batch if isinstance(item, dict))
                page_token = str(
                    payload.get("next_page_token") or payload.get("nextPageToken") or ""
                ).strip()
                if not page_token:
                    break
        return environments

    async def delete_environment(self, env_id: str, *, api_key: str | None = None) -> None:
        self.require_api_key()
        env_id = environment_id_of({"id": env_id}) or (env_id or "").strip()
        if not env_id:
            raise GeminiClientError("environment_id 不能为空。")
        if ".." in env_id or "\x00" in env_id:
            raise GeminiClientError("environment_id 含非法路径字符。")
        target_key = api_key or (self.api_keys[0] if self.api_keys else None)
        if not target_key:
            raise GeminiClientError("没有可用的 Gemini API Key。")
        encoded = quote(env_id, safe="")
        url = f"{GEMINI_ENVIRONMENTS_URL}/{encoded}"
        async with httpx.AsyncClient(
            **self._client_kwargs(ENV_DELETE_TIMEOUT, target_key)
        ) as client:
            try:
                resp = await client.delete(url)
            except httpx.HTTPError as e:
                raise GeminiClientError(f"删除沙盒环境网络错误: {e}") from e
        if resp.status_code >= 400:
            raise GeminiClientError(self._format_http_error(resp, "删除沙盒环境"))

    def _parse_json_response(self, resp: httpx.Response, *, action: str) -> dict[str, Any]:
        if resp.status_code >= 400:
            raise GeminiClientError(self._format_http_error(resp, action))
        try:
            payload = resp.json()
        except Exception as e:
            snippet = (resp.text or "")[:500]
            raise GeminiClientError(f"{action}返回的不是 JSON: {e}; body={snippet}") from e
        data = unwrap_interaction(payload)
        if not isinstance(data, dict):
            raise GeminiClientError(f"{action}响应格式无法解析。")
        return data

    def _format_http_error(self, resp: httpx.Response, action: str) -> str:
        return self._format_http_error_body(resp.status_code, resp.content, action)

    @staticmethod
    def _http_error_body_preview(resp: httpx.Response, *, limit: int = 1500) -> str:
        try:
            payload = resp.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                parts: list[str] = []
                for key in ("status", "code", "message"):
                    value = err.get(key)
                    if value not in (None, ""):
                        parts.append(f"{key}={value}")
                details = err.get("details")
                if details:
                    parts.append(f"details={json.dumps(details, ensure_ascii=False)}")
                if parts:
                    return " ".join(parts)[:limit]
            try:
                return json.dumps(payload, ensure_ascii=False)[:limit]
            except (TypeError, ValueError):
                pass
        text = (resp.text or "").strip().replace("\r\n", "\n")
        if not text:
            return "<empty>"
        return text[:limit]

    @staticmethod
    def _format_http_error_body(status: int, body: bytes, action: str) -> str:
        text = ""
        try:
            text = body.decode("utf-8", errors="replace")[:1500]
        except Exception:
            text = "<unreadable>"
        hint = ""
        if status in (401, 403):
            hint = "（请检查 API Key 是否有效、是否有 Interactions/Agents 权限）"
        elif status == 404:
            hint = "（任务或沙盒可能已过期：免费档交互约保留 1 天，沙盒闲置 7 天后删除）"
        elif status == 429:
            lowered = text.lower()
            if any(marker in lowered for marker in STORAGE_QUOTA_MARKERS):
                hint = "（项目沙盒环境存储配额已满。请删除闲置 environment，不要只换 Key）"
            else:
                hint = "（触发配额/速率限制。免费档有用量配额；单次任务常消耗 10 万+ token）"
        elif status == 400:
            hint = "（请求参数被拒绝。延续对话时前序任务必须已完成，且需同时传 environment）"
        return f"{action}失败 HTTP {status}{hint}: {text}"
