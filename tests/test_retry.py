import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1]))
sys.argv = [sys.argv[0], "--cpu"]
import execution
import nodes
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from server import PromptServer


class Server:
    def __init__(self):
        self.routes = web.RouteTableDef()
        self.client_id = "test-client"
        self.last_node_id = None
        self.sent = []
        self.callback = None
        self.number = 0
        self.queued = []
        self.prompt_queue = types.SimpleNamespace(put=self.queued.append,
            get_current_queue_volatile=lambda: ([], list(self.queued)))

    def send_sync(self, event, payload, client_id):
        self.sent.append((event, payload))
        if self.callback:
            self.callback(event, payload)


server = Server()
PromptServer.instance = server
spec = importlib.util.spec_from_file_location("interactive_retry_test", ROOT / "__init__.py",
                                            submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
retry = sys.modules[spec.name + ".retry"]
controls = sys.modules[spec.name + ".nodes"]


class Source:
    calls = 0
    FUNCTION = "run"
    RETURN_TYPES = ("INT",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    def run(self):
        type(self).calls += 1
        return (42,)


class Step:
    calls = 0
    FUNCTION = "run"
    RETURN_TYPES = ("INT",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("INT",)}}

    def run(self, value):
        type(self).calls += 1
        return (value + 1,)


class Output(Step):
    OUTPUT_NODE = True
    calls = 0
    values = []

    def run(self, value):
        type(self).calls += 1
        self.values.append(value)
        return (value,)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        retry._completed.clear()
        server.queued.clear()
        Source.calls = Step.calls = Output.calls = 0
        Output.values = []
        server.sent.clear()
        server.callback = None
        self.mapping = patch.dict(nodes.NODE_CLASS_MAPPINGS, {
            "TestSource": Source, "TestStep": Step, "TestOutput": Output,
            **package.NODE_CLASS_MAPPINGS})
        self.mapping.start()
        self.addCleanup(self.mapping.stop)
        self.executor = execution.PromptExecutor(server, cache_type=execution.CacheType.CLASSIC,
                                                cache_args={"ram": 0, "ram_inactive": 0})
        self.prompt = {
            "1": {"class_type": "TestSource", "inputs": {}},
            "2": {"class_type": "ComfyUIRetry", "inputs": {"value": ["1", 0]}},
            "3": {"class_type": "TestStep", "inputs": {"value": ["2", 0]}},
            "4": {"class_type": "TestOutput", "inputs": {"value": ["3", 0]}},
        }

    async def execute(self):
        await asyncio.wait_for(self.executor.execute_async(self.prompt, "job", {"client_id": "test-client"}, ["4"]), 5)
        self.assertTrue(self.executor.success, server.sent)
        self.assertFalse(retry._runs)
        self.assertNotIsInstance(self.executor.caches.outputs, retry.RetryCache)

    async def test_passthrough_and_cleanup(self):
        await self.execute()
        self.assertEqual((Source.calls, Step.calls, Output.calls), (1, 1, 1))
        self.assertEqual(Output.values, [43])
        self.assertTrue(any(event == "retry_ready" for event, _ in server.sent))
        self.assertTrue(any(event == "retry_available" for event, _ in server.sent))

    async def test_rewinds_twice_without_repeating_upstream(self):
        def callback(event, payload):
            if event == "executing" and payload["node"] == "4" and Step.calls <= 2:
                run, node_id = next(iter(retry._runs.values()))
                run.request(node_id)
        server.callback = callback
        await self.execute()
        self.assertEqual(Source.calls, 1)
        self.assertEqual(Step.calls, 3)
        self.assertEqual(Output.calls, 3)
        self.assertEqual(Output.values, [43, 43, 43])

    async def test_retry_while_proceed_waits(self):
        self.prompt["4"] = {"class_type": "ComfyUIProceedButton", "inputs": {
            "value": ["3", 0], "title": "Test", "description": "Wait"}}
        waits = []
        def callback(event, payload):
            if event == "proceed_btn_waiting":
                waits.append(payload["token"])
                if len(waits) == 1:
                    run, node_id = next(iter(retry._runs.values()))
                    run.request(node_id)
                else:
                    waiter = controls._pending[payload["token"]]
                    waiter["decision"] = "continue"
                    waiter["event"].set()
        server.callback = callback
        await self.execute()
        self.assertEqual(Source.calls, 1)
        self.assertEqual(Step.calls, 2)
        self.assertEqual(len(waits), 2)
        self.assertFalse(controls._pending)

    async def test_retry_with_disabled_and_lru_caches(self):
        for mode in (execution.CacheType.NONE, execution.CacheType.LRU, execution.CacheType.RAM_PRESSURE):
            with self.subTest(mode=mode):
                Source.calls = Step.calls = Output.calls = 0
                self.executor = execution.PromptExecutor(server, cache_type=mode,
                    cache_args={"ram": 0, "ram_inactive": 0, "lru": 1})
                def callback(event, payload):
                    if event == "executing" and payload["node"] == "4" and Step.calls == 1:
                        run, node_id = next(iter(retry._runs.values()))
                        run.request(node_id)
                server.callback = callback
                await self.execute()
                self.assertEqual(Source.calls, 1)
                self.assertEqual(Step.calls, 2)

    async def test_active_http_retry_and_stale_token(self):
        self.prompt["4"] = {"class_type": "ComfyUIProceedButton", "inputs": {
            "value": ["3", 0], "title": "Test", "description": "Wait"}}
        ready = asyncio.Event()
        tokens = []
        def callback(event, payload):
            if event == "proceed_btn_waiting":
                tokens.append(payload["token"])
                ready.set()
        server.callback = callback
        app = web.Application()
        app.add_routes(server.routes)
        async with TestClient(TestServer(app)) as client:
            task = asyncio.create_task(self.execute())
            try:
                await asyncio.wait_for(ready.wait(), 3)
                token = next(iter(retry._runs))
                ready.clear()
                response = await client.post("/interactive-nodes/retry", json={"token": token})
                self.assertEqual(response.status, 200)
                await asyncio.wait_for(ready.wait(), 3)
                self.assertEqual(Step.calls, 2)
                self.assertNotIn(tokens[0], controls._pending)
                waiter = controls._pending[tokens[-1]]
                waiter["decision"] = "continue"
                waiter["event"].set()
                await task
                response = await client.post("/interactive-nodes/retry", json={"token": token})
                self.assertEqual(response.status, 200)
                self.assertEqual(len(server.queued), 1)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_unrelated_completed_branch_is_not_repeated(self):
        self.prompt["other"] = {"class_type": "TestOutput", "inputs": {"value": 7}}
        def callback(event, payload):
            if event == "executing" and payload["node"] == "4" and Step.calls == 1:
                run, node_id = next(iter(retry._runs.values()))
                run.request(node_id)
        server.callback = callback
        self.executor = execution.PromptExecutor(server, cache_type=execution.CacheType.NONE,
                                                cache_args={"ram": 0, "ram_inactive": 0})
        await asyncio.wait_for(self.executor.execute_async(self.prompt, "job", {"client_id": "test-client"}, ["other", "4"]), 5)
        self.assertTrue(self.executor.success)
        self.assertEqual(Output.values.count(7), 1)
        self.assertEqual(Step.calls, 2)

    async def test_cancellation_removes_checkpoints_and_waiters(self):
        self.prompt["4"] = {"class_type": "ComfyUIProceedButton", "inputs": {
            "value": ["3", 0], "title": "Test", "description": "Wait"}}
        ready = asyncio.Event()
        server.callback = lambda event, payload: ready.set() if event == "proceed_btn_waiting" else None
        task = asyncio.create_task(self.executor.execute_async(self.prompt, "job", {"client_id": "test-client"}, ["4"]))
        await asyncio.wait_for(ready.wait(), 3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(retry._runs)
        self.assertFalse(controls._pending)
        self.assertNotIsInstance(self.executor.caches.outputs, retry.RetryCache)

    async def test_completed_retry_queues_new_prompt_from_checkpoint(self):
        self.executor = execution.PromptExecutor(server, cache_type=execution.CacheType.NONE,
                                                cache_args={"ram": 0, "ram_inactive": 0})
        await self.execute()
        token = next(iter(retry._completed))
        app = web.Application()
        app.add_routes(server.routes)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/interactive-nodes/retry", json={"token": token})
            self.assertEqual(response.status, 200)
            result = await response.json()
            self.assertNotEqual(result["prompt_id"], "job")
            response = await client.post("/interactive-nodes/retry", json={"token": token})
            self.assertEqual(response.status, 200)
            self.assertEqual(len(server.queued), 1)
        item = server.queued.pop()
        await asyncio.wait_for(self.executor.execute_async(item[2], item[1], item[3], item[4]), 5)
        self.assertTrue(self.executor.success)
        self.assertEqual(Source.calls, 1)
        self.assertEqual(Step.calls, 2)
        self.assertEqual(Output.values, [43, 43])
        self.assertNotIn(token, retry._completed)
        self.assertEqual(len(retry._completed), 1)

    async def test_deleted_queued_retry_can_be_queued_again(self):
        await self.execute()
        token = next(iter(retry._completed))
        app = web.Application()
        app.add_routes(server.routes)
        async with TestClient(TestServer(app)) as client:
            first = await client.post("/interactive-nodes/retry", json={"token": token})
            first_id = (await first.json())["prompt_id"]
            server.queued.clear()
            second = await client.post("/interactive-nodes/retry", json={"token": token})
            self.assertEqual(second.status, 200)
            self.assertNotEqual((await second.json())["prompt_id"], first_id)
            self.assertEqual(len(server.queued), 1)

    async def test_completed_retry_preserves_other_inputs_with_no_cache(self):
        class Add(Step):
            @classmethod
            def INPUT_TYPES(cls):
                return {"required": {"value": ("INT",), "other": ("INT",)}}

            def run(self, value, other):
                Step.calls += 1
                return (value + other,)

        with patch.dict(nodes.NODE_CLASS_MAPPINGS, {"TestAdd": Add}):
            self.prompt["other"] = {"class_type": "TestSource", "inputs": {}}
            self.prompt["3"] = {"class_type": "TestAdd", "inputs": {"value": ["2", 0], "other": ["other", 0]}}
            self.executor = execution.PromptExecutor(server, cache_type=execution.CacheType.NONE,
                                                    cache_args={"ram": 0, "ram_inactive": 0})
            await self.execute()
            token = next(iter(retry._completed))
            app = web.Application()
            app.add_routes(server.routes)
            async with TestClient(TestServer(app)) as client:
                response = await client.post("/interactive-nodes/retry", json={"token": token})
                self.assertEqual(response.status, 200)
            item = server.queued.pop()
            await asyncio.wait_for(self.executor.execute_async(item[2], item[1], item[3], item[4]), 5)
            self.assertTrue(self.executor.success)
            self.assertEqual(Source.calls, 2)
            self.assertEqual(Step.calls, 2)
            self.assertEqual(Output.values, [84, 84])

    async def test_new_workflow_replaces_completed_checkpoint(self):
        await self.execute()
        old_token = next(iter(retry._completed))
        await self.execute()
        self.assertNotIn(old_token, retry._completed)
        self.assertEqual(len(retry._completed), 1)

    async def test_http_rejects_inactive_and_malformed_tokens(self):
        app = web.Application()
        app.add_routes(server.routes)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/interactive-nodes/retry", json={"token": "old"})
            self.assertEqual(response.status, 409)
            response = await client.post("/interactive-nodes/retry", json=[])
            self.assertEqual(response.status, 400)

    def test_wildcard_and_lists(self):
        node = retry.Retry()
        value = [object(), object()]
        self.assertIs(node.passthrough(value)[0], value)
        self.assertFalse(retry.Retry.RETURN_TYPES[0] != "IMAGE")
        self.assertEqual(set(retry.Retry.INPUT_TYPES()["required"]), {"value"})


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
