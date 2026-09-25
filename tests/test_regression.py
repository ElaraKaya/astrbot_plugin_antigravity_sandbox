"""Regression checks for the 1.6 sandbox changes. No live Gemini calls."""

from __future__ import annotations

import ast
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from gemini_client import (  # noqa: E402
    CHAT_PULL_MAX_BYTES,
    CHAT_PULL_TIMEOUT_SECONDS,
    DEFAULT_RECEIPT_TRUNCATE_CHARS,
    MSG_ENV_404,
    MSG_FILE_MISSING,
    MSG_FILE_TOO_LARGE,
    MSG_KEY_INVALID,
    MSG_LIST_EMPTY,
    MSG_NO_CAPACITY,
    MSG_PULL_TIMEOUT,
    RETRIEVE_GET_RETRIES,
    RETRIEVE_GET_TIMEOUT,
    UI_PULL_CONCURRENCY,
    UI_PULL_PROGRESS_INTERVAL,
    GeminiClientError,
    GeminiFileTooLargeError,
    GeminiPullCancelled,
    GeminiRetrieveQueryError,
    GeminiSandboxClient,
    ProgressThrottle,
    build_httpx_client_kwargs,
    clip_text,
    count_key_in_progress,
    ensure_md_filename,
    environment_media_url,
    files_error_kind,
    fixed_files_message,
    select_idle_keys,
    slim_environment_file_entry,
    workspace_download_path,
)

MAIN = ROOT / "main.py"


class ProbeClient(GeminiSandboxClient):
    def __init__(self, transport: httpx.MockTransport, **kwargs):
        super().__init__(**kwargs)
        self.transport = transport
        self.seen_timeout = None

    def _client_kwargs(self, timeout, api_key=None, **kwargs):
        self.seen_timeout = timeout
        options = super()._client_kwargs(timeout, api_key, **kwargs)
        options["transport"] = self.transport
        options.pop("proxy", None)
        return options


def _run(coro):
    return asyncio.run(coro)


class PolicyTests(unittest.TestCase):
    def test_chinese_download_url_prefixes_workspace_and_encodes(self):
        from urllib.parse import unquote

        name = unquote("%E5%B9%BB%E6%99%9D_05.mp3")
        rel = workspace_download_path(name)
        self.assertEqual(rel, f"workspace/{name}")
        url = environment_media_url("b9b3b2b72a652497c7d337db187e072f", rel)
        self.assertIn("/files/workspace/%E5%B9%BB%E6%99%9D_05.mp3?alt=media", url)
        self.assertEqual(workspace_download_path(f"workspace/{name}"), rel)

    def test_slim_list_keeps_only_four_fields(self):
        row = slim_environment_file_entry(
            {
                "name": "a.md",
                "path": "workspace/a.md",
                "type": "FILE",
                "sizeBytes": "12",
                "mime_type": "text/markdown",
                "created": "yesterday",
            }
        )
        self.assertEqual(set(row), {"name", "path", "type", "size_bytes"})
        self.assertEqual(row["size_bytes"], 12)
        self.assertEqual(row["type"], "file")

    def test_missing_suffix_defaults_to_md(self):
        self.assertEqual(ensure_md_filename("notes"), ("notes.md", True))
        self.assertEqual(ensure_md_filename("幻舟.mp3"), ("幻舟.mp3", False))

    def test_clip_keeps_full_text_only_when_image_receipt_is_certain(self):
        self.assertEqual(DEFAULT_RECEIPT_TRUNCATE_CHARS, 2000)
        body = "x" * 20
        clipped = clip_text(body, 8, keep_full=False)
        self.assertTrue(clipped.startswith("x" * 8))
        self.assertIn("截断", clipped)
        self.assertEqual(clip_text(body, 8, keep_full=True), body)
        self.assertEqual(clip_text("short", 2000, keep_full=False), "short")

    def test_in_progress_limit_uses_local_cache_and_does_not_switch_full_keys(self):
        items = [
            {"key": "fp-a", "last_status": "in_progress"},
            {"key": "fp-a", "last_status": "completed"},
            {"key": "fp-b", "last_status": "queued"},
        ]
        self.assertEqual(count_key_in_progress(items, "fp-a"), 1)
        self.assertEqual(count_key_in_progress(items, "fp-b"), 0)
        idle = select_idle_keys(["key-a", "key-b"], {"key-a": 1, "key-b": 0}, 1)
        self.assertEqual(idle, ["key-b"])
        self.assertEqual(select_idle_keys(["key-a"], {"key-a": 1}, 1), [])
        self.assertEqual(MSG_NO_CAPACITY, "目前无空余沙盒分配")

    def test_fixed_file_replies_are_distinct(self):
        self.assertEqual(fixed_files_message("env"), MSG_ENV_404)
        self.assertEqual(fixed_files_message("key"), MSG_KEY_INVALID)
        self.assertEqual(fixed_files_message("timeout"), MSG_PULL_TIMEOUT)
        self.assertEqual(fixed_files_message("empty"), MSG_LIST_EMPTY)
        self.assertEqual(fixed_files_message("file"), MSG_FILE_MISSING)
        self.assertEqual(fixed_files_message("large"), MSG_FILE_TOO_LARGE)
        self.assertEqual(files_error_kind(404, "列出沙盒文件失败 HTTP 404", listing=True), "env")
        self.assertEqual(files_error_kind(404, "下载沙盒文件失败 HTTP 404", listing=False), "file")
        self.assertEqual(files_error_kind(401, "HTTP 401", listing=True), "key")
        self.assertEqual(files_error_kind(403, "HTTP 403", listing=False), "key")
        kinds = {MSG_ENV_404, MSG_KEY_INVALID, MSG_PULL_TIMEOUT, MSG_LIST_EMPTY, MSG_FILE_MISSING}
        self.assertEqual(len(kinds), 5)

    def test_progress_throttle_is_500ms_and_ui_cap_is_two(self):
        self.assertEqual(UI_PULL_PROGRESS_INTERVAL, 0.5)
        self.assertEqual(UI_PULL_CONCURRENCY, 2)
        throttle = ProgressThrottle(0.5)
        self.assertTrue(throttle.allow(1.0))
        self.assertFalse(throttle.allow(1.2))
        self.assertTrue(throttle.allow(1.2, force=True))
        self.assertTrue(throttle.allow(1.8))

    def test_proxy_applies_to_gemini_and_not_to_webhook_helper(self):
        proxied = GeminiSandboxClient(api_keys=["k"], proxy="http://127.0.0.1:7890")
        options = proxied._client_kwargs(5, "k")
        self.assertEqual(options["proxy"], "http://127.0.0.1:7890")
        self.assertFalse(options["trust_env"])
        direct = GeminiSandboxClient(api_keys=["k"], proxy="  ")
        self.assertNotIn("proxy", direct._client_kwargs(5, "k"))
        webhook = build_httpx_client_kwargs(
            timeout=5,
            proxy="http://127.0.0.1:7890",
            use_proxy=False,
        )
        self.assertNotIn("proxy", webhook)
        self.assertFalse(webhook["trust_env"])

    def test_continue_rejects_interaction_sources(self):
        client = GeminiSandboxClient(api_keys=["k"])
        with self.assertRaises(GeminiClientError):
            client.build_create_payload(
                prompt="继续",
                new_sandbox=False,
                sandbox_id="env",
                new_session=False,
                previous_task_id="prev",
                sources=[{"type": "inline", "target": "/workspace/a.md", "content": "hi"}],
            )
        payload = client.build_create_payload(
            prompt="继续",
            new_sandbox=False,
            sandbox_id="env",
            new_session=False,
            previous_task_id="prev",
            sources=None,
        )
        self.assertEqual(payload["environment"], "env")
        self.assertNotIn("sources", payload["environment"] if isinstance(payload["environment"], dict) else {})

    def test_limits(self):
        self.assertEqual(CHAT_PULL_MAX_BYTES, 20 * 1024 * 1024)
        self.assertEqual(CHAT_PULL_TIMEOUT_SECONDS, 90.0)

    def test_background_payload_carries_store_true(self):
        client = GeminiSandboxClient(api_keys=["k"])
        bg = client.build_create_payload(
            prompt="hi",
            new_sandbox=True,
            sandbox_id=None,
            new_session=True,
            previous_task_id=None,
            sources=None,
            background=True,
        )
        self.assertIs(bg["background"], True)
        # background 任务必须 store，否则后续按 id 取回/续接时背面链路查不到
        self.assertIs(bg["store"], True)

        sync = client.build_create_payload(
            prompt="hi",
            new_sandbox=True,
            sandbox_id=None,
            new_session=True,
            previous_task_id=None,
            sources=None,
            background=False,
        )
        self.assertNotIn("background", sync)
        self.assertNotIn("store", sync)

    def test_retrieve_get_timeout_is_short_and_retries_once(self):
        self.assertEqual(RETRIEVE_GET_TIMEOUT, 7.0)
        self.assertEqual(RETRIEVE_GET_RETRIES, 1)
        self.assertLess(RETRIEVE_GET_TIMEOUT, 15.0)


class HttpTests(unittest.TestCase):
    def test_put_uses_environment_upload_url(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "name": "notes.md",
                            "path": "workspace/notes.md",
                            "type": "FILE",
                            "size_bytes": "4",
                        }
                    ]
                },
            )

        client = ProbeClient(httpx.MockTransport(handler), api_keys=["test-key"])

        async def run():
            return await client.put_environment_file(
                "env123",
                "workspace/notes.md",
                b"data",
                content_type="text/markdown",
                api_key="test-key",
            )

        meta = _run(run())
        self.assertEqual(meta["name"], "notes.md")
        self.assertEqual(len(seen), 1)
        request = seen[0]
        self.assertEqual(request.method, "PUT")
        self.assertIn("/upload/v1beta/environments/env123/files/workspace/notes.md", str(request.url))
        self.assertNotIn("/interactions", str(request.url))
        self.assertEqual(request.content, b"data")
        self.assertEqual(request.headers["x-goog-api-key"], "test-key")

    def test_pull_aborts_over_20mb_and_on_cancel(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "HEAD":
                return httpx.Response(200, headers={"content-length": "4"})
            return httpx.Response(200, content=b"0123456789")

        client = ProbeClient(httpx.MockTransport(handler), api_keys=["k"])

        async def too_big():
            async for _chunk in client.iter_environment_file(
                "env",
                "workspace/a.bin",
                api_key="k",
                max_bytes=4,
                timeout=90,
            ):
                pass

        with self.assertRaises(GeminiFileTooLargeError) as ctx:
            _run(too_big())
        self.assertEqual(str(ctx.exception), MSG_FILE_TOO_LARGE)

        cancel = asyncio.Event()
        cancel.set()

        async def cancelled():
            async for _chunk in client.iter_environment_file(
                "env",
                "workspace/a.bin",
                api_key="k",
                cancel_event=cancel,
            ):
                pass

        with self.assertRaises(GeminiPullCancelled):
            _run(cancelled())

        async def head():
            return await client.head_environment_file_size(
                "env",
                "workspace/a.bin",
                api_key="k",
                timeout=CHAT_PULL_TIMEOUT_SECONDS,
            )

        self.assertEqual(_run(head()), 4)
        self.assertEqual(client.seen_timeout, CHAT_PULL_TIMEOUT_SECONDS)

    def test_retrieve_get_retries_once_then_succeeds(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(504, json={"error": {"message": "deadline_exceeded"}})
            return httpx.Response(200, json={"id": "task1", "status": "completed"})

        client = ProbeClient(httpx.MockTransport(handler), api_keys=["k"])

        async def run():
            return await client.get_interaction("task1", api_key="k")

        data = _run(run())
        self.assertEqual(data["status"], "completed")
        # 第一次 504 后原地重试，共两次请求
        self.assertEqual(calls["n"], 2)

    def test_retrieve_get_persistent_failure_raises_query_error(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(504, json={"error": {"message": "deadline_exceeded"}})

        client = ProbeClient(httpx.MockTransport(handler), api_keys=["k"])

        async def run():
            return await client.get_interaction("task1", api_key="k")

        with self.assertRaises(GeminiRetrieveQueryError):
            _run(run())
        self.assertEqual(calls["n"], 2)

    def test_retrieve_get_500_internal_error_retries_once_then_succeeds(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    500,
                    json={
                        "error": {
                            "message": "Internal error encountered.",
                            "code": "api_error",
                        }
                    },
                )
            return httpx.Response(200, json={"id": "task1", "status": "completed"})

        client = ProbeClient(httpx.MockTransport(handler), api_keys=["k"])

        async def run():
            return await client.get_interaction("task1", api_key="k")

        data = _run(run())
        self.assertEqual(data["status"], "completed")
        self.assertEqual(calls["n"], 2)

    def test_retrieve_get_persistent_500_raises_query_error(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                500,
                json={
                    "error": {
                        "message": "Internal error encountered.",
                        "code": "api_error",
                    }
                },
            )

        client = ProbeClient(httpx.MockTransport(handler), api_keys=["k"])

        async def run():
            return await client.get_interaction("task1", api_key="k")

        with self.assertRaises(GeminiRetrieveQueryError):
            _run(run())
        self.assertEqual(calls["n"], 2)

    def test_retrieve_get_200_quoting_internal_error_is_not_gateway_failure(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "id": "task1",
                    "status": "completed",
                    "output_text": (
                        'HTTP 500: {"error":{"message":"Internal error encountered.",'
                        '"code":"api_error"}}'
                    ),
                },
            )

        client = ProbeClient(httpx.MockTransport(handler), api_keys=["k"])

        async def run():
            return await client.get_interaction("task1", api_key="k")

        data = _run(run())
        self.assertEqual(data["status"], "completed")
        self.assertEqual(calls["n"], 1)


class SourceTests(unittest.TestCase):
    def test_commands_aliases_and_tools(self):
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        commands: dict[str, set[str]] = {}
        tool_names: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if (
                        isinstance(stmt, ast.AnnAssign)
                        and isinstance(stmt.target, ast.Name)
                        and stmt.target.id == "name"
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)
                        and stmt.value.value.endswith("_sandbox_task")
                    ):
                        tool_names.add(stmt.value.value)
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if not isinstance(stmt, ast.AsyncFunctionDef):
                    continue
                for deco in stmt.decorator_list:
                    call = deco
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                        if call.func.attr != "command" or not call.args:
                            continue
                        if not isinstance(call.args[0], ast.Constant):
                            continue
                        aliases: set[str] = set()
                        for keyword in call.keywords:
                            if keyword.arg == "alias" and isinstance(keyword.value, ast.Set):
                                aliases = {
                                    elt.value
                                    for elt in keyword.value.elts
                                    if isinstance(elt, ast.Constant)
                                }
                        commands[call.args[0].value] = aliases
        self.assertEqual(commands["agsubmit"], {"ags"})
        self.assertEqual(commands["agretrieve"], {"agr"})
        self.assertEqual(commands["agcontinue"], {"agc"})
        self.assertEqual(commands["agenvlist"], {"agels"})
        self.assertEqual(commands["agenvcleanup"], {"agecl"})
        self.assertIn("agls", commands)
        self.assertIn("agget", commands)
        self.assertEqual(
            tool_names,
            {
                "submit_sandbox_task",
                "retrieve_sandbox_task",
                "continue_sandbox_task",
                "list_sandbox_task",
                "get_sandbox_task",
            },
        )

    def test_chat_pull_does_not_share_ui_semaphore(self):
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        chat_src = ""
        ui_src = ""
        upload_src = ""
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_pull_chat_file_inner":
                chat_src = ast.get_source_segment(MAIN.read_text(encoding="utf-8"), node) or ""
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ui_pull_worker":
                ui_src = ast.get_source_segment(MAIN.read_text(encoding="utf-8"), node) or ""
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_upload_file_bytes":
                upload_src = ast.get_source_segment(MAIN.read_text(encoding="utf-8"), node) or ""
        self.assertNotIn("_ui_pull_sem", chat_src)
        self.assertIn("_chat_pull_blocked", chat_src)
        self.assertIn("_ui_pull_sem", ui_src)
        self.assertIn("use_proxy=False", upload_src)
        text = MAIN.read_text(encoding="utf-8")
        self.assertIn("Comp.File", text)
        self.assertIn("sources=None", text)
        self.assertIn("沙盒网络存疑", text)
        self.assertIn("truncate_chars", text)
        self.assertIn("max_in_progress_per_key", text)
        client_text = (ROOT / "gemini_client.py").read_text(encoding="utf-8")
        self.assertIn('payload["store"] = True', client_text)
        self.assertIn("AGHELP_TEXT", text)
        self.assertIn("_list_files_table_text", text)
        self.assertIn("_single_latest_short_for_sandbox", text)
        self.assertIn('f"任务编号: {short}"', text)
        self.assertIn("/agls {short} 查看文件；/agget {short} <完整路径> 拉取文件。", text)
        # 帮助文案：/agget 用「任务编号 + 完整路径」两个参数
        self.assertIn("/agget <任务编号> <完整路径>", text)
        self.assertIn("/agls <任务编号>", text)
        self.assertIn("/agretrieve 或 /agr <任务编号>", text)
        self.assertIn("未完成任务无法续接", text)
        self.assertIn("def _chat_pull_blocked", text)
        self.assertIn('endswith(".token")', text)
        # 续接说明按新修订：默认 md 那句单独成行，附件提示另起一句
        self.assertIn(
            "  在同一沙盒会话中续接任务。不写类型时默认 md，并返回对应预期网址。\\n",
            text,
        )
        self.assertNotIn('f"taskid: {short}"', text)


class AgGetParseTests(unittest.TestCase):
    """校验 /agget 的完整路径解析，覆盖优雅风格与旧写法兼容。"""

    @staticmethod
    def _parse(raw: str) -> tuple[str, str]:
        """与 agget 中的解析逻辑保持一致（抽出来便于回归）。"""
        import re as _re

        drive = _re.compile(r"^[A-Za-z]:$")
        raw = (raw or "").strip()
        task_ref = ""
        name = raw
        if (raw[:2] and drive.fullmatch(raw[:2])) or raw.startswith(("/", "\\")):
            name = raw
        elif ":" in raw:
            head, _, tail = raw.partition(":")
            head = head.strip()
            tail = tail.strip().lstrip("/")
            if head.isdigit() and tail:
                task_ref = head
                name = tail
        elif " " in raw:
            head, _, tail = raw.partition(" ")
            if head.strip().isdigit() and tail.strip():
                task_ref = head.strip()
                name = tail.strip()
        return task_ref, name

    def test_space_form_is_primary(self):
        self.assertEqual(
            self._parse("0003 workspace/dist/index.html"),
            ("0003", "workspace/dist/index.html"),
        )
        self.assertEqual(self._parse("0003 index.html"), ("0003", "index.html"))

    def test_path_with_spaces_kept_after_first_split(self):
        self.assertEqual(
            self._parse("0003 workspace/my report v2.md"),
            ("0003", "workspace/my report v2.md"),
        )

    def test_colon_form_still_supported(self):
        self.assertEqual(
            self._parse("0003:workspace/index.html"),
            ("0003", "workspace/index.html"),
        )
        self.assertEqual(
            self._parse("0003:  /workspace/a.md"),
            ("0003", "workspace/a.md"),
        )

    def test_paths_are_kept_whole(self):
        # 绝对路径不带编号，整体当作路径
        self.assertEqual(self._parse("workspace/a.md"), ("", "workspace/a.md"))
        self.assertEqual(self._parse("/workspace/a.md"), ("", "/workspace/a.md"))
        # Windows 风格路径不会被误拆成任务编号
        self.assertEqual(self._parse("C:\\tmp\\a.md"), ("", "C:\\tmp\\a.md"))

    def test_empty_is_rejected(self):
        self.assertEqual(self._parse("   "), ("", ""))


if __name__ == "__main__":
    unittest.main()
