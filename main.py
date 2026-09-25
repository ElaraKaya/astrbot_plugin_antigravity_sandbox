# AstrBot plugin: Google Antigravity managed-agent sandbox tasks.
#
# Sandbox-task field mapping (new sandbox / new session / interaction vs environment)
# follows OnsWayn/antigravity-agent-webui semantics, but this plugin talks to
# Gemini REST only. No local WebUI, no OpenAI/Gemini protocol gateway.
# Official docs:
#   https://ai.google.dev/gemini-api/docs/antigravity-agent
#   https://ai.google.dev/gemini-api/docs/managed-agents-quickstart
#   https://ai.google.dev/gemini-api/docs/agent-environment
#   https://ai.google.dev/gemini-api/docs/interactions-overview
#   https://ai.google.dev/gemini-api/docs/custom-agents
#   https://ai.google.dev/gemini-api/docs/background-execution
#   https://ai.google.dev/api/interactions  (REST)
#   https://ai.google.dev/api/environments  (REST Environments API)
# Environment cleanup launched 2026-09-08.

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
import uuid
import stat
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import astrbot.api.message_components as Comp
import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.star.filter.command import GreedyStr
from pydantic import Field
from pydantic.dataclasses import dataclass

try:
    from .gemini_client import (
        CHAT_PULL_MAX_BYTES,
        CHAT_PULL_TIMEOUT_SECONDS,
        DEFAULT_AGENT,
        DEFAULT_IN_PROGRESS_PER_KEY,
        DEFAULT_RECEIPT_TRUNCATE_CHARS,
        MSG_FILE_MISSING,
        MSG_FILE_TOO_LARGE,
        MSG_LIST_EMPTY,
        MSG_NO_CAPACITY,
        MSG_PULL_TIMEOUT,
        RUNNING_STATUS,
        UI_PULL_CONCURRENCY,
        UI_PULL_PROGRESS_INTERVAL,
        GeminiClientError,
        GeminiFileTimeoutError,
        GeminiFileTooLargeError,
        GeminiPullCancelled,
        GeminiRetrieveQueryError,
        GeminiSandboxClient,
        GeminiSubmitTimeoutError,
        ProgressThrottle,
        build_httpx_client_kwargs,
        clip_text,
        count_key_in_progress,
        ensure_md_filename,
        environment_id_of,
        is_safe_local_file_path,
        environment_size_bytes,
        extract_output_text,
        files_error_kind,
        fixed_files_message,
        normalize_environment_file_path,
        select_idle_keys,
        slim_environment_file_entry,
        summarize_steps,
        workspace_download_path,
    )
except ImportError:  # loaded as a loose main.py, not a package
    from gemini_client import (
        CHAT_PULL_MAX_BYTES,
        CHAT_PULL_TIMEOUT_SECONDS,
        DEFAULT_AGENT,
        DEFAULT_IN_PROGRESS_PER_KEY,
        DEFAULT_RECEIPT_TRUNCATE_CHARS,
        MSG_FILE_MISSING,
        MSG_FILE_TOO_LARGE,
        MSG_LIST_EMPTY,
        MSG_NO_CAPACITY,
        MSG_PULL_TIMEOUT,
        RUNNING_STATUS,
        UI_PULL_CONCURRENCY,
        UI_PULL_PROGRESS_INTERVAL,
        GeminiClientError,
        GeminiFileTimeoutError,
        GeminiFileTooLargeError,
        GeminiPullCancelled,
        GeminiRetrieveQueryError,
        GeminiSandboxClient,
        GeminiSubmitTimeoutError,
        ProgressThrottle,
        build_httpx_client_kwargs,
        clip_text,
        count_key_in_progress,
        ensure_md_filename,
        environment_id_of,
        is_safe_local_file_path,
        environment_size_bytes,
        extract_output_text,
        files_error_kind,
        fixed_files_message,
        normalize_environment_file_path,
        select_idle_keys,
        slim_environment_file_entry,
        summarize_steps,
        workspace_download_path,
    )

try:
    from astrbot.core import html_renderer
except ImportError:  # 本体版本差异时退化为纯文本回执
    html_renderer = None

PLUGIN_NAME = "astrbot_plugin_antigravity_sandbox"
STATE_FILES = (
    "task_keys.json",
    "task_index.json",
    "env_meta.json",
    "auto_retrieve.json",
)
ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
SHORT_INDEX_LIMIT = 2000
SHORT_INDEX_DROP = 1000
SHORT_ID_WIDTH = 4
SHORT_ID_SUBMIT_START = 1
SHORT_ID_SUBMIT_WRAP = 9999
CONTINUE_SHORT_TIME_WIDTH = 6
CONTINUE_SHORT_RE = re.compile(rf"^(\d+)_(\d{{{CONTINUE_SHORT_TIME_WIDTH}}})$")
# Windows 盘符前缀（C: / c:），用于 /agget 解析时区分绝对路径与 "<短号>:<路径>"
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:$")
TOKEN_TARGET = "/workspace/upload.token"
SANDBOX_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.I)
ENV_META_LIMIT = 2000
PROTECT_RECENT_SECONDS = 15 * 60
EMERGENCY_RUNNING_SECONDS = 2 * 60 * 60
DEFAULT_IDLE_TTL_HOURS = 24
DEFAULT_KEEP_RECENT = 2
AUTO_RETRIEVE_SECONDS = 60 * 60
AUTO_RETRIEVE_TICK_SECONDS = 30.0
PULL_TEMP_TTL_SECONDS = 30 * 60

RETRIEVE_QUERY_FAIL_PREFIX = "取回失败（查询超时或网络错误）"


def _retrieve_query_failure_message(short: str = "") -> str:
    """User-facing receipt when interaction GET times out / network / 504."""
    short = (short or "").strip()
    retry = f"/agretrieve {short}" if short else "/agretrieve <short>"
    return (
        f"{RETRIEVE_QUERY_FAIL_PREFIX}\n"
        "任务仍可能在沙盒里继续跑。\n"
        "可能是 antigravity-preview-09 沙盒取回的 bug。\n"
        f"请稍后再试 {retry}"
    )


def _is_retrieve_query_failure_text(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if RETRIEVE_QUERY_FAIL_PREFIX in raw:
        return True
    lowered = raw.lower()
    markers = (
        "查询任务超时或网络错误",
        "查询任务网络错误",
        "deadline_exceeded",
        "deadline exceeded",
        "http 504",
        "readtimeout",
        "connecttimeout",
        "timeoutexception",
    )
    return any(m in lowered for m in markers)

# 回执图片渲染宽度（px）。仅在网络模板渲染（remote）时作为 options.viewport_width 传出；
# 不传时远端默认 800。本地 PIL 兜底渲染不走这里。
RECEIPT_T2I_VIEWPORT_WIDTH = 1600
OUTPUT_EXTS = frozenset(
    {
        "html",
        "htm",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
        "bmp",
        "svg",
        "ico",
        "pdf",
        "txt",
        "md",
        "json",
        "csv",
        "xml",
        "yaml",
        "yml",
        "zip",
        "gz",
        "tgz",
        "tar",
        "tar.gz",
        "7z",
        "rar",
        "docx",
        "xlsx",
        "pptx",
        "doc",
        "xls",
        "ppt",
        "odt",
        "mp3",
        "mp4",
        "wav",
        "webm",
        "ogg",
        "flac",
        "mov",
        "py",
        "js",
        "ts",
        "css",
        "sh",
    }
)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"true", "1", "yes", "y", "on", "是"}:
        return True
    if s in {"false", "0", "no", "n", "off", "否"}:
        return False
    return default


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def receipt_image_applies(
    enabled: bool,
    status: str,
    text: str | int = "",
    min_length: int = 200,
) -> bool:
    """图片回执只覆盖状态为 completed 且文本字符数达到阈值的取回回执。"""
    if not bool(enabled) or _as_str(status).lower() != "completed":
        return False
    try:
        threshold = int(min_length)
    except (TypeError, ValueError):
        threshold = 200
    if threshold <= 0:
        return True
    if isinstance(text, (int, float)):
        return text >= threshold
    content = _as_str(text).strip()
    return len(content) >= threshold


_FLAT_CONFIG_KEYS = (
    "gemini_api_keys",
    "gemini_api_key",
    "default_model",
    "sandbox_agent",
    "submit_background",
    "max_total_tokens",
    "image_receipt_min_length",
    "upload_render_image",
    "upload_webhook_url",
    "upload_public_base_url",
    "upload_base_url",
    "upload_token",
    "upload_prefix",
    "env_auto_cleanup",
    "env_idle_ttl_hours",
    "env_keep_recent",
    "env_cleanup_scope",
    "env_cleanup_on_quota",
    "auto_retrieve",
)
_GROUPED_CONFIG_NAMES = ("model", "receipt", "image_host", "environment")


def _saved_plugin_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / f"{PLUGIN_NAME}_config.json"


def _config_path_candidates() -> list[Path]:
    paths = [_saved_plugin_config_path()]
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_config_path

        paths.append(Path(get_astrbot_config_path()) / f"{PLUGIN_NAME}_config.json")
    except Exception as exc:
        logger.warning(f"读取 AstrBot 配置目录失败，跳过该路径: {type(exc).__name__}")
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _pick_flat(flat: dict[str, Any], key: str, default: Any) -> Any:
    if key in flat and flat[key] is not None:
        return flat[key]
    return default


def grouped_config_from_flat(flat: dict[str, Any]) -> dict[str, Any]:
    """把 1.5.8 及更早的平铺配置收成四个分组。不记录、不返回日志用的密钥原文。"""
    keys = _pick_flat(flat, "gemini_api_keys", [])
    if isinstance(keys, str):
        keys = [keys] if _as_str(keys) else []
    elif isinstance(keys, list):
        keys = [item for item in keys if _as_str(item)]
    else:
        keys = []
    legacy_key = _as_str(flat.get("gemini_api_key"))
    if legacy_key and legacy_key not in keys:
        keys.append(legacy_key)

    webhook = _as_str(flat.get("upload_webhook_url"))
    public_base = _as_str(flat.get("upload_public_base_url"))
    legacy_base = _as_str(flat.get("upload_base_url"))
    if not webhook and legacy_base:
        webhook = legacy_base.rstrip("/") + "/Webhook/upload"
    if not public_base and legacy_base:
        public_base = legacy_base.rstrip("/")

    agent = _as_str(_pick_flat(flat, "sandbox_agent", DEFAULT_AGENT)) or DEFAULT_AGENT
    return {
        "model": {
            "gemini_api_keys": keys,
            "default_model": _as_str(_pick_flat(flat, "default_model", "auto")) or "auto",
            "sandbox_agent": agent,
            "submit_background": _as_bool(_pick_flat(flat, "submit_background", True), True),
            "max_total_tokens": _pick_flat(flat, "max_total_tokens", 0),
        },
        "receipt": {
            "image_receipt": True,
            "image_receipt_min_length": _pick_flat(flat, "image_receipt_min_length", 200),
            "receipt_template": _as_str(_pick_flat(flat, "receipt_template", "")),
            "auto_retrieve": _as_bool(_pick_flat(flat, "auto_retrieve", True), True),
        },
        "image_host": {
            "enabled": _as_bool(_pick_flat(flat, "upload_render_image", True), True),
            "webhook_url": webhook,
            "public_base_url": public_base,
            "token": _as_str(flat.get("upload_token")),
            "prefix": _as_str(_pick_flat(flat, "upload_prefix", "agysb")) or "agysb",
        },
        "environment": {
            "auto_cleanup": _as_bool(_pick_flat(flat, "env_auto_cleanup", True), True),
            "idle_ttl_hours": _pick_flat(flat, "env_idle_ttl_hours", DEFAULT_IDLE_TTL_HOURS),
            "keep_recent": _pick_flat(flat, "env_keep_recent", DEFAULT_KEEP_RECENT),
            "scope": _as_str(_pick_flat(flat, "env_cleanup_scope", "tracked")) or "tracked",
            "on_quota": _as_bool(_pick_flat(flat, "env_cleanup_on_quota", True), True),
        },
    }


def needs_flat_config_migration(data: dict[str, Any]) -> bool:
    if any(isinstance(data.get(name), dict) for name in _GROUPED_CONFIG_NAMES):
        return False
    return any(key in data for key in _FLAT_CONFIG_KEYS)


def migrate_saved_plugin_config(path: Path | None = None) -> bool:
    """在 AstrBot 按新 schema 裁剪配置之前，把平铺项收进分组。"""
    targets = [path] if path is not None else _config_path_candidates()
    migrated = False
    for target in targets:
        if target is None or not target.is_file():
            continue
        try:
            data = json.loads(target.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or not needs_flat_config_migration(data):
            continue
        grouped = grouped_config_from_flat(data)
        target.write_text(
            json.dumps(grouped, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8-sig",
        )
        migrated = True
    return migrated


def _looks_like_api_key(value: str) -> bool:
    text = _as_str(value)
    if not text or len(text) < 16:
        return False
    if text.startswith("AQ.") or text.startswith("AIza"):
        return True
    return "sha256:" not in text and len(text) >= 24 and " " not in text


def _key_fingerprint(value: str) -> str:
    text = _as_str(value)
    if not text:
        return ""
    if text.startswith("sha256:"):
        return text
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chmod_secret(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _write_json(path: Path, data: Any, *, secret: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    if secret:
        _chmod_secret(path)


def _now() -> datetime:
    return datetime.now().astimezone()


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(raw: str) -> datetime | None:
    text = _as_str(raw)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_now().tzinfo)
    return dt


def _fmt_bytes(n: int) -> str:
    size = float(max(0, int(n or 0)))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


def _submit_stamp() -> str:
    return datetime.now().strftime("%y%m%d%H%M%S")


def _short_serial(raw: str) -> int | None:
    text = _as_str(raw)
    matched = CONTINUE_SHORT_RE.fullmatch(text)
    if matched:
        return int(matched.group(1))
    return _parse_short_int(text)


def _output_file_names(output_files: str) -> list[str]:
    return [x.strip() for x in _as_str(output_files).split(",") if x.strip()]


def _safe_upload_name(name: str) -> str:
    """把上传文件名清理成安全文件名，保留扩展名点号。

    中文等非 ASCII 字符会变成下划线，主名清完只剩符号时回退 file。
    必须先拆主名和后缀再分别清理：直接对整名 strip("._") 会把扩展名前的
    点号一起剥掉，中文名（文转图模板.tar）就会变成没有扩展名的 tar。
    """
    raw = _as_str(name).strip().replace("\\", "/")
    path = PurePosixPath(raw)
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", path.stem).strip("._")
    suffix = re.sub(r"[^A-Za-z0-9.]", "", path.suffix)
    if suffix in ("", "."):
        suffix = ""
    return f"{stem or 'file'}{suffix}"


def _stamp_upload_name(name: str, stamp: str) -> str:
    safe = _safe_upload_name(name)
    stamp = _as_str(stamp)
    if stamp and safe.startswith(f"{stamp}_"):
        return safe
    return f"{stamp}_{safe}" if stamp else safe


def sanitize_id_for_fs(raw: str, *, fallback: str = "unknown") -> str:
    text = _as_str(raw)
    if not text or ".." in text or "\x00" in text:
        return fallback
    text = text.replace("/", "_").replace("\\", "_")
    text = ID_SAFE_RE.sub("_", text)
    text = text.strip("._") or fallback
    return text[:180]


def _parse_short_int(raw: str) -> int | None:
    text = _as_str(raw)
    if not text.isdigit():
        return None
    return int(text)


def _format_short(n: int) -> str:
    return f"{n:0{SHORT_ID_WIDTH}d}"


def _user_source_count(sources: list[dict[str, Any]] | None) -> int:
    count = 0
    for item in sources or []:
        if isinstance(item, dict) and item.get("target") == TOKEN_TARGET:
            continue
        count += 1
    return count


def _token_exts(token: str) -> list[str] | None:
    pieces = [p.strip().lstrip(".").lower() for p in _as_str(token).split(",") if p.strip()]
    if not pieces or any(p not in OUTPUT_EXTS for p in pieces):
        return None
    return pieces


def _parse_command_prompt(raw: str, *, default_ext: str | None = "md") -> tuple[str, str]:
    """Parse `/agsubmit [类型...] <任务文本>` / continue 的同类写法。

    类型必须写在任务文本前面；吃完类型后，剩余字符串原样当任务文本
    （只去掉两端空白，中间空格保留），避免从末尾剥类型时把最后一个词吃掉。
    """
    text = _as_str(raw)
    default_files = f"result.{default_ext}" if default_ext else ""
    if not text:
        return "", default_files
    rest = text
    groups: list[list[str]] = []
    while rest:
        parts = rest.split(None, 1)
        parsed = _token_exts(parts[0])
        if not parsed:
            break
        groups.append(parsed)
        rest = parts[1] if len(parts) > 1 else ""
    prompt = rest.strip()
    if not groups:
        return prompt, default_files
    exts: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for ext in group:
            if ext in seen:
                continue
            seen.add(ext)
            exts.append(ext)
    return prompt, ",".join(f"result.{ext}" for ext in exts)


def _split_continue_rest(rest: str) -> tuple[str, str]:
    """把 `/agcontinue` 的贪婪余段切成短号 + 任务原文。"""
    text = _as_str(rest)
    if not text:
        return "", ""
    parts = text.split(None, 1)
    task_ref = parts[0]
    prompt_raw = parts[1] if len(parts) > 1 else ""
    return task_ref, prompt_raw


def _body_sha256(text: str) -> str:
    return hashlib.sha256(_as_str(text).encode("utf-8")).hexdigest()


def _is_md_only_outputs(output_files: str) -> bool:
    names = _output_file_names(output_files)
    if not names:
        return False
    return all(Path(name).suffix.lower() == ".md" for name in names)


def _split_stored_urls(raw: str) -> list[str]:
    text = _as_str(raw).replace(",", "\n")
    urls: list[str] = []
    for piece in text.split():
        item = piece.strip()
        if item.startswith(("http://", "https://")):
            urls.append(item)
    return urls


def _is_image_blank(image_path: str) -> bool:
    """检查渲染出的图片是否正文为空白（如仅含顶栏或正文区域无有效内容）。"""
    if not image_path or not os.path.isfile(image_path):
        return True
    try:
        from PIL import Image

        with Image.open(image_path) as im:
            width, height = im.size
            if width <= 0 or height <= 0:
                return True
            crop_y = min(140, height // 3)
            if height <= crop_y:
                return True
            crop = im.convert("RGB").crop((0, crop_y, width, height))

            # 缩放至小图快速提取背景主色
            small = crop.resize((50, 50))
            colors = small.getcolors(2500)
            if not colors:
                return False
            bg = max(colors, key=lambda c: c[0])[1]
            bg_r, bg_g, bg_b = bg
            threshold_sq = 30 * 30

            # 跨步采样统计正文区域与背景色差异显著的像素
            pixels = crop.load()
            diff_count = 0
            total = 0
            step = 4
            for y in range(0, crop.height, step):
                for x in range(0, crop.width, step):
                    total += 1
                    r, g, b = pixels[x, y]
                    if (r - bg_r) ** 2 + (g - bg_g) ** 2 + (b - bg_b) ** 2 > threshold_sq:
                        diff_count += 1

            if total <= 0:
                return True
            # 若不同于背景的采样像素过少（<25 个或占比 <0.5%），判定为空白图片
            if diff_count < 25 or (diff_count / total) < 0.005:
                return True
    except Exception as e:
        logger.warning(f"检查图片空白时出错，默认视为非空白: {e}")
        return False
    return False


def _patch_template_for_safe_text(tmpl_str: str) -> str:
    """如果模板将 text 放在 JS 模板字符串中，将其改写为从 hidden textarea 读取，
    避免 JS 转义与 ${...} ReferenceError 导致白屏。"""
    if not tmpl_str:
        return ""

    def _inject_textarea(html: str) -> str:
        if '<textarea id="markdown-source"' in html:
            return html
        tag = '  <textarea id="markdown-source" hidden>{{ text | safe }}</textarea>\n'
        if '</body>' in html:
            return html.replace('</body>', f'{tag}</body>', 1)
        return html + f'\n{tag}'

    pattern = re.compile(r'marked\.parse\(\s*`\{\{\s*text(\s*\|\s*safe)?\s*\}\}`\s*\)')
    if pattern.search(tmpl_str):
        tmpl_str = pattern.sub(
            'marked.parse(document.getElementById("markdown-source").value)',
            tmpl_str,
        )
        tmpl_str = _inject_textarea(tmpl_str)

    backtick_pattern = re.compile(r'`\{\{\s*text(\s*\|\s*safe)?\s*\}\}`')
    if backtick_pattern.search(tmpl_str):
        tmpl_str = backtick_pattern.sub(
            'document.getElementById("markdown-source").value',
            tmpl_str,
        )
        tmpl_str = _inject_textarea(tmpl_str)

    return tmpl_str


def _sanitize_for_template(text: str) -> str:
    """对注入到 HTML 模板中的正文做安全转义，防止 </textarea> 闭合标签被误识别破坏 DOM。"""
    if not text:
        return ""
    return re.sub(r"</textarea>", "&lt;/textarea&gt;", text, flags=re.IGNORECASE)


def _t2i_hard_breaks(text: str) -> str:
    """render_t2i 走 markdown 渲染，给非代码块行补两个空格做硬换行，保留回执排版。"""
    if not text:
        return ""
    lines = text.split("\n")
    result: list[str] = []
    in_fence = False
    fence_char = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            cur_fence = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_char = cur_fence
                result.append(line)
            elif cur_fence == fence_char:
                in_fence = False
                fence_char = ""
                result.append(line)
            else:
                result.append(line)
        elif in_fence:
            result.append(line)
        elif not stripped:
            result.append("")
        elif stripped.startswith(("#", "|")):
            result.append(line)
        else:
            result.append(line + "  ")
    return "\n".join(result)


def _tool_event(context: Any) -> Any:
    """从工具调用上下文取事件；取不到返回 None（工具退化为纯文本回执）。"""
    event = getattr(context, "event", None)
    if event is None:
        event = getattr(getattr(context, "context", None), "event", None)
    return event


CONTINUE_GATE_HINT = (
    "续接前若未取回会先自动取回上一轮并发到聊天；status 不是 completed 则只返回当前状态、不续接。"
)

AGHELP_TEXT = (
    "Antigravity 沙盒指令：\n"
    "/agsubmit 或 /ags [类型...] <任务文本>\n"
    "  提交新任务。默认产出 result.md。例如：/ags png 查询今日新闻\n"
    "  可在本条消息附带图片/文件，或回复一条带图/文件的消息后再发送本指令。\n"
    "/agretrieve 或 /agr <任务编号>\n"
    "  取回任务回执。确定会发图片回执时正文不截断；其余按配置字数截断。\n"
    "/agcontinue 或 /agc [类型...] <任务文本>\n"
    "  在同一沙盒会话中续接任务。不写类型时默认 md，并返回对应预期网址。\n"
    "  附件可用本条消息附带图片/文件，或回复一条带图/文件的消息后再发送本指令。\n"
    "  若上一轮尚未取回会先自动取回。未完成任务无法续接。\n"
    "/agls <任务编号>\n"
    "  列出该短号沙盒 workspace 文件。\n"
    "/agget <任务编号> <完整路径>\n"
    "  拉取文件发到聊天。先按大小卡住 20MB，超过请改用图床或 WebUI。"
    "90 秒超时会直接中止。沙盒网络存疑，不一定能拉取成功。\n"
    "/agenvlist 或 /agels\n"
    "  管理员：列出当前项目沙盒环境数量与占用。\n"
    "/agenvcleanup 或 /agecl [all|短号]\n"
    "  管理员：回收沙盒。不带参数只扫本插件建过且已闲置的；"
    "all 扫整个项目可回收环境；短号立即删除。\n"
    "/aghelp\n"
    "  查看本说明"
)


class HandlerReceipt:
    def __init__(
        self,
        text: str,
        *,
        ok: bool = True,
        task_id: str = "",
        sandbox_id: str = "",
        status: str = "",
        source_count: int = 0,
        output: str = "",
        expected_urls: list[str] | None = None,
        steps: str = "",
        continue_blocked: bool = False,
        auto_retrieved: bool = False,
        pre_retrieve_reply: str = "",
        pre_receipt: HandlerReceipt | None = None,
        pre_image_sent: bool = False,
        image_path: str = "",
        image_url: str = "",
        put_notes: list[str] | None = None,
    ) -> None:
        self.text = text
        self.ok = ok
        self.task_id = task_id
        self.sandbox_id = sandbox_id
        self.status = status
        self.source_count = source_count
        self.output = output
        self.expected_urls = list(expected_urls or [])
        self.steps = steps
        self.continue_blocked = continue_blocked
        self.auto_retrieved = auto_retrieved
        self.pre_retrieve_reply = pre_retrieve_reply
        self.pre_receipt = pre_receipt
        self.pre_image_sent = pre_image_sent
        self.image_path = image_path
        self.image_url = image_url
        self.put_notes = list(put_notes or [])


SUBMIT_TOOL_DESC = (
"在 Linux 沙盒环境中异步执行耗时任务、长脚本，或进行深度网络调研与复杂项目分析时调用此工具。\n"
"支持传入指令并附带本地文件，返回任务编号 及产物预期公网地址。后续取回/续接只使用短号，不要向用户发送内部长 ID。\n"
"主要触发场景：\n"
"1. 深度调查与溯源：追查图片与文件来源、深度抓取与分析目标网站、检视开源项目源码等（此类深度任务优先于普通网页搜索调用）；\n"
"2. 长时间后台任务：耗时计算、批量处理、编译运行或生成复杂文件。"
)

RETRIEVE_TOOL_DESC = (
    "用提交或续接时返回的任务编号 查询 Antigravity 沙盒交互回执，"
    "获取会话状态、文本和步骤摘要。不要传内部长 ID。"
    "仅当 status 为 completed、开启了图片回执且正文字数达到阈值时，插件会把回执渲染成图片直接发给用户；"
    "其它状态或短文本发文本。"
    "除非用户明确要求文字细节，不要在回复里重复回执全文。"
    "不会下载或解压环境快照。"
)

CONTINUE_TOOL_DESC = (
    "基于已有沙盒任务的任务编号，异步提交后续交互指令进行追问、修正或执行下一步。"
    "插件会按短号查找当时绑定的 Key，并续接该沙盒最新一轮交互（不能从祖先 id 分叉）。"
    "续接不换 Key；该 Key 的 in_progress 已达上限时直接拒绝。"
    "附件用 Environments Files PUT 写入已有沙盒 workspace，不要走 interaction sources。"
    "无后缀的附件按 .md 写入。写入失败仍继续续接。"
    "若上一轮尚未取回，会先自动取回：status 为 completed、开启了图片回执且正文字数达标时把回执图片直接发给用户，否则发文本。"
    "status 不是 completed 则只返回当前状态、不提交续接。"
    "不要传内部长 ID。短号不变，覆盖为最新一轮。"
)

LIST_TOOL_DESC = (
    "列出某个 Antigravity 沙盒 workspace 里的文件。只使用任务编号。"
    "返回字段只有 name、path、type、size_bytes。"
    "沙盒网络存疑，不一定能成功列出或稍后拉取文件。"
)

GET_TOOL_DESC = (
    "从 Antigravity 沙盒 workspace 拉取一个文件发给用户。只使用任务编号 和文件名。"
    "沙盒网络存疑，不一定能成功拉取文件。超过 20MB 或 90 秒会中止，请改用图床或 WebUI。"
    "不要把文件内容复述进回复。"
)


def _upload_parameters() -> dict:
    return {
        "type": "string",
        "description": (
            "任务完成后需要上传到图床的文件名，逗号分隔，例如 "
            "result.jpg,report.pdf,archive.tar.gz；留空表示不上传文件。"
        ),
    }


def _submit_parameters() -> dict:
    return {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "要交给 Antigravity 智能体的任务文本（必填）。请严格聚焦当前任务，提交任务时请不要加入和用户需求无关的信息，不要强制关联记忆上下文。",
            },
            "output_files": {
                "type": "string",
                "description": (
                    "由调用方指定的任务产出原始文件名，逗号分隔；例如 "
                    "result.jpg,report.pdf,archive.tar.gz。"
                    "插件提交时自动加 YYMMDDHHMMSS_ 前缀（如 260901131456_new.docx），"
                    "用于上传路径和预期公网地址，避免多次任务覆盖同一文件名。插件不替调用方决定后缀。"
                ),
            },
            "file_paths": {
                "type": "string",
                "description": (
                    "逗号分隔的本地文件路径，作为 environment.sources inline 挂载。"
                    "默认 target=/workspace/<文件名>；若路径已是 /workspace/... 则沿用"
                ),
            },
            "file_contents": {
                "type": "string",
                "description": (
                    "额外内联文件的 JSON 列表，例如 "
                    '[{"target":"/workspace/notes.md","content":"# hi"}]'
                ),
            },
        },
        "required": ["prompt"],
    }


def _retrieve_parameters() -> dict:
    return {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "提交或续接时返回的任务编号，例如 0001",
            },
        },
        "required": ["task_id"],
    }


def _continue_parameters() -> dict:
    return {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "在现有沙盒及会话中继续执行的任务/追问指令（必填）。",
            },
            "task_id": {
                "type": "string",
                "description": (
                    "上一轮任务的任务编号，例如 0001。"
                    "插件据此查找当时绑定的 Key、沙盒和会话，并续接该沙盒最新一轮。"
                ),
            },
            "output_files": {
                "type": "string",
                "description": (
                    "本轮产出需上传到图床的文件名，逗号分隔；例如 "
                    "result.jpg,report.pdf。插件自动加时间戳前缀。"
                    "留空则默认 result.md，并返回对应预期网址。"
                ),
            },
            "file_paths": {
                "type": "string",
                "description": (
                    "逗号分隔的本地文件路径。续接时用 PUT 写入已有沙盒的 workspace，"
                    "不要放进 interaction sources。无后缀的文件会改成 .md。"
                    "某个文件写入失败时仍继续续接。"
                ),
            },
        },
        "required": ["prompt", "task_id"],
    }


def _list_parameters() -> dict:
    return {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "提交或续接时返回的任务编号，例如 0001",
            },
        },
        "required": ["task_id"],
    }


def _get_parameters() -> dict:
    return {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "提交或续接时返回的任务编号，例如 0001",
            },
            "name": {
                "type": "string",
                "description": "要拉取的文件名或 workspace 相对路径。中文名按原样传入。",
            },
        },
        "required": ["task_id", "name"],
    }


class AntigravitySandboxPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._data_dir = self._resolve_data_dir()
        self._migrate_legacy_state_files()
        self._key_mapping: dict[str, str] = self._load_key_mapping()
        self._short_index: dict[str, dict[str, str]] = self._load_short_index()
        self._env_meta: dict[str, dict[str, str]] = self._load_env_meta()
        self._pending_retrieve: dict[str, dict[str, str]] = self._load_pending_retrieve()
        self._scrub_persisted_secrets()
        self._cleanup_lock = asyncio.Lock()
        self._startup_task: asyncio.Task[None] | None = None
        self._auto_retrieve_task: asyncio.Task[None] | None = None
        self._pull_cleanup_task: asyncio.Task[None] | None = None
        self._ui_pull_sem = asyncio.Semaphore(UI_PULL_CONCURRENCY)
        self._ui_jobs: dict[str, dict[str, Any]] = {}
        self._ui_jobs_lock = asyncio.Lock()
        submit_tool = SubmitSandboxTaskTool()
        retrieve_tool = RetrieveSandboxTaskTool()
        continue_tool = ContinueSandboxTaskTool()
        list_tool = ListSandboxTaskTool()
        get_tool = GetSandboxTaskTool()
        submit_tool.plugin = self
        retrieve_tool.plugin = self
        continue_tool.plugin = self
        list_tool.plugin = self
        get_tool.plugin = self
        self.context.add_llm_tools(
            submit_tool, retrieve_tool, continue_tool, list_tool, get_tool
        )
        logger.info(
            "已注册 LLM 工具: submit_sandbox_task, retrieve_sandbox_task, "
            "continue_sandbox_task, list_sandbox_task, get_sandbox_task"
        )
        self._register_sandbox_files_web_api()

    async def initialize(self):
        self._auto_retrieve_task = asyncio.create_task(self._auto_retrieve_loop())
        self._pull_cleanup_task = asyncio.create_task(self._pull_temp_cleanup_loop())
        if not self._env_auto_cleanup():
            return
        self._startup_task = asyncio.create_task(self._startup_sweep())

    async def _startup_sweep(self) -> None:
        try:
            await self._sweep_environments(
                scope=self._env_cleanup_scope(),
                reason="startup",
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"启动时环境回收失败: {e}")

    def _resolve_data_dir(self) -> Path:
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME)).resolve()
        except Exception:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            path = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
            path.mkdir(parents=True, exist_ok=True)
            return path.resolve()

    def _legacy_plugin_dir(self) -> Path:
        return Path(__file__).resolve().parent

    def _migrate_legacy_state_files(self) -> None:
        src_dir = self._legacy_plugin_dir()
        dst_dir = self._data_dir
        dst_dir.mkdir(parents=True, exist_ok=True)
        for name in STATE_FILES:
            src = src_dir / name
            dst = dst_dir / name
            if not src.is_file():
                continue
            try:
                if not dst.exists():
                    shutil.copy2(src, dst)
                    _chmod_secret(dst)
                    logger.info(f"已迁移状态文件到 plugin_data: {name}")
                src.unlink()
                logger.info(f"已从插件目录移除状态文件: {name}")
            except OSError as e:
                logger.warning(f"迁移状态文件 {name} 失败: {e}")

    def _mapping_file(self) -> Path:
        return self._data_dir / "task_keys.json"

    def _short_index_file(self) -> Path:
        return self._data_dir / "task_index.json"

    def _env_meta_file(self) -> Path:
        return self._data_dir / "env_meta.json"

    def _pending_retrieve_file(self) -> Path:
        return self._data_dir / "auto_retrieve.json"

    def _group(self, name: str) -> dict[str, Any]:
        raw = (self.config or {}).get(name)
        return raw if isinstance(raw, dict) else {}

    def _setting(
        self,
        group: str,
        key: str,
        legacy: str | None = None,
        default: Any = None,
    ) -> Any:
        section = self._group(group)
        if key in section and section[key] is not None:
            return section[key]
        cfg = self.config or {}
        if legacy and legacy in cfg and cfg[legacy] is not None:
            return cfg[legacy]
        return default

    def _configured_api_keys(self) -> list[str]:
        configured = self._setting("model", "gemini_api_keys", "gemini_api_keys", []) or []
        if isinstance(configured, str):
            configured = [configured]
        keys = [_as_str(key) for key in configured if _as_str(key)]
        legacy_key = _as_str(self._setting("model", "gemini_api_key", "gemini_api_key", ""))
        if legacy_key:
            keys.append(legacy_key)
        return list(dict.fromkeys(keys))

    def _materialize_key(self, stored: str) -> str:
        stored = _as_str(stored)
        if not stored:
            return ""
        if _looks_like_api_key(stored) and not stored.startswith("sha256:"):
            return stored
        fp = _key_fingerprint(stored)
        for key in self._configured_api_keys():
            if _key_fingerprint(key) == fp:
                return key
        return ""

    def _stored_key_ref(self, key: str) -> str:
        key = _as_str(key)
        if not key:
            return ""
        return _key_fingerprint(key)

    def _scrub_persisted_secrets(self) -> None:
        mapping_changed = False
        index_changed = False
        cleaned_mapping: dict[str, str] = {}
        for ident, stored in self._key_mapping.items():
            ident = _as_str(ident)
            stored = _as_str(stored)
            if not ident or not stored:
                continue
            ref = self._stored_key_ref(stored)
            if not ref:
                continue
            if stored != ref:
                mapping_changed = True
            cleaned_mapping[ident] = ref
        if cleaned_mapping != self._key_mapping:
            self._key_mapping = cleaned_mapping
            mapping_changed = True
        for item in self._short_index.values():
            stored = _as_str(item.get("key"))
            if not stored:
                continue
            ref = self._stored_key_ref(stored)
            if stored != ref:
                if ref:
                    item["key"] = ref
                else:
                    item.pop("key", None)
                index_changed = True
            elif not stored.startswith("sha256:"):
                item["key"] = ref
                index_changed = True
        for item in self._short_index.values():
            if _as_str(item.get("key")):
                continue
            ref = _as_str(self._key_mapping.get(_as_str(item.get("task_id")))) or _as_str(
                self._key_mapping.get(_as_str(item.get("sandbox_id")))
            )
            if ref:
                item["key"] = self._stored_key_ref(ref)
                index_changed = True
        if mapping_changed:
            self._save_key_mapping()
        if index_changed:
            self._save_short_index()

    def _load_key_mapping(self) -> dict[str, str]:
        path = self._mapping_file()
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"读取 task_keys.json 失败: {e}")
            return {}

    def _save_key_mapping(self) -> None:
        path = self._mapping_file()
        try:
            sanitized: dict[str, str] = {}
            for ident, stored in self._key_mapping.items():
                ident = _as_str(ident)
                ref = self._stored_key_ref(stored)
                if ident and ref:
                    sanitized[ident] = ref
            self._key_mapping = sanitized
            if len(self._key_mapping) > 2000:
                keys_to_del = list(self._key_mapping.keys())[:-2000]
                for k in keys_to_del:
                    self._key_mapping.pop(k, None)
            _write_json(path, self._key_mapping, secret=True)
        except Exception as e:
            logger.warning(f"写入 task_keys.json 失败: {e}")

    def _record_key(
        self,
        key: str,
        *,
        task_id: str = "",
        sandbox_id: str = "",
        previous_task_id: str = "",
        overwrite_short: str = "",
    ) -> str:
        stored = self._stored_key_ref(key)
        if stored:
            updated = False
            if task_id:
                self._key_mapping[task_id] = stored
                updated = True
            if sandbox_id:
                self._key_mapping[sandbox_id] = stored
                updated = True
            if updated:
                self._save_key_mapping()
        short = ""
        if task_id and sandbox_id:
            short = self._record_short(
                task_id,
                sandbox_id,
                previous_task_id=previous_task_id,
                key=stored,
                overwrite_short=overwrite_short,
            )
        return short

    def _load_env_meta(self) -> dict[str, dict[str, str]]:
        path = self._env_meta_file()
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            meta: dict[str, dict[str, str]] = {}
            for key, value in data.items():
                sid = _as_str(key)
                if not sid:
                    continue
                if isinstance(value, dict):
                    meta[sid] = {
                        "last_used_at": _as_str(value.get("last_used_at")),
                        "status": _as_str(value.get("status")),
                    }
            return meta
        except Exception as e:
            logger.warning(f"读取 env_meta.json 失败: {e}")
            return {}

    def _save_env_meta(self) -> None:
        path = self._env_meta_file()
        try:
            if len(self._env_meta) > ENV_META_LIMIT:
                ranked = sorted(
                    self._env_meta.items(),
                    key=lambda item: _parse_iso(_as_str(item[1].get("last_used_at")))
                    or datetime.min.replace(tzinfo=_now().tzinfo),
                )
                for key, _ in ranked[: len(self._env_meta) - ENV_META_LIMIT]:
                    self._env_meta.pop(key, None)
            _write_json(path, self._env_meta)
        except Exception as e:
            logger.warning(f"写入 env_meta.json 失败: {e}")

    def _load_pending_retrieve(self) -> dict[str, dict[str, str]]:
        path = self._pending_retrieve_file()
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            pending: dict[str, dict[str, str]] = {}
            for key, value in data.items():
                short = _as_str(key)
                if not short or not isinstance(value, dict):
                    continue
                task_id = _as_str(value.get("task_id"))
                sandbox_id = _as_str(value.get("sandbox_id"))
                due_at = _as_str(value.get("due_at"))
                if not task_id or not sandbox_id or not due_at:
                    continue
                pending[short] = {
                    "task_id": task_id,
                    "sandbox_id": sandbox_id,
                    "due_at": due_at,
                }
            return pending
        except Exception as e:
            logger.warning(f"读取 auto_retrieve.json 失败: {e}")
            return {}

    def _save_pending_retrieve(self) -> None:
        path = self._pending_retrieve_file()
        try:
            _write_json(path, self._pending_retrieve)
        except Exception as e:
            logger.warning(f"写入 auto_retrieve.json 失败: {e}")

    def _auto_retrieve_enabled(self) -> bool:
        return _as_bool(self._setting("receipt", "auto_retrieve", "auto_retrieve", True), True)

    def _schedule_auto_retrieve(
        self,
        short: str,
        *,
        task_id: str,
        sandbox_id: str,
        due_at: datetime | None = None,
    ) -> None:
        short = _as_str(short)
        task_id = _as_str(task_id)
        sandbox_id = _as_str(sandbox_id)
        if not short or not task_id or not sandbox_id:
            return
        if not self._auto_retrieve_enabled():
            return
        when = due_at or (_now() + timedelta(seconds=AUTO_RETRIEVE_SECONDS))
        self._pending_retrieve[short] = {
            "task_id": task_id,
            "sandbox_id": sandbox_id,
            "due_at": when.isoformat(),
        }
        self._save_pending_retrieve()

    def _cancel_auto_retrieve(self, short: str) -> None:
        short = _as_str(short)
        if not short or short not in self._pending_retrieve:
            return
        self._pending_retrieve.pop(short, None)
        self._save_pending_retrieve()

    async def _auto_retrieve_loop(self) -> None:
        try:
            while True:
                try:
                    delay = await self._auto_retrieve_tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"自动取回循环异常: {e}")
                    delay = AUTO_RETRIEVE_TICK_SECONDS
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

    async def _auto_retrieve_tick(self) -> float:
        if not self._auto_retrieve_enabled():
            return AUTO_RETRIEVE_TICK_SECONDS
        now = _now()
        due_shorts = []
        next_due: datetime | None = None
        for short, info in list(self._pending_retrieve.items()):
            due_at = _parse_iso(_as_str(info.get("due_at")))
            if due_at is None:
                due_shorts.append(short)
                continue
            if due_at <= now:
                due_shorts.append(short)
                continue
            if next_due is None or due_at < next_due:
                next_due = due_at
        for short in due_shorts:
            await self._auto_retrieve_one(short)
        if next_due is None:
            return AUTO_RETRIEVE_TICK_SECONDS
        wait = (next_due - _now()).total_seconds()
        return max(1.0, min(AUTO_RETRIEVE_TICK_SECONDS, wait))

    async def _auto_retrieve_one(self, short: str) -> None:
        short = _as_str(short)
        info = self._pending_retrieve.pop(short, None)
        if info is not None:
            self._save_pending_retrieve()
        if not info:
            return
        if short not in self._short_index:
            logger.info(f"自动取回跳过 {short}：短号已清理")
            return
        task_id = _as_str(info.get("task_id"))
        sandbox_id = _as_str(info.get("sandbox_id"))
        try:
            receipt = await self._do_retrieve(
                task_id=task_id,
                sandbox_id=sandbox_id,
                cancel_auto=False,
            )
        except Exception as e:
            logger.warning(f"自动取回 {short} 失败: {e}")
            return
        if receipt.ok:
            logger.info(f"自动取回 {short} status={receipt.status or 'unknown'}")
        else:
            first_line = (receipt.text or "").split("\n", 1)[0]
            logger.warning(f"自动取回 {short} 失败: {first_line}")

    def _touch_sandbox(self, sandbox_id: str, *, status: str = "") -> None:
        sandbox_id = _as_str(sandbox_id)
        if not sandbox_id:
            return
        item = dict(self._env_meta.get(sandbox_id) or {})
        item["last_used_at"] = _now_iso()
        if status:
            item["status"] = _as_str(status)
        self._env_meta[sandbox_id] = item
        self._save_env_meta()

    def _env_auto_cleanup(self) -> bool:
        return _as_bool(
            self._setting("environment", "auto_cleanup", "env_auto_cleanup", True),
            True,
        )

    def _env_idle_ttl_hours(self) -> float:
        try:
            value = float(
                self._setting(
                    "environment",
                    "idle_ttl_hours",
                    "env_idle_ttl_hours",
                    DEFAULT_IDLE_TTL_HOURS,
                )
                or DEFAULT_IDLE_TTL_HOURS
            )
        except (TypeError, ValueError):
            value = float(DEFAULT_IDLE_TTL_HOURS)
        return max(0.0, value)

    def _env_keep_recent(self) -> int:
        try:
            value = int(
                self._setting(
                    "environment",
                    "keep_recent",
                    "env_keep_recent",
                    DEFAULT_KEEP_RECENT,
                )
                or DEFAULT_KEEP_RECENT
            )
        except (TypeError, ValueError):
            value = DEFAULT_KEEP_RECENT
        return max(0, value)

    def _env_cleanup_scope(self) -> str:
        raw = _as_str(
            self._setting("environment", "scope", "env_cleanup_scope", "tracked")
        ).lower()
        return "all" if raw == "all" else "tracked"

    def _env_cleanup_on_quota(self) -> bool:
        return _as_bool(
            self._setting("environment", "on_quota", "env_cleanup_on_quota", True),
            True,
        )

    def _tracked_sandbox_ids(self) -> set[str]:
        ids: set[str] = set()
        for item in self._short_index.values():
            sid = _as_str(item.get("sandbox_id"))
            if SANDBOX_ID_RE.fullmatch(sid):
                ids.add(sid)
        for key in self._key_mapping:
            if SANDBOX_ID_RE.fullmatch(key):
                ids.add(key)
        for key in self._env_meta:
            if SANDBOX_ID_RE.fullmatch(key):
                ids.add(key)
        return ids

    def _env_activity_at(self, env_id: str, remote: dict[str, Any]) -> datetime | None:
        local = self._env_meta.get(env_id) or {}
        candidates = [
            _parse_iso(_as_str(local.get("last_used_at"))),
            _parse_iso(_as_str(remote.get("last_accessed") or remote.get("lastAccessed"))),
            _parse_iso(_as_str(remote.get("updated"))),
            _parse_iso(_as_str(remote.get("created"))),
        ]
        times = [item for item in candidates if item is not None]
        if not times:
            return None
        return max(times)

    def _should_protect_env(
        self,
        *,
        env_id: str,
        activity: datetime | None,
        emergency: bool,
        now: datetime,
        ttl_seconds: float,
    ) -> bool:
        age = (now - activity).total_seconds() if activity is not None else 10**9
        status = _as_str((self._env_meta.get(env_id) or {}).get("status")).lower()
        if emergency:
            if age < PROTECT_RECENT_SECONDS:
                return True
            if status in RUNNING_STATUS and age < EMERGENCY_RUNNING_SECONDS:
                return True
            return False
        if age < ttl_seconds:
            return True
        if status in RUNNING_STATUS and age < max(ttl_seconds, EMERGENCY_RUNNING_SECONDS):
            return True
        return False

    async def _on_storage_quota(self, api_key: str) -> int:
        if not self._env_cleanup_on_quota():
            return 0
        result = await self._sweep_environments(
            scope="all",
            api_keys=[api_key],
            reason="quota",
            emergency=True,
        )
        return int(result.get("deleted") or 0)

    async def _sweep_environments(
        self,
        *,
        scope: str,
        api_keys: list[str] | None = None,
        reason: str = "",
        emergency: bool = False,
    ) -> dict[str, int]:
        client = self._client()
        keys = [k for k in (api_keys or client.api_keys) if k]
        if not keys:
            return {"listed": 0, "deleted": 0, "skipped": 0, "failed": 0, "bytes_freed": 0}
        scope = "all" if scope == "all" else "tracked"
        ttl_seconds = 0.0 if emergency else self._env_idle_ttl_hours() * 3600
        keep_recent = self._env_keep_recent()
        tracked = self._tracked_sandbox_ids()
        now = _now()
        listed = deleted = skipped = failed = bytes_freed = 0
        async with self._cleanup_lock:
            for key_index, key in enumerate(keys):
                try:
                    envs = await client.list_environments(api_key=key)
                except Exception as e:
                    logger.warning(f"列出 Key #{key_index + 1} 沙盒环境失败: {e}")
                    failed += 1
                    continue
                listed += len(envs)
                rows: list[tuple[datetime, str, dict[str, Any]]] = []
                fallback = datetime.min.replace(tzinfo=now.tzinfo)
                for env in envs:
                    eid = environment_id_of(env)
                    if not eid:
                        skipped += 1
                        continue
                    activity = self._env_activity_at(eid, env)
                    rows.append((activity or fallback, eid, env))
                rows.sort(key=lambda item: item[0], reverse=True)
                protected = {eid for _, eid, _ in rows[:keep_recent]}
                changed = False
                for activity, eid, env in rows:
                    if scope != "all" and eid not in tracked:
                        skipped += 1
                        continue
                    if eid in protected or self._should_protect_env(
                        env_id=eid,
                        activity=None if activity == fallback else activity,
                        emergency=emergency,
                        now=now,
                        ttl_seconds=ttl_seconds,
                    ):
                        skipped += 1
                        continue
                    try:
                        await client.delete_environment(eid, api_key=key)
                    except Exception as e:
                        logger.warning(f"删除环境 {eid[:8]}… 失败: {e}")
                        failed += 1
                        continue
                    deleted += 1
                    bytes_freed += environment_size_bytes(env)
                    if self._forget_sandbox(eid, save=False):
                        changed = True
                if changed:
                    self._save_env_meta()
                    self._save_short_index()
                    self._save_key_mapping()
                    self._save_pending_retrieve()
        logger.info(
            f"环境回收[{reason or 'manual'}] scope={scope} listed={listed} "
            f"deleted={deleted} skipped={skipped} failed={failed} "
            f"freed={_fmt_bytes(bytes_freed)}"
        )
        return {
            "listed": listed,
            "deleted": deleted,
            "skipped": skipped,
            "failed": failed,
            "bytes_freed": bytes_freed,
        }

    def _format_sweep_result(self, *, scope: str, result: dict[str, int]) -> str:
        return (
            f"环境回收完成（scope={scope}）\n"
            f"列出 {result['listed']} 个，删除 {result['deleted']} 个，"
            f"跳过 {result['skipped']} 个，失败 {result['failed']} 个，"
            f"释放 {_fmt_bytes(result['bytes_freed'])}。\n"
            "不带参数只扫本插件记录过的闲置沙盒；all 扫整个项目里可回收的。"
            "两者都会跳过未过 TTL、正在跑、以及每个 Key 最近保留的。"
            "要立刻删某个沙盒，用 /agenvcleanup <短号>。"
        )

    async def _delete_sandbox_target(self, ref: str) -> str:
        ref = _as_str(ref)
        sandbox_id = ""
        short = ""
        assigned_key = ""
        found = self._resolve_index_entry(ref)
        if found:
            short, item = found
            sandbox_id = _as_str(item.get("sandbox_id"))
            assigned_key = self._bound_key_of(item, sandbox_id=sandbox_id)
        elif SANDBOX_ID_RE.fullmatch(ref):
            sandbox_id = ref.lower()
            short = self._latest_short_for_sandbox(sandbox_id)
            assigned_key = self._find_key_for(sandbox_id=sandbox_id) or ""
        else:
            return (
                f"未找到 {ref}。"
                "用法: /agenvcleanup、/agenvcleanup all、/agenvcleanup <短号>"
            )
        if not sandbox_id:
            return f"短号 {short or ref} 没有绑定 sandbox。"
        client = self._client()
        keys = [assigned_key] if assigned_key else list(client.api_keys)
        keys = [k for k in keys if k]
        if not keys:
            return "未配置 Gemini API Key。"
        last_error = ""
        missing = False
        deleted = False
        for key in keys:
            try:
                await client.delete_environment(sandbox_id, api_key=key)
                deleted = True
                break
            except GeminiClientError as e:
                msg = str(e)
                last_error = msg
                lowered = msg.lower()
                if "404" in msg or "not_found" in lowered or "not found" in lowered:
                    missing = True
                continue
            except Exception as e:
                last_error = str(e)
                continue
        if not deleted and not missing:
            return f"删除失败: {last_error or '未知错误'}"
        self._forget_sandbox(sandbox_id)
        label = short or f"{sandbox_id[:8]}…"
        if deleted:
            return f"已删除沙盒 {label}。"
        return f"远端已不存在 {label}，已清本地记录。"

    def _find_key_for(self, *, task_id: str = "", sandbox_id: str = "") -> str | None:
        candidates: list[str] = []
        if task_id and task_id in self._key_mapping:
            candidates.append(self._key_mapping[task_id])
        if sandbox_id and sandbox_id in self._key_mapping:
            candidates.append(self._key_mapping[sandbox_id])
        if task_id:
            for item in self._short_index.values():
                if _as_str(item.get("task_id")) == task_id:
                    candidates.append(_as_str(item.get("key")))
        if sandbox_id:
            for item in self._short_index.values():
                if _as_str(item.get("sandbox_id")) == sandbox_id:
                    candidates.append(_as_str(item.get("key")))
        for stored in candidates:
            key = self._materialize_key(stored)
            if key:
                return key
        return None

    def _forget_sandbox(self, sandbox_id: str, *, save: bool = True) -> bool:
        sandbox_id = _as_str(sandbox_id)
        if not sandbox_id:
            return False
        changed = False
        shorts = [
            short
            for short, item in self._short_index.items()
            if _as_str(item.get("sandbox_id")) == sandbox_id
        ]
        for short in shorts:
            item = self._short_index.pop(short, None)
            if short in self._pending_retrieve:
                self._pending_retrieve.pop(short, None)
                changed = True
            if item:
                tid = _as_str(item.get("task_id"))
                if tid:
                    self._key_mapping.pop(tid, None)
            changed = True
        if sandbox_id in self._key_mapping:
            self._key_mapping.pop(sandbox_id, None)
            changed = True
        if sandbox_id in self._env_meta:
            self._env_meta.pop(sandbox_id, None)
            changed = True
        if changed and save:
            self._save_env_meta()
            self._save_short_index()
            self._save_key_mapping()
            self._save_pending_retrieve()
        return changed

    def _load_short_index(self) -> dict[str, dict[str, str]]:
        path = self._short_index_file()
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            index: dict[str, dict[str, str]] = {}
            for key, value in data.items():
                if isinstance(value, dict) and value.get("task_id") and value.get("sandbox_id"):
                    raw_key = str(key)
                    if CONTINUE_SHORT_RE.fullmatch(raw_key):
                        store_key = raw_key
                    else:
                        n = _parse_short_int(raw_key)
                        if n is None:
                            store_key = raw_key
                        elif len(raw_key) > SHORT_ID_WIDTH:
                            store_key = raw_key
                        else:
                            store_key = _format_short(n)
                    item = {
                        "task_id": str(value["task_id"]),
                        "sandbox_id": str(value["sandbox_id"]),
                    }
                    bound_key = self._stored_key_ref(value.get("key"))
                    if bound_key:
                        item["key"] = bound_key
                    for extra_key, extra_val in value.items():
                        if extra_key in {"task_id", "sandbox_id", "key"}:
                            continue
                        if isinstance(extra_val, (str, int, float, bool)):
                            text = _as_str(extra_val)
                            if text:
                                item[str(extra_key)] = text
                    index[store_key] = item
            return index
        except Exception as e:
            logger.warning(f"读取 task_index.json 失败: {e}")
            return {}

    def _save_short_index(self) -> None:
        path = self._short_index_file()
        try:
            for item in self._short_index.values():
                stored = _as_str(item.get("key"))
                if not stored:
                    continue
                ref = self._stored_key_ref(stored)
                if ref:
                    item["key"] = ref
                else:
                    item.pop("key", None)
            _write_json(path, self._short_index, secret=True)
        except Exception as e:
            logger.warning(f"写入 task_index.json 失败: {e}")

    def _trim_short_index(self, drop: int = SHORT_INDEX_DROP) -> None:
        if drop <= 0 or not self._short_index:
            return
        keys = list(self._short_index.keys())
        drop = min(drop, len(keys))
        pending_changed = False
        for key in keys[:drop]:
            self._short_index.pop(key, None)
            if key in self._pending_retrieve:
                self._pending_retrieve.pop(key, None)
                pending_changed = True
        if pending_changed:
            self._save_pending_retrieve()
        self._save_short_index()
        logger.info(
            f"task_index.json 已满 {SHORT_INDEX_LIMIT} 条，丢弃最早 {drop} 条，剩余 {len(self._short_index)}"
        )

    def _short_for_task(self, task_id: str) -> str:
        task_id = _as_str(task_id)
        if not task_id:
            return ""
        for short, item in self._short_index.items():
            if item.get("task_id") == task_id:
                return short
        return ""

    def _used_submit_serials(self) -> set[int]:
        used: set[int] = set()
        for key in self._short_index:
            n = _short_serial(key)
            if n is None or n < SHORT_ID_SUBMIT_START:
                continue
            used.add(n)
        return used

    def _alloc_submit_short(self) -> str:
        # 四位短号，取最小空号。续接覆盖同一短号，不另占号。
        used = self._used_submit_serials()
        for serial in range(SHORT_ID_SUBMIT_START, SHORT_ID_SUBMIT_WRAP + 1):
            if serial not in used:
                return _format_short(serial)
        self._trim_short_index()
        used = self._used_submit_serials()
        for serial in range(SHORT_ID_SUBMIT_START, SHORT_ID_SUBMIT_WRAP + 1):
            if serial not in used:
                return _format_short(serial)
        return _format_short(SHORT_ID_SUBMIT_START)

    def _short_item(
        self,
        task_id: str,
        sandbox_id: str,
        *,
        key: str = "",
        recorded_at: str = "",
    ) -> dict[str, str]:
        item = {
            "task_id": task_id,
            "sandbox_id": sandbox_id,
            "recorded_at": recorded_at or _now_iso(),
        }
        stored = self._stored_key_ref(key)
        if stored:
            item["key"] = stored
        return item

    def _copy_retrieve_meta(self, src: dict[str, str], dest: dict[str, str]) -> None:
        retrieved = _as_str(src.get("retrieved"))
        if retrieved:
            dest["retrieved"] = retrieved
        last_status = _as_str(src.get("last_status"))
        if last_status:
            dest["last_status"] = last_status

    def _copy_md_meta(self, src: dict[str, str], dest: dict[str, str]) -> None:
        for key in ("md_hash", "md_url", "md_name"):
            val = _as_str(src.get(key))
            if val:
                dest[key] = val

    def _mark_round_outputs(
        self,
        short: str,
        *,
        plugin_md: bool,
        chat_reply: str,
        expected_urls: list[str] | None = None,
    ) -> None:
        short = _as_str(short)
        if not short or short not in self._short_index:
            return
        item = dict(self._short_index[short])
        if plugin_md:
            item["plugin_md"] = "1"
        else:
            item.pop("plugin_md", None)
        reply = _as_str(chat_reply)
        if reply:
            item["chat_reply"] = reply
        else:
            item.pop("chat_reply", None)
        urls = [u for u in (expected_urls or []) if _as_str(u)]
        if urls:
            item["expected_urls"] = "\n".join(urls)
        else:
            item.pop("expected_urls", None)
        self._short_index[short] = item
        self._save_short_index()

    def _set_short_status(self, short: str, status: str) -> None:
        short = _as_str(short)
        if not short or short not in self._short_index:
            return
        item = dict(self._short_index[short])
        cleaned = _as_str(status).lower() or "unknown"
        item["last_status"] = cleaned
        self._short_index[short] = item
        self._save_short_index()

    def _remember_md_plan(self, short: str, name: str) -> None:
        short = _as_str(short)
        name = _as_str(name)
        if not short or not name or short not in self._short_index:
            return
        item = dict(self._short_index[short])
        item["md_plan_name"] = name
        self._short_index[short] = item
        self._save_short_index()

    def _in_progress_limit(self) -> int:
        raw = self._setting(
            "model",
            "max_in_progress_per_key",
            "max_in_progress_per_key",
            DEFAULT_IN_PROGRESS_PER_KEY,
        )
        try:
            return int(raw)
        except (TypeError, ValueError):
            return DEFAULT_IN_PROGRESS_PER_KEY

    def _in_progress_count(self, key: str) -> int:
        fp = self._stored_key_ref(key)
        return count_key_in_progress(list(self._short_index.values()), fp)

    def _idle_keys_from_cache(self, keys: list[str]) -> list[str]:
        counts = {key: self._in_progress_count(key) for key in keys}
        return select_idle_keys(keys, counts, self._in_progress_limit())

    async def _refresh_in_progress_cache(self, client: GeminiSandboxClient) -> None:
        """GET each locally in_progress short once. Failures keep the old status."""
        pending = [
            (short, dict(item))
            for short, item in self._short_index.items()
            if _as_str(item.get("last_status")).lower() == "in_progress"
        ]
        for short, item in pending:
            task_id = _as_str(item.get("task_id"))
            key = self._materialize_key(item.get("key"))
            if not task_id or not key:
                continue
            try:
                data = await client.get_interaction(task_id, api_key=key)
            except Exception as e:
                logger.warning(f"刷新短号 {short} 状态失败，仍按 in_progress 占用: {e}")
                continue
            status = _as_str(data.get("status")) or "unknown"
            self._set_short_status(short, status)

    async def _idle_keys_for_new_task(self, client: GeminiSandboxClient) -> list[str]:
        """Local cache first. Refresh once only when every key looks full."""
        keys = list(client.api_keys)
        idle = self._idle_keys_from_cache(keys)
        if idle:
            return idle
        await self._refresh_in_progress_cache(client)
        return self._idle_keys_from_cache(keys)

    def _round_chat_link_only(self, short: str) -> bool:
        item = self._short_index.get(_as_str(short)) or {}
        return _as_str(item.get("chat_reply")).lower() == "link"

    def _mark_short_retrieved(self, short: str, status: str) -> None:
        short = _as_str(short)
        if not short or short not in self._short_index:
            return
        item = dict(self._short_index[short])
        item["retrieved"] = "1"
        item["last_status"] = _as_str(status) or "unknown"
        self._short_index[short] = item
        self._save_short_index()

    def _short_retrieved_completed(self, short: str) -> bool:
        item = self._short_index.get(_as_str(short)) or {}
        if _as_str(item.get("retrieved")).lower() not in {"1", "true", "yes"}:
            return False
        return _as_str(item.get("last_status")).lower() == "completed"

    def _record_short(
        self,
        task_id: str,
        sandbox_id: str,
        *,
        previous_task_id: str = "",
        key: str = "",
        overwrite_short: str = "",
    ) -> str:
        task_id = _as_str(task_id)
        sandbox_id = _as_str(sandbox_id)
        key = (
            _as_str(key)
            or self._find_key_for(
                task_id=previous_task_id or task_id, sandbox_id=sandbox_id
            )
            or ""
        )
        if not task_id or not sandbox_id:
            return ""
        existing_short = self._short_for_task(task_id)
        if existing_short:
            old = self._short_index.get(existing_short) or {}
            item = self._short_item(
                task_id,
                sandbox_id,
                key=key or _as_str(old.get("key")),
                recorded_at=_as_str(old.get("recorded_at")),
            )
            self._copy_retrieve_meta(old, item)
            self._short_index[existing_short] = item
            self._save_short_index()
            return existing_short
        previous_task_id = _as_str(previous_task_id)
        short = _as_str(overwrite_short) or (
            self._short_for_task(previous_task_id) if previous_task_id else ""
        )
        if short:
            old = self._short_index.get(short) or {}
            item = self._short_item(
                task_id,
                sandbox_id,
                key=key or _as_str(old.get("key")),
            )
            self._copy_md_meta(old, item)
            self._short_index[short] = item
            self._save_short_index()
            return short
        if len(self._short_index) >= SHORT_INDEX_LIMIT:
            self._trim_short_index()
        short = self._alloc_submit_short()
        self._short_index[short] = self._short_item(task_id, sandbox_id, key=key)
        self._save_short_index()
        return short

    def _bound_key_of(self, item: dict[str, str] | None, *, task_id: str = "", sandbox_id: str = "") -> str:
        item = item or {}
        key = self._materialize_key(item.get("key"))
        if key:
            return key
        return self._find_key_for(
            task_id=task_id or _as_str(item.get("task_id")),
            sandbox_id=sandbox_id or _as_str(item.get("sandbox_id")),
        ) or ""

    def _resolve_index_entry(self, ref: str) -> tuple[str, dict[str, str]] | None:
        ref = _as_str(ref)
        if not ref:
            return None
        keys: list[str] = [ref]
        matched = CONTINUE_SHORT_RE.fullmatch(ref)
        if matched:
            prefix_n = int(matched.group(1))
            suffix = matched.group(2)
            keys.extend(
                [
                    f"{prefix_n}_{suffix}",
                    f"{prefix_n:04d}_{suffix}",
                    f"{prefix_n:05d}_{suffix}",
                ]
            )
        n = _parse_short_int(ref)
        if n is not None:
            keys.extend([_format_short(n), f"{n:05d}", str(n)])
        seen: set[str] = set()
        for key in keys:
            if key in seen:
                continue
            seen.add(key)
            item = self._short_index.get(key)
            if isinstance(item, dict):
                return key, item
        for key, value in self._short_index.items():
            if value.get("task_id") == ref:
                return key, value
        return None

    def _resolve_ids(self, ref: str) -> tuple[str, str, str] | None:
        found = self._resolve_index_entry(ref)
        if not found:
            return None
        _short, item = found
        task_id = _as_str(item.get("task_id"))
        sandbox_id = _as_str(item.get("sandbox_id"))
        if not task_id or not sandbox_id:
            return None
        return task_id, sandbox_id, self._bound_key_of(item, task_id=task_id, sandbox_id=sandbox_id)

    def _follow_latest_on_sandbox(
        self, task_id: str, sandbox_id: str, assigned_key: str
    ) -> tuple[str, str, str, str]:
        """续接必须接到该沙盒最新一轮，不能从祖先 interaction 分叉。"""
        latest_short = self._latest_short_for_sandbox(sandbox_id)
        user_short = self._short_for_task(task_id)
        if not latest_short:
            return task_id, sandbox_id, assigned_key, user_short
        item = self._short_index.get(latest_short) or {}
        latest_tid = _as_str(item.get("task_id"))
        latest_sid = _as_str(item.get("sandbox_id")) or sandbox_id
        if not latest_tid:
            return task_id, sandbox_id, assigned_key, user_short or latest_short
        latest_key = self._bound_key_of(
            item, task_id=latest_tid, sandbox_id=latest_sid
        )
        return (
            latest_tid,
            latest_sid,
            latest_key or assigned_key,
            user_short or latest_short,
        )

    def _single_latest_short_for_sandbox(self) -> str:
        """全部短号指向同一个沙盒时返回其中最新一个，否则返回空串。

        用于 /agget <完整路径> 省略短号前缀时兜底定位沙盒；存在多个沙盒时
        必须显式写 <短号>:<路径>，避免取错环境。
        """
        sandboxes = {
            _as_str(item.get("sandbox_id"))
            for item in self._short_index.values()
            if isinstance(item, dict)
        }
        sandboxes.discard("")
        if len(sandboxes) != 1:
            return ""
        only = next(iter(sandboxes))
        return self._latest_short_for_sandbox(only)

    def _latest_short_for_sandbox(self, sandbox_id: str) -> str:
        sandbox_id = _as_str(sandbox_id)
        if not sandbox_id:
            return ""
        best_rank: tuple[int, datetime, int] | None = None
        fallback = datetime.min.replace(tzinfo=_now().tzinfo)
        for short, item in self._short_index.items():
            if _as_str(item.get("sandbox_id")) != sandbox_id:
                continue
            ts = _parse_iso(_as_str(item.get("recorded_at")))
            matched = CONTINUE_SHORT_RE.fullmatch(short)
            if matched:
                n = int(matched.group(1)) * (10**CONTINUE_SHORT_TIME_WIDTH) + int(
                    matched.group(2)
                )
            else:
                parsed = _parse_short_int(short)
                n = (parsed * (10**CONTINUE_SHORT_TIME_WIDTH)) if parsed is not None else -1
            rank = (1 if ts is not None else 0, ts or fallback, n)
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best_short = short
        return best_short

    def _url_block(self, receipt: HandlerReceipt) -> str:
        if not receipt.expected_urls:
            return ""
        return "\n预期公网地址:\n" + "\n".join(receipt.expected_urls)

    def _public_task_short(self, receipt: HandlerReceipt) -> str:
        short = self._short_for_task(receipt.task_id)
        if short:
            return short
        if receipt.task_id and receipt.sandbox_id:
            return self._record_short(receipt.task_id, receipt.sandbox_id)
        return ""

    def _command_ack(self, receipt: HandlerReceipt) -> str:
        url_block = self._url_block(receipt)
        if receipt.continue_blocked:
            short = self._public_task_short(receipt) or self._short_for_task(
                receipt.task_id
            ) or "(未知)"
            status = receipt.status or "unknown"
            return (
                f"任务编号: {short}\n"
                f"status: {status}\n"
                f"上一轮尚未 completed，已自动取回并中止续接。完成后再 /agcontinue {short} [类型...] <任务文本>"
            )
        if not receipt.ok:
            text = receipt.text or "提交失败。"
            if "回执超时" in text:
                return "提交超时，未取得任务 id。详情见后台日志。" + url_block
            if _is_retrieve_query_failure_text(text):
                return text
            first = text.split("\n", 1)[0]
            if receipt.status:
                return f"{first}\nstatus: {receipt.status}"
            return first
        if not receipt.task_id:
            text = receipt.text or "提交失败。"
            if "回执超时" in text:
                return "提交超时，未取得任务 id。详情见后台日志。" + url_block
            return text.split("\n", 1)[0]
        short = self._public_task_short(receipt)
        lines = [
            f"任务编号: {short}",
            f"status: {receipt.status or 'unknown'}",
            f"挂载文件数: {receipt.source_count}",
        ]
        if receipt.auto_retrieved:
            lines.append("已自动取回上一轮（completed）后提交续接。")
        if url_block:
            lines.append(url_block.lstrip("\n"))
        if receipt.put_notes:
            lines.append("续接写入:")
            lines.extend(receipt.put_notes)
        lines.extend(
            [
                f"后续 /agr {short} 取回，",
                f"/agc {short} [类型...] <任务文本> 续接任务，",
                CONTINUE_GATE_HINT,
            ]
        )
        return "\n".join(lines)

    def _llm_ack(self, receipt: HandlerReceipt) -> str:
        url_block = self._url_block(receipt)
        prefix = ""
        if receipt.pre_image_sent:
            prefix = "【上一轮已自动取回】回执图片已直接发送给用户，无需重复其全文。\n\n"
        elif receipt.pre_retrieve_reply:
            prefix = "【上一轮已自动取回】\n" + receipt.pre_retrieve_reply + "\n\n"
        if receipt.continue_blocked:
            short = self._public_task_short(receipt) or self._short_for_task(
                receipt.task_id
            ) or "(未知)"
            status = receipt.status or "unknown"
            lines = [
                "【续接已中止】",
                f"任务编号: {short}",
                f"status: {status}",
                "上一轮交互尚未 completed，已自动取回当前状态，未提交续接。",
                "请等待 completed 后再调用 continue_sandbox_task。",
            ]
            if not prefix:
                output = (receipt.output or "").strip()
                if output:
                    clipped = output if len(output) <= 800 else output[:800] + "\n…(截断)"
                    lines.extend(["", "当前 output_text:", clipped])
            return prefix + "\n".join(lines)
        if not receipt.ok or not receipt.task_id:
            text = receipt.text or "提交失败。"
            if "回执超时" in text:
                return prefix + "提交超时，未取得任务 id。详情见后台日志。" + url_block
            if _is_retrieve_query_failure_text(text):
                return prefix + text + url_block
            first = text.split("\n", 1)[0]
            if receipt.status:
                return prefix + f"{first}\nstatus: {receipt.status}" + url_block
            return prefix + first + url_block
        short = self._public_task_short(receipt)
        lines = [
            "【Antigravity 沙盒任务已受理】",
            f"任务编号: {short}",
            f"status: {receipt.status or 'unknown'}",
            f"挂载文件数: {receipt.source_count}",
        ]
        if receipt.auto_retrieved:
            lines.append("已自动取回上一轮（completed）后提交续接。")
        if url_block:
            lines.append(url_block.lstrip("\n"))
        if receipt.put_notes:
            lines.append("续接写入:")
            lines.extend(receipt.put_notes)
        lines.extend(
            [
                "",
                f"后续调用 retrieve_sandbox_task，传入 task_id={short} 取回；",
                f"调用 continue_sandbox_task，传入 task_id={short} 与 prompt 续接；",
                f"调用 list_sandbox_task / get_sandbox_task 时同样使用 task_id={short}。",
                CONTINUE_GATE_HINT,
                "只使用短号任务编号，不要向用户发送内部长 ID。",
            ]
        )
        return prefix + "\n".join(lines)

    def _receipt_link_urls(self, receipt: HandlerReceipt) -> list[str]:
        urls = [_as_str(u) for u in receipt.expected_urls if _as_str(u)]
        if urls:
            return urls
        short = self._short_for_task(receipt.task_id)
        item = self._short_index.get(_as_str(short)) or {}
        return _split_stored_urls(
            _as_str(item.get("md_url")) or _as_str(item.get("expected_urls"))
        )

    def _truncate_chars(self) -> int:
        raw = self._setting(
            "receipt",
            "truncate_chars",
            "truncate_chars",
            DEFAULT_RECEIPT_TRUNCATE_CHARS,
        )
        try:
            return int(raw)
        except (TypeError, ValueError):
            return DEFAULT_RECEIPT_TRUNCATE_CHARS

    def _clip_user_text(self, text: str, *, keep_full: bool) -> str:
        return clip_text(text, self._truncate_chars(), keep_full=keep_full)

    def _command_retrieve_reply(self, receipt: HandlerReceipt, *, keep_full: bool = False) -> str:
        if not receipt.ok:
            text = receipt.text or "取回失败。"
            if _is_retrieve_query_failure_text(text):
                return text
            return text.split("\n", 1)[0]
        short = self._short_for_task(receipt.task_id) or "(未知)"
        status = receipt.status or "unknown"
        output = (receipt.output or "").strip() or "(尚无 output_text)"
        output = self._clip_user_text(output, keep_full=keep_full)
        lines = [f"任务编号: {short}", f"status: {status}", "", output]
        urls = self._receipt_link_urls(receipt)
        if urls:
            lines.append("")
            lines.extend(urls)
        return "\n".join(lines)

    def _llm_retrieve_reply(self, receipt: HandlerReceipt) -> str:
        if not receipt.ok:
            text = receipt.text or "取回失败。"
            if _is_retrieve_query_failure_text(text):
                return text
            return text.split("\n", 1)[0]
        short = self._short_for_task(receipt.task_id) or "(未知)"
        output = (receipt.output or "").strip() or "(尚无 output_text)"
        output = self._clip_user_text(output, keep_full=False)
        lines = [
            "【Antigravity 沙盒任务回执】",
            f"任务编号: {short}",
            f"status: {receipt.status or 'unknown'}",
            "",
            "—— 回执文本（output_text）——",
            output,
        ]
        if receipt.expected_urls:
            lines.extend(["", "产物链接:"] + list(receipt.expected_urls))
        if receipt.steps:
            lines.extend(["", "—— steps 摘要 ——", receipt.steps])
        return "\n".join(lines)

    def _llm_retrieve_brief(self, receipt: HandlerReceipt) -> str:
        """回执图片已直接发给用户时，只给 LLM 简报，避免它再复述全文刷屏。"""
        short = self._short_for_task(receipt.task_id) or "(未知)"
        lines = [
            "【Antigravity 沙盒任务回执】",
            f"任务编号: {short}",
            f"status: {receipt.status or 'unknown'}",
            "回执已由插件渲染成图片并直接发送给用户。",
            "除非用户明确要求文字细节，不要在回复中重复回执全文。",
        ]
        return "\n".join(lines)

    def _log_command_receipt(self, tag: str, text: str) -> None:
        logger.info(f"{tag}\n{text}")

    def _named_temp_copy(self, src: str, preferred_name: str) -> str:
        src_path = Path(src)
        name = _safe_upload_name(preferred_name) if preferred_name else src_path.name
        if not name:
            name = src_path.name or "file"
        if src_path.name == name:
            return str(src_path)
        dest = src_path.parent / name
        if dest.exists() and dest.resolve() != src_path.resolve():
            stem = Path(name).stem
            suffix = Path(name).suffix
            i = 2
            while True:
                candidate = src_path.parent / f"{stem}_{i}{suffix}"
                if not candidate.exists():
                    dest = candidate
                    break
                i += 1
        shutil.copy2(src_path, dest)
        return str(dest)

    async def _collect_event_file_paths(self, event: AstrMessageEvent) -> list[str]:
        messages = list(event.get_messages() or [])
        segs: list[Any] = []
        for seg in messages:
            segs.append(seg)
            if isinstance(seg, Comp.Reply) and getattr(seg, "chain", None):
                segs.extend(seg.chain or [])
        paths: list[str] = []
        seen: set[str] = set()
        for seg in segs:
            local = ""
            preferred = ""
            try:
                if isinstance(seg, Comp.Image):
                    local = await seg.convert_to_file_path()
                    preferred = Path(local).name if local else ""
                elif isinstance(seg, Comp.File):
                    local = await seg.get_file()
                    preferred = _as_str(getattr(seg, "name", "")) or Path(local).name
            except Exception as e:
                logger.warning(f"提取指令附件失败: {e}")
                continue
            if not local:
                continue
            src = Path(local)
            if not src.is_file():
                continue
            named = self._named_temp_copy(str(src), preferred)
            key = str(Path(named).resolve())
            if key in seen:
                continue
            seen.add(key)
            paths.append(named)
        return paths

    def _client(self) -> GeminiSandboxClient:
        try:
            max_tokens = int(self._setting("model", "max_total_tokens", "max_total_tokens", 0) or 0)
        except (TypeError, ValueError):
            max_tokens = 0
        keys = self._configured_api_keys()
        return GeminiSandboxClient(
            api_keys=keys,
            default_model=_as_str(self._setting("model", "default_model", "default_model", "auto"))
            or "auto",
            agent=_as_str(self._setting("model", "sandbox_agent", "sandbox_agent", ""))
            or DEFAULT_AGENT,
            submit_background=_as_bool(
                self._setting("model", "submit_background", "submit_background", True),
                True,
            ),
            max_total_tokens=max_tokens,
            proxy=self._proxy_url(),
        )

    def _proxy_url(self) -> str:
        return _as_str(self._setting("network", "proxy", "proxy", ""))

    def _image_receipt_enabled(self) -> bool:
        return _as_bool(self._setting("receipt", "image_receipt", None, True), True)

    def _image_receipt_min_length(self) -> int:
        val = self._setting(
            "receipt", "image_receipt_min_length", "image_receipt_min_length", 200
        )
        try:
            return int(val)
        except (TypeError, ValueError):
            return 200

    def _receipt_template(self) -> str:
        return _as_str(
            self._setting("receipt", "receipt_template", "t2i_template", "")
            or self._setting("receipt", "t2i_template", None, "")
        ).strip()

    def _resolve_t2i_template(self) -> str:
        """获取回执渲染使用的模板名称，留空则沿用 AstrBot 当前选中的模板。"""
        cfg_template = self._receipt_template()
        if cfg_template:
            return cfg_template
        config_obj = self._get_context_config()
        if hasattr(config_obj, "get"):
            try:
                active = config_obj.get("t2i_active_template")
                if isinstance(active, str) and active.strip():
                    return active.strip()
            except Exception as e:
                logger.debug(f"获取 t2i_active_template 出错: {e}")
        return "base"

    def _resolve_t2i_use_network(self) -> bool:
        """获取是否使用网络渲染策略（跟随 AstrBot 全局 t2i_strategy 配置）。"""
        config_obj = self._get_context_config()
        if hasattr(config_obj, "get"):
            try:
                strategy = config_obj.get("t2i_strategy", "remote")
                if isinstance(strategy, str):
                    return strategy != "local"
            except Exception as e:
                logger.debug(f"获取 t2i_strategy 出错: {e}")
        return True

    def _image_host_enabled(self) -> bool:
        return _as_bool(
            self._setting("image_host", "enabled", "upload_render_image", True),
            True,
        )

    def _stamped_upload_names(self, output_files: str, stamp: str) -> list[str]:
        return [_stamp_upload_name(name, stamp) for name in _output_file_names(output_files)]

    def _resolved_upload_cfg(self) -> tuple[str, str, str, str]:
        if not self._image_host_enabled():
            return "", "", "", ""
        webhook = _as_str(self._setting("image_host", "webhook_url", "upload_webhook_url", ""))
        public_base = _as_str(
            self._setting("image_host", "public_base_url", "upload_public_base_url", "")
        )
        legacy_base = _as_str(self._setting("image_host", "base_url", "upload_base_url", ""))
        if not webhook and legacy_base:
            webhook = legacy_base.rstrip("/") + "/Webhook/upload"
        if not public_base and legacy_base:
            public_base = legacy_base.rstrip("/")
        token = _as_str(self._setting("image_host", "token", "upload_token", ""))
        prefix = _as_str(self._setting("image_host", "prefix", "upload_prefix", "")) or "agysb"
        return webhook, public_base, token, prefix

    def _public_file_url(self, public_base: str, prefix: str, name: str) -> str:
        root = public_base.rstrip("/") + "/" + prefix.strip("/")
        return f"{root}/{name}"

    def _expected_public_urls(self, output_files: str, stamp: str) -> list[str]:
        _webhook, public_base, _token, prefix = self._resolved_upload_cfg()
        names = self._stamped_upload_names(output_files, stamp)
        if not public_base or not names:
            return []
        return [self._public_file_url(public_base, prefix, name) for name in names]

    def _upload_instruction(self, output_files: str, stamp: str) -> str:
        webhook, public_base, token, prefix = self._resolved_upload_cfg()
        names = self._stamped_upload_names(output_files, stamp)
        if not webhook or not public_base or not token or not names:
            return ""
        mapping = ", ".join(
            f"{orig} -> {stamped}" for orig, stamped in zip(_output_file_names(output_files), names)
        )
        return (
            "\n\n【文件上传要求】任务完成后，将以下文件上传到图床："
            + ", ".join(names)
            + f"。原始文件名与上传文件名对应：{mapping}。"
            f"上传接口：{webhook}；上传目录：{prefix}/；"
            + f"公网基础地址：{public_base.rstrip(chr(47))}。"
            "必须使用上述带时间戳前缀的文件名上传，禁止使用未加前缀的原始文件名，避免覆盖历史文件。"
            "从 /workspace/upload.token 读取 Bearer Token，禁止输出 Token。"
            "文件后缀以调用方指定为准；如需上传大包，建议调用方指定 .tar.gz。上传后用 GET 验证公网文件 URL 返回 200，"
            "并在最终回执中列出每个文件的公网 URL。"
        )

    def _public_url_from_upload_payload(
        self, payload: dict[str, Any], *, public_base: str, fallback: str
    ) -> str:
        url = _as_str(payload.get("full_url") or payload.get("url"))
        if url.startswith("/uploads/"):
            url = public_base.rstrip("/") + url[len("/uploads") :]
        elif url.startswith("/"):
            url = public_base.rstrip("/") + url
        return url or fallback

    async def _upload_file_bytes(self, *, data: bytes, filename: str) -> str:
        webhook, public_base, token, prefix = self._resolved_upload_cfg()
        name = _safe_upload_name(filename)
        if not webhook or not public_base or not token or not name:
            return ""
        dest_path = f"{prefix.strip('/')}/{name}"
        fallback = self._public_file_url(public_base, prefix, name)
        headers = {"Authorization": f"Bearer {token}"}
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        files = {
            "file": (name, data, content_type),
        }
        try:
            # 图床 Webhook 不走插件代理；沙盒内部访问图床也无法被该代理覆盖。
            async with httpx.AsyncClient(
                **build_httpx_client_kwargs(
                    timeout=httpx.Timeout(30.0, connect=15.0),
                    proxy=self._proxy_url(),
                    use_proxy=False,
                )
            ) as client:
                resp = await client.post(
                    webhook,
                    headers=headers,
                    data={"path": dest_path},
                    files=files,
                )
        except httpx.HTTPError as e:
            logger.warning(f"插件上传 {name} 失败: {e}")
            return ""
        if resp.status_code >= 400:
            preview = (resp.text or "")[:300].replace("\n", " ")
            logger.warning(f"插件上传 {name} HTTP {resp.status_code}: {preview}")
            return ""
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return self._public_url_from_upload_payload(
            payload, public_base=public_base, fallback=fallback
        )

    async def _upload_markdown_bytes(self, *, body: str, filename: str) -> str:
        return await self._upload_file_bytes(
            data=body.encode("utf-8"), filename=filename
        )

    async def _do_render_t2i(
        self,
        *,
        text: str,
        template_name: str,
        use_network: bool,
    ) -> str:
        """执行 T2I 渲染。优先使用网络策略与指定模板，失败时尝试本地策略兜底。"""
        if use_network and hasattr(html_renderer, "network_strategy"):
            net = getattr(html_renderer, "network_strategy", None)
            if net is not None:
                try:
                    try:
                        tmpl_str = await net.get_template(name=template_name)
                    except Exception as e:
                        logger.warning(
                            f"获取模板 {template_name} 失败: {e}，回退使用 base 模板"
                        )
                        if template_name != "base":
                            tmpl_str = await net.get_template(name="base")
                        else:
                            raise

                    safe_tmpl = _patch_template_for_safe_text(tmpl_str)
                    safe_text = _sanitize_for_template(text)
                    version = getattr(net, "version", "")
                    if not version:
                        try:
                            from astrbot.core.version import VERSION

                            version = f"v{VERSION}"
                        except Exception:
                            version = ""
                    tmpl_data = {
                        "text": safe_text,
                        "version": version,
                    }
                    img_path = await html_renderer.render_custom_template(
                        safe_tmpl,
                        tmpl_data,
                        return_url=False,
                        options={"viewport_width": RECEIPT_T2I_VIEWPORT_WIDTH},
                    )
                    if img_path and not _is_image_blank(img_path):
                        return img_path
                    logger.warning("网络模板渲染成功但图片正文为空白，尝试本地渲染兜底")
                except Exception as e:
                    logger.warning(f"网络渲染回执失败: {e}，尝试本地策略兜底")

        # 本地策略渲染兜底
        local = getattr(html_renderer, "local_strategy", None)
        if local is not None:
            try:
                local_path = await local.render(text)
                if local_path and not _is_image_blank(local_path):
                    return local_path
                logger.warning("本地渲染出的图片正文为空白")
            except Exception as e:
                logger.warning(f"本地渲染回执失败: {e}")

        return ""

    async def _render_receipt_image(self, text: str) -> str:
        """html_renderer.render_t2i 把回执渲染成图片，返回本地路径；失败返回 ""。"""
        body = _as_str(text)
        if not body.strip():
            return ""
        if html_renderer is None:
            logger.warning("html_renderer 不可用，取回回执按纯文本发送")
            return ""

        prepared_text = _t2i_hard_breaks(body)
        template_name = self._resolve_t2i_template()
        use_network = self._resolve_t2i_use_network()

        try:
            image_path = await self._do_render_t2i(
                text=prepared_text,
                template_name=template_name,
                use_network=use_network,
            )
            if image_path and not _is_image_blank(image_path):
                return image_path
        except Exception as e:
            logger.warning(f"回执渲染图片发生异常: {e}")

        logger.warning("回执渲染图片失败或结果为空白，按纯文本发送")
        return ""

    async def _attach_receipt_image(self, receipt: HandlerReceipt) -> None:
        """取回成功且达到字数阈值时把回执渲染成图片，直接保留本地图片路径。"""
        if not receipt.ok:
            return
        image_path = await self._render_receipt_image(
            self._command_retrieve_reply(receipt, keep_full=True)
        )
        if not image_path:
            return
        receipt.image_path = image_path
        receipt.image_url = ""

    async def _send_receipt_image(
        self,
        event: Any,
        image_path: str,
        extra_urls: list[str] | str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        """工具调用途中或指令处理中把回执图片直接发给用户，不再拼接参考链接。"""
        if not image_path or event is None:
            return False
        actual_urls: list[str] = []
        if isinstance(extra_urls, list):
            actual_urls = extra_urls
        elif isinstance(extra_urls, str) and extra_urls and not extra_urls.startswith("http"):
            actual_urls = [extra_urls]
        if args and isinstance(args[0], list):
            actual_urls = args[0]
        if "urls" in kwargs and isinstance(kwargs["urls"], list):
            actual_urls = kwargs["urls"]

        try:
            link_text = self._receipt_link_text(actual_urls)
            result = event.make_result().file_image(image_path)
            if link_text:
                result.message("\n" + link_text)
            await event.send(result)
            return True
        except Exception as e:
            logger.warning(f"发送回执图片失败，回退文本回执: {e}")
            return False

    def _receipt_link_text(
        self,
        urls: list[str] | str | None = None,
        *args: Any,
    ) -> str:
        """格式化回执附加产物链接（不再拼接回执图片参考链接）。"""
        target: list[str] = []
        if isinstance(urls, list):
            target = urls
        elif args and isinstance(args[0], list):
            target = args[0]
        clean = [_as_str(u) for u in target if _as_str(u)]
        return "\n".join(clean)

    async def _maybe_plugin_upload_md(
        self, short: str, output: str, status: str
    ) -> list[str]:
        short = _as_str(short)
        item = self._short_index.get(short) or {}
        stored_expected = _split_stored_urls(_as_str(item.get("expected_urls")))
        if _as_str(item.get("plugin_md")).lower() not in {"1", "true", "yes"}:
            return stored_expected
        if _as_str(status).lower() != "completed":
            md_url = _as_str(item.get("md_url"))
            return [md_url] if md_url else stored_expected
        body = (output or "").strip()
        if not body:
            md_url = _as_str(item.get("md_url"))
            return [md_url] if md_url else stored_expected
        digest = _body_sha256(body)
        old_hash = _as_str(item.get("md_hash"))
        old_url = _as_str(item.get("md_url"))
        if digest == old_hash and old_url:
            return [old_url]
        planned = _as_str(item.get("md_plan_name"))
        stamp = _submit_stamp()
        name = planned or _stamp_upload_name("result.md", stamp)
        url = await self._upload_markdown_bytes(body=body, filename=name)
        if not url:
            return [old_url] if old_url else stored_expected
        item = dict(self._short_index.get(short) or item)
        item["md_hash"] = digest
        item["md_url"] = url
        item["md_name"] = name
        if short in self._short_index:
            self._short_index[short] = item
            self._save_short_index()
        return [url]

    async def handle_submit(
        self,
        *,
        prompt: str = "",
        file_paths: str = "",
        file_contents: str = "",
        output_files: str = "",
        **_kwargs: Any,
    ) -> str:
        receipt = await self._do_submit(
            prompt=prompt,
            file_paths=file_paths,
            file_contents=file_contents,
            output_files=output_files,
        )
        self._log_command_receipt("【submit_sandbox_task 完整回执】", receipt.text)
        return self._llm_ack(receipt)

    async def _do_submit(
        self,
        *,
        prompt: str = "",
        file_paths: str = "",
        file_contents: str = "",
        output_files: str = "",
    ) -> HandlerReceipt:
        stamp = _submit_stamp()
        expected_urls = self._expected_public_urls(output_files, stamp)
        stamped_names = self._stamped_upload_names(output_files, stamp)
        prompt = _as_str(prompt) + self._upload_instruction(output_files, stamp)
        upload_token = self._resolved_upload_cfg()[2].strip()
        if upload_token:
            try:
                existing = json.loads(_as_str(file_contents)) if file_contents else []
                if not isinstance(existing, list):
                    existing = []
                has_token = any(
                    isinstance(item, dict) and item.get("target") == TOKEN_TARGET
                    for item in existing
                )
                if not has_token:
                    existing.append({
                        "target": TOKEN_TARGET,
                        "content": upload_token,
                    })
                file_contents = json.dumps(existing, ensure_ascii=False)
            except json.JSONDecodeError:
                return HandlerReceipt(
                    "提交失败: file_contents 不是有效 JSON，无法挂载上传 Token。",
                    ok=False,
                )
        sources: list[dict[str, Any]] = []
        source_count = 0
        try:
            client = self._client()
            if self._env_auto_cleanup():
                try:
                    await self._sweep_environments(
                        scope=self._env_cleanup_scope(),
                        reason="submit",
                    )
                except Exception as e:
                    logger.warning(f"提交前环境回收失败: {e}")
            sources = client.build_sources_from_files(
                _as_str(file_paths) or None,
                _as_str(file_contents) or None,
            )
            source_count = _user_source_count(sources)
            if not client.api_keys:
                return HandlerReceipt(
                    "提交失败: 未配置 Gemini API Key。请在插件设置「接入与模型」中填写。",
                    ok=False,
                    source_count=source_count,
                    expected_urls=expected_urls,
                )
            idle_keys = await self._idle_keys_for_new_task(client)
            if not idle_keys:
                return HandlerReceipt(
                    MSG_NO_CAPACITY,
                    ok=False,
                    source_count=source_count,
                    expected_urls=expected_urls,
                )
            payload = client.build_create_payload(
                prompt=_as_str(prompt),
                new_sandbox=True,
                sandbox_id=None,
                new_session=True,
                previous_task_id=None,
                sources=sources,
                background=True,
            )
            quota_hook = self._on_storage_quota if self._env_cleanup_on_quota() else None
            data, used_key = await client.create_interaction(
                payload,
                candidate_keys=idle_keys,
                on_storage_quota=quota_hook,
            )
        except GeminiSubmitTimeoutError as e:
            lines = [
                "【Antigravity 沙盒提交回执超时】",
                str(e),
                "可能已经创建任务，不能据此判断提交失败，也不要自动重复提交。",
                "由于未收到响应，本次没有 task_id 和 sandbox_id，暂时无法调用 retrieve_sandbox_task 查询。",
            ]
            if stamped_names:
                lines.append("预期上传文件名: " + ", ".join(stamped_names))
            if expected_urls:
                lines.extend(
                    [
                        "",
                        "预期公网地址（仅按文件名拼接，不代表已经上传成功）:",
                        *expected_urls,
                    ]
                )
            lines.extend(
                [
                    "",
                    "请把上述超时状态和预期公网地址原样告知用户；稍后可直接检查地址是否可访问。",
                ]
            )
            return HandlerReceipt(
                "\n".join(lines),
                ok=False,
                source_count=source_count,
                expected_urls=expected_urls,
            )
        except GeminiClientError as e:
            return HandlerReceipt(
                f"提交失败: {e}",
                ok=False,
                source_count=source_count,
                expected_urls=expected_urls,
            )
        except Exception as e:
            logger.error(f"submit_sandbox_task 未预期错误: {e}")
            return HandlerReceipt(
                f"提交失败（内部错误）: {e}",
                ok=False,
                source_count=source_count,
                expected_urls=expected_urls,
            )

        task_id = _as_str(data.get("id"))
        sandbox = _as_str(data.get("environment_id"))
        short = self._record_key(used_key or "", task_id=task_id, sandbox_id=sandbox)
        self._mark_round_outputs(
            short,
            plugin_md=False,
            chat_reply="link" if _is_md_only_outputs(output_files) else "",
            expected_urls=expected_urls,
        )
        self._schedule_auto_retrieve(short, task_id=task_id, sandbox_id=sandbox)
        status = _as_str(data.get("status")) or "unknown"
        if status in {"", "unknown"} and bool(payload.get("background")):
            status = "in_progress"
        self._set_short_status(short, status)
        self._touch_sandbox(sandbox, status=status)
        output = extract_output_text(data)
        bg = bool(payload.get("background"))
        lines = [
            "【Antigravity 沙盒任务已受理】",
            f"任务编号: {short or '(未分配)'}",
            f"task_id: {task_id or '(响应中未找到 id)'}",
            f"sandbox_id: {sandbox or '(响应中未找到 environment_id)'}",
            f"status: {status}",
            f"agent: {client.agent_label}",
            f"model: {client.model_label}",
            f"background: {bg}",
        ]
        if client.agent_warning:
            lines.append(client.agent_warning)
        if stamped_names:
            lines.append("上传文件名已加提交时间戳前缀: " + ", ".join(stamped_names))
        if sources:
            lines.append(f"已挂载 inline 文件数: {len(sources)}")
        if bg and (not output) and status in RUNNING_STATUS | {"", "unknown"}:
            lines.append("任务已提交，尚未完成，请稍后取回。")
        elif output:
            lines.append("当前 output_text:")
            lines.append(self._clip_user_text(output, keep_full=False))
        if expected_urls:
            lines.extend(["", "预期公网地址（后台任务未完成前可能暂时无法访问）:"])
            lines.extend(expected_urls)
        lines.extend(
            [
                "",
                f"后续 /agretrieve {short} 取回；/agcontinue {short} [类型...] <任务文本> 续接；"
                f"/agls {short} 查看文件；/agget {short} <完整路径> 拉取文件。",
                "请注意：不要把内部长 task_id 和 sandbox_id 发给用户，后续只说任务编号。",
                CONTINUE_GATE_HINT,
                "请根据产物类型自行判断：稍后下载并直接发送给用户，或让用户稍后访问上述地址。",
                "提交工具不轮询；需要确认状态或读取最终回执时，再调用 retrieve_sandbox_task。",
            ]
        )
        return HandlerReceipt(
            "\n".join(lines),
            task_id=task_id,
            sandbox_id=sandbox,
            status=status,
            source_count=source_count,
            output=output,
            expected_urls=expected_urls,
        )

    async def handle_continue(
        self,
        *,
        prompt: str = "",
        task_id: str = "",
        sandbox_id: str = "",
        output_files: str = "",
        file_paths: str = "",
        event: Any = None,
        **_kwargs: Any,
    ) -> str:
        plugin_md = not bool(_output_file_names(output_files))
        receipt = await self._do_continue(
            prompt=prompt,
            task_id=task_id,
            sandbox_id=sandbox_id,
            output_files=output_files,
            file_paths=file_paths,
            plugin_md=plugin_md,
            render_image=event is not None,
        )
        self._log_command_receipt("【continue_sandbox_task 完整回执】", receipt.text)
        if event is not None:
            await self._maybe_send_pre_retrieve_image(event, receipt)
        return self._llm_ack(receipt)

    async def _maybe_send_pre_retrieve_image(
        self, event: Any, receipt: HandlerReceipt
    ) -> None:
        pre = receipt.pre_receipt
        if pre is None or not pre.image_path:
            return
        if await self._send_receipt_image(event, pre.image_path):
            receipt.pre_image_sent = True

    async def _put_continue_uploads(
        self,
        client: GeminiSandboxClient,
        sandbox_id: str,
        api_key: str,
        file_paths: str,
    ) -> list[str]:
        """PUT chat attachments into a living env. Failures are notes; the caller still continues."""
        notes: list[str] = []
        for piece in str(file_paths or "").split(","):
            local = piece.strip()
            if not local:
                continue
            path = Path(local).expanduser()
            display = path.name or "upload"
            if not path.is_file() or not is_safe_local_file_path(path):
                notes.append(f"{display}: 未能写入沙盒，已继续续接")
                continue
            try:
                size = path.stat().st_size
                data = path.read_bytes()
            except OSError as e:
                logger.warning(f"读取续接附件失败 {display}: {e}")
                notes.append(f"{display}: 未能写入沙盒，已继续续接")
                continue
            if size > CHAT_PULL_MAX_BYTES:
                notes.append(f"{display}: 超过 20MB，未写入，已继续续接")
                continue
            target_name, added = ensure_md_filename(display)
            rel = f"workspace/{target_name}"
            mime = mimetypes.guess_type(target_name)[0] or (
                "text/markdown" if target_name.lower().endswith(".md") else "application/octet-stream"
            )
            try:
                await client.put_environment_file(
                    sandbox_id,
                    rel,
                    data,
                    content_type=mime,
                    api_key=api_key or None,
                )
            except Exception as e:
                logger.warning(f"续接 PUT 写入失败 {target_name}: {e}")
                notes.append(f"{target_name}: 写入失败，已继续续接")
                continue
            suffix_note = "（无后缀，已按 md 写入）" if added else ""
            public = ""
            try:
                stamped = _stamp_upload_name(target_name, _submit_stamp())
                public = await self._upload_file_bytes(data=data, filename=stamped)
            except Exception as e:
                logger.warning(f"续接附件图床地址生成失败 {target_name}: {e}")
            if public:
                notes.append(f"{public}{suffix_note}")
            else:
                notes.append(f"/workspace/{target_name}{suffix_note}")
        return notes

    async def _do_continue(
        self,
        *,
        prompt: str = "",
        task_id: str = "",
        sandbox_id: str = "",
        file_paths: str = "",
        output_files: str = "",
        plugin_md: bool = False,
        render_image: bool = False,
    ) -> HandlerReceipt:
        prompt_str = _as_str(prompt).strip()
        task_id = _as_str(task_id).strip()
        sandbox_id = _as_str(sandbox_id).strip()
        if not prompt_str:
            return HandlerReceipt("续接交互失败: prompt 不能为空。", ok=False)
        resolved = self._resolve_ids(task_id)
        overwrite_short = ""
        if resolved:
            task_id, sandbox_id, assigned_key = resolved
            task_id, sandbox_id, assigned_key, overwrite_short = self._follow_latest_on_sandbox(
                task_id, sandbox_id, assigned_key
            )
        else:
            assigned_key = self._find_key_for(task_id=task_id, sandbox_id=sandbox_id) or ""
            if sandbox_id:
                task_id, sandbox_id, assigned_key, overwrite_short = (
                    self._follow_latest_on_sandbox(task_id, sandbox_id, assigned_key)
                )
        if not task_id or not sandbox_id:
            return HandlerReceipt(
                "续接交互失败: 未找到该任务编号。请使用提交回执中的任务编号。",
                ok=False,
            )
        if ".." in task_id or ".." in sandbox_id or "\x00" in task_id + sandbox_id:
            return HandlerReceipt("续接交互失败: id 含非法路径字符。", ok=False)

        gate_short = overwrite_short or self._short_for_task(task_id)
        auto_retrieved = False
        pre_retrieve_reply = ""
        pre: HandlerReceipt | None = None
        if not self._short_retrieved_completed(gate_short):
            pre = await self._do_retrieve(
                task_id=task_id,
                sandbox_id=sandbox_id,
                cancel_auto=True,
                render_image=render_image,
            )
            if not pre.ok:
                # Prefer the retrieve receipt itself when it is already the
                # friendly query-timeout copy (covers 09 sandbox GET bug).
                if _is_retrieve_query_failure_text(pre.text or ""):
                    fail_text = pre.text
                else:
                    fail_text = f"续接交互失败: 自动取回失败。\n{pre.text}"
                return HandlerReceipt(
                    fail_text,
                    ok=False,
                    task_id=pre.task_id or task_id,
                    sandbox_id=pre.sandbox_id or sandbox_id,
                    status=pre.status,
                    pre_receipt=pre,
                )
            pre_retrieve_reply = self._command_retrieve_reply(pre)
            status = _as_str(pre.status).lower() or "unknown"
            if status != "completed":
                short_label = gate_short or self._short_for_task(pre.task_id) or "(未知)"
                lines = [
                    "【续接已中止】",
                    f"任务编号: {short_label}",
                    f"status: {pre.status or 'unknown'}",
                    "上一轮交互尚未 completed，已自动取回当前状态，未提交续接。",
                ]
                output = (pre.output or "").strip()
                if output:
                    clipped = output if len(output) <= 800 else output[:800] + "\n…(截断)"
                    lines.extend(["", "当前 output_text:", clipped])
                return HandlerReceipt(
                    "\n".join(lines),
                    ok=False,
                    task_id=pre.task_id or task_id,
                    sandbox_id=pre.sandbox_id or sandbox_id,
                    status=pre.status or "unknown",
                    output=pre.output,
                    steps=pre.steps,
                    continue_blocked=True,
                    pre_retrieve_reply=pre_retrieve_reply,
                    pre_receipt=pre,
                )
            auto_retrieved = True

        assigned_key = assigned_key or self._find_key_for(
            task_id=task_id, sandbox_id=sandbox_id
        ) or ""
        if assigned_key and self._in_progress_count(assigned_key) >= self._in_progress_limit():
            return HandlerReceipt(
                MSG_NO_CAPACITY,
                ok=False,
                task_id=task_id,
                sandbox_id=sandbox_id,
                pre_retrieve_reply=pre_retrieve_reply,
                pre_receipt=pre,
            )

        if plugin_md or not _output_file_names(output_files):
            plugin_md = True
            output_files = output_files or "result.md"
            stamp = _submit_stamp()
            expected_urls = self._expected_public_urls(output_files, stamp)
            stamped_names = self._stamped_upload_names(output_files, stamp)
            full_prompt = prompt_str
        else:
            stamp = _submit_stamp()
            expected_urls = self._expected_public_urls(output_files, stamp)
            stamped_names = self._stamped_upload_names(output_files, stamp)
            full_prompt = prompt_str + self._upload_instruction(output_files, stamp)
        put_notes: list[str] = []
        try:
            client = self._client()
            if _as_str(file_paths):
                put_notes = await self._put_continue_uploads(
                    client, sandbox_id, assigned_key, file_paths
                )
                if put_notes:
                    full_prompt += "\n\n【用户附件】已尝试写入沙盒:\n" + "\n".join(put_notes)
            # 延续会话时禁止 interaction sources。附件只走上面的 PUT。
            payload = client.build_create_payload(
                prompt=full_prompt,
                new_sandbox=False,
                sandbox_id=sandbox_id,
                new_session=False,
                previous_task_id=task_id,
                sources=None,
                background=True,
            )
            quota_hook = self._on_storage_quota if self._env_cleanup_on_quota() else None
            data, used_key = await client.create_interaction(
                payload, api_key=assigned_key, on_storage_quota=quota_hook
            )
        except GeminiSubmitTimeoutError as e:
            lines = [
                "【Antigravity 沙盒续接提交回执超时】",
                str(e),
                "可能已经在沙盒内创建后续交互，不能据此判断提交失败，也不要自动重复提交。",
            ]
            if stamped_names:
                lines.append("预期上传文件名: " + ", ".join(stamped_names))
            if expected_urls:
                lines.extend(
                    [
                        "",
                        "预期公网地址（仅按文件名拼接，不代表已经上传成功）:",
                        *expected_urls,
                    ]
                )
            return HandlerReceipt(
                "\n".join(lines),
                ok=False,
                expected_urls=expected_urls,
                pre_retrieve_reply=pre_retrieve_reply,
                pre_receipt=pre,
                put_notes=put_notes,
            )
        except GeminiClientError as e:
            return HandlerReceipt(
                f"续接交互失败: {e}",
                ok=False,
                expected_urls=expected_urls,
                pre_retrieve_reply=pre_retrieve_reply,
                pre_receipt=pre,
                put_notes=put_notes,
            )
        except Exception as e:
            logger.error(f"continue_sandbox_task 未预期错误: {e}")
            return HandlerReceipt(
                f"续接交互失败（内部错误）: {e}",
                ok=False,
                expected_urls=expected_urls,
                pre_retrieve_reply=pre_retrieve_reply,
                pre_receipt=pre,
                put_notes=put_notes,
            )

        new_task_id = _as_str(data.get("id"))
        returned_sandbox = _as_str(data.get("environment_id")) or sandbox_id
        short = self._record_key(
            used_key or assigned_key or "",
            task_id=new_task_id,
            sandbox_id=returned_sandbox,
            previous_task_id=task_id,
            overwrite_short=overwrite_short,
        )
        chat_reply = ""
        if plugin_md or _is_md_only_outputs(output_files):
            chat_reply = "link"
        self._mark_round_outputs(
            short,
            plugin_md=plugin_md,
            chat_reply=chat_reply,
            expected_urls=expected_urls,
        )
        if plugin_md and stamped_names:
            self._remember_md_plan(short, stamped_names[0])
        self._schedule_auto_retrieve(
            short, task_id=new_task_id, sandbox_id=returned_sandbox
        )
        status = _as_str(data.get("status")) or "unknown"
        if status in {"", "unknown"} and bool(payload.get("background")):
            status = "in_progress"
        self._set_short_status(short, status)
        self._touch_sandbox(returned_sandbox, status=status)
        output = extract_output_text(data)
        bg = bool(payload.get("background"))
        lines = [
            "【Antigravity 沙盒续接任务已受理】",
            f"任务编号: {short or '(未分配)'}",
            f"task_id: {new_task_id or '(响应中未找到 id)'}",
            f"previous_task_id: {task_id}",
            f"sandbox_id: {returned_sandbox}",
            f"status: {status}",
            f"agent: {client.agent_label}",
            f"model: {client.model_label}",
            f"background: {bg}",
        ]
        if client.agent_warning:
            lines.append(client.agent_warning)
        if auto_retrieved:
            lines.append("已自动取回上一轮（completed）后提交续接。")
        if stamped_names:
            lines.append("上传文件名已加提交时间戳前缀: " + ", ".join(stamped_names))
        if bg and (not output) and status in RUNNING_STATUS | {"", "unknown"}:
            lines.append("任务已提交，尚未完成，请稍后取回。")
        elif output:
            lines.append("当前 output_text:")
            lines.append(self._clip_user_text(output, keep_full=False))
        if expected_urls:
            lines.extend(["", "预期公网地址（后台任务未完成前可能暂时无法访问）:"])
            lines.extend(expected_urls)
        if put_notes:
            lines.extend(["", "续接写入:"] + put_notes)
        lines.extend(
            [
                "",
                f"后续 /agretrieve {short} 取回；/agcontinue {short} [类型...] <任务文本> 续接；"
                f"/agls {short} 查看文件；/agget {short} <完整路径> 拉取文件。",
                "请注意：不要把内部长 task_id 和 sandbox_id 发给用户，后续只说任务编号。",
                CONTINUE_GATE_HINT,
            ]
        )
        return HandlerReceipt(
            "\n".join(lines),
            task_id=new_task_id,
            sandbox_id=returned_sandbox,
            status=status,
            source_count=sum(1 for note in put_notes if "失败" not in note and "未能" not in note and "超过" not in note),
            output=output,
            expected_urls=expected_urls,
            auto_retrieved=auto_retrieved,
            pre_retrieve_reply=pre_retrieve_reply,
            pre_receipt=pre,
            put_notes=put_notes,
        )

    async def handle_retrieve(
        self,
        *,
        task_id: str = "",
        sandbox_id: str = "",
        event: Any = None,
        **_kwargs: Any,
    ) -> str:
        receipt = await self._do_retrieve(
            task_id=task_id, sandbox_id=sandbox_id, render_image=event is not None
        )
        self._log_command_receipt("【retrieve_sandbox_task 完整回执】", receipt.text)
        image_sent = False
        if receipt.ok and receipt.image_path and event is not None:
            image_sent = await self._send_receipt_image(
                event, receipt.image_path
            )
        if image_sent:
            return self._llm_retrieve_brief(receipt)
        return self._llm_retrieve_reply(receipt)

    async def _do_retrieve(
        self,
        *,
        task_id: str = "",
        sandbox_id: str = "",
        cancel_auto: bool = True,
        render_image: bool = False,
    ) -> HandlerReceipt:
        task_id = _as_str(task_id)
        sandbox_id = _as_str(sandbox_id)
        resolved = self._resolve_ids(task_id)
        short = ""
        assigned_key = ""
        if resolved:
            task_id, sandbox_id, assigned_key = resolved
            short = self._short_for_task(task_id)
        else:
            assigned_key = self._find_key_for(task_id=task_id, sandbox_id=sandbox_id) or ""
        if not task_id or not sandbox_id:
            return HandlerReceipt(
                "取回失败: 未找到该任务编号。请使用提交回执中的任务编号。",
                ok=False,
            )
        if ".." in task_id or ".." in sandbox_id or "\x00" in task_id + sandbox_id:
            return HandlerReceipt("取回失败: id 含非法路径字符。", ok=False)
        assigned_key = assigned_key or self._find_key_for(
            task_id=task_id, sandbox_id=sandbox_id
        )
        client = self._client()
        try:
            data = await self._get_interaction_bound(
                client, task_id, assigned_key=assigned_key
            )
        except GeminiRetrieveQueryError as e:
            fail_short = short or self._short_for_task(task_id)
            logger.warning(f"retrieve GET 查询超时/网络错误: {e}")
            return HandlerReceipt(
                _retrieve_query_failure_message(fail_short),
                ok=False,
                task_id=task_id,
                sandbox_id=sandbox_id,
            )
        except GeminiClientError as e:
            msg = str(e).strip()
            if _is_retrieve_query_failure_text(msg) or not msg or msg in (
                "查询任务网络错误:",
                "查询任务失败:",
            ):
                fail_short = short or self._short_for_task(task_id)
                logger.warning(f"retrieve GET 映射为查询失败回执: {e}")
                return HandlerReceipt(
                    _retrieve_query_failure_message(fail_short),
                    ok=False,
                    task_id=task_id,
                    sandbox_id=sandbox_id,
                )
            return HandlerReceipt(
                f"查询任务失败: {e}",
                ok=False,
                task_id=task_id,
                sandbox_id=sandbox_id,
            )
        except Exception as e:
            logger.error(f"retrieve GET 未预期错误: {e}")
            err = str(e).strip().lower()
            if (
                "timeout" in err
                or "deadline" in err
                or "504" in err
                or not str(e).strip()
            ):
                fail_short = short or self._short_for_task(task_id)
                return HandlerReceipt(
                    _retrieve_query_failure_message(fail_short),
                    ok=False,
                    task_id=task_id,
                    sandbox_id=sandbox_id,
                )
            return HandlerReceipt(
                f"查询任务失败（内部错误）: {e}",
                ok=False,
                task_id=task_id,
                sandbox_id=sandbox_id,
            )
        status = _as_str(data.get("status")) or "unknown"
        if not short:
            short = self._short_for_task(task_id)
        latest = self._latest_short_for_sandbox(sandbox_id)
        if short and latest and short != latest:
            self._touch_sandbox(sandbox_id)
        else:
            self._touch_sandbox(sandbox_id, status=status)
        if cancel_auto and short:
            self._cancel_auto_retrieve(short)
            self._mark_short_retrieved(short, status)
        output = extract_output_text(data)
        steps = summarize_steps(data)
        expected_urls: list[str] = []
        if short:
            try:
                expected_urls = await self._maybe_plugin_upload_md(short, output, status)
            except Exception as e:
                logger.warning(f"取回后插件上传 md 失败: {e}")
        lines = [
            "【Antigravity 沙盒任务回执】",
            f"task_id: {task_id}",
            f"sandbox_id: {sandbox_id}",
            f"status: {status}",
            "",
            "—— 回执文本（output_text）——",
            output.strip() if output.strip() else "(尚无 output_text)",
            "",
            "—— steps 摘要 ——",
            steps,
        ]
        if expected_urls:
            lines.extend(["", "产物链接:"] + expected_urls)
        usage = data.get("usage")
        if isinstance(usage, dict) and usage:
            lines.extend(["", "usage: " + json.dumps(usage, ensure_ascii=False)[:800]])
        receipt = HandlerReceipt(
            "\n".join(lines),
            task_id=task_id,
            sandbox_id=sandbox_id,
            status=status,
            output=output,
            steps=steps,
            expected_urls=expected_urls,
        )
        if render_image and receipt_image_applies(
            self._image_receipt_enabled(),
            status,
            output,
            self._image_receipt_min_length(),
        ):
            await self._attach_receipt_image(receipt)
        return receipt

    async def _get_interaction_bound(
        self,
        client: GeminiSandboxClient,
        task_id: str,
        *,
        assigned_key: str | None,
    ) -> dict[str, Any]:
        if assigned_key:
            return await client.get_interaction(task_id, api_key=assigned_key)
        last_error: Exception | None = None
        for key in client.api_keys:
            try:
                return await client.get_interaction(task_id, api_key=key)
            except GeminiClientError as e:
                last_error = e
                continue
        if last_error is not None:
            raise last_error
        raise GeminiClientError("没有可用的 Gemini API Key。")

    @filter.command("agsubmit", alias={"ags"})
    async def agsubmit(self, event: AstrMessageEvent, prompt: GreedyStr):
        """提交 Antigravity 沙盒任务。默认产出 md；类型写在任务文本前，如 png 或 svg png。"""
        prompt_text, output_files = _parse_command_prompt(str(prompt), default_ext="md")
        file_list = await self._collect_event_file_paths(event)
        if not prompt_text and not file_list:
            yield event.plain_result("用法: /agsubmit [类型...] <任务文本>")
            return
        if not prompt_text:
            prompt_text = "请查看已挂载到 /workspace 的用户附件并完成相应处理。"
        elif file_list:
            names = ", ".join(f"/workspace/{Path(p).name}" for p in file_list)
            prompt_text = prompt_text + f"\n\n【用户附件】已挂载到沙盒: {names}"
        receipt = await self._do_submit(
            prompt=prompt_text,
            output_files=output_files,
            file_paths=",".join(file_list),
        )
        self._log_command_receipt("【agsubmit 完整回执】", receipt.text)
        yield event.plain_result(self._command_ack(receipt))

    @filter.command("agretrieve", alias={"agr"})
    async def agretrieve(self, event: AstrMessageEvent, task_ref: str = ""):
        """取回沙盒任务: /agretrieve <任务编号>"""
        task_ref = str(task_ref).strip()
        if not task_ref:
            yield event.plain_result("用法: /agretrieve <任务编号>")
            return
        resolved = self._resolve_ids(task_ref)
        if not resolved:
            yield event.plain_result(
                "未找到该任务编号。请使用提交回执中的任务编号，例如 /agretrieve 0001"
            )
            return
        task_id, sandbox_id, _assigned_key = resolved
        receipt = await self._do_retrieve(
            task_id=task_id, sandbox_id=sandbox_id, render_image=True
        )
        self._log_command_receipt("【agretrieve 完整回执】", receipt.text)
        if receipt.image_path and await self._send_receipt_image(
            event,
            receipt.image_path,
            extra_urls=self._receipt_link_urls(receipt),
        ):
            return
        yield event.plain_result(self._command_retrieve_reply(receipt))

    @filter.command("agcontinue", alias={"agc"})
    async def agcontinue(self, event: AstrMessageEvent, rest: GreedyStr):
        """在已有沙盒会话中续接任务: /agcontinue <任务编号> [类型...] <任务文本>"""
        task_ref, prompt_raw = _split_continue_rest(str(rest))
        prompt_text, output_files = _parse_command_prompt(
            prompt_raw, default_ext=None
        )
        plugin_md = not bool(_output_file_names(output_files))
        prompt_preview = prompt_text if len(prompt_text) <= 500 else prompt_text[:500] + "…"
        logger.info(
            f"agcontinue 解析 task_ref={task_ref} plugin_md={plugin_md} "
            f"output_files={output_files or '-'} prompt={prompt_preview}"
        )
        if not task_ref or not prompt_text:
            yield event.plain_result("用法: /agcontinue <任务编号> [类型...] <任务文本>")
            return
        resolved = self._resolve_ids(task_ref)
        if not resolved:
            yield event.plain_result(
                "未找到该任务编号。请使用提交回执中的任务编号，例如 /agcontinue 0001 继续查"
            )
            return
        task_id, sandbox_id, _assigned_key = resolved
        file_list = await self._collect_event_file_paths(event)
        receipt = await self._do_continue(
            prompt=prompt_text,
            task_id=task_id,
            sandbox_id=sandbox_id,
            output_files=output_files,
            file_paths=",".join(file_list),
            plugin_md=plugin_md,
            render_image=True,
        )
        self._log_command_receipt("【agcontinue 完整回执】", receipt.text)
        pre = receipt.pre_receipt
        if pre is not None and pre.image_path:
            receipt.pre_image_sent = await self._send_receipt_image(
                event, pre.image_path, extra_urls=self._receipt_link_urls(pre)
            )
        if not receipt.pre_image_sent and receipt.pre_retrieve_reply:
            yield event.plain_result(receipt.pre_retrieve_reply)
        yield event.plain_result(self._command_ack(receipt))

    @filter.command("aghelp")
    async def aghelp(self, event: AstrMessageEvent):
        """查看 Antigravity 沙盒指令说明。"""
        yield event.plain_result(AGHELP_TEXT)

    @filter.command("agls")
    async def agls(self, event: AstrMessageEvent, task_ref: str = ""):
        """列出短号沙盒 workspace 文件，渲染成表格图片发送。"""
        task_ref = str(task_ref).strip()
        if not task_ref:
            yield event.plain_result("用法: /agls <任务编号>")
            return
        table_text = await self._list_files_table_text(task_ref)
        image_path = await self._render_receipt_image(table_text) if table_text else ""
        if image_path:
            try:
                await event.send(event.make_result().file_image(image_path))
                return
            except Exception as e:
                logger.warning(f"发送文件列表图片失败，回退文本: {e}")
        yield event.plain_result(table_text or await self.handle_list_files(task_ref))

    @filter.command("agget")
    async def agget(self, event: AstrMessageEvent, rest: GreedyStr):
        """按完整路径拉取沙盒文件并发到聊天。不与 WebUI 下载共用并发限制。

        用法 /agget <任务编号> <完整路径>，例如
        /agget 0003 workspace/dist/index.html。只有一个沙盒时可省略编号，
        直接 /agget workspace/index.html。早期写法 <短号>:<路径> 仍兼容。
        """
        raw = str(rest).strip()
        if not raw:
            yield event.plain_result(
                "用法: /agget <任务编号> <完整路径>，例如 /agget 0003 workspace/a.md"
            )
            return
        task_ref = ""
        name = raw
        if _DRIVE_PREFIX_RE.fullmatch(raw[:2]) or raw.startswith(("/", "\\")):
            # 绝对路径（含 Windows 盘符），整体当路径，不拆任务编号
            name = raw
        elif ":" in raw:
            # 兼容早期写法：<短号>:<路径>
            head, _, tail = raw.partition(":")
            head = head.strip()
            tail = tail.strip().lstrip("/")
            if head.isdigit() and tail:
                task_ref = head
                name = tail
        elif " " in raw:
            # 主写法：<任务编号> <完整路径>，路径本身可含空格，只在首个空格切
            head, _, tail = raw.partition(" ")
            if head.strip().isdigit() and tail.strip():
                task_ref = head.strip()
                name = tail.strip()
        if not name:
            yield event.plain_result(
                "用法: /agget <任务编号> <完整路径>，例如 /agget 0003 workspace/a.md"
            )
            return
        short = task_ref or self._single_latest_short_for_sandbox()
        text, path, display = await self._pull_chat_file(short, name)
        if path is not None:
            if hasattr(event, "track_temporary_local_file"):
                event.track_temporary_local_file(str(path))
            yield event.chain_result(
                [
                    Comp.Plain(text),
                    Comp.File(name=display or path.name, file=str(path)),
                ]
            )
            return
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("agenvlist", alias={"agels"})
    async def agenvlist(self, event: AstrMessageEvent):
        """列出当前 Gemini 项目中的沙盒环境占用。管理员指令。"""
        client = self._client()
        if not client.api_keys:
            yield event.plain_result("未配置 Gemini API Key。")
            return
        lines = ["沙盒环境占用："]
        for index, key in enumerate(client.api_keys):
            try:
                envs = await client.list_environments(api_key=key)
            except Exception as e:
                lines.append(f"Key #{index + 1}: 列出失败: {e}")
                continue
            total = sum(environment_size_bytes(env) for env in envs)
            lines.append(
                f"Key #{index + 1}: {len(envs)} 个环境，合计 {_fmt_bytes(total)}"
            )
            ranked = sorted(envs, key=environment_size_bytes, reverse=True)
            for env in ranked[:8]:
                eid = environment_id_of(env)
                last = _as_str(env.get("last_accessed") or env.get("lastAccessed")) or "-"
                files = env.get("file_count")
                if files is None:
                    files = env.get("fileCount")
                label = self._latest_short_for_sandbox(eid) or (
                    f"{eid[:8]}…" if eid else "(无 id)"
                )
                lines.append(
                    f"  {label} size={_fmt_bytes(environment_size_bytes(env))}"
                    f" files={files if files is not None else '-'} last={last}"
                )
            if len(ranked) > 8:
                lines.append(f"  ... 另有 {len(ranked) - 8} 个")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("agenvcleanup", alias={"agecl"})
    async def agenvcleanup(self, event: AstrMessageEvent, target: str = ""):
        """回收沙盒。默认 tracked；all 扫整个项目闲置环境；短号立即删除指定沙盒。管理员指令。"""
        raw = _as_str(target).strip()
        raw_l = raw.lower()
        if raw_l in {"", "all", "tracked"}:
            chosen = "all" if raw_l == "all" else (raw_l or self._env_cleanup_scope())
            try:
                result = await self._sweep_environments(scope=chosen, reason="command")
            except Exception as e:
                yield event.plain_result(f"环境回收失败: {e}")
                return
            yield event.plain_result(self._format_sweep_result(scope=chosen, result=result))
            return
        try:
            yield event.plain_result(await self._delete_sandbox_target(raw))
        except Exception as e:
            yield event.plain_result(f"删除沙盒失败: {e}")


    def _files_failure_text(self, exc: Exception, *, listing: bool) -> str:
        if isinstance(exc, (asyncio.TimeoutError, GeminiFileTimeoutError)):
            return MSG_PULL_TIMEOUT
        if isinstance(exc, GeminiFileTooLargeError):
            return MSG_FILE_TOO_LARGE
        status = getattr(exc, "status_code", None)
        kind = files_error_kind(status if isinstance(status, int) else None, str(exc), listing=listing)
        fixed = fixed_files_message(kind)
        return fixed or (str(exc) or "操作失败。")

    def _resolve_file_target(self, task_ref: str) -> tuple[str, str, str, str]:
        found = self._resolve_index_entry(task_ref)
        if not found:
            raise ValueError("未找到该任务编号。请使用提交回执中的任务编号。")
        short, item = found
        sandbox_id = _as_str(item.get("sandbox_id"))
        task_id = _as_str(item.get("task_id"))
        key = self._bound_key_of(item, task_id=task_id, sandbox_id=sandbox_id)
        if not sandbox_id or not key:
            raise ValueError("未找到该任务绑定的沙盒或 API Key。")
        return short, task_id, sandbox_id, key

    def _match_listed_file(self, files: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
        want = _as_str(name).replace("\\", "/").lstrip("/")
        if not want:
            return None
        rel = workspace_download_path(want)
        leaf = PurePosixPath(rel).name
        exact: list[dict[str, Any]] = []
        by_name: list[dict[str, Any]] = []
        for item in files:
            slim = slim_environment_file_entry(item)
            path = _as_str(slim.get("path")).replace("\\", "/").lstrip("/")
            fname = _as_str(slim.get("name"))
            if path == rel or path == want:
                exact.append(slim)
            elif fname == leaf or fname == want or path.endswith("/" + want):
                by_name.append(slim)
        if exact:
            return exact[0]
        if len(by_name) == 1:
            return by_name[0]
        return None

    async def handle_list_files(self, task_ref: str) -> str:
        try:
            return await asyncio.wait_for(
                self._list_files_inner(task_ref),
                CHAT_PULL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return MSG_PULL_TIMEOUT

    async def _list_files_table_text(self, task_ref: str) -> str:
        """把 workspace 文件列表整理成 markdown 表格，供 /agls 渲染成图片。

        渲染失败（模板不可用、图片空白等）时返回空串，由调用方回退原文本列表。
        内部错误仍返回固定文案，保证用户在两种形式下看到一致的错误信息。
        """
        try:
            short, _task_id, sandbox_id, key = self._resolve_file_target(task_ref)
        except ValueError as e:
            return str(e)
        client = self._client()
        try:
            files = await client.list_environment_files(
                sandbox_id,
                "workspace",
                recursive=True,
                api_key=key,
            )
        except GeminiClientError as e:
            return self._files_failure_text(e, listing=True)
        except asyncio.TimeoutError:
            return MSG_PULL_TIMEOUT
        except Exception as e:
            logger.warning(f"列出沙盒文件失败: {e}")
            return self._files_failure_text(e, listing=True)
        slim = [slim_environment_file_entry(item) for item in files]
        if not slim:
            return MSG_LIST_EMPTY
        dirs = [f for f in slim if f["type"] == "directory"]
        regular = [f for f in slim if f["type"] != "directory"]
        # 目录在前、文件在后，各按大小降序，长列表也不用翻到底部找大文件
        dirs.sort(key=lambda f: (f["path"] or "").lower())
        regular.sort(key=lambda f: int(f["size_bytes"] or 0), reverse=True)
        rows = dirs + regular
        lines = [
            f"沙盒 `{short}` workspace 文件",
            "",
            "| 名称 | 路径 | 类型 | 大小 |",
            "| --- | --- | --- | --- |",
        ]
        for item in rows:
            name = (item["name"] or "").replace("|", "\\|") or "-"
            path = (item["path"] or "").replace("|", "\\|") or "-"
            ftype = "目录" if item["type"] == "directory" else "文件"
            size = "-" if item["type"] == "directory" else _fmt_bytes(item["size_bytes"])
            lines.append(f"| {name} | {path} | {ftype} | {size} |")
        lines.append("")
        lines.append(f"共 {len(dirs)} 个目录、{len(regular)} 个文件。")
        return "\n".join(lines)

    async def _list_files_inner(self, task_ref: str) -> str:
        try:
            short, _task_id, sandbox_id, key = self._resolve_file_target(task_ref)
        except ValueError as e:
            return str(e)
        client = self._client()
        try:
            files = await client.list_environment_files(
                sandbox_id,
                "workspace",
                recursive=True,
                api_key=key,
            )
        except GeminiClientError as e:
            return self._files_failure_text(e, listing=True)
        slim = [slim_environment_file_entry(item) for item in files]
        if not slim:
            return MSG_LIST_EMPTY
        lines = [f"任务编号: {short}", "workspace:"]
        for item in slim:
            lines.append(
                f"{item['name']}\t{item['path']}\t{item['type']}\t{item['size_bytes']}"
            )
        return "\n".join(lines)

    def _chat_pull_dest(self, name: str) -> Path:
        directory = self._data_dir / "chat_pull"
        directory.mkdir(parents=True, exist_ok=True)
        suffix = Path(name).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9.]{1,16}", suffix or ""):
            suffix = ""
        return directory / f"{uuid.uuid4().hex}{suffix}"

    async def _pull_chat_file(
        self, task_ref: str, name: str
    ) -> tuple[str, Path | None, str]:
        """Chat/LLM pull. Does not take the WebUI download semaphore."""
        try:
            return await asyncio.wait_for(
                self._pull_chat_file_inner(task_ref, name),
                CHAT_PULL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return MSG_PULL_TIMEOUT, None, ""

    async def _pull_chat_file_inner(
        self, task_ref: str, name: str
    ) -> tuple[str, Path | None, str]:
        try:
            short, _task_id, sandbox_id, key = self._resolve_file_target(task_ref)
        except ValueError as e:
            return str(e), None, ""
        client = self._client()
        try:
            files = await client.list_environment_files(
                sandbox_id,
                "workspace",
                recursive=True,
                api_key=key,
            )
        except GeminiClientError as e:
            return self._files_failure_text(e, listing=True), None, ""
        if not files:
            return MSG_LIST_EMPTY, None, ""
        match = self._match_listed_file(files, name)
        if not match or _as_str(match.get("type")) == "directory":
            return MSG_FILE_MISSING, None, ""
        size = int(match.get("size_bytes") or 0)
        if size > CHAT_PULL_MAX_BYTES:
            return MSG_FILE_TOO_LARGE, None, ""
        rel = workspace_download_path(_as_str(match.get("path")) or _as_str(match.get("name")))
        try:
            remote_size = await client.head_environment_file_size(
                sandbox_id,
                rel,
                api_key=key,
                timeout=CHAT_PULL_TIMEOUT_SECONDS,
            )
        except GeminiClientError as e:
            kind = files_error_kind(getattr(e, "status_code", None), str(e), listing=False)
            if kind == "timeout":
                return MSG_PULL_TIMEOUT, None, ""
            if kind in {"key", "env"}:
                return fixed_files_message(kind), None, ""
            remote_size = None
        if remote_size is not None and remote_size > CHAT_PULL_MAX_BYTES:
            return MSG_FILE_TOO_LARGE, None, ""
        display = _as_str(match.get("name")) or PurePosixPath(rel).name or "download.bin"
        dest = self._chat_pull_dest(display)
        try:
            with dest.open("wb") as out:
                async for chunk in client.iter_environment_file(
                    sandbox_id,
                    rel,
                    api_key=key,
                    max_bytes=CHAT_PULL_MAX_BYTES,
                    timeout=CHAT_PULL_TIMEOUT_SECONDS,
                ):
                    out.write(chunk)
        except (GeminiFileTooLargeError, GeminiFileTimeoutError, GeminiClientError) as e:
            dest.unlink(missing_ok=True)
            return self._files_failure_text(e, listing=False), None, ""
        except Exception as e:
            dest.unlink(missing_ok=True)
            logger.warning(f"拉取沙盒文件失败: {e}")
            return self._files_failure_text(e, listing=False), None, ""
        return f"任务编号: {short}\n已拉取 {display}", dest, display

    async def handle_get_file(
        self,
        *,
        task_id: str = "",
        name: str = "",
        event: Any = None,
        **_kwargs: Any,
    ) -> str:
        text, path, display = await self._pull_chat_file(task_id, name)
        if path is None or event is None:
            return text
        try:
            if hasattr(event, "track_temporary_local_file"):
                event.track_temporary_local_file(str(path))
            chain = event.chain_result(
                [
                    Comp.Plain(text),
                    Comp.File(name=display or path.name, file=str(path)),
                ]
            )
            await event.send(chain)
        except Exception as e:
            logger.warning(f"发送沙盒文件失败: {e}")
            return text + "\n文件已下载，但发送到聊天失败。"
        return (
            f"已把 {display} 发给用户。"
            "沙盒网络存疑，不一定能成功拉取文件；超过 20MB 或超时请改用图床或 WebUI。"
        )

    async def handle_list_sandbox_task(self, *, task_id: str = "", **_kwargs: Any) -> str:
        return await self.handle_list_files(task_id)

    def _register_sandbox_files_web_api(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.info("当前 AstrBot 无 register_web_api，跳过沙盒文件 WebUI API")
            return
        try:
            register(
                f"/{PLUGIN_NAME}/sandbox-files/tasks",
                self._web_sandbox_files_tasks,
                ["GET"],
                "List sandbox tasks for file desk",
            )
            register(
                f"/{PLUGIN_NAME}/sandbox-files/files",
                self._web_sandbox_files_list,
                ["GET"],
                "List environment files",
            )
            register(
                f"/{PLUGIN_NAME}/sandbox-files/download",
                self._web_sandbox_files_download,
                ["GET"],
                "Download environment file",
            )
            register(
                f"/{PLUGIN_NAME}/sandbox-files/upload/<sandbox_id>",
                self._web_sandbox_files_upload,
                ["POST"],
                "Upload environment file",
            )
            register(
                f"/{PLUGIN_NAME}/sandbox-files/delete",
                self._web_sandbox_files_delete,
                ["POST"],
                "Delete sandbox from file desk",
            )
            register(
                f"/{PLUGIN_NAME}/sandbox-files/pull",
                self._web_sandbox_files_pull_start,
                ["POST"],
                "Start a cancellable sandbox file pull",
            )
            register(
                f"/{PLUGIN_NAME}/sandbox-files/pull/events",
                self._web_sandbox_files_pull_events,
                ["GET"],
                "SSE progress for a sandbox file pull",
            )
            register(
                f"/{PLUGIN_NAME}/sandbox-files/pull/cancel",
                self._web_sandbox_files_pull_cancel,
                ["POST"],
                "Cancel a sandbox file pull",
            )
            register(
                f"/{PLUGIN_NAME}/sandbox-files/pull/file",
                self._web_sandbox_files_pull_file,
                ["GET"],
                "Download a finished sandbox file pull",
            )
            logger.info("已注册沙盒文件 WebUI API（sandbox-files）")
        except Exception as e:
            logger.warning(f"注册沙盒文件 WebUI API 失败: {e}")

    def _web_task_status_of(self, *, sandbox_id: str, item: dict[str, str] | None = None) -> str:
        item = item or {}
        status = _as_str(item.get("last_status")).lower()
        if not status:
            status = _as_str((self._env_meta.get(sandbox_id) or {}).get("status")).lower()
        if status in RUNNING_STATUS:
            return "running"
        if status in {"completed", "failed", "cancelled", "incomplete", "budget_exceeded", "requires_action"}:
            return status
        return status or "idle"

    def _web_collect_local_tasks(self) -> list[dict[str, Any]]:
        """Build task rows from short_index, one row per sandbox (latest short)."""
        best: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        fallback = datetime.min.replace(tzinfo=_now().tzinfo)
        for short, item in self._short_index.items():
            if not isinstance(item, dict):
                continue
            sandbox_id = _as_str(item.get("sandbox_id"))
            if not sandbox_id:
                continue
            ts = _parse_iso(_as_str(item.get("recorded_at")))
            matched = CONTINUE_SHORT_RE.fullmatch(short)
            if matched:
                n = int(matched.group(1)) * (10**CONTINUE_SHORT_TIME_WIDTH) + int(
                    matched.group(2)
                )
            else:
                parsed = _parse_short_int(short)
                n = (parsed * (10**CONTINUE_SHORT_TIME_WIDTH)) if parsed is not None else -1
            rank = (1 if ts is not None else 0, ts or fallback, n)
            status = self._web_task_status_of(sandbox_id=sandbox_id, item=item)
            row = {
                "short": short,
                "sandbox_id": sandbox_id,
                "status": status,
                "label": short or f"{sandbox_id[:8]}…",
                "recorded_at": _as_str(item.get("recorded_at")),
                "task_id": _as_str(item.get("task_id")),
                "source": "local",
            }
            prev = best.get(sandbox_id)
            if prev is None or rank > prev[0]:
                best[sandbox_id] = (rank, row)
        rows = [row for _, row in best.values()]
        rows.sort(key=lambda r: _as_str(r.get("recorded_at")), reverse=True)
        return rows

    async def _web_merge_remote_tasks(self, local_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        known = {_as_str(r.get("sandbox_id")) for r in local_rows if _as_str(r.get("sandbox_id"))}
        client = self._client()
        if not client.api_keys:
            return local_rows
        remote_rows: list[dict[str, Any]] = []
        for key in client.api_keys:
            try:
                envs = await client.list_environments(api_key=key)
            except Exception as e:
                logger.warning(f"沙盒文件页合并远端环境失败: {e}")
                continue
            for env in envs:
                if not isinstance(env, dict):
                    continue
                eid = environment_id_of(env)
                if not eid or eid in known:
                    continue
                known.add(eid)
                meta = self._env_meta.get(eid) or {}
                status = self._web_task_status_of(sandbox_id=eid, item={"last_status": _as_str(meta.get("status"))})
                short = self._latest_short_for_sandbox(eid)
                remote_rows.append(
                    {
                        "short": short,
                        "sandbox_id": eid,
                        "status": status,
                        "label": short or f"{eid[:8]}…",
                        "recorded_at": _as_str(meta.get("last_used_at")),
                        "task_id": "",
                        "source": "remote",
                    }
                )
        return local_rows + remote_rows

    def _web_resolve_sandbox_key(
        self, *, sandbox_id: str = "", ref: str = ""
    ) -> tuple[str, str, str]:
        """Return (sandbox_id, short_or_label, api_key). Never expose key to callers of JSON."""
        sandbox_id = _as_str(sandbox_id)
        ref = _as_str(ref)
        short = ""
        key = ""
        if ref:
            found = self._resolve_index_entry(ref)
            if found:
                short, item = found
                sandbox_id = sandbox_id or _as_str(item.get("sandbox_id"))
                key = self._bound_key_of(item, sandbox_id=sandbox_id)
            elif SANDBOX_ID_RE.fullmatch(ref):
                sandbox_id = sandbox_id or ref.lower()
        if sandbox_id and SANDBOX_ID_RE.fullmatch(sandbox_id):
            sandbox_id = sandbox_id.lower()
        elif sandbox_id:
            # allow non-hex ids from API but still sanitize
            sandbox_id = sandbox_id.strip()
        if not sandbox_id:
            raise ValueError("缺少 sandbox_id")
        if not short:
            short = self._latest_short_for_sandbox(sandbox_id)
        if not key:
            key = self._find_key_for(sandbox_id=sandbox_id) or ""
        if not key:
            client = self._client()
            key = client.api_keys[0] if client.api_keys else ""
        if not key:
            raise ValueError("未配置 Gemini API Key")
        return sandbox_id, short, key

    async def _web_sandbox_files_tasks(self):
        try:
            from astrbot.api.web import error_response, json_response, request
        except ImportError:
            return {"status": "error", "message": "当前 AstrBot 不支持 Plugin Web API"}
        try:
            local_rows = self._web_collect_local_tasks()
            # 默认只返回本地短号索引，避免全量 list_environments 拖垮/刷屏。
            # 需要远端补全时传 include_remote=1。
            include_remote = False
            try:
                raw = _as_str(request.query.get("include_remote", "0")).lower()
                include_remote = raw in {"1", "true", "yes", "on"}
            except Exception:
                include_remote = False
            rows = (
                await self._web_merge_remote_tasks(local_rows)
                if include_remote
                else local_rows
            )
            # Strip any accidental key fields
            safe = []
            for row in rows:
                safe.append(
                    {
                        "short": _as_str(row.get("short")),
                        "sandbox_id": _as_str(row.get("sandbox_id")),
                        "status": _as_str(row.get("status")) or "idle",
                        "label": _as_str(row.get("label"))
                        or _as_str(row.get("short"))
                        or f"{_as_str(row.get('sandbox_id'))[:8]}…",
                        "recorded_at": _as_str(row.get("recorded_at")),
                        "source": _as_str(row.get("source")) or "local",
                    }
                )
            return json_response({"tasks": safe})
        except Exception as e:
            logger.warning(f"sandbox-files/tasks 失败: {e}")
            return error_response(str(e) or "列出任务失败")

    async def _web_sandbox_files_list(self):
        try:
            from astrbot.api.web import error_response, json_response, request
        except ImportError:
            return {"status": "error", "message": "当前 AstrBot 不支持 Plugin Web API"}
        try:
            sandbox_id = _as_str(request.query.get("sandbox_id", ""))
            ref = _as_str(request.query.get("ref", ""))
            raw_path = request.query.get("path")
            if raw_path is None:
                path = "workspace"
            else:
                path = _as_str(raw_path).strip()
            recursive_raw = _as_str(request.query.get("recursive", "1")).lower()
            recursive = recursive_raw not in {"0", "false", "no", "off"}
            sid, short, key = self._web_resolve_sandbox_key(sandbox_id=sandbox_id, ref=ref)
            client = self._client()
            norm_path = normalize_environment_file_path(path)
            try:
                files = await client.list_environment_files(
                    sid, norm_path, recursive=recursive, api_key=key
                )
            except GeminiClientError as e:
                err_text = str(e).lower()
                is_not_found = (
                    "404" in err_text
                    or "not_found" in err_text
                    or "not found" in err_text
                    or "不存在" in err_text
                )
                if norm_path == "workspace" and is_not_found:
                    logger.info(f"沙盒 {sid} 路径 workspace 不存在(404)，自动回退列出根目录")
                    norm_path = ""
                    files = await client.list_environment_files(
                        sid, norm_path, recursive=recursive, api_key=key
                    )
                else:
                    raise
            return json_response(
                {
                    "sandbox_id": sid,
                    "short": short,
                    "path": norm_path,
                    "files": files,
                }
            )
        except ValueError as e:
            return error_response(str(e))
        except GeminiClientError as e:
            return error_response(str(e))
        except Exception as e:
            logger.warning(f"sandbox-files/files 失败: {e}")
            return error_response(str(e) or "列出文件失败")

    async def _web_sandbox_files_download(self):
        try:
            from urllib.parse import quote

            from astrbot.api.web import error_response, file_response, request
            try:
                from astrbot.api.web import stream_response
            except ImportError:
                stream_response = None
        except ImportError:
            return {"status": "error", "message": "当前 AstrBot 不支持 Plugin Web API"}
        tmp_path: Path | None = None
        try:
            sandbox_id = _as_str(request.query.get("sandbox_id", ""))
            ref = _as_str(request.query.get("ref", ""))
            path = _as_str(request.query.get("path", ""))
            if not path:
                return error_response("缺少 path")
            sid, _short, key = self._web_resolve_sandbox_key(sandbox_id=sandbox_id, ref=ref)
            rel = normalize_environment_file_path(path)
            filename = PurePosixPath(rel).name or "download.bin"
            mime, _ = mimetypes.guess_type(filename)
            content_type = mime or "application/octet-stream"
            client = self._client()

            # 优先使用 stream_response 流式传输，不占本地磁盘
            if callable(stream_response):
                encoded_name = quote(filename)
                headers = {
                    "Content-Disposition": f'attachment; filename="{encoded_name}"; filename*=utf-8\'\'{encoded_name}',
                }
                return stream_response(
                    client.iter_environment_file(sid, rel, api_key=key),
                    content_type=content_type,
                    headers=headers,
                )

            # 兼容旧环境：落盘临时文件并通过后台任务清理
            suffix = Path(filename).suffix
            fd, tmp_name = tempfile.mkstemp(prefix="ag_sandbox_", suffix=suffix or ".bin")
            os.close(fd)
            tmp_path = Path(tmp_name)
            await client.download_environment_file_to(sid, rel, tmp_path, api_key=key)
            resp = file_response(tmp_path, filename=filename, content_type=content_type)
            try:
                from starlette.background import BackgroundTask

                resp.background = BackgroundTask(tmp_path.unlink, missing_ok=True)
            except Exception:
                pass
            return resp
        except ValueError as e:
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            return error_response(str(e))
        except GeminiClientError as e:
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            return error_response(str(e))
        except Exception as e:
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            logger.warning(f"sandbox-files/download 失败: {e}")
            return error_response(str(e) or "下载失败")

    async def _web_sandbox_files_upload(self, sandbox_id: str = ""):
        try:
            from astrbot.api.web import error_response, json_response, request
        except ImportError:
            return {"status": "error", "message": "当前 AstrBot 不支持 Plugin Web API"}
        try:
            sid = _as_str(sandbox_id) or _as_str(request.query.get("sandbox_id", ""))
            raw_dest = request.query.get("path")
            files = await request.files()
            upload = files.get("file") if files else None
            if upload is None:
                return error_response("缺少上传文件（字段名 file）")
            filename = _as_str(getattr(upload, "filename", "") or "upload.bin")
            if raw_dest is None:
                dest = f"workspace/{filename}"
            else:
                clean = _as_str(raw_dest).strip().replace("\\", "/").rstrip("/")
                if not clean:
                    dest = filename
                elif clean in {"workspace", "."}:
                    dest = f"workspace/{filename}"
                else:
                    is_dir = _as_str(raw_dest).strip().replace("\\", "/").endswith("/")
                    dest = normalize_environment_file_path(clean)
                    if is_dir:
                        dest = f"{dest.rstrip('/')}/{filename}"
            sid, short, key = self._web_resolve_sandbox_key(sandbox_id=sid)
            content = await upload.read()
            content_type = _as_str(getattr(upload, "content_type", "") or "") or (
                mimetypes.guess_type(filename)[0] or "application/octet-stream"
            )
            # Soft size guard (bridge timeout ~60s); still allow reasonably large files.
            max_bytes = 100 * 1024 * 1024
            if len(content) > max_bytes:
                return error_response(f"文件过大（>{max_bytes // (1024 * 1024)}MB）")
            client = self._client()
            meta = await client.upload_environment_file(
                sid,
                dest,
                content,
                content_type=content_type,
                overwrite=True,
                api_key=key,
            )
            return json_response(
                {
                    "ok": True,
                    "sandbox_id": sid,
                    "short": short,
                    "path": dest,
                    "file": meta,
                }
            )
        except ValueError as e:
            return error_response(str(e))
        except GeminiClientError as e:
            return error_response(str(e))
        except Exception as e:
            logger.warning(f"sandbox-files/upload 失败: {e}")
            return error_response(str(e) or "上传失败")

    async def _web_sandbox_files_delete(self):
        try:
            from astrbot.api.web import error_response, json_response, request
        except ImportError:
            return {"status": "error", "message": "当前 AstrBot 不支持 Plugin Web API"}
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                payload = {}
            ref = _as_str(payload.get("ref") or request.query.get("ref", ""))
            if not ref:
                return error_response("缺少 ref（短号或 sandbox_id）")
            message = await self._delete_sandbox_target(ref)
            ok = not message.startswith("删除失败") and not message.startswith("未找到") and not message.startswith("未配置")
            return json_response({"ok": ok, "message": message})
        except Exception as e:
            logger.warning(f"sandbox-files/delete 失败: {e}")
            return error_response(str(e) or "删除失败")

    def _ui_snapshot(self, job: dict[str, Any]) -> dict[str, Any]:
        total = int(job.get("total") or 0)
        loaded = int(job.get("loaded") or 0)
        pct = min(100, int(loaded * 100 / total)) if total > 0 else int(job.get("pct") or 0)
        return {
            "job_id": job.get("id") or "",
            "status": job.get("status") or "queued",
            "loaded": loaded,
            "total": total,
            "pct": pct,
            "speed": job.get("speed") or "",
            "name": job.get("name") or "",
            "message": job.get("message") or "",
        }

    def _ui_emit(self, job: dict[str, Any], *, force: bool = False) -> None:
        throttle = job.get("throttle")
        if not isinstance(throttle, ProgressThrottle):
            return
        if not throttle.allow(time.monotonic(), force=force):
            return
        snap = self._ui_snapshot(job)
        job["last_snap"] = snap
        for listener in list(job.get("listeners") or []):
            try:
                listener.put_nowait(snap)
            except Exception:
                continue

    @staticmethod
    def _sse_event(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def _ui_pull_worker(self, job: dict[str, Any]) -> None:
        sem = self._ui_pull_sem
        acquired = False
        try:
            self._ui_emit(job, force=True)
            await sem.acquire()
            acquired = True
            if job["cancel"].is_set():
                job["status"] = "cancelled"
                job["speed"] = "已取消"
                return
            job["status"] = "running"
            job["speed"] = "连接中"
            self._ui_emit(job, force=True)
            client = self._client()
            if not job.get("total"):
                try:
                    remote = await client.head_environment_file_size(
                        job["sandbox_id"],
                        job["path"],
                        api_key=job["key"],
                    )
                    if remote:
                        job["total"] = int(remote)
                except Exception as e:
                    logger.info(f"沙盒文件拉取 HEAD 未取得大小: {e}")
            dest_dir = self._data_dir / "ui_pull"
            dest_dir.mkdir(parents=True, exist_ok=True)
            suffix = Path(str(job.get("name") or "")).suffix
            if not re.fullmatch(r"\.[A-Za-z0-9.]{1,16}", suffix or ""):
                suffix = ""
            dest = dest_dir / f"{job['id']}{suffix}"
            job["dest"] = dest
            started = time.monotonic()
            loaded = 0
            with dest.open("wb") as out:
                async for chunk in client.iter_environment_file(
                    job["sandbox_id"],
                    job["path"],
                    api_key=job["key"],
                    cancel_event=job["cancel"],
                ):
                    out.write(chunk)
                    loaded += len(chunk)
                    job["loaded"] = loaded
                    elapsed = max(time.monotonic() - started, 0.001)
                    total = int(job.get("total") or 0)
                    job["speed"] = (
                        f"{_fmt_bytes(loaded)}/{_fmt_bytes(total)} "
                        f"{_fmt_bytes(int(loaded / elapsed))}/s"
                    )
                    self._ui_emit(job)
            if job["cancel"].is_set():
                job["status"] = "cancelled"
                job["speed"] = "已取消"
                dest.unlink(missing_ok=True)
                job["dest"] = None
            else:
                job["status"] = "done"
                job["pct"] = 100
                job["loaded"] = loaded
                job["speed"] = f"{_fmt_bytes(loaded)} 完成"
        except GeminiPullCancelled:
            job["status"] = "cancelled"
            job["speed"] = "已取消"
            dest_path = job.get("dest")
            if dest_path:
                Path(dest_path).unlink(missing_ok=True)
                job["dest"] = None
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            job["speed"] = "已取消"
            dest_path = job.get("dest")
            if dest_path:
                Path(dest_path).unlink(missing_ok=True)
                job["dest"] = None
            raise
        except Exception as e:
            job["status"] = "error"
            job["message"] = str(e) or "下载失败"
            job["speed"] = "失败"
            logger.warning(f"沙盒文件页拉取失败: {e}")
            dest_path = job.get("dest")
            if dest_path:
                Path(dest_path).unlink(missing_ok=True)
                job["dest"] = None
        finally:
            if acquired:
                sem.release()
            self._ui_emit(job, force=True)

    async def _web_sandbox_files_pull_start(self):
        try:
            from astrbot.api.web import error_response, json_response, request
        except ImportError:
            return {"status": "error", "message": "当前 AstrBot 不支持 Plugin Web API"}
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                payload = {}
            sandbox_id = _as_str(payload.get("sandbox_id") or request.query.get("sandbox_id", ""))
            path = _as_str(payload.get("path") or request.query.get("path", ""))
            if not path:
                return error_response("缺少 path")
            sid, _short, key = self._web_resolve_sandbox_key(sandbox_id=sandbox_id)
            rel = normalize_environment_file_path(path)
            filename = PurePosixPath(rel).name or "download.bin"
            try:
                total = int(payload.get("size_bytes") or 0)
            except (TypeError, ValueError):
                total = 0
            job_id = uuid.uuid4().hex
            job: dict[str, Any] = {
                "id": job_id,
                "status": "queued",
                "loaded": 0,
                "total": max(total, 0),
                "pct": 0,
                "speed": "排队",
                "name": filename,
                "message": "",
                "path": rel,
                "sandbox_id": sid,
                "key": key,
                "cancel": asyncio.Event(),
                "listeners": [],
                "throttle": ProgressThrottle(UI_PULL_PROGRESS_INTERVAL),
                "dest": None,
                "task": None,
                "created": time.time(),
            }
            self._ui_jobs[job_id] = job
            job["task"] = asyncio.create_task(self._ui_pull_worker(job))
            return json_response({"job_id": job_id, "status": "queued"})
        except ValueError as e:
            return error_response(str(e))
        except Exception as e:
            logger.warning(f"sandbox-files/pull 失败: {e}")
            return error_response(str(e) or "无法开始拉取")

    async def _web_sandbox_files_pull_events(self):
        try:
            from astrbot.api.web import error_response, request, stream_response
        except ImportError:
            return {"status": "error", "message": "当前 AstrBot 不支持 Plugin Web API"}
        job_id = _as_str(request.query.get("job_id", ""))
        job = self._ui_jobs.get(job_id)
        if not job:
            return error_response("拉取任务不存在")

        async def gen():
            listener: asyncio.Queue = asyncio.Queue()
            job["listeners"].append(listener)
            try:
                snap = self._ui_snapshot(job)
                yield self._sse_event("progress", snap)
                if snap.get("status") in {"done", "error", "cancelled"}:
                    return
                while True:
                    try:
                        item = await asyncio.wait_for(listener.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield self._sse_event("progress", item)
                    if item.get("status") in {"done", "error", "cancelled"}:
                        break
            finally:
                listeners = job.get("listeners") or []
                if listener in listeners:
                    listeners.remove(listener)

        return stream_response(
            gen(),
            content_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    async def _web_sandbox_files_pull_cancel(self):
        try:
            from astrbot.api.web import error_response, json_response, request
        except ImportError:
            return {"status": "error", "message": "当前 AstrBot 不支持 Plugin Web API"}
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            payload = {}
        job_id = _as_str(payload.get("job_id") or request.query.get("job_id", ""))
        job = self._ui_jobs.get(job_id)
        if not job:
            return error_response("拉取任务不存在")
        job["cancel"].set()
        task = job.get("task")
        if task is not None and not task.done():
            task.cancel()
        return json_response({"ok": True, "job_id": job_id, "status": "cancelled"})

    async def _web_sandbox_files_pull_file(self):
        try:
            from astrbot.api.web import error_response, file_response, request
        except ImportError:
            return {"status": "error", "message": "当前 AstrBot 不支持 Plugin Web API"}
        job_id = _as_str(request.query.get("job_id", ""))
        job = self._ui_jobs.get(job_id)
        if not job or job.get("status") != "done":
            return error_response("文件还没拉取完成")
        dest = job.get("dest")
        path = Path(dest) if dest else None
        if path is None or not path.is_file():
            return error_response("拉取结果不存在")
        filename = _as_str(job.get("name")) or path.name
        mime, _encoding = mimetypes.guess_type(filename)
        resp = file_response(path, filename=filename, content_type=mime or "application/octet-stream")
        try:
            from starlette.background import BackgroundTask

            def _cleanup() -> None:
                path.unlink(missing_ok=True)
                self._ui_jobs.pop(job_id, None)

            resp.background = BackgroundTask(_cleanup)
        except Exception:
            pass
        return resp

    async def _pull_temp_cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                self._sweep_pull_temps()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"清理拉取临时文件失败: {e}")

    def _sweep_pull_temps(self) -> None:
        now = time.time()
        for folder in ("chat_pull", "ui_pull"):
            directory = self._data_dir / folder
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                try:
                    if not path.is_file():
                        continue
                    if now - path.stat().st_mtime > PULL_TEMP_TTL_SECONDS:
                        path.unlink()
                except OSError:
                    continue
        stale = [
            job_id
            for job_id, job in self._ui_jobs.items()
            if job.get("status") in {"done", "error", "cancelled"}
            and now - float(job.get("created") or now) > PULL_TEMP_TTL_SECONDS
        ]
        for job_id in stale:
            self._ui_jobs.pop(job_id, None)

    async def terminate(self):
        for job in list(self._ui_jobs.values()):
            cancel = job.get("cancel")
            if cancel is not None:
                cancel.set()
            task = job.get("task")
            if task is not None and not task.done():
                task.cancel()
        for task in (self._startup_task, self._auto_retrieve_task, self._pull_cleanup_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        logger.info("Antigravity 沙盒任务插件已卸载")


try:
    if migrate_saved_plugin_config():
        logger.info("已将插件配置迁移为分组结构")
except Exception as exc:
    logger.warning(f"插件配置分组迁移失败: {type(exc).__name__}")


@dataclass
class SubmitSandboxTaskTool(FunctionTool[AstrAgentContext]):
    name: str = "submit_sandbox_task"
    description: str = SUBMIT_TOOL_DESC
    parameters: dict = Field(default_factory=_submit_parameters)
    plugin: Any = Field(default=None, repr=False)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        plugin: AntigravitySandboxPlugin = self.plugin
        return await plugin.handle_submit(**kwargs)


@dataclass
class RetrieveSandboxTaskTool(FunctionTool[AstrAgentContext]):
    name: str = "retrieve_sandbox_task"
    description: str = RETRIEVE_TOOL_DESC
    parameters: dict = Field(default_factory=_retrieve_parameters)
    plugin: Any = Field(default=None, repr=False)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        plugin: AntigravitySandboxPlugin = self.plugin
        return await plugin.handle_retrieve(event=_tool_event(context), **kwargs)


@dataclass
class ContinueSandboxTaskTool(FunctionTool[AstrAgentContext]):
    name: str = "continue_sandbox_task"
    description: str = CONTINUE_TOOL_DESC
    parameters: dict = Field(default_factory=_continue_parameters)
    plugin: Any = Field(default=None, repr=False)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        plugin: AntigravitySandboxPlugin = self.plugin
        return await plugin.handle_continue(event=_tool_event(context), **kwargs)


@dataclass
class ListSandboxTaskTool(FunctionTool[AstrAgentContext]):
    name: str = "list_sandbox_task"
    description: str = LIST_TOOL_DESC
    parameters: dict = Field(default_factory=_list_parameters)
    plugin: Any = Field(default=None, repr=False)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        plugin: AntigravitySandboxPlugin = self.plugin
        return await plugin.handle_list_sandbox_task(**kwargs)


@dataclass
class GetSandboxTaskTool(FunctionTool[AstrAgentContext]):
    name: str = "get_sandbox_task"
    description: str = GET_TOOL_DESC
    parameters: dict = Field(default_factory=_get_parameters)
    plugin: Any = Field(default=None, repr=False)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        plugin: AntigravitySandboxPlugin = self.plugin
        return await plugin.handle_get_file(event=_tool_event(context), **kwargs)

