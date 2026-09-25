"""Gemini Interactions + Files API client for Antigravity sandboxes.

Direct Gemini REST only (no local WebUI, no protocol gateway):
  POST https://generativelanguage.googleapis.com/v1beta/interactions
  GET  https://generativelanguage.googleapis.com/v1beta/interactions/{id}
  GET  https://generativelanguage.googleapis.com/v1beta/environments
  DELETE https://generativelanguage.googleapis.com/v1beta/environments/{id}
  GET  https://generativelanguage.googleapis.com/v1beta/environments/{id}/files/{path}
  GET  https://generativelanguage.googleapis.com/v1beta/environments/{id}/files/{path}?alt=media
  PUT  https://generativelanguage.googleapis.com/upload/v1beta/environments/{id}/files/{path}
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
GEMINI_MODEL_PREFIX = "gemini"
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
# 09 沙盒的取回 GET 会偶发 504/deadline_exceeded，以及 HTTP 500
# Internal error encountered / api_error（任务仍在 Google 侧跑）。
# 单个请求的超时压到 7s，超时、网关失败或 500 后原地重试一次；依旧失败才抛给上层。
RETRIEVE_GET_TIMEOUT = 7.0
RETRIEVE_GET_RETRIES = 1
DOWNLOAD_TIMEOUT = 15 * 60.0
ENV_LIST_TIMEOUT = 30.0
ENV_DELETE_TIMEOUT = 30.0
ENV_FILES_LIST_TIMEOUT = 60.0
ENV_FILES_DOWNLOAD_TIMEOUT = 15 * 60.0
ENV_FILES_UPLOAD_TIMEOUT = 15 * 60.0
GEMINI_UPLOAD_ENV_BASE = "https://generativelanguage.googleapis.com/upload/v1beta/environments"
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

CHAT_PULL_MAX_BYTES = 20 * 1024 * 1024
CHAT_PULL_TIMEOUT_SECONDS = 90.0
UI_PULL_CONCURRENCY = 2
UI_PULL_PROGRESS_INTERVAL = 0.5
DEFAULT_RECEIPT_TRUNCATE_CHARS = 2000
DEFAULT_IN_PROGRESS_PER_KEY = 1

MSG_NO_CAPACITY = "目前无空余沙盒分配"
MSG_ENV_404 = "沙盒环境不存在或已过期。"
MSG_KEY_INVALID = "API Key 无效或没有权限。"
MSG_PULL_TIMEOUT = "拉取超时（90 秒），已中止。请改用图床或 WebUI 获取。"
MSG_LIST_EMPTY = "该沙盒文件列表为空。"
MSG_FILE_MISSING = "沙盒中没有这个文件。"
MSG_FILE_TOO_LARGE = "文件超过 20MB，请使用图床或 WebUI 获取。"

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


def normalize_environment_file_path(path: str | None, *, default: str = "") -> str:
    """Normalize an environment file path (keep '/', reject '..')."""
    text = (path or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    text = text.lstrip("/")
    if not text:
        text = default
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or "\x00" in text:
        raise GeminiClientError("文件路径含非法片段。")
    return "/".join(parts) if parts else default


def is_safe_local_file_path(path: Path) -> bool:
    """Validate that local file path does not access sensitive system paths."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    if not resolved.is_file():
        return False
    parts = set(resolved.parts)
    for part in parts:
        if part in {".ssh", ".gnupg", ".aws", ".docker", ".kube"} or part.startswith(".env"):
            return False
    sensitive_roots = ("/etc", "/proc", "/sys", "/dev", "/root", "/var/run", "/var/log")
    res_str = str(resolved).replace("\\", "/")
    for root in sensitive_roots:
        if res_str == root or res_str.startswith(f"{root}/"):
            return False
    return True


def encode_environment_file_path(path: str) -> str:
    """URL-encode each path segment; keep '/' separators."""
    clean = normalize_environment_file_path(path, default="")
    if not clean:
        return ""
    return "/".join(quote(seg, safe="") for seg in clean.split("/"))


def environment_media_url(env_id: str, path: str) -> str:
    """Download URL: .../files/<encoded path>?alt=media. Chinese names are percent-encoded."""
    encoded_env = quote((env_id or "").strip(), safe="")
    encoded_path = encode_environment_file_path(path)
    if encoded_path:
        return f"{GEMINI_ENVIRONMENTS_URL}/{encoded_env}/files/{encoded_path}?alt=media"
    return f"{GEMINI_ENVIRONMENTS_URL}/{encoded_env}/files?alt=media"


def workspace_download_path(name: str) -> str:
    """Prefix a chat/UI file name with workspace/ unless it already lives there."""
    raw = (name or "").strip().replace("\\", "/").lstrip("/")
    if not raw or raw == "workspace":
        rel = "workspace"
    elif raw.startswith("workspace/"):
        rel = raw
    else:
        rel = f"workspace/{raw}"
    return normalize_environment_file_path(rel, default="workspace")


def ensure_md_filename(name: str) -> tuple[str, bool]:
    """Append .md when the uploaded name has no suffix. Returns (filename, added)."""
    base = Path((name or "").replace("\\", "/")).name.strip() or "upload"
    suffix = Path(base).suffix
    if suffix and suffix != ".":
        return base, False
    stem = base[:-1] if base.endswith(".") else base
    stem = stem or "upload"
    return f"{stem}.md", True


def slim_environment_file_entry(item: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only the fields chat/LLM list replies are allowed to show."""
    norm = normalize_environment_file_entry(item)
    return {
        "name": norm.get("name") or "",
        "path": norm.get("path") or "",
        "type": norm.get("type") or "file",
        "size_bytes": int(norm.get("size_bytes") or 0),
    }


def count_key_in_progress(items: list[dict[str, Any]], key_fp: str) -> int:
    """Count local-cache rows whose last_status is exactly in_progress for one key fingerprint."""
    key_fp = (key_fp or "").strip()
    if not key_fp:
        return 0
    total = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("last_status") or "").strip().lower() != "in_progress":
            continue
        if str(item.get("key") or "").strip() == key_fp:
            total += 1
    return total


def select_idle_keys(keys: list[str], counts: dict[str, int], limit: int) -> list[str]:
    """Keys still under the in_progress cap, preserving the configured order."""
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = DEFAULT_IN_PROGRESS_PER_KEY
    idle: list[str] = []
    for key in keys:
        if not key:
            continue
        if int(counts.get(key, 0) or 0) < cap:
            idle.append(key)
    return idle


def select_balanced_keys(
    keys: list[str],
    counts: dict[str, int],
    limit: int,
    current_key: str = "",
) -> list[str]:
    """Order a new submit: fewest in_progress first, then the key after current_key.

    Keys at the cap are omitted. Ties keep a rotation through the configured
    list, starting at the key immediately after current_key. With no current
    key, equally idle keys stay in configured order. Later keys are fallbacks
    for 401, 403, and 429.
    """
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = DEFAULT_IN_PROGRESS_PER_KEY
    ordered: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    eligible: list[tuple[str, int]] = []
    for key in ordered:
        try:
            count = int(counts.get(key, 0) or 0)
        except (TypeError, ValueError):
            count = 0
        if count < cap:
            eligible.append((key, count))
    if not eligible:
        return []
    size = len(ordered)
    current = (current_key or "").strip()
    start = ordered.index(current) if current in ordered else -1

    def rank(item: tuple[str, int]) -> tuple[int, int]:
        key, count = item
        index = ordered.index(key)
        if start < 0:
            rotation = index
        else:
            rotation = (index - start - 1) % size
        return (count, rotation)

    eligible.sort(key=rank)
    return [key for key, _ in eligible]


def clip_text(text: str, limit: int, *, keep_full: bool) -> str:
    """Truncate user-visible text. keep_full is only for a receipt that will be sent as an image."""
    body = text or ""
    if keep_full:
        return body
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = DEFAULT_RECEIPT_TRUNCATE_CHARS
    if cap <= 0 or len(body) <= cap:
        return body
    return body[:cap] + "\n…(过长已截断，全文见下方链接)"


def files_error_kind(status_code: int | None, message: str, *, listing: bool) -> str:
    """Map an environment-files failure onto a fixed reply kind."""
    text = (message or "").lower()
    if status_code in (401, 403) or "http 401" in text or "http 403" in text:
        return "key"
    if status_code == 404 or "http 404" in text or "not_found" in text or "not found" in text:
        return "env" if listing else "file"
    if "timeout" in text or "timed out" in text or "超时" in text:
        return "timeout"
    return "other"


def fixed_files_message(kind: str) -> str:
    if kind == "key":
        return MSG_KEY_INVALID
    if kind == "env":
        return MSG_ENV_404
    if kind == "file":
        return MSG_FILE_MISSING
    if kind == "timeout":
        return MSG_PULL_TIMEOUT
    if kind == "empty":
        return MSG_LIST_EMPTY
    if kind == "large":
        return MSG_FILE_TOO_LARGE
    return ""


class ProgressThrottle:
    """Emit at most once per interval unless force=True (terminal events)."""

    def __init__(self, interval: float = UI_PULL_PROGRESS_INTERVAL) -> None:
        self.interval = interval
        self.last: float | None = None

    def allow(self, now: float, *, force: bool = False) -> bool:
        if force or self.last is None or (now - self.last) >= self.interval:
            self.last = now
            return True
        return False


def build_httpx_client_kwargs(
    *,
    timeout: httpx.Timeout | float,
    proxy: str = "",
    use_proxy: bool = False,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Plugin HTTP options. trust_env is off so an empty proxy really means direct.

    Image-host webhook calls pass use_proxy=False. Gemini calls pass use_proxy=True.
    """
    timeout_obj = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout, connect=30.0)
    kwargs: dict[str, Any] = {
        "timeout": timeout_obj,
        "follow_redirects": True,
        "trust_env": False,
    }
    if headers:
        kwargs["headers"] = headers
    cleaned = (proxy or "").strip()
    if use_proxy and cleaned:
        kwargs["proxy"] = cleaned
    return kwargs


async def consume_bounded(
    chunks: Any,
    *,
    max_bytes: int | None = None,
    cancel_event: Any = None,
) -> bytes:
    """Read an async byte stream, aborting on cancel or when max_bytes is crossed."""
    total = 0
    out: list[bytes] = []
    async for chunk in chunks:
        if cancel_event is not None and cancel_event.is_set():
            raise GeminiPullCancelled("下载已取消")
        if not chunk:
            continue
        if max_bytes is not None and total + len(chunk) > max_bytes:
            raise GeminiFileTooLargeError(MSG_FILE_TOO_LARGE)
        total += len(chunk)
        out.append(chunk)
    return b"".join(out)


def normalize_environment_file_entry(item: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize camelCase/snake_case environment file metadata for WebUI."""
    if not isinstance(item, dict):
        return {}
    raw_size = item.get("size_bytes")
    if raw_size is None:
        raw_size = item.get("sizeBytes")
    try:
        size_bytes = int(raw_size or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    path = str(item.get("path") or "").strip()
    name = str(item.get("name") or "").strip()
    if not name and path:
        name = path.rstrip("/").split("/")[-1]
    ftype = str(item.get("type") or "").strip().lower()
    if not ftype:
        mime = str(item.get("mime_type") or item.get("mimeType") or "")
        if mime.endswith("directory") or path.endswith("/"):
            ftype = "directory"
        else:
            ftype = "file"
    if ftype in {"dir", "folder", "directory"}:
        ftype = "directory"
    else:
        ftype = "file"
    return {
        "name": name or path or "(unnamed)",
        "path": path or name,
        "type": ftype,
        "size_bytes": size_bytes,
        "mime_type": str(item.get("mime_type") or item.get("mimeType") or ""),
        "created": str(item.get("created") or item.get("create_time") or item.get("createTime") or ""),
        "modified": str(item.get("modified") or item.get("update_time") or item.get("updateTime") or ""),
    }


def is_storage_quota_error(resp: httpx.Response | None) -> bool:
    if resp is None or resp.status_code != 429:
        return False
    text = (resp.text or "").lower()
    return any(marker in text for marker in STORAGE_QUOTA_MARKERS)


class GeminiClientError(Exception):
    """User-facing API/client error (message is Chinese)."""

    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GeminiFileTooLargeError(GeminiClientError):
    """Chat/UI pull exceeded the configured byte cap."""


class GeminiPullCancelled(GeminiClientError):
    """Caller cancelled an in-flight environment file pull."""


class GeminiFileTimeoutError(GeminiClientError):
    """Environment file pull exceeded its deadline and was aborted."""


class GeminiSubmitTimeoutError(GeminiClientError):
    """Submit timed out after the request may already have reached Google."""


class GeminiRetrieveQueryError(GeminiClientError):
    """Retrieve GET timed out / network / gateway failure while querying interaction."""


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


def resolve_agent(raw: str | None) -> tuple[str, str]:
    """Return (agent id, warning) for the Interactions `agent` field.

    Google 更新沙盒型号后只需改配置，不用改代码。空值回退默认型号；
    误填 gemini-* 模型名时 Interactions API 会整请求 400，这里回退默认型号
    并给出警告，让任务继续可跑。
    """
    agent = (raw or "").strip()
    if not agent:
        return DEFAULT_AGENT, ""
    if agent.lower().startswith(GEMINI_MODEL_PREFIX):
        return DEFAULT_AGENT, (
            f"sandbox_agent 填的是 Gemini 模型名（{agent}），不是 Antigravity 沙盒型号，"
            f"已按默认 {DEFAULT_AGENT} 提交；底层模型请改「接入与模型」里的底层模型。"
        )
    return agent, ""


class GeminiSandboxClient:
    def __init__(
        self,
        *,
        api_key: str = "",
        api_keys: list[str] | None = None,
        default_model: str = "auto",
        agent: str = DEFAULT_AGENT,
        submit_background: bool = True,
        max_total_tokens: int = 0,
        proxy: str = "",
    ) -> None:
        raw_keys = list(api_keys or [])
        if api_key:
            raw_keys.append(api_key)
        self.api_keys = list(
            dict.fromkeys(str(key).strip() for key in raw_keys if str(key).strip())
        )
        self.api_key = self.api_keys[0] if self.api_keys else ""
        self.default_model = resolve_model(default_model)
        self.agent, self.agent_warning = resolve_agent(agent)
        self.submit_background = bool(submit_background)
        try:
            self.max_total_tokens = int(max_total_tokens or 0)
        except (TypeError, ValueError):
            self.max_total_tokens = 0
        self.proxy = (proxy or "").strip()

    @property
    def model_label(self) -> str:
        if self.default_model:
            return self.default_model
        return "auto (omit agent_config.model)"

    @property
    def agent_label(self) -> str:
        return self.agent or DEFAULT_AGENT

    def _headers(self, api_key: str | None = None) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key or self.api_key,
            "Api-Revision": API_REVISION,
        }

    def _client_kwargs(
        self,
        timeout: float,
        api_key: str | None = None,
        *,
        content_type: str | None = "application/json",
    ) -> dict[str, Any]:
        headers = {
            "x-goog-api-key": api_key or self.api_key,
            "Api-Revision": API_REVISION,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return build_httpx_client_kwargs(
            timeout=httpx.Timeout(timeout, connect=30.0),
            proxy=self.proxy,
            use_proxy=True,
            headers=headers,
        )

    def require_api_key(self) -> None:
        if not self.api_keys:
            raise GeminiClientError("未配置 Gemini API Key。请在插件设置「接入与模型」中填写。")

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
                if not is_safe_local_file_path(path):
                    raise GeminiClientError(f"安全限制：禁止读取受保护的文件路径: {local}")
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
            "agent": self.agent or DEFAULT_AGENT,
            "input": prompt,
            "environment": env,
        }
        agent_config = self._agent_config()
        if agent_config:
            payload["agent_config"] = agent_config
        bg = self.submit_background if background is None else bool(background)
        if bg:
            payload["background"] = True
            # background 任务必须落库，否则后续按 id 查询/续接时背面链路取不到
            # 这条交互。仅 09 沙盒上没配 store 时会表现为取回 504 / 查不到。
            payload["store"] = True
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
        candidate_keys: list[str] | None = None,
        on_storage_quota: OnStorageQuota | None = None,
    ) -> tuple[dict[str, Any], str]:
        self.require_api_key()
        logger.info("提交沙盒任务：Gemini Interactions API")
        if api_key:
            keys_to_try = [api_key]
        elif candidate_keys is not None:
            keys_to_try = [key for key in candidate_keys if key]
        else:
            keys_to_try = list(self.api_keys)
        if not keys_to_try:
            raise GeminiClientError(MSG_NO_CAPACITY)
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
        """查询一次交互。短超时 + 原地重试，避免 09 沙盒偶发网关失败直接判负。

        重试是安全的：任务状态存在 Google 侧，与本次连接无关，重试只是再探一次。
        """
        self.require_api_key()
        task_id = (task_id or "").strip()
        if not task_id:
            raise GeminiClientError("task_id 不能为空。")
        encoded = quote(task_id, safe="")
        url = f"{GEMINI_INTERACTIONS_URL}/{encoded}"
        target_key = api_key or (self.api_keys[0] if self.api_keys else None)
        if not target_key:
            raise GeminiClientError("没有可用的 Gemini API Key。")
        attempts = 1 + max(0, int(RETRIEVE_GET_RETRIES))
        last_query_error: GeminiRetrieveQueryError | None = None
        for attempt in range(attempts):
            retrying = attempt + 1 < attempts
            async with httpx.AsyncClient(
                **self._client_kwargs(RETRIEVE_GET_TIMEOUT, target_key)
            ) as client:
                try:
                    resp = await client.get(url)
                except httpx.TimeoutException as e:
                    last_query_error = GeminiRetrieveQueryError(
                        "查询任务超时或网络错误（可能仍在跑）"
                    )
                    if retrying:
                        logger.warning(
                            f"查询任务 {attempt + 1}/{attempts} 超时"
                            f"（>{int(RETRIEVE_GET_TIMEOUT)}s），原地重试一次"
                        )
                        continue
                    raise last_query_error from e
                except httpx.HTTPError as e:
                    detail = str(e).strip()
                    # Empty httpx messages used to surface as bare "查询任务网络错误: "
                    last_query_error = GeminiRetrieveQueryError(
                        "查询任务超时或网络错误（可能仍在跑）"
                        + (f": {detail}" if detail else "")
                    )
                    if retrying:
                        logger.warning(
                            f"查询任务 {attempt + 1}/{attempts} 网络错误，原地重试一次: {detail}"
                        )
                        continue
                    raise last_query_error from e
            if self._is_retrieve_query_gateway_failure(resp):
                last_query_error = GeminiRetrieveQueryError(
                    "查询任务超时或网络错误（可能仍在跑）"
                )
                preview = self._http_error_body_preview(resp)
                if retrying:
                    logger.warning(
                        f"查询任务 {attempt + 1}/{attempts} 命中网关失败"
                        f"（HTTP {resp.status_code}），原地重试一次: {preview}"
                    )
                    continue
                logger.warning(
                    f"查询任务 {attempt + 1}/{attempts} 命中网关失败"
                    f"（HTTP {resp.status_code}），已用尽重试: {preview}"
                )
                raise last_query_error
            return self._parse_json_response(resp, action="查询任务")
        # 循环只会在 raise 时退出；兜底以防 attempts 计算异常
        if last_query_error is not None:
            raise last_query_error
        raise GeminiRetrieveQueryError("查询任务超时或网络错误（可能仍在跑）")

    @staticmethod
    def _is_retrieve_query_gateway_failure(resp: httpx.Response) -> bool:
        """Transient poll failures: HTTP 500/504, or deadline_exceeded.

        500 here is Google's ``Internal error encountered`` / ``api_error`` on
        interaction GET. The task state lives server-side, so the poll can be
        retried. A 200 body may quote the same words and must not match.
        """
        status = resp.status_code
        body = (resp.text or "").lower()
        if status in (500, 504):
            return True
        # Only on error responses — a 200 body may quote these strings (e.g. agent
        # edited this plugin's source) and must not be treated as a gateway failure.
        if status >= 400 and (
            "deadline_exceeded" in body or "deadline exceeded" in body
        ):
            return True
        # Empty or opaque gateway bodies on 502/503 often mean upstream hang.
        if status in (502, 503) and (
            not body.strip()
            or "gateway" in body
            or "timeout" in body
            or "timed out" in body
        ):
            return True
        return False

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


    async def list_environment_files(
        self,
        env_id: str,
        path: str = "",
        *,
        recursive: bool = True,
        page_size: int = 1000,
        api_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """List files under an environment path (Environments Files API)."""
        self.require_api_key()
        env_id = environment_id_of({"id": env_id}) or (env_id or "").strip()
        if not env_id:
            raise GeminiClientError("environment_id 不能为空。")
        if ".." in env_id or "\x00" in env_id:
            raise GeminiClientError("environment_id 含非法路径字符。")
        target_key = api_key or (self.api_keys[0] if self.api_keys else None)
        if not target_key:
            raise GeminiClientError("没有可用的 Gemini API Key。")
        rel = normalize_environment_file_path(path, default="")
        encoded_env = quote(env_id, safe="")
        encoded_path = encode_environment_file_path(rel)
        if encoded_path:
            url = f"{GEMINI_ENVIRONMENTS_URL}/{encoded_env}/files/{encoded_path}"
        else:
            url = f"{GEMINI_ENVIRONMENTS_URL}/{encoded_env}/files"
        # Environments Files List 不接受 pageSize/pageToken（会 400）。
        # page_size 形参保留以免破坏调用方，实际忽略。
        _ = page_size
        files: list[dict[str, Any]] = []
        params: dict[str, Any] = {}
        if recursive:
            params["recursive"] = "true"
        async with httpx.AsyncClient(
            **self._client_kwargs(ENV_FILES_LIST_TIMEOUT, target_key)
        ) as client:
            try:
                resp = await client.get(url, params=params or None)
            except httpx.HTTPError as e:
                raise GeminiClientError(f"列出沙盒文件网络错误: {e}") from e
            if resp.status_code >= 400:
                raise GeminiClientError(
                    self._format_http_error(resp, "列出沙盒文件"),
                    status_code=resp.status_code,
                )
            try:
                payload = resp.json()
            except Exception as e:
                snippet = (resp.text or "")[:500]
                raise GeminiClientError(
                    f"列出沙盒文件返回的不是 JSON: {e}; body={snippet}"
                ) from e
            if not isinstance(payload, dict):
                raise GeminiClientError("列出沙盒文件响应格式无法解析。")
            batch = payload.get("files") or []
            if isinstance(batch, list):
                for item in batch:
                    if isinstance(item, dict):
                        files.append(normalize_environment_file_entry(item))
        return files

    async def download_environment_file(
        self,
        env_id: str,
        path: str,
        *,
        api_key: str | None = None,
    ) -> bytes:
        """Download a single environment file into memory."""
        chunks: list[bytes] = []
        async for chunk in self.iter_environment_file(env_id, path, api_key=api_key):
            chunks.append(chunk)
        return b"".join(chunks)

    def _environment_file_url(self, env_id: str, path: str) -> tuple[str, str, str]:
        """Return (env_id, relative path, media-less files URL)."""
        env_id = environment_id_of({"id": env_id}) or (env_id or "").strip()
        if not env_id:
            raise GeminiClientError("environment_id 不能为空。")
        if ".." in env_id or "\x00" in env_id:
            raise GeminiClientError("environment_id 含非法路径字符。")
        rel = normalize_environment_file_path(path, default="")
        encoded_env = quote(env_id, safe="")
        encoded_path = encode_environment_file_path(rel)
        if encoded_path:
            url = f"{GEMINI_ENVIRONMENTS_URL}/{encoded_env}/files/{encoded_path}"
        else:
            url = f"{GEMINI_ENVIRONMENTS_URL}/{encoded_env}/files"
        return env_id, rel, url

    async def head_environment_file_size(
        self,
        env_id: str,
        path: str,
        *,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> int | None:
        """HEAD alt=media and return Content-Length. None if the server omits it or rejects HEAD."""
        self.require_api_key()
        target_key = api_key or (self.api_keys[0] if self.api_keys else None)
        if not target_key:
            raise GeminiClientError("没有可用的 Gemini API Key。")
        _env, _rel, url = self._environment_file_url(env_id, path)
        client_timeout = ENV_FILES_DOWNLOAD_TIMEOUT if timeout is None else timeout
        async with httpx.AsyncClient(
            **self._client_kwargs(client_timeout, target_key, content_type=None)
        ) as client:
            try:
                resp = await client.head(url, params={"alt": "media"})
            except httpx.HTTPError as e:
                raise GeminiClientError(f"检查沙盒文件大小网络错误: {e}") from e
        if resp.status_code in {405, 501}:
            return None
        if resp.status_code >= 400:
            raise GeminiClientError(
                self._format_http_error(resp, "检查沙盒文件大小"),
                status_code=resp.status_code,
            )
        raw = resp.headers.get("content-length")
        if not raw:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    async def iter_environment_file(
        self,
        env_id: str,
        path: str,
        *,
        api_key: str | None = None,
        chunk_size: int = 1024 * 1024,
        cancel_event: Any = None,
        max_bytes: int | None = None,
        timeout: float | None = None,
    ):
        """Stream an environment file (alt=media) as byte chunks.

        cancel_event stops the read between chunks. max_bytes aborts instead of truncating.
        """
        self.require_api_key()
        target_key = api_key or (self.api_keys[0] if self.api_keys else None)
        if not target_key:
            raise GeminiClientError("没有可用的 Gemini API Key。")
        _env, _rel, url = self._environment_file_url(env_id, path)
        client_timeout = ENV_FILES_DOWNLOAD_TIMEOUT if timeout is None else timeout
        total = 0
        async with httpx.AsyncClient(
            **self._client_kwargs(client_timeout, target_key, content_type=None)
        ) as client:
            try:
                async with client.stream("GET", url, params={"alt": "media"}) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        raise GeminiClientError(
                            self._format_http_error_body(resp.status_code, body, "下载沙盒文件"),
                            status_code=resp.status_code,
                        )
                    async for chunk in resp.aiter_bytes(chunk_size):
                        if cancel_event is not None and cancel_event.is_set():
                            raise GeminiPullCancelled("下载已取消")
                        if not chunk:
                            continue
                        if max_bytes is not None and total + len(chunk) > max_bytes:
                            raise GeminiFileTooLargeError(MSG_FILE_TOO_LARGE)
                        total += len(chunk)
                        yield chunk
            except GeminiClientError:
                raise
            except httpx.TimeoutException as e:
                raise GeminiFileTimeoutError(MSG_PULL_TIMEOUT) from e
            except httpx.HTTPError as e:
                raise GeminiClientError(f"下载沙盒文件网络错误: {e}") from e

    async def put_environment_file(
        self,
        env_id: str,
        path: str,
        content: bytes,
        *,
        content_type: str = "application/octet-stream",
        overwrite: bool = True,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """PUT bytes onto a living environment. Does not use interaction sources.

        Official (2026-09-17):
        PUT /upload/v1beta/environments/{id}/files/{path}
        """
        self.require_api_key()
        env_id = environment_id_of({"id": env_id}) or (env_id or "").strip()
        if not env_id:
            raise GeminiClientError("environment_id 不能为空。")
        if ".." in env_id or "\x00" in env_id:
            raise GeminiClientError("environment_id 含非法路径字符。")
        target_key = api_key or (self.api_keys[0] if self.api_keys else None)
        if not target_key:
            raise GeminiClientError("没有可用的 Gemini API Key。")
        if content is None:
            raise GeminiClientError("上传内容不能为空。")
        data = content if isinstance(content, (bytes, bytearray)) else bytes(content)
        rel = normalize_environment_file_path(path, default="")
        if not rel:
            raise GeminiClientError("上传路径不能为空。")
        encoded_env = quote(env_id, safe="")
        encoded_path = encode_environment_file_path(rel)
        url = f"{GEMINI_UPLOAD_ENV_BASE}/{encoded_env}/files/{encoded_path}"
        mime = (content_type or "application/octet-stream").strip() or "application/octet-stream"
        params: dict[str, Any] = {"overwrite": "true"} if overwrite else {}
        async with httpx.AsyncClient(
            **self._client_kwargs(
                ENV_FILES_UPLOAD_TIMEOUT,
                target_key,
                content_type=mime,
            )
        ) as client:
            try:
                resp = await client.put(url, params=params or None, content=data)
            except httpx.HTTPError as e:
                raise GeminiClientError(f"写入沙盒文件网络错误: {e}") from e
        if resp.status_code >= 400:
            raise GeminiClientError(
                self._format_http_error(resp, "写入沙盒文件"),
                status_code=resp.status_code,
            )
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            files = payload.get("files")
            if isinstance(files, list) and files and isinstance(files[0], dict):
                normalized = normalize_environment_file_entry(files[0])
                if normalized:
                    return normalized
            if payload:
                normalized = normalize_environment_file_entry(payload)
                if normalized.get("name") or normalized.get("path"):
                    return normalized
        return {
            "name": rel.split("/")[-1],
            "path": rel,
            "type": "file",
            "size_bytes": len(data),
            "mime_type": mime,
        }

    async def download_environment_file_to(
        self,
        env_id: str,
        path: str,
        dest: Path,
        *,
        api_key: str | None = None,
    ) -> Path:
        """Stream an environment file onto disk; returns dest path."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as out:
            async for chunk in self.iter_environment_file(env_id, path, api_key=api_key):
                out.write(chunk)
        return dest

    async def upload_environment_file(
        self,
        env_id: str,
        path: str,
        content: bytes,
        *,
        content_type: str = "application/octet-stream",
        overwrite: bool = True,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Upload bytes into an environment via resumable Scotty protocol."""
        self.require_api_key()
        env_id = environment_id_of({"id": env_id}) or (env_id or "").strip()
        if not env_id:
            raise GeminiClientError("environment_id 不能为空。")
        if ".." in env_id or "\x00" in env_id:
            raise GeminiClientError("environment_id 含非法路径字符。")
        target_key = api_key or (self.api_keys[0] if self.api_keys else None)
        if not target_key:
            raise GeminiClientError("没有可用的 Gemini API Key。")
        if content is None:
            raise GeminiClientError("上传内容不能为空。")
        data = content if isinstance(content, (bytes, bytearray)) else bytes(content)
        rel = normalize_environment_file_path(path, default="")
        encoded_env = quote(env_id, safe="")
        encoded_path = encode_environment_file_path(rel)
        if encoded_path:
            start_url = f"{GEMINI_UPLOAD_ENV_BASE}/{encoded_env}/files/{encoded_path}"
        else:
            start_url = f"{GEMINI_UPLOAD_ENV_BASE}/{encoded_env}/files"
        params: dict[str, Any] = {}
        if overwrite:
            params["overwrite"] = "true"
        mime = (content_type or "application/octet-stream").strip() or "application/octet-stream"
        start_headers = {
            "x-goog-api-key": target_key,
            "Api-Revision": API_REVISION,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(data)),
            "X-Goog-Upload-Header-Content-Type": mime,
        }
        timeout = httpx.Timeout(ENV_FILES_UPLOAD_TIMEOUT, connect=30.0)
        async with httpx.AsyncClient(
            **build_httpx_client_kwargs(timeout=timeout, proxy=self.proxy, use_proxy=True)
        ) as client:
            try:
                start_resp = await client.put(start_url, params=params, headers=start_headers)
            except httpx.HTTPError as e:
                raise GeminiClientError(f"开始上传沙盒文件网络错误: {e}") from e
            if start_resp.status_code >= 400:
                raise GeminiClientError(self._format_http_error(start_resp, "开始上传沙盒文件"))
            upload_url = (
                start_resp.headers.get("x-goog-upload-url")
                or start_resp.headers.get("X-Goog-Upload-URL")
                or ""
            ).strip()
            if not upload_url:
                raise GeminiClientError("开始上传沙盒文件未返回 x-goog-upload-url。")
            put_headers = {
                "X-Goog-Upload-Command": "upload, finalize",
                "X-Goog-Upload-Offset": "0",
                "Content-Type": mime,
            }
            try:
                put_resp = await client.put(upload_url, content=data, headers=put_headers)
            except httpx.HTTPError as e:
                raise GeminiClientError(f"上传沙盒文件网络错误: {e}") from e
            if put_resp.status_code >= 400:
                raise GeminiClientError(self._format_http_error(put_resp, "上传沙盒文件"))
            try:
                payload = put_resp.json()
            except Exception:
                payload = {}
            if isinstance(payload, dict) and payload:
                return normalize_environment_file_entry(payload) or {
                    "name": rel.split("/")[-1],
                    "path": rel,
                    "type": "file",
                    "size_bytes": len(data),
                    "mime_type": mime,
                }
            return {
                "name": rel.split("/")[-1],
                "path": rel,
                "type": "file",
                "size_bytes": len(data),
                "mime_type": mime,
            }

    def _parse_json_response(self, resp: httpx.Response, *, action: str) -> dict[str, Any]:
        if resp.status_code >= 400:
            raise GeminiClientError(
                self._format_http_error(resp, action),
                status_code=resp.status_code,
            )
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
