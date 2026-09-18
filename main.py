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
import os
import re
import shutil
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
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
        RUNNING_STATUS,
        GeminiClientError,
        GeminiSandboxClient,
        GeminiSubmitTimeoutError,
        environment_id_of,
        environment_size_bytes,
        extract_output_text,
        summarize_steps,
    )
except ImportError:  # loaded as a loose main.py, not a package
    from gemini_client import (
        RUNNING_STATUS,
        GeminiClientError,
        GeminiSandboxClient,
        GeminiSubmitTimeoutError,
        environment_id_of,
        environment_size_bytes,
        extract_output_text,
        summarize_steps,
    )

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
TOKEN_TARGET = "/workspace/upload.token"
SANDBOX_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.I)
ENV_META_LIMIT = 2000
PROTECT_RECENT_SECONDS = 15 * 60
EMERGENCY_RUNNING_SECONDS = 2 * 60 * 60
DEFAULT_IDLE_TTL_HOURS = 24
DEFAULT_KEEP_RECENT = 2
AUTO_RETRIEVE_SECONDS = 60 * 60
AUTO_RETRIEVE_TICK_SECONDS = 30.0
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
    text = re.sub(r"[^A-Za-z0-9._-]", "_", _as_str(name)).strip("._")
    return text or "file"


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
    text = _as_str(raw)
    default_files = f"result.{default_ext}" if default_ext else ""
    if not text:
        return "", default_files
    parts = text.split()
    groups: list[list[str]] = []
    while parts:
        parsed = _token_exts(parts[-1])
        if not parsed:
            break
        groups.append(parsed)
        parts.pop()
    prompt = " ".join(parts).strip()
    if not prompt or not groups:
        return text, default_files
    exts: list[str] = []
    seen: set[str] = set()
    for group in reversed(groups):
        for ext in group:
            if ext in seen:
                continue
            seen.add(ext)
            exts.append(ext)
    return prompt, ",".join(f"result.{ext}" for ext in exts)


CONTINUE_GATE_HINT = (
    "续接前若未取回会先自动取回上一轮；status 不是 completed 则只返回当前状态、不续接。"
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


SUBMIT_TOOL_DESC = (
"在 Linux 沙盒环境中异步执行耗时任务、长脚本，或进行深度网络调研与复杂项目分析时调用此工具。\n"
"支持传入指令并附带本地文件，返回短号 taskid 及产物预期公网地址。后续取回/续接只使用短号，不要向用户发送内部长 ID。\n"
"主要触发场景：\n"
"1. 深度调查与溯源：追查图片与文件来源、深度抓取与分析目标网站、检视开源项目源码等（此类深度任务优先于普通网页搜索调用）；\n"
"2. 长时间后台任务：耗时计算、批量处理、编译运行或生成复杂文件。"
)

RETRIEVE_TOOL_DESC = (
    "用提交或续接时返回的短号 taskid 查询 Antigravity 沙盒交互回执，"
    "获取会话状态、文本和步骤摘要。不要传内部长 ID。"
    "不会下载或解压环境快照。"
)

CONTINUE_TOOL_DESC = (
    "基于已有沙盒任务的短号 taskid，异步提交后续交互指令进行追问、修正或执行下一步。"
    "插件会按短号查找当时绑定的 Key，并续接该沙盒最新一轮交互（不能从祖先 id 分叉）。"
    "若上一轮尚未取回，会先自动取回；status 不是 completed 则只返回当前状态、不提交续接。"
    "不要传内部长 ID。短号不变，覆盖为最新一轮。"
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
                "description": "提交或续接时返回的短号 taskid，例如 0001",
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
                    "上一轮任务的短号 taskid，例如 0001。"
                    "插件据此查找当时绑定的 Key、沙盒和会话，并续接该沙盒最新一轮。"
                ),
            },
            "output_files": {
                "type": "string",
                "description": (
                    "本轮产出需上传到图床的文件名，逗号分隔；例如 "
                    "result.jpg,report.pdf。插件自动加时间戳前缀。"
                ),
            },
        },
        "required": ["prompt", "task_id"],
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
        submit_tool = SubmitSandboxTaskTool()
        retrieve_tool = RetrieveSandboxTaskTool()
        continue_tool = ContinueSandboxTaskTool()
        submit_tool.plugin = self
        retrieve_tool.plugin = self
        continue_tool.plugin = self
        self.context.add_llm_tools(submit_tool, retrieve_tool, continue_tool)
        logger.info("已注册 LLM 工具: submit_sandbox_task, retrieve_sandbox_task, continue_sandbox_task")

    async def initialize(self):
        self._auto_retrieve_task = asyncio.create_task(self._auto_retrieve_loop())
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

    def _configured_api_keys(self) -> list[str]:
        cfg = self.config or {}
        configured = cfg.get("gemini_api_keys") or []
        if isinstance(configured, str):
            configured = [configured]
        keys = [_as_str(key) for key in configured if _as_str(key)]
        legacy_key = _as_str(cfg.get("gemini_api_key"))
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
        return _as_bool((self.config or {}).get("auto_retrieve"), True)

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
        return _as_bool((self.config or {}).get("env_auto_cleanup"), True)

    def _env_idle_ttl_hours(self) -> float:
        try:
            value = float((self.config or {}).get("env_idle_ttl_hours") or DEFAULT_IDLE_TTL_HOURS)
        except (TypeError, ValueError):
            value = float(DEFAULT_IDLE_TTL_HOURS)
        return max(0.0, value)

    def _env_keep_recent(self) -> int:
        try:
            value = int((self.config or {}).get("env_keep_recent") or DEFAULT_KEEP_RECENT)
        except (TypeError, ValueError):
            value = DEFAULT_KEEP_RECENT
        return max(0, value)

    def _env_cleanup_scope(self) -> str:
        raw = _as_str((self.config or {}).get("env_cleanup_scope")).lower()
        return "all" if raw == "all" else "tracked"

    def _env_cleanup_on_quota(self) -> bool:
        return _as_bool((self.config or {}).get("env_cleanup_on_quota"), True)

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
                    recorded_at = _as_str(value.get("recorded_at"))
                    if recorded_at:
                        item["recorded_at"] = recorded_at
                    retrieved = _as_str(value.get("retrieved"))
                    if retrieved:
                        item["retrieved"] = retrieved
                    last_status = _as_str(value.get("last_status"))
                    if last_status:
                        item["last_status"] = last_status
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
            self._short_index[short] = self._short_item(
                task_id,
                sandbox_id,
                key=key or _as_str(old.get("key")),
            )
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

    def _latest_short_for_sandbox(self, sandbox_id: str) -> str:
        sandbox_id = _as_str(sandbox_id)
        if not sandbox_id:
            return ""
        best_short = ""
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
                f"taskid: {short}\n"
                f"status: {status}\n"
                f"上一轮尚未 completed，已自动取回并中止续接。完成后再 /agcontinue {short} <任务文本>"
            )
        if not receipt.ok:
            text = receipt.text or "提交失败。"
            if "回执超时" in text:
                return "提交超时，未取得任务 id。详情见后台日志。" + url_block
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
            f"taskid: {short}",
            f"status: {receipt.status or 'unknown'}",
            f"挂载文件数: {receipt.source_count}",
        ]
        if receipt.auto_retrieved:
            lines.append("已自动取回上一轮（completed）后提交续接。")
        if url_block:
            lines.append(url_block.lstrip("\n"))
        lines.extend(
            [
                f"后续用 /agretrieve {short} 取回，",
                f"/agcontinue {short} <任务文本> 续接任务。",
                CONTINUE_GATE_HINT,
            ]
        )
        return "\n".join(lines)

    def _llm_ack(self, receipt: HandlerReceipt) -> str:
        url_block = self._url_block(receipt)
        if receipt.continue_blocked:
            short = self._public_task_short(receipt) or self._short_for_task(
                receipt.task_id
            ) or "(未知)"
            status = receipt.status or "unknown"
            lines = [
                "【续接已中止】",
                f"taskid: {short}",
                f"status: {status}",
                "上一轮交互尚未 completed，已自动取回当前状态，未提交续接。",
                "请等待 completed 后再调用 continue_sandbox_task。",
            ]
            output = (receipt.output or "").strip()
            if output:
                clipped = output if len(output) <= 800 else output[:800] + "\n…(截断)"
                lines.extend(["", "当前 output_text:", clipped])
            return "\n".join(lines)
        if not receipt.ok or not receipt.task_id:
            text = receipt.text or "提交失败。"
            if "回执超时" in text:
                return "提交超时，未取得任务 id。详情见后台日志。" + url_block
            first = text.split("\n", 1)[0]
            if receipt.status:
                return f"{first}\nstatus: {receipt.status}" + url_block
            return first + url_block
        short = self._public_task_short(receipt)
        lines = [
            "【Antigravity 沙盒任务已受理】",
            f"taskid: {short}",
            f"status: {receipt.status or 'unknown'}",
            f"挂载文件数: {receipt.source_count}",
        ]
        if receipt.auto_retrieved:
            lines.append("已自动取回上一轮（completed）后提交续接。")
        if url_block:
            lines.append(url_block.lstrip("\n"))
        lines.extend(
            [
                "",
                f"后续调用 retrieve_sandbox_task，传入 task_id={short} 取回；"
                f"调用 continue_sandbox_task，传入 task_id={short} 与 prompt 续接。",
                CONTINUE_GATE_HINT,
                "只使用短号 taskid，不要向用户发送内部长 ID。",
            ]
        )
        return "\n".join(lines)

    def _command_retrieve_reply(self, receipt: HandlerReceipt) -> str:
        if not receipt.ok:
            return (receipt.text or "取回失败。").split("\n", 1)[0]
        short = self._short_for_task(receipt.task_id) or "(未知)"
        output = (receipt.output or "").strip() or "(尚无 output_text)"
        return f"taskid: {short}\nstatus: {receipt.status or 'unknown'}\n\n{output}"

    def _llm_retrieve_reply(self, receipt: HandlerReceipt) -> str:
        if not receipt.ok:
            return (receipt.text or "取回失败。").split("\n", 1)[0]
        short = self._short_for_task(receipt.task_id) or "(未知)"
        output = (receipt.output or "").strip() or "(尚无 output_text)"
        lines = [
            "【Antigravity 沙盒任务回执】",
            f"taskid: {short}",
            f"status: {receipt.status or 'unknown'}",
            "",
            "—— 回执文本（output_text）——",
            output,
        ]
        if receipt.steps:
            lines.extend(["", "—— steps 摘要 ——", receipt.steps])
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
        cfg = self.config or {}
        try:
            max_tokens = int(cfg.get("max_total_tokens") or 0)
        except (TypeError, ValueError):
            max_tokens = 0
        keys = self._configured_api_keys()
        return GeminiSandboxClient(
            api_keys=keys,
            default_model=_as_str(cfg.get("default_model")) or "auto",
            submit_background=_as_bool(cfg.get("submit_background"), True),
            max_total_tokens=max_tokens,
        )

    def _stamped_upload_names(self, output_files: str, stamp: str) -> list[str]:
        return [_stamp_upload_name(name, stamp) for name in _output_file_names(output_files)]

    def _expected_public_urls(self, output_files: str, stamp: str) -> list[str]:
        cfg = self.config or {}
        public_base = _as_str(cfg.get("upload_public_base_url"))
        if not public_base:
            public_base = _as_str(cfg.get("upload_base_url"))
        prefix = _as_str(cfg.get("upload_prefix")) or "agysb"
        names = self._stamped_upload_names(output_files, stamp)
        if not public_base or not names:
            return []
        root = public_base.rstrip("/") + "/" + prefix.strip("/")
        return [f"{root}/{name}" for name in names]

    def _upload_instruction(self, output_files: str, stamp: str) -> str:
        cfg = self.config or {}
        webhook = _as_str(cfg.get("upload_webhook_url"))
        public_base = _as_str(cfg.get("upload_public_base_url"))
        legacy_base = _as_str(cfg.get("upload_base_url"))
        if not webhook and legacy_base:
            webhook = legacy_base.rstrip("/") + "/Webhook/upload"
        if not public_base and legacy_base:
            public_base = legacy_base.rstrip("/")
        token = _as_str(cfg.get("upload_token"))
        prefix = _as_str(cfg.get("upload_prefix")) or "agysb"
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
        upload_token = _as_str(self.config.get("upload_token")).strip()
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
                payload, on_storage_quota=quota_hook
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
        self._schedule_auto_retrieve(short, task_id=task_id, sandbox_id=sandbox)
        status = _as_str(data.get("status")) or "unknown"
        self._touch_sandbox(sandbox, status=status)
        output = extract_output_text(data)
        bg = bool(payload.get("background"))
        lines = [
            "【Antigravity 沙盒任务已受理】",
            f"task_id: {task_id or '(响应中未找到 id)'}",
            f"sandbox_id: {sandbox or '(响应中未找到 environment_id)'}",
            f"status: {status}",
            "agent: antigravity-preview-05-2026",
            f"model: {client.model_label}",
            f"background: {bg}",
        ]
        if stamped_names:
            lines.append("上传文件名已加提交时间戳前缀: " + ", ".join(stamped_names))
        if sources:
            lines.append(f"已挂载 inline 文件数: {len(sources)}")
        if bg and (not output) and status in RUNNING_STATUS | {"", "unknown"}:
            lines.append("任务已提交，尚未完成，请稍后取回。")
        elif output:
            clipped = output if len(output) <= 2000 else output[:2000] + "\n…(截断)"
            lines.append("当前 output_text:")
            lines.append(clipped)
        if expected_urls:
            lines.extend(["", "预期公网地址（后台任务未完成前可能暂时无法访问）:"])
            lines.extend(expected_urls)
        lines.extend(
            [
                "",
                f"短号 taskid: {short or '(未分配)'}",
                "请注意：不要将内部长 task_id 和 sandbox_id 发送给用户。后续取回/续接只使用短号 taskid。",
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
        **_kwargs: Any,
    ) -> str:
        receipt = await self._do_continue(
            prompt=prompt,
            task_id=task_id,
            sandbox_id=sandbox_id,
            output_files=output_files,
        )
        self._log_command_receipt("【continue_sandbox_task 完整回执】", receipt.text)
        return self._llm_ack(receipt)

    async def _do_continue(
        self,
        *,
        prompt: str = "",
        task_id: str = "",
        sandbox_id: str = "",
        output_files: str = "",
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
                "续接交互失败: 未找到该 taskid。请使用提交回执中的短号 taskid。",
                ok=False,
            )
        if ".." in task_id or ".." in sandbox_id or "\x00" in task_id + sandbox_id:
            return HandlerReceipt("续接交互失败: id 含非法路径字符。", ok=False)

        gate_short = overwrite_short or self._short_for_task(task_id)
        auto_retrieved = False
        if not self._short_retrieved_completed(gate_short):
            pre = await self._do_retrieve(
                task_id=task_id, sandbox_id=sandbox_id, cancel_auto=True
            )
            if not pre.ok:
                return HandlerReceipt(
                    f"续接交互失败: 自动取回失败。\n{pre.text}",
                    ok=False,
                    task_id=pre.task_id or task_id,
                    sandbox_id=pre.sandbox_id or sandbox_id,
                    status=pre.status,
                )
            status = _as_str(pre.status).lower() or "unknown"
            if status != "completed":
                short_label = gate_short or self._short_for_task(pre.task_id) or "(未知)"
                lines = [
                    "【续接已中止】",
                    f"短号 taskid: {short_label}",
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
                )
            auto_retrieved = True

        stamp = _submit_stamp()
        expected_urls = self._expected_public_urls(output_files, stamp)
        stamped_names = self._stamped_upload_names(output_files, stamp)
        full_prompt = prompt_str + self._upload_instruction(output_files, stamp)
        # 延续同一个会话（previous_interaction_id）时，会话上下文与环境绑定，
        # Google Interactions API 明确不允许在请求中重复塞 sources 增量挂载文件，
        # environment 必须传纯 string environment_id，因此强制 sources=None，避免 400 invalid_request。
        assigned_key = assigned_key or self._find_key_for(
            task_id=task_id, sandbox_id=sandbox_id
        )
        try:
            client = self._client()
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
            return HandlerReceipt("\n".join(lines), ok=False, expected_urls=expected_urls)
        except GeminiClientError as e:
            return HandlerReceipt(f"续接交互失败: {e}", ok=False, expected_urls=expected_urls)
        except Exception as e:
            logger.error(f"continue_sandbox_task 未预期错误: {e}")
            return HandlerReceipt(
                f"续接交互失败（内部错误）: {e}",
                ok=False,
                expected_urls=expected_urls,
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
        self._schedule_auto_retrieve(
            short, task_id=new_task_id, sandbox_id=returned_sandbox
        )
        status = _as_str(data.get("status")) or "unknown"
        self._touch_sandbox(returned_sandbox, status=status)
        output = extract_output_text(data)
        bg = bool(payload.get("background"))
        lines = [
            "【Antigravity 沙盒续接任务已受理】",
            f"task_id: {new_task_id or '(响应中未找到 id)'}",
            f"previous_task_id: {task_id}",
            f"sandbox_id: {returned_sandbox}",
            f"status: {status}",
            "agent: antigravity-preview-05-2026",
            f"model: {client.model_label}",
            f"background: {bg}",
        ]
        if auto_retrieved:
            lines.append("已自动取回上一轮（completed）后提交续接。")
        if stamped_names:
            lines.append("上传文件名已加提交时间戳前缀: " + ", ".join(stamped_names))
        if bg and (not output) and status in RUNNING_STATUS | {"", "unknown"}:
            lines.append("任务已提交，尚未完成，请稍后取回。")
        elif output:
            clipped = output if len(output) <= 2000 else output[:2000] + "\n…(截断)"
            lines.append("当前 output_text:")
            lines.append(clipped)
        if expected_urls:
            lines.extend(["", "预期公网地址（后台任务未完成前可能暂时无法访问）:"])
            lines.extend(expected_urls)
        lines.extend(
            [
                "",
                f"短号 taskid: {short or '(未分配)'}",
                "请注意：不要将内部长 task_id 和 sandbox_id 发送给用户。后续取回/续接只使用短号 taskid。",
                CONTINUE_GATE_HINT,
            ]
        )
        return HandlerReceipt(
            "\n".join(lines),
            task_id=new_task_id,
            sandbox_id=returned_sandbox,
            status=status,
            source_count=0,
            output=output,
            expected_urls=expected_urls,
            auto_retrieved=auto_retrieved,
        )

    async def handle_retrieve(
        self, *, task_id: str = "", sandbox_id: str = "", **_kwargs: Any
    ) -> str:
        receipt = await self._do_retrieve(task_id=task_id, sandbox_id=sandbox_id)
        self._log_command_receipt("【retrieve_sandbox_task 完整回执】", receipt.text)
        return self._llm_retrieve_reply(receipt)

    async def _do_retrieve(
        self,
        *,
        task_id: str = "",
        sandbox_id: str = "",
        cancel_auto: bool = True,
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
                "取回失败: 未找到该 taskid。请使用提交回执中的短号 taskid。",
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
        except GeminiClientError as e:
            return HandlerReceipt(
                f"查询任务失败: {e}",
                ok=False,
                task_id=task_id,
                sandbox_id=sandbox_id,
            )
        except Exception as e:
            logger.error(f"retrieve GET 未预期错误: {e}")
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
        usage = data.get("usage")
        if isinstance(usage, dict) and usage:
            lines.extend(["", "usage: " + json.dumps(usage, ensure_ascii=False)[:800]])
        return HandlerReceipt(
            "\n".join(lines),
            task_id=task_id,
            sandbox_id=sandbox_id,
            status=status,
            output=output,
            steps=steps,
        )

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

    @filter.command("agsubmit")
    async def agsubmit(self, event: AstrMessageEvent, prompt: GreedyStr):
        """提交 Antigravity 沙盒任务。默认产出 md；末尾可写一种或多种类型，如 png 或 svg png。"""
        prompt_text, output_files = _parse_command_prompt(str(prompt), default_ext="md")
        file_list = await self._collect_event_file_paths(event)
        if not prompt_text and not file_list:
            yield event.plain_result("用法: /agsubmit <任务文本> [类型...]")
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

    @filter.command("agretrieve")
    async def agretrieve(self, event: AstrMessageEvent, task_ref: str = ""):
        """取回沙盒任务: /agretrieve <taskid>"""
        task_ref = str(task_ref).strip()
        if not task_ref:
            yield event.plain_result("用法: /agretrieve <taskid>")
            return
        resolved = self._resolve_ids(task_ref)
        if not resolved:
            yield event.plain_result(
                "未找到该 taskid。请使用提交回执中的 taskid，例如 /agretrieve 0001"
            )
            return
        task_id, sandbox_id, _assigned_key = resolved
        receipt = await self._do_retrieve(task_id=task_id, sandbox_id=sandbox_id)
        self._log_command_receipt("【agretrieve 完整回执】", receipt.text)
        yield event.plain_result(self._command_retrieve_reply(receipt))

    @filter.command("agcontinue")
    async def agcontinue(
        self,
        event: AstrMessageEvent,
        task_ref: str = "",
        prompt: GreedyStr = "",
    ):
        """在已有沙盒会话中续接任务: /agcontinue <taskid> <任务文本> [类型]"""
        task_ref = str(task_ref).strip()
        prompt_text, output_files = _parse_command_prompt(str(prompt), default_ext="md")
        if not task_ref or not prompt_text:
            yield event.plain_result("用法: /agcontinue <taskid> <任务文本> [类型...]")
            return
        resolved = self._resolve_ids(task_ref)
        if not resolved:
            yield event.plain_result(
                "未找到该 taskid。请使用提交回执中的 taskid，例如 /agcontinue 0001 继续查"
            )
            return
        task_id, sandbox_id, _assigned_key = resolved
        receipt = await self._do_continue(
            prompt=prompt_text,
            task_id=task_id,
            sandbox_id=sandbox_id,
            output_files=output_files,
        )
        self._log_command_receipt("【agcontinue 完整回执】", receipt.text)
        yield event.plain_result(self._command_ack(receipt))

    @filter.command("aghelp")
    async def aghelp(self, event: AstrMessageEvent):
        """查看 Antigravity 沙盒指令说明。"""
        yield event.plain_result(
            "Antigravity 沙盒指令：\n"
            "/agsubmit <任务文本> [类型...]\n"
            "  提交新任务。默认产出 result.md。"
            "末尾可指定一种或多种类型，例如：/agsubmit 查询今日新闻 png\n"
            "  或：/agsubmit 查询今日新闻 svg png\n"
            "  可在本条消息附带图片/文件，或回复一条带图/文件的消息后再发送本指令。\n"
            "/agretrieve <taskid>\n"
            "  取回任务回执\n"
            "/agcontinue <taskid> <任务文本> [类型...]\n"
            "  在同一沙盒会话中续接；默认产出 result.md，末尾可指定类型。"
            "续接后短号不变，覆盖为该沙盒最新一轮。续接不能再挂新文件。\n"
            "  若上一轮尚未取回会先自动取回；status 不是 completed 则只返回当前状态、不续接。\n"
            "/agenvlist\n"
            "  管理员：列出当前项目沙盒环境数量与占用。\n"
            "/agenvcleanup [all|短号]\n"
            "  管理员：回收沙盒。不带参数只扫本插件建过且已闲置的；"
            "all 扫整个 Gemini 项目里可回收的闲置环境（仍跳过未过 TTL、正在跑、最近保留的）。"
            "指定短号（如 0002）立即删除该沙盒，不受闲置时间限制。\n"
            "/aghelp\n"
            "  查看本说明"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("agenvlist")
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
    @filter.command("agenvcleanup")
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

    async def terminate(self):
        for task in (self._startup_task, self._auto_retrieve_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        logger.info("Antigravity 沙盒任务插件已卸载")


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
        return await plugin.handle_retrieve(**kwargs)


@dataclass
class ContinueSandboxTaskTool(FunctionTool[AstrAgentContext]):
    name: str = "continue_sandbox_task"
    description: str = CONTINUE_TOOL_DESC
    parameters: dict = Field(default_factory=_continue_parameters)
    plugin: Any = Field(default=None, repr=False)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        plugin: AntigravitySandboxPlugin = self.plugin
        return await plugin.handle_continue(**kwargs)

