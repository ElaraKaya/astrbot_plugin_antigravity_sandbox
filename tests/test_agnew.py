"""Exercise the real draft plugin in AstrBot's runtime, with only HTTP mocked.

The import-time migration sees no existing config; the plugin constructor
is not invoked. All state writes go to a TemporaryDirectory, never real config.
Run in the astrbot container with python -B -m unittest discover -s <tests>.
"""
from __future__ import annotations

import importlib.util
import asyncio
import copy
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from gemini_client import GeminiClientError, GeminiSandboxClient  # noqa: E402


class _ClientStub:
    """删除路径用：可控的远端状态 + 删除记录，其余一律失败。"""

    def __init__(self, delete_exc=None, status="completed", status_exc=None):
        self.deleted: list[str] = []
        self.cancelled: list[str] = []
        self.delete_exc = delete_exc
        self.status = status
        self.status_exc = status_exc

    async def delete_interaction(self, task_id, *, api_key=None):
        if self.delete_exc is not None:
            raise self.delete_exc
        self.deleted.append(task_id)

    async def cancel_interaction(self, task_id, *, api_key=None):
        self.cancelled.append(task_id)

    async def get_interaction_status(self, task_id, *, api_key=None):
        if self.status_exc is not None:
            raise self.status_exc
        return self.status

    async def get_environment(self, env_id, *, api_key=None):
        raise AssertionError("unexpected environment fetch")


class _AdoptClient:
    """收养远端沙盒用：先探测 Key 可达性，再固定该 Key 提交。"""

    def __init__(self, test, reachable: bool = True):
        self.api_keys = ["key-default", "key-bound"]
        self.submit_background = True
        self.agent_label = "agent"
        self.model_label = "model"
        self.agent_warning = ""
        self.reachable = reachable
        self.probed_keys: list[str] = []
        self.used_keys: list[str] = []

    async def get_environment(self, env_id, *, api_key=None):
        self.probed_keys.append(api_key)
        if self.reachable and api_key == "key-bound":
            return {"id": env_id, "status": "active"}
        raise RuntimeError("not reachable")

    def build_create_payload(self, **kwargs):
        payload = {
            "input": kwargs["prompt"],
            "environment": kwargs["sandbox_id"],
            "background": True,
            "store": True,
        }
        if not kwargs.get("new_session"):
            payload["previous_interaction_id"] = kwargs.get("previous_task_id")
        return payload

    async def create_interaction(self, payload, api_key=None, candidate_keys=None, on_storage_quota=None):
        self.used_keys.append(api_key)
        return {"id": "task-new", "environment_id": payload["environment"], "status": ""}, api_key

    def build_sources_from_files(self, file_paths, file_contents):
        return []


def load_plugin_definition():
    import astrbot.api  # noqa: F401

    path = ROOT / "main.py"
    spec = importlib.util.spec_from_file_location("sandbox_agnew_behavior_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    # Import the unchanged whole module. Migration sees no existing config,
    # so its module-level hook cannot write outside the test directory.
    with patch.object(Path, "is_file", return_value=False):
        spec.loader.exec_module(module)
    return module


class AgNewBehaviorTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin_definition()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agnew_test_")
        self.addCleanup(self.temp.cleanup)
        self.requests = []
        self.status = 200
        self.response = {"id": "task-new", "environment_id": "env-shared"}

        def transport(request):
            self.requests.append(request)
            if request.method == "GET" and "/environments/" in request.url.path:
                if self.status == 404:
                    return httpx.Response(404, json={"error": {"message": "not found"}})
                return httpx.Response(200, json={"id": "env-shared", "status": "active"})
            if request.method == "POST":
                return httpx.Response(self.status, json=self.response)
            raise AssertionError(f"Unexpected HTTP: {request.method} {request.url}")

        self.transport = httpx.MockTransport(transport)

        class Client(GeminiSandboxClient):
            def _client_kwargs(inner, timeout, api_key=None, **kwargs):
                options = super()._client_kwargs(timeout, api_key, **kwargs)
                options["transport"] = self.transport
                return options

        self.client = Client(api_keys=["key-default", "key-bound"])
        plugin = object.__new__(self.module.AntigravitySandboxPlugin)
        plugin.config = {
            "model": {"gemini_api_keys": ["key-default", "key-bound"]},
            "image_host": {"enabled": False},
            "environment": {"auto_cleanup": True},
            "receipt": {"auto_retrieve": True, "file_status_poll": True},
        }
        plugin._data_dir = Path(self.temp.name)
        plugin._short_index = {
            "0001": {
                "task_id": "task-old",
                "sandbox_id": "env-shared",
                "key": self.module._key_fingerprint("key-bound"),
                "recorded_at": "2026-10-01T00:00:00+08:00",
                "last_status": "completed",
                "expected_paths": "/workspace/old_result.md",
            }
        }
        plugin._key_mapping = {
            "task-old": self.module._key_fingerprint("key-bound"),
            "env-shared": self.module._key_fingerprint("key-bound"),
        }
        plugin._submit_key_fp = ""
        plugin._env_meta = {}
        plugin._pending_retrieve = {}
        plugin._file_polls = {}
        plugin._cleanup_lock = asyncio.Lock()
        plugin._client = lambda: self.client
        plugin._sweep_environments = AsyncMock(side_effect=AssertionError("must not sweep reused env"))
        self.plugin = plugin
        self.original = copy.deepcopy(plugin._short_index["0001"])

    async def submit(self, **kwargs):
        return await self.plugin._do_submit(
            prompt="Analyze the existing files", reuse_task_ref="0001", **kwargs
        )

    async def test_reuse_creates_new_short_and_persists_binding_without_old_history(self):
        receipt = await self.submit()
        self.assertTrue(receipt.ok, receipt.text)
        self.assertEqual(self.plugin._short_index["0001"], self.original)
        self.assertEqual(self.plugin._short_for_task("task-new"), "0002")
        row = self.plugin._short_index["0002"]
        self.assertEqual(row["sandbox_id"], "env-shared")
        self.assertEqual(row["session_short"], "0002")
        self.assertEqual(row["key"], self.module._key_fingerprint("key-bound"))
        self.assertEqual([r.method for r in self.requests], ["GET", "POST"])
        self.assertTrue(all(r.headers["x-goog-api-key"] == "key-bound" for r in self.requests))
        payload = json.loads(self.requests[-1].content)
        self.assertEqual(payload["environment"], "env-shared")
        self.assertNotIn("previous_interaction_id", payload)
        self.assertTrue(payload["background"])
        self.assertTrue(payload["store"])
        self.assertIn("result.md", payload["input"])
        for filename in ("task_index.json", "task_keys.json"):
            saved = json.loads((self.plugin._data_dir / filename).read_text(encoding="utf-8"))
            self.assertTrue(saved)
            self.assertNotIn("key-bound", json.dumps(saved))
        self.assertIn("0002", self.plugin._pending_retrieve)
        self.assertIn("0002", self.plugin._file_polls)
        self.plugin._sweep_environments.assert_not_awaited()

    async def test_invalid_short_is_rejected_without_requests(self):
        result = await self.plugin._do_submit(prompt="hi", reuse_task_ref="9999")
        self.assertFalse(result.ok)
        self.assertEqual(self.requests, [])

    async def test_removed_explicit_binding_never_falls_back_to_other_task_key(self):
        self.plugin.config["model"]["gemini_api_keys"] = ["key-default"]
        self.plugin._key_mapping["env-shared"] = self.module._key_fingerprint("key-default")
        result = await self.submit()
        self.assertFalse(result.ok)
        self.assertIn("绑定的 API Key 不可用", result.text)
        self.assertEqual(self.requests, [])

    async def test_missing_environment_blocks_before_interaction_and_leaves_index(self):
        self.status = 404
        result = await self.submit()
        self.assertFalse(result.ok)
        self.assertEqual([r.method for r in self.requests], ["GET"])
        self.assertEqual(self.plugin._short_index, {"0001": self.original})
        self.assertEqual(self.plugin._file_polls, {})

    async def test_fixed_key_http_errors_do_not_switch_keys_or_allocate(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.requests.clear()
                self.status = status
                result = await self.submit()
                self.assertFalse(result.ok)
                self.assertEqual([r.method for r in self.requests], ["GET", "POST"])
                self.assertTrue(all(r.headers["x-goog-api-key"] == "key-bound" for r in self.requests))
                self.assertEqual(self.plugin._short_index, {"0001": self.original})

    async def test_full_bound_key_does_not_use_an_idle_other_key(self):
        for i in range(4):
            self.plugin._short_index[str(10 + i)] = {
                "task_id": f"running-{i}", "sandbox_id": f"env-{i}",
                "key": self.module._key_fingerprint("key-bound"), "last_status": "in_progress",
            }
        self.plugin._refresh_in_progress_cache = AsyncMock()
        result = await self.submit()
        self.assertFalse(result.ok)
        self.assertIn("无空余", result.text)
        self.assertFalse(any(r.method == "POST" for r in self.requests))

    async def test_reused_response_must_not_overwrite_an_existing_task(self):
        for response in ({"id": "task-old"}, {"id": "task-new", "environment_id": "other-env"}, {}):
            with self.subTest(response=response):
                self.response = response
                result = await self.submit()
                self.assertFalse(result.ok)
                self.assertEqual(self.plugin._short_index, {"0001": self.original})

    async def test_new_session_without_environment_id_uses_verified_environment(self):
        self.response = {"id": "task-new"}
        result = await self.submit()
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.sandbox_id, "env-shared")

    async def test_new_session_rejects_sources_instead_of_remounting_environment(self):
        result = await self.submit(file_contents='[{"target":"a.txt","content":"hi"}]')
        self.assertFalse(result.ok)
        self.assertEqual(self.requests, [])

    async def test_new_session_rejects_file_paths_sources(self):
        result = await self.submit(file_paths="/tmp/a.png")
        self.assertFalse(result.ok)
        self.assertEqual(self.requests, [])

    async def test_reuse_submit_sends_chat_images_as_multimodal_input(self):
        images = [
            {"name": "shot.png", "mime_type": "image/png", "data": "QUJD"},
            {"name": "pic.jpg", "mime_type": "image/jpeg", "data": "REVG"},
        ]
        receipt = await self.submit(images=images)
        self.assertTrue(receipt.ok, receipt.text)
        self.assertEqual(receipt.image_count, 2)
        self.assertIn("已随任务直接发送图片 2 张", receipt.text)
        payload = json.loads(self.requests[-1].content)
        # input 变成 Content 数组：text 分节 + 两个 image 分节
        self.assertEqual(payload["input"][0]["type"], "text")
        self.assertEqual(
            payload["input"][1],
            {"type": "image", "mime_type": "image/png", "data": "QUJD"},
        )
        self.assertEqual(
            payload["input"][2],
            {"type": "image", "mime_type": "image/jpeg", "data": "REVG"},
        )
        # 复用环境依旧不挂 sources，也不继承对话
        env = payload["environment"]
        self.assertNotIn("sources", env if isinstance(env, dict) else {})
        self.assertNotIn("previous_interaction_id", payload)

    async def test_continuing_one_session_never_follows_other_shared_environment_session(self):
        await self.submit()
        old = self.plugin._follow_latest_on_sandbox("task-old", "env-shared", "key-bound")
        new = self.plugin._follow_latest_on_sandbox("task-new", "env-shared", "key-bound")
        self.assertEqual(old[0], "task-old")
        self.assertEqual(new[0], "task-new")
        self.plugin._record_key("key-bound", task_id="task-new-next", sandbox_id="env-shared",
                                previous_task_id="task-new", overwrite_short="0002", allocate=False)
        self.assertEqual(self.plugin._short_index["0002"]["session_short"], "0002")
        self.assertEqual(self.plugin._short_index["0001"], self.original)

    async def test_page_has_two_rows_per_environment_and_delete_cleans_both_polls(self):
        await self.submit()
        rows = self.plugin._web_collect_local_tasks()
        self.assertEqual({r["short"] for r in rows}, {"0001", "0002"})
        self.assertTrue(all(r["sandbox_id"] == "env-shared" for r in rows))
        self.assertTrue(all("key" not in r for r in rows))
        self.plugin._file_polls["0001"] = {"task_id": "task-old"}
        self.plugin._forget_sandbox("env-shared")
        self.assertEqual(self.plugin._short_index, {})
        self.assertEqual(self.plugin._file_polls, {})
        self.assertEqual(self.plugin._pending_retrieve, {})

    async def test_completed_session_does_not_hide_another_running_session(self):
        await self.submit()
        self.plugin._touch_sandbox("env-shared", status="completed")
        self.assertEqual(self.plugin._env_meta["env-shared"]["status"], "in_progress")

    async def test_web_reference_keeps_binding_and_rejects_mismatched_environment(self):
        with self.assertRaises(ValueError):
            self.plugin._web_resolve_sandbox_key(sandbox_id="other-env", ref="0001")
        resolved = self.plugin._web_resolve_sandbox_key(sandbox_id="env-shared", ref="0001")
        self.assertEqual(resolved, ("env-shared", "0001", "key-bound"))

    async def test_index_full_never_drops_old_rows_for_a_new_session(self):
        self.plugin._short_index = {
            f"{i:04d}": {
                "task_id": f"t{i}",
                "sandbox_id": f"env{i}",
                "key": self.module._key_fingerprint("key-bound"),
                "recorded_at": "2026-10-01T00:00:00+08:00",
            }
            for i in range(self.module.SHORT_INDEX_LIMIT)
        }
        before = {k: copy.deepcopy(v) for k, v in self.plugin._short_index.items()}
        result = await self.submit()
        self.assertFalse(result.ok)
        self.assertIn("索引", result.text)
        self.assertEqual(self.plugin._short_index, before)
        self.assertEqual(self.requests, [])

    async def test_stale_long_id_in_multi_session_environment_is_refused(self):
        await self.submit()
        stale = self.plugin._follow_latest_on_sandbox("unknown-old-id", "env-shared", "key-bound")
        self.assertEqual(stale[0], "")
        self.assertEqual(stale[3], "")

    async def test_web_reference_rejects_removed_binding_even_with_other_mapping(self):
        self.plugin.config["model"]["gemini_api_keys"] = ["key-default", "key-rebound"]
        self.plugin._key_mapping["env-shared"] = self.module._key_fingerprint("key-default")
        self.plugin._short_index["0001"] = dict(
            self.plugin._short_index["0001"], key=""
        )
        with self.assertRaises(ValueError):
            self.plugin._web_resolve_sandbox_key(sandbox_id="env-shared", ref="0001")

    async def test_reuse_rejects_when_shared_env_has_a_running_sibling_at_full_capacity(self):
        for i in range(self.module.DEFAULT_IN_PROGRESS_PER_KEY):
            self.plugin._short_index[f"9{i:03d}"] = {
                "task_id": f"busy{i}",
                "sandbox_id": "env-shared",
                "key": self.module._key_fingerprint("key-bound"),
                "last_status": "in_progress",
            }
        self.plugin._refresh_in_progress_cache = AsyncMock()
        result = await self.submit()
        self.assertFalse(result.ok)
        self.assertIn("无空余", result.text)
        self.assertFalse(any(r.method == "POST" for r in self.requests))
        self.assertNotIn("0002", self.plugin._short_index)

    async def test_agget_without_short_never_picks_the_wrong_session(self):
        # 单沙盒单会话：省略编号可用兜底短号
        self.assertEqual(
            self.plugin._single_latest_short_for_sandbox(), "0001"
        )
        # 同沙盒出现第二个独立会话后，兜底仍然唯一（全部指向同一沙盒）
        await self.submit()
        self.assertEqual(
            self.plugin._single_latest_short_for_sandbox(), "0002"
        )
        # 出现第二个沙盒时省略编号必须失效，要求显式写短号
        self.plugin._short_index["0009"] = {
            "task_id": "task-other",
            "sandbox_id": "env-other",
            "key": self.module._key_fingerprint("key-bound"),
        }
        self.assertEqual(self.plugin._single_latest_short_for_sandbox(), "")

    async def test_explicit_zero_ttl_and_keep_recent_are_not_swallowed(self):
        self.plugin.config["environment"]["idle_ttl_hours"] = 0
        self.plugin.config["environment"]["keep_recent"] = 0
        self.assertEqual(self.plugin._env_idle_ttl_hours(), 0.0)
        self.assertEqual(self.plugin._env_keep_recent(), 0)
        # 缺失（None/空串）仍回退默认，不误判为 0
        self.plugin.config["environment"]["idle_ttl_hours"] = None
        self.plugin.config["environment"]["keep_recent"] = ""
        self.assertEqual(
            self.plugin._env_idle_ttl_hours(),
            float(self.module.DEFAULT_IDLE_TTL_HOURS),
        )
        self.assertEqual(self.plugin._env_keep_recent(), self.module.DEFAULT_KEEP_RECENT)
        # 非数字不炸，回退默认
        self.plugin.config["environment"]["idle_ttl_hours"] = "abc"
        self.plugin.config["environment"]["keep_recent"] = "x"
        self.assertEqual(
            self.plugin._env_idle_ttl_hours(),
            float(self.module.DEFAULT_IDLE_TTL_HOURS),
        )
        self.assertEqual(self.plugin._env_keep_recent(), self.module.DEFAULT_KEEP_RECENT)

    async def test_submit_background_config_reaches_payload(self):
        # 默认 True：payload 带 background + store
        receipt = await self.submit()
        self.assertTrue(receipt.ok, receipt.text)
        payload = json.loads(self.requests[-1].content)
        self.assertTrue(payload.get("background"))
        self.assertTrue(payload.get("store"))
        # 显式关闭：submit_background 必须生效而不是被业务入口覆盖
        self.client.submit_background = False
        self.requests.clear()
        self.plugin._short_index.pop("0002", None)
        receipt = await self.submit()
        self.assertTrue(receipt.ok, receipt.text)
        payload = json.loads(self.requests[-1].content)
        self.assertNotIn("background", payload)
        self.assertNotIn("store", payload)

    async def test_invalid_base64_inline_falls_back_to_length_without_crash(self):
        result = await self.plugin._do_submit(
            prompt="inline binary",
            reuse_task_ref="0001",
            file_contents='[{"target":"a.bin","content":"!!!not-base64!!!","encoding":"base64"}]',
        )
        self.assertFalse(result.ok)
        self.assertIn("不挂载", result.text)
        # 直接走客户端构造：坏 base64 不再抛异常，按原串长度参与大小检查
        sources = self.client.build_sources_from_files(
            None, '[{"target":"a.bin","content":"@@@","encoding":"base64"}]'
        )
        self.assertEqual(sources[0]["encoding"], "base64")
        self.assertEqual(sources[0]["content"], "@@@")

    async def test_session_status_records_status_and_checked_at(self):
        class _StatusClient:
            async def get_interaction_status(self, task_id, *, api_key=None):
                assert task_id == "task-old"
                assert api_key == "key-bound"
                return "completed"

        self.plugin._client = lambda: _StatusClient()
        ok, status, message = await self.plugin._session_status("0001")
        self.assertTrue(ok, message)
        self.assertEqual(status, "completed")
        row = self.plugin._short_index["0001"]
        self.assertEqual(row["last_status"], "completed")
        self.assertTrue(row.get("last_checked_at"))
        saved = json.loads((self.plugin._data_dir / "task_index.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["0001"]["last_status"], "completed")
        self.assertTrue(saved["0001"].get("last_checked_at"))

    async def test_session_status_rejects_unknown_ref_without_index_change(self):
        ok, status, message = await self.plugin._session_status("9999")
        self.assertFalse(ok)
        self.assertEqual(status, "")
        self.assertIn("未找到", message)

    async def test_delete_session_deletes_running_directly(self):
        # 运行中也直接删（不再取消——实测 :cancel 端点不存在，DELETE 本身就能删运行中的交互）
        for running in ("in_progress", "queued", "requires_action"):
            with self.subTest(running=running):
                self.plugin._short_index["0001"] = copy.deepcopy(self.original)
                self.plugin._short_index["0001"]["last_status"] = "completed"
                client = _ClientStub(status=running)
                self.plugin._client = lambda: client
                ok, message = await self.plugin._delete_session("0001")
                self.assertTrue(ok, message)
                self.assertEqual(client.cancelled, [])
                self.assertEqual(client.deleted, ["task-old"])
                self.assertIn(f"原状态 {running}，已直接删除", message)
                self.assertNotIn("0001", self.plugin._short_index)

    async def test_delete_session_remote_in_progress_beats_stale_local_completed(self):
        # 远端仍在跑、本地是过期 completed：同样删，不受陈旧本地状态阻拦。
        self.plugin._short_index["0001"]["last_status"] = "completed"
        client = _ClientStub(status="in_progress")
        self.plugin._client = lambda: client
        ok, message = await self.plugin._delete_session("0001")
        self.assertTrue(ok, message)
        self.assertEqual(client.deleted, ["task-old"])
        self.assertNotIn("0001", self.plugin._short_index)

    async def test_delete_session_unknown_status_still_deletes(self):
        # 远端状态查不到（非 404 失败）时也直接删，不再要求先「获取状态」。
        self.plugin._short_index["0001"]["last_status"] = ""
        client = _ClientStub(status_exc=GeminiClientError("gateway 500"))
        self.plugin._client = lambda: client
        ok, message = await self.plugin._delete_session("0001")
        self.assertTrue(ok, message)
        self.assertEqual(client.deleted, ["task-old"])
        self.assertIn("状态未知，直接删除", message)
        self.assertNotIn("0001", self.plugin._short_index)

    async def test_delete_session_remote_failure_still_cleans_local(self):
        # 远端删除失败也清本地记录，但如实告知远端可能还在跑。
        client = _ClientStub(
            status="in_progress",
            delete_exc=GeminiClientError("boom", status_code=500),
        )
        self.plugin._client = lambda: client
        ok, message = await self.plugin._delete_session("0001")
        self.assertTrue(ok, message)
        self.assertIn("本地记录", message)
        self.assertIn("可能仍在沙盒中运行", message)
        self.assertNotIn("0001", self.plugin._short_index)

    async def test_delete_session_cleans_only_that_session(self):
        await self.submit()
        client = _ClientStub(status="completed")
        self.plugin._client = lambda: client
        ok, message = await self.plugin._delete_session("0002")
        self.assertTrue(ok, message)
        self.assertEqual(client.deleted, ["task-new"])
        self.assertNotIn("0002", self.plugin._short_index)
        self.assertIn("0001", self.plugin._short_index)
        self.assertEqual(self.plugin._short_index["0001"], self.original)
        self.assertEqual(self.plugin._key_mapping.get("task-old"), self.module._key_fingerprint("key-bound"))

    async def test_delete_session_404_only_cleans_local(self):
        from gemini_client import GeminiClientError

        client = _ClientStub(
            status_exc=GeminiClientError("gone", status_code=404),
            delete_exc=GeminiClientError("gone", status_code=404),
        )
        self.plugin._client = lambda: client
        ok, message = await self.plugin._delete_session("0001")
        self.assertTrue(ok, message)
        self.assertIn("远端交互已不存在", message)
        self.assertNotIn("0001", self.plugin._short_index)

    async def test_sweep_sessions_keeps_newest_per_sandbox_and_skips_running(self):
        await self.submit()
        # 0002 最新（保留），0001 已终态且够旧（清理），0003 运行中（跳过）
        self.plugin._short_index["0001"]["recorded_at"] = "2026-10-01T00:00:00+08:00"
        self.plugin._short_index["0001"]["last_status"] = "completed"
        self.plugin._short_index["0002"]["recorded_at"] = "2026-10-02T00:00:00+08:00"
        self.plugin._short_index["0002"]["last_status"] = "completed"
        self.plugin._short_index["0003"] = {
            "task_id": "task-run",
            "sandbox_id": "env-shared",
            "key": self.module._key_fingerprint("key-bound"),
            "recorded_at": "2026-10-03T00:00:00+08:00",
            "last_status": "in_progress",
        }
        client = _ClientStub()
        self.plugin._client = lambda: client
        result = await self.plugin._sweep_sessions(reason="test")
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(client.deleted, ["task-old"])
        self.assertNotIn("0001", self.plugin._short_index)
        self.assertIn("0002", self.plugin._short_index)
        self.assertIn("0003", self.plugin._short_index)

    async def test_sweep_sessions_keeps_unknown_status_and_polling_sessions(self):
        for i, (status, poll) in enumerate((
            ("", False),
            ("completed", True),
        )):
            with self.subTest(status=status, poll=poll):
                self.plugin._short_index = {
                    "0001": dict(
                        self.original,
                        recorded_at="2026-10-01T00:00:00+08:00",
                        last_status=status,
                    )
                }
                if poll:
                    self.plugin._file_polls["0001"] = {"task_id": "task-old"}
                else:
                    self.plugin._file_polls = {}
                client = _ClientStub()
                self.plugin._client = lambda: client
                result = await self.plugin._sweep_sessions(reason="test")
                self.assertEqual(result["deleted"], 0)
                self.assertEqual(client.deleted, [])
                self.assertIn("0001", self.plugin._short_index)

    async def test_agnew_adopts_remote_sandbox_without_local_session(self):
        orphan = "a" * 31 + "b"
        adopted = _AdoptClient(self)
        self.plugin._client = lambda: adopted
        receipt = await self.plugin._do_submit(
            prompt="adopt this sandbox", reuse_task_ref=orphan
        )
        self.assertTrue(receipt.ok, receipt.text)
        self.assertEqual(receipt.sandbox_id, orphan)
        # 先探测 default（不可达）再命中 bound；提交前还会用 bound 复核一次
        self.assertEqual(adopted.probed_keys, ["key-default", "key-bound", "key-bound"])
        new_short = self.plugin._short_for_task("task-new")
        self.assertTrue(new_short)
        row = self.plugin._short_index[new_short]
        self.assertEqual(row["sandbox_id"], orphan)
        self.assertEqual(adopted.used_keys, ["key-bound"])

    async def test_agnew_rejects_unreachable_remote_sandbox(self):
        orphan = "c" * 31 + "d"
        self.plugin._client = lambda: _AdoptClient(self, reachable=False)
        receipt = await self.plugin._do_submit(
            prompt="adopt", reuse_task_ref=orphan
        )
        self.assertFalse(receipt.ok)
        self.assertIn("没有可访问该沙盒的 Key", receipt.text)
        self.assertEqual(self.plugin._short_index, {"0001": self.original})

    async def test_command_uses_existing_type_parser_and_framework_greedy_annotation(self):

        from astrbot.core.star.filter.command import CommandFilter, GreedyStr

        handler = self.module.AntigravitySandboxPlugin.agnew
        md = types.SimpleNamespace(handler=handler)
        parser = CommandFilter("agnew", handler_md=md)
        self.assertIs(parser.handler_params["rest"], GreedyStr)
        parsed = parser.validate_and_convert_params(["0001", "svg", "png", "正文", "含空格"], parser.handler_params)
        self.plugin._do_submit = AsyncMock(return_value=self.module.HandlerReceipt("failed", ok=False))
        self.plugin._log_command_receipt = Mock()
        event = types.SimpleNamespace(plain_result=lambda text: text)
        replies = [reply async for reply in handler(self.plugin, event, parsed["rest"])]
        self.assertTrue(replies)
        params = self.plugin._do_submit.await_args.kwargs
        self.assertEqual(params["reuse_task_ref"], "0001")
        self.assertEqual(params["prompt"], "正文 含空格")
        self.assertEqual(params["output_files"], "result.svg,result.png")


class ImageRoutingTests(unittest.IsolatedAsyncioTestCase):
    """聊天图片识别与分流：图片走多模态 input，其余留给 Files PUT。"""

    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin_definition()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="img_test_")
        self.addCleanup(self.temp.cleanup)
        self.plugin = object.__new__(self.module.AntigravitySandboxPlugin)

    def _write(self, name: str, data: bytes) -> str:
        path = Path(self.temp.name) / name
        path.write_bytes(data)
        return str(path)

    def test_sniff_image_mime_recognizes_headers(self):
        sniff = self.module.sniff_image_mime
        self.assertEqual(sniff(b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertEqual(sniff(b"\x89PNG\r\n\x1a\n"), "image/png")
        self.assertEqual(sniff(b"GIF87a rest"), "image/gif")
        self.assertEqual(sniff(b"GIF89a rest"), "image/gif")
        self.assertEqual(sniff(b"BM\x36\x00"), "image/bmp")
        self.assertEqual(sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        self.assertEqual(sniff(b"plain text"), "")
        self.assertEqual(sniff(b""), "")

    async def test_split_image_payloads_routes_images_and_others(self):
        png = self._write("a.png", b"\x89PNG\r\n\x1a\n" + b"x" * 100)
        jpg = self._write("b.jpg", b"\xff\xd8\xff" + b"y" * 100)
        text = self._write("c.md", b"# hello")
        empty = self._write("d.png", b"")
        big = self._write("e.png", b"\x89PNG\r\n\x1a\n" + b"z" * (11 * 1024 * 1024))
        images, others = self.plugin._split_image_payloads([png, jpg, text, empty, big])
        self.assertEqual([i["name"] for i in images], ["a.png", "b.jpg"])
        self.assertEqual(images[0]["mime_type"], "image/png")
        self.assertEqual(images[1]["mime_type"], "image/jpeg")
        self.assertEqual(others, [text])
        # 0 字节与超限的图片被跳过（不产生 base64 负载）
        self.assertEqual(len(images), 2)

    async def test_split_image_payloads_handles_empty_list(self):
        images, others = self.plugin._split_image_payloads([])
        self.assertEqual((images, others), ([], []))


if __name__ == "__main__":
    unittest.main()
