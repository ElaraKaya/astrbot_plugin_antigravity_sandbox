"""Regression checks for the 1.6 sandbox changes. No live Gemini calls."""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from gemini_client import (  # noqa: E402
    CHAT_PULL_MAX_BYTES,
    CHAT_PULL_TIMEOUT_SECONDS,
    DEFAULT_RECEIPT_TRUNCATE_CHARS,
    IMAGE_HOST_CREDENTIAL_ID,
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
    GeminiSubmitTimeoutError,
    ProgressThrottle,
    build_httpx_client_kwargs,
    build_upload_instruction,
    clip_text,
    count_key_in_progress,
    ensure_md_filename,
    environment_media_url,
    files_error_kind,
    fixed_files_message,
    image_host_network,
    select_balanced_keys,
    select_idle_keys,
    slim_environment_file_entry,
    webhook_upload_host,
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

    def test_submit_balances_toward_idler_key_then_next_key(self):
        keys = ["key-a", "key-b", "key-c"]
        # 当前 Key 只剩 1 个额度（上限 3、进行中 2），换到更空闲的。
        self.assertEqual(
            select_balanced_keys(
                keys,
                {"key-a": 2, "key-b": 0, "key-c": 2},
                3,
                current_key="key-a",
            )[0],
            "key-b",
        )
        # 剩余额度相同：提交到当前 Key 的下一个，并绕回开头。
        self.assertEqual(
            select_balanced_keys(keys, {"key-a": 0, "key-b": 0, "key-c": 0}, 1, current_key="key-a"),
            ["key-b", "key-c", "key-a"],
        )
        self.assertEqual(
            select_balanced_keys(keys, {"key-a": 0, "key-b": 0, "key-c": 0}, 1, current_key="key-b")[0],
            "key-c",
        )
        self.assertEqual(
            select_balanced_keys(keys, {"key-a": 0, "key-b": 0, "key-c": 0}, 1, current_key="key-c")[0],
            "key-a",
        )
        # 没有上一把时，同样空闲的 Key 保持配置顺序。
        self.assertEqual(
            select_balanced_keys(keys, {"key-a": 0, "key-b": 0, "key-c": 0}, 1),
            ["key-a", "key-b", "key-c"],
        )
        # 已满的跳过，从当前 Key 后面继续转。
        self.assertEqual(
            select_balanced_keys(keys, {"key-a": 1, "key-b": 0, "key-c": 0}, 1, current_key="key-a"),
            ["key-b", "key-c"],
        )
        self.assertEqual(
            select_balanced_keys(
                ["key-a", "", "key-a", "key-b"],
                {"key-a": 0, "key-b": 0},
                1,
                current_key="key-a",
            ),
            ["key-b", "key-a"],
        )
        self.assertEqual(
            select_balanced_keys(["key-a"], {"key-a": 1}, 1, current_key="key-a"),
            [],
        )

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

    def test_proxy_switch_applies_only_when_enabled(self):
        proxied = GeminiSandboxClient(api_keys=["k"], proxy="http://127.0.0.1:7890")
        options = proxied._client_kwargs(5, "k")
        self.assertEqual(options["proxy"], "http://127.0.0.1:7890")
        self.assertFalse(options["trust_env"])
        self.assertTrue(options["follow_redirects"])
        direct = GeminiSandboxClient(api_keys=["k"], proxy="  ")
        self.assertNotIn("proxy", direct._client_kwargs(5, "k"))
        direct_only = build_httpx_client_kwargs(
            timeout=5,
            proxy="http://127.0.0.1:7890",
            use_proxy=False,
            follow_redirects=False,
        )
        self.assertNotIn("proxy", direct_only)
        self.assertFalse(direct_only["trust_env"])
        self.assertFalse(direct_only["follow_redirects"])

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
        network = image_host_network("img.example.com")
        fresh = client.build_create_payload(
            prompt="新建",
            new_sandbox=True,
            sandbox_id=None,
            new_session=True,
            previous_task_id=None,
            sources=[{"type": "inline", "target": "/workspace/a.md", "content": "hi"}],
            network=network,
        )
        self.assertEqual(fresh["environment"]["type"], "remote")
        self.assertIn("sources", fresh["environment"])
        self.assertEqual(fresh["environment"]["network"], network)
        bare = client.build_create_payload(
            prompt="新建",
            new_sandbox=True,
            sandbox_id=None,
            new_session=True,
            previous_task_id=None,
            sources=None,
            network=network,
        )
        self.assertNotIn("sources", bare["environment"])
        continued = client.build_create_payload(
            prompt="继续",
            new_sandbox=False,
            sandbox_id="env",
            new_session=False,
            previous_task_id="prev",
            sources=None,
            network=network,
        )
        self.assertEqual(continued["previous_interaction_id"], "prev")
        self.assertEqual(continued["environment"]["environment_id"], "env")
        self.assertNotIn("sources", continued["environment"])
        rules = continued["environment"]["network"]["allowlist"]
        self.assertEqual(rules[0]["domain"], "img.example.com")
        self.assertEqual(rules[0]["credential"], IMAGE_HOST_CREDENTIAL_ID)
        self.assertEqual(rules[1], {"domain": "*"})

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
        submit_src = ""
        continue_src = ""
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_pull_chat_file_inner":
                chat_src = ast.get_source_segment(MAIN.read_text(encoding="utf-8"), node) or ""
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ui_pull_worker":
                ui_src = ast.get_source_segment(MAIN.read_text(encoding="utf-8"), node) or ""
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_do_submit":
                submit_src = ast.get_source_segment(MAIN.read_text(encoding="utf-8"), node) or ""
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_do_continue":
                continue_src = ast.get_source_segment(MAIN.read_text(encoding="utf-8"), node) or ""
        self.assertNotIn("_ui_pull_sem", chat_src)
        self.assertIn("_chat_pull_blocked", chat_src)
        self.assertIn("_ui_pull_sem", ui_src)
        self.assertLess(
            submit_src.find("ensure_credentials_for_keys"),
            submit_src.find("_remember_submit_key"),
        )
        self.assertLess(
            continue_src.find("ensure_bearer_credential"),
            continue_src.find("_put_continue_uploads"),
        )
        self.assertIn("api_key=assigned_key", continue_src)
        text = MAIN.read_text(encoding="utf-8")
        self.assertNotIn("def _upload_file_bytes", text)
        self.assertNotIn("def _public_url_from_upload_payload", text)
        self.assertIn("任务绑定的 API Key 不可用，请恢复对应配置。", text)
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
        self.assertIn(
            "  指定其它类型时仍会额外要求一份带时间戳的 result.md。\\n",
            text,
        )
        self.assertIn("【产物放置要求】", text)
        self.assertIn('"/agget {short} <文件路径> 获取文件(可能需先取回任务)"', text)
        self.assertIn('"后续:"', text)
        self.assertNotIn("后续 /agr {short} 取回，", text)
        self.assertNotIn("def _maybe_plugin_upload_md", text)
        self.assertNotIn("plugin_md=", text)
        self.assertNotIn("default_ext=None", text)
        schema = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        self.assertIn("沙盒路径回执", schema)
        self.assertIn("回执基础地址", schema)
        self.assertIn("回执图床 URL", schema)
        self.assertIn("完成标记提醒", schema)
        self.assertIn("完成标记查询间隔", schema)
        self.assertNotIn("取回文件查询状态", schema)
        self.assertIn('"probe"', schema)
        self.assertIn("测试功能", schema)
        self.assertIn("未备案域名", schema)
        self.assertNotIn('f"taskid: {short}"', text)


class CredentialTests(unittest.TestCase):
    SENTINEL_TOKEN = "sentinel-token-value"
    SENTINEL_KEY = "sentinel-api-key-value"

    def setUp(self):
        import gemini_client as gc

        self._gc = gc
        self._old_logger = gc.logger
        self.lines: list[str] = []

        class Capture:
            def __init__(self, sink: list[str]):
                self.sink = sink

            def info(self, *args, **kwargs):
                self.sink.append(" ".join(str(arg) for arg in args))

            warning = info
            error = info

        gc.logger = Capture(self.lines)

    def tearDown(self):
        self._gc.logger = self._old_logger

    def _assert_secret_hidden(self, *chunks: str) -> None:
        blob = "\n".join(chunks) + "\n" + "\n".join(self.lines)
        self.assertNotIn(self.SENTINEL_TOKEN, blob)
        self.assertNotIn(self.SENTINEL_KEY, blob)

    def test_webhook_host_accepts_https_and_rejects_unsafe_forms(self):
        self.assertEqual(
            webhook_upload_host("https://img.example.com/Webhook/upload"),
            "img.example.com",
        )
        self.assertEqual(
            webhook_upload_host("https://img.example.com:8443/hook"),
            "img.example.com",
        )
        bad_urls = [
            "http://img.example.com/hook",
            "https:///hook",
            "https://*/hook",
            "https://*.example.com/hook",
            "https://user:pass@img.example.com/hook",
            "https://img.example.com:abc/hook",
            "https://img.example.com:99999/hook",
            "not a url",
            "",
        ]
        for raw in bad_urls:
            with self.subTest(raw=raw):
                with self.assertRaises(GeminiClientError) as ctx:
                    webhook_upload_host(raw)
                if raw:
                    self.assertNotIn(raw, str(ctx.exception))
                self.assertEqual(str(ctx.exception), "图床上传地址无效。")

    def test_upload_instruction_does_not_ask_for_the_token_file(self):
        text = build_upload_instruction(
            names=["260101_result.md"],
            original_names=["result.md"],
            webhook="https://img.example.com/Webhook/upload",
            public_base="https://img.example.com",
            prefix="agysb",
        )
        self.assertNotIn("从 /workspace/upload.token 读取", text)
        self.assertIn("不要设置 Authorization", text)
        self.assertIn("不要读取 /workspace/upload.token", text)

    def test_sources_drop_reserved_token_paths(self):
        client = GeminiSandboxClient(api_keys=["k"])
        import json

        contents = json.dumps(
            [
                {"target": "/workspace/./upload.token", "content": self.SENTINEL_TOKEN},
                {"target": "upload.token", "content": self.SENTINEL_TOKEN},
                {"target": "/workspace//upload.token", "content": self.SENTINEL_TOKEN},
                {"target": "/workspace/ok.txt", "content": "hi"},
            ]
        )
        sources = client.build_sources_from_files(
            file_paths="E:/no-such-dir/upload.token",
            file_contents=contents,
        )
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["target"], "/workspace/ok.txt")
        self.assertNotIn(self.SENTINEL_TOKEN, str(sources))

    def test_credential_create_update_and_redaction(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.method == "POST":
                return httpx.Response(
                    409,
                    json={
                        "error": {
                            "code": "aborted",
                            "message": f"leak {self.SENTINEL_TOKEN} {self.SENTINEL_KEY}",
                        }
                    },
                )
            if request.method == "PATCH":
                return httpx.Response(200, json={"id": IMAGE_HOST_CREDENTIAL_ID})
            return httpx.Response(500, text=self.SENTINEL_TOKEN)

        client = ProbeClient(httpx.MockTransport(handler), api_keys=["other-key"])
        _run(
            client.ensure_bearer_credential(
                self.SENTINEL_KEY,
                IMAGE_HOST_CREDENTIAL_ID,
                self.SENTINEL_TOKEN,
            )
        )
        self.assertEqual([item.method for item in seen], ["POST", "PATCH"])
        self.assertEqual(seen[0].url.path, "/v1beta/credentials")
        self.assertEqual(seen[1].url.path, f"/v1beta/credentials/{IMAGE_HOST_CREDENTIAL_ID}")
        for item in seen:
            self.assertEqual(item.headers["x-goog-api-key"], self.SENTINEL_KEY)
            payload = json.loads(item.content)
            self.assertEqual(payload["type"], "bearer_token")
            self.assertEqual(payload["token"], self.SENTINEL_TOKEN)
            self.assertEqual(payload["header_name"], "Authorization")
            self.assertEqual(payload["prefix"], "Bearer")
        self.assertEqual(json.loads(seen[0].content)["id"], IMAGE_HOST_CREDENTIAL_ID)
        self.assertNotIn("id", json.loads(seen[1].content))
        self._assert_secret_hidden()

    def test_credential_create_accepts_200_and_201(self):
        for status in (200, 201):
            with self.subTest(status=status):
                def handler(request: httpx.Request, status=status) -> httpx.Response:
                    return httpx.Response(status, json={"id": IMAGE_HOST_CREDENTIAL_ID})

                client = ProbeClient(httpx.MockTransport(handler), api_keys=["k"])
                _run(client.ensure_bearer_credential("k", IMAGE_HOST_CREDENTIAL_ID, "tok"))

    def test_non_aborted_409_does_not_patch(self):
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            return httpx.Response(
                409,
                json={"error": {"code": "already_exists", "message": self.SENTINEL_TOKEN}},
            )

        client = ProbeClient(httpx.MockTransport(handler), api_keys=["other"])
        with self.assertRaises(GeminiClientError) as ctx:
            _run(
                client.ensure_bearer_credential(
                    self.SENTINEL_KEY,
                    IMAGE_HOST_CREDENTIAL_ID,
                    self.SENTINEL_TOKEN,
                )
            )
        self.assertEqual(methods, ["POST"])
        self.assertIn("HTTP 409", str(ctx.exception))
        self._assert_secret_hidden(
            str(ctx.exception),
            f"提交失败: {ctx.exception}",
            f"续接交互失败: {ctx.exception}",
        )
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_credential_failures_stay_sanitized(self):
        cases = {
            "patch": lambda request: (
                httpx.Response(
                    409,
                    json={"error": {"code": "aborted", "message": self.SENTINEL_TOKEN}},
                )
                if request.method == "POST"
                else httpx.Response(500, text=f"{self.SENTINEL_TOKEN} {self.SENTINEL_KEY}")
            ),
            "html": lambda request: httpx.Response(200, text=f"<html>{self.SENTINEL_TOKEN}</html>"),
            "redirect": lambda request: httpx.Response(
                302,
                headers={"Location": "https://example.invalid/next"},
                text=self.SENTINEL_TOKEN,
            ),
            "timeout": None,
        }

        def run_case(kind: str):
            def handler(request: httpx.Request) -> httpx.Response:
                if kind == "timeout":
                    raise httpx.TimeoutException(self.SENTINEL_TOKEN)
                result = cases[kind](request)
                if not isinstance(result, httpx.Response):
                    raise AssertionError(kind)
                return result

            client = ProbeClient(httpx.MockTransport(handler), api_keys=["other-key"])
            with self.assertRaises(GeminiClientError) as ctx:
                _run(
                    client.ensure_bearer_credential(
                        self.SENTINEL_KEY,
                        IMAGE_HOST_CREDENTIAL_ID,
                        self.SENTINEL_TOKEN,
                    )
                )
            self.assertNotIsInstance(ctx.exception, GeminiSubmitTimeoutError)
            self._assert_secret_hidden(
                str(ctx.exception),
                f"提交失败: {ctx.exception}",
                f"续接交互失败: {ctx.exception}",
            )
            self.assertTrue(ctx.exception.__suppress_context__)

        for kind in cases:
            with self.subTest(kind=kind):
                self.lines.clear()
                run_case(kind)

    def test_empty_key_does_not_call_or_fall_back(self):
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(201, json={"id": "x"})

        client = ProbeClient(httpx.MockTransport(handler), api_keys=["real-key"])
        with self.assertRaises(GeminiClientError) as ctx:
            _run(client.ensure_bearer_credential("  ", IMAGE_HOST_CREDENTIAL_ID, "tok"))
        self.assertFalse(called)
        self.assertIn("API Key 为空", str(ctx.exception))

    def test_key_filter_keeps_success_order_and_hides_secrets(self):
        def handler(request: httpx.Request) -> httpx.Response:
            key = request.headers["x-goog-api-key"]
            if key == "key-a":
                return httpx.Response(500, text=f"{self.SENTINEL_TOKEN} {self.SENTINEL_KEY}")
            if key == "key-b":
                return httpx.Response(201, json={"id": IMAGE_HOST_CREDENTIAL_ID})
            return httpx.Response(500, text="unexpected")

        client = ProbeClient(
            httpx.MockTransport(handler),
            api_keys=["key-a", "key-b", "key-c"],
        )
        ready = _run(
            client.ensure_credentials_for_keys(
                ["key-a", "key-b"],
                IMAGE_HOST_CREDENTIAL_ID,
                self.SENTINEL_TOKEN,
            )
        )
        self.assertEqual(ready, ["key-b"])
        self._assert_secret_hidden()

        def fail_all(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text=self.SENTINEL_TOKEN)

        failing = ProbeClient(httpx.MockTransport(fail_all), api_keys=["key-a", "key-b"])
        with self.assertRaises(GeminiClientError) as ctx:
            _run(
                failing.ensure_credentials_for_keys(
                    ["key-a", "key-b"],
                    IMAGE_HOST_CREDENTIAL_ID,
                    self.SENTINEL_TOKEN,
                )
            )
        self._assert_secret_hidden(str(ctx.exception))

    def test_interaction_failover_stays_inside_prepared_keys(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            key = request.headers["x-goog-api-key"]
            seen.append(key)
            if key == "key-a":
                return httpx.Response(429, json={"error": {"message": "rate"}})
            return httpx.Response(200, json={"id": "task", "status": "in_progress"})

        client = ProbeClient(
            httpx.MockTransport(handler),
            api_keys=["key-a", "key-b", "key-c"],
        )
        payload = {"input": "hi"}
        _data, used = _run(client.create_interaction(payload, candidate_keys=["key-a", "key-b"]))
        self.assertEqual(seen, ["key-a", "key-b"])
        self.assertEqual(used, "key-b")
        seen.clear()
        with self.assertRaises(GeminiClientError):
            _run(client.create_interaction(payload, api_key="key-a"))
        self.assertEqual(seen, ["key-a"])


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


class ReceiptFormatTests(unittest.TestCase):
    @staticmethod
    def _helpers() -> dict:
        text = MAIN.read_text(encoding="utf-8")
        tree = ast.parse(text)
        wanted = {
            "workspace_product_paths",
            "swap_url_origin",
            "placement_instruction",
            "ensure_result_md",
            "completed_marker_name",
            "stamped_completed_path",
            "is_completed_marker_path",
            "completed_marker_instruction",
            "file_poll_notice",
            "file_poll_should_stop",
            "_output_file_names",
            "_as_str",
        }
        chunks: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in wanted:
                segment = ast.get_source_segment(text, node)
                if segment:
                    chunks.append(segment)
        namespace: dict = {}
        exec(
            "from typing import Any\nfrom urllib.parse import urlsplit, urlunsplit\n"
            "from datetime import datetime\n\n"
            "RUNNING_STATUS = frozenset({'in_progress', 'queued'})\n"
            "TERMINAL_STATUS = frozenset({"
            "'completed', 'failed', 'cancelled', 'incomplete', "
            "'budget_exceeded', 'requires_action'})\n\n"
            + "\n\n".join(chunks),
            namespace,
        )
        namespace["COMPLETED_MARKER_SUFFIX"] = ".completed"
        namespace["_submit_stamp"] = lambda: "260928153045"
        return namespace

    def test_workspace_path_uses_stamped_filename(self):
        helpers = self._helpers()
        self.assertEqual(
            helpers["workspace_product_paths"](["260927153045_result.md"]),
            ["/workspace/260927153045_result.md"],
        )

    def test_receipt_base_replaces_origin_only(self):
        helpers = self._helpers()
        swap = helpers["swap_url_origin"]
        url = "https://real.example/agysb/260927153045_result.md?x=1"
        self.assertEqual(swap(url, ""), url)
        self.assertEqual(
            swap(url, "https://spare.example"),
            "https://spare.example/agysb/260927153045_result.md?x=1",
        )
        self.assertEqual(
            swap(url, "https://spare.example/ignored"),
            "https://spare.example/agysb/260927153045_result.md?x=1",
        )
        self.assertEqual(
            swap(url, "spare.example"),
            "https://spare.example/agysb/260927153045_result.md?x=1",
        )

    def test_placement_tells_sandbox_to_keep_md_for_this_round(self):
        helpers = self._helpers()
        place = helpers["placement_instruction"]
        md = place(["/workspace/260927153045_result.md"])
        self.assertIn("【产物放置要求】", md)
        self.assertIn("/workspace/260927153045_result.md", md)
        self.assertIn("不要把上一轮已有报告复制后交差", md)
        png = place(["/workspace/260927153045_result.png"])
        self.assertNotIn("不要把上一轮已有报告复制后交差", png)
        self.assertEqual(place([]), "")

    def test_png_still_adds_one_result_md(self):
        helpers = self._helpers()
        ensure = helpers["ensure_result_md"]
        self.assertEqual(ensure(""), "result.md")
        self.assertEqual(ensure("result.png"), "result.png,result.md")
        self.assertEqual(ensure("result.png,result.html"), "result.png,result.html,result.md")
        self.assertEqual(ensure("result.md"), "result.md")
        self.assertEqual(ensure("notes.md,result.md"), "notes.md,result.md")

    def test_poll_watches_only_this_round_completed_marker(self):
        helpers = self._helpers()
        self.assertEqual(
            helpers["completed_marker_name"]("260928153045"),
            "260928153045.completed",
        )
        self.assertEqual(
            helpers["stamped_completed_path"]("260928153045"),
            "/workspace/260928153045.completed",
        )
        # 与 result.md 用同一时间戳，工具输出路径差一个后缀
        self.assertEqual(
            helpers["stamped_completed_path"]("260928153045"),
            "/workspace/260928153045_result.md".replace("_result.md", ".completed"),
        )
        self.assertEqual(helpers["file_poll_notice"]("0001"), "任务 0001 可能已经完成，请使用 /agr 0001 取回")

    def test_completed_marker_path_filter(self):
        helpers = self._helpers()
        is_marker = helpers["is_completed_marker_path"]
        self.assertTrue(is_marker("/workspace/260928153045.completed"))
        self.assertTrue(is_marker("workspace/260928153045.COMPLETED"))
        self.assertFalse(is_marker("/workspace/260928153045_result.md"))
        self.assertFalse(is_marker(""))

    def test_completed_marker_instruction_text(self):
        helpers = self._helpers()
        text = helpers["completed_marker_instruction"]("260928153045")
        self.assertIn("完成所有任务后请在工作空间创建文件 260928153045.completed的空文件,此项不需要汇报。", text)
        self.assertTrue(text.startswith("\n\n"))

    def test_in_progress_does_not_stop_completed_poll(self):
        helpers = self._helpers()
        stop = helpers["file_poll_should_stop"]
        self.assertFalse(stop("in_progress"))
        self.assertFalse(stop("IN_PROGRESS"))
        self.assertFalse(stop("queued"))
        self.assertFalse(stop("unknown"))
        self.assertFalse(stop(""))
        self.assertTrue(stop("completed"))
        self.assertTrue(stop("failed"))
        self.assertTrue(stop("cancelled"))
        self.assertTrue(stop("incomplete"))


class LatestRoundTests(unittest.TestCase):
    @staticmethod
    def _pick():
        text = MAIN.read_text(encoding="utf-8")
        tree = ast.parse(text)
        wanted = {
            "_as_str",
            "_now",
            "_parse_iso",
            "_parse_short_int",
            "_index_item_is_round",
            "pick_latest_indexed_short",
        }
        chunks: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in wanted:
                segment = ast.get_source_segment(text, node)
                if segment:
                    chunks.append(segment)
        namespace: dict = {}
        exec(
            "import re\n"
            "from datetime import datetime\n"
            "SHORT_ID_WIDTH = 4\n"
            "CONTINUE_SHORT_TIME_WIDTH = 6\n"
            "CONTINUE_SHORT_RE = re.compile(r'^(\\d+)_(\\d{6})$')\n\n"
            + "\n\n".join(chunks),
            namespace,
        )
        return namespace["pick_latest_indexed_short"]

    def test_side_rows_do_not_outrank_a_real_round(self):
        pick = self._pick()
        sandbox = "env-1"
        index = {
            "0006": {
                "sandbox_id": sandbox,
                "task_id": "task-new",
                "recorded_at": "2026-09-28T18:47:08+08:00",
                "expected_paths": "/workspace/260928184708_result.md",
                "retrieved": "1",
                "last_status": "completed",
            },
            "0014": {
                "sandbox_id": sandbox,
                "task_id": "task-old",
                "recorded_at": "2026-09-28T18:47:10+08:00",
                "retrieved": "1",
                "last_status": "completed",
            },
        }
        self.assertEqual(pick(index, sandbox), "0006")

    def test_without_round_rows_newest_stamp_still_wins(self):
        pick = self._pick()
        sandbox = "env-1"
        index = {
            "0001": {
                "sandbox_id": sandbox,
                "recorded_at": "2026-09-28T18:00:00+08:00",
            },
            "0002": {
                "sandbox_id": sandbox,
                "recorded_at": "2026-09-28T19:00:00+08:00",
            },
        }
        self.assertEqual(pick(index, sandbox), "0002")
        self.assertEqual(pick(index, "missing"), "")

    def test_continue_gate_uses_the_followed_task_and_reply_does_not_allocate(self):
        text = MAIN.read_text(encoding="utf-8")
        self.assertIn(
            "gate_short = self._short_for_task(task_id) or overwrite_short",
            text,
        )
        self.assertNotIn(
            "return self._record_short(receipt.task_id, receipt.sandbox_id)",
            text,
        )
        self.assertIn("allocate=False", text)

    def test_continue_record_never_mints_a_short(self):
        text = MAIN.read_text(encoding="utf-8")
        tree = ast.parse(text)
        segment = ""
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, ast.FunctionDef) and child.name == "_record_short":
                        segment = ast.get_source_segment(text, child) or ""
        self.assertTrue(segment)
        namespace: dict = {"_as_str": lambda value: "" if value is None else str(value).strip()}
        exec(textwrap.dedent(segment), namespace)
        record = namespace["_record_short"]

        class Box:
            def __init__(self):
                self._short_index = {
                    "0006": {"task_id": "old", "sandbox_id": "env", "key": "k"},
                }
                self.allocated = False

            def _find_key_for(self, **_kwargs):
                return ""

            def _short_for_task(self, task_id):
                for short, item in self._short_index.items():
                    if item.get("task_id") == task_id:
                        return short
                return ""

            def _short_item(self, task_id, sandbox_id, *, key="", recorded_at=""):
                item = {"task_id": task_id, "sandbox_id": sandbox_id}
                if key:
                    item["key"] = key
                if recorded_at:
                    item["recorded_at"] = recorded_at
                return item

            def _copy_retrieve_meta(self, _src, _dest):
                return None

            def _save_short_index(self):
                return None

            def _trim_short_index(self):
                raise AssertionError("continue trimmed the short index")

            def _alloc_submit_short(self):
                self.allocated = True
                raise AssertionError("continue allocated a short")

        box = Box()
        got = record(
            box,
            "new-task",
            "env",
            previous_task_id="old",
            overwrite_short="0006",
            allocate=False,
        )
        self.assertEqual(got, "0006")
        self.assertEqual(box._short_index["0006"]["task_id"], "new-task")
        self.assertEqual(list(box._short_index), ["0006"])
        self.assertFalse(box.allocated)

        empty = Box()
        empty._short_index = {}
        missing = record(empty, "new-task", "env", allocate=False)
        self.assertEqual(missing, "")
        self.assertEqual(empty._short_index, {})
        self.assertFalse(empty.allocated)


class CompletedNotifyOriginTests(unittest.TestCase):
    """完成标记提示按提交来源区分：指令照常，工具默认静默。"""

    @staticmethod
    def _gate():
        """抽出 _completed_notify_enabled_for，用桩 config 跑真实判定。"""
        text = MAIN.read_text(encoding="utf-8")
        tree = ast.parse(text)
        wanted = {"_completed_notify_enabled_for"}
        segments = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, ast.FunctionDef) and child.name in wanted:
                        segment = ast.get_source_segment(text, child)
                        if segment:
                            segments.append(textwrap.dedent(segment))
        assert segments, "_completed_notify_enabled_for not found in main.py"
        namespace: dict = {
            "_as_str": lambda v: "" if v is None else str(v).strip(),
            "_as_bool": lambda v, d: (
                str(v).strip().lower() in {"1", "true", "yes", "on"} if v is not None else d
            ),
        }
        exec("\n\n".join(segments), namespace)
        return namespace["_completed_notify_enabled_for"]

    def _stub(self, **probe):
        class Stub:
            def _setting(self, group, key, legacy=None, default=None):
                assert group == "probe"
                return probe.get(key, default)

        return Stub()

    def test_command_origin_always_notifies(self):
        gate = self._gate()
        self.assertTrue(gate(self._stub(), "command"))
        # 即使开关关着也发，指令是用户自己打的
        self.assertTrue(gate(self._stub(completed_notify=False), "command"))

    def test_tool_origin_stays_silent_by_default(self):
        gate = self._gate()
        # 外层模型自主提交：默认不往会话里弹提示
        self.assertFalse(gate(self._stub(), "tool"))
        # 开关显式关同样不发
        self.assertFalse(gate(self._stub(completed_notify=False), "tool"))
        # 读到空或缺省都回落静默
        self.assertFalse(gate(self._stub(completed_notify=None), "tool"))

    def test_tool_origin_notifies_only_when_switch_on(self):
        gate = self._gate()
        self.assertTrue(gate(self._stub(completed_notify=True), "tool"))
        self.assertTrue(gate(self._stub(completed_notify="true"), "tool"))

    def test_unknown_origin_treated_as_tool(self):
        # 来源字段缺失或未知值时按更保守的工具提交处理
        gate = self._gate()
        self.assertFalse(gate(self._stub(), ""))
        self.assertFalse(gate(self._stub(), "cron"))
        self.assertTrue(gate(self._stub(completed_notify=True), ""))

    def test_notify_routes_by_origin(self):
        """_notify_file_ready 只对允许的来源调用 send_message。"""
        text = MAIN.read_text(encoding="utf-8")
        tree = ast.parse(text)
        segment = ""
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if (
                        isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and child.name == "_notify_file_ready"
                    ):
                        segment = ast.get_source_segment(text, child) or ""
        assert segment
        # 指令来源仍然 @提交人
        self.assertIn('if origin == "command" and sender_id:', segment)
        self.assertIn("self._completed_notify_enabled_for(origin)", segment)


if __name__ == "__main__":
    unittest.main()
