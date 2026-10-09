import asyncio
import contextvars
import copy
import time
import functools
import threading
import uuid

from aiohttp import web
import execution
from comfy_execution.graph import ExecutionList
from comfy_execution.graph_utils import is_link
from server import PromptServer

from .nodes import ANY_TYPE


_current_run = contextvars.ContextVar("interactive_retry_run", default=None)
_runs = {}
_completed = {}
_lock = threading.Lock()


def downstream(dynprompt, target):
    affected = {target}
    changed = True
    while changed:
        changed = False
        for node_id in dynprompt.all_node_ids() - affected:
            node = dynprompt.get_node(node_id)
            if (dynprompt.get_parent_node_id(node_id) in affected or
                    any(is_link(value) and value[0] in affected for value in node["inputs"].values())):
                affected.add(node_id)
                changed = True
    return affected


class RetryCache:
    def __init__(self, cache):
        self.cache = cache
        self.invalid = set()
        self.pinned = {}

    def __getattr__(self, name):
        return getattr(self.cache, name)

    async def get(self, node_id):
        if node_id in self.invalid:
            return None
        if node_id in self.pinned:
            return self.pinned[node_id]
        return await self.cache.get(node_id)

    def get_local(self, node_id):
        if node_id in self.invalid:
            return None
        return self.pinned.get(node_id) or self.cache.get_local(node_id)

    async def set(self, node_id, value):
        await self.cache.set(node_id, value)
        self.invalid.discard(node_id)

    def set_local(self, node_id, value):
        if node_id not in self.invalid:
            self.cache.set_local(node_id, value)


class RetryRun:
    def __init__(self, server, prompt_id, outputs, cache):
        self.server = server
        self.client_id = server.client_id
        self.prompt_id = prompt_id
        self.outputs = outputs
        self.cache = cache
        self.loop = asyncio.get_running_loop()
        self.event = asyncio.Event()
        self.requested = None
        self.checkpoints = {}
        self.frame = None
        self.prompt = None
        self.extra_data = {}

    def send(self, event, data):
        self.server.send_sync(event, {"prompt_id": self.prompt_id, **data}, self.client_id)

    def checkpoint(self, node_id, execution_list):
        if node_id in self.checkpoints:
            self.send("retry_ready", {"node_id": node_id, "token": self.checkpoints[node_id]["token"]})
            return
        inputs = execution_list.dynprompt.get_node(node_id)["inputs"]
        sources = {}
        for value in inputs.values():
            if is_link(value):
                entry = execution_list.get_cache(value[0], node_id)
                if entry is not None:
                    sources[value[0]] = entry
        token = uuid.uuid4().hex
        self.checkpoints[node_id] = {"token": token, "sources": sources,
                                     "affected": downstream(execution_list.dynprompt, node_id)}
        with _lock:
            _runs[token] = (self, node_id)
        self.send("retry_ready", {"node_id": node_id, "token": token})

    def capture_inputs(self, node_id, execution_list):
        inputs = execution_list.dynprompt.get_node(node_id)["inputs"]
        for checkpoint in self.checkpoints.values():
            affected = checkpoint["affected"]
            if node_id not in affected:
                continue
            for value in inputs.values():
                if is_link(value) and value[0] not in affected:
                    entry = execution_list.get_cache(value[0], node_id)
                    if entry is not None:
                        checkpoint["sources"][value[0]] = entry

    def finish(self):
        for node_id, checkpoint in self.checkpoints.items():
            saved = {"node_id": node_id, "client_id": self.client_id,
                     "prompt": self.prompt, "extra_data": self.extra_data,
                     "outputs": list(set(self.outputs) & checkpoint["affected"] | {node_id}),
                     "sources": checkpoint["sources"], "affected": checkpoint["affected"],
                     "queued": None, "submitting": False}
            with _lock:
                _runs.pop(checkpoint["token"], None)
                _completed[checkpoint["token"]] = saved
            self.send("retry_available", {"node_id": node_id, "token": checkpoint["token"]})
        self.checkpoints.clear()

    def close_checkpoint(self, node_id):
        checkpoint = self.checkpoints.pop(node_id)
        with _lock:
            _runs.pop(checkpoint["token"], None)
        self.send("retry_closed", {"node_id": node_id, "token": checkpoint["token"]})

    def request(self, node_id):
        if node_id in self.checkpoints and self.requested is None:
            self.requested = node_id
            self.event.set()

    async def rewind(self, execution_list):
        target = self.requested
        self.requested = None
        self.event.clear()
        if target not in self.checkpoints:
            return
        dynprompt = execution_list.dynprompt
        affected = downstream(dynprompt, target)

        executed, subgraphs, pending, ui_outputs = self.frame
        cancelled = []
        for node_id in affected:
            for task in pending.pop(node_id, []):
                if isinstance(task, asyncio.Task):
                    task.cancel()
                    cancelled.append(task)
            subgraphs.pop(node_id, None)
            ui_outputs.pop(node_id, None)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
            # Let execution's completion callbacks release their external blocks.
            await asyncio.sleep(0)
        executed.difference_update(affected)
        sources = self.checkpoints[target]["sources"]
        for node_id in list(self.checkpoints):
            if node_id in affected and node_id != target:
                self.close_checkpoint(node_id)
        self.cache.invalid.update(affected)
        self.cache.pinned = {source: entry for checkpoint in self.checkpoints.values()
                             for source, entry in checkpoint["sources"].items() if source not in affected}
        self.cache.pinned.update(sources)

        remaining = set(execution_list.pendingNodes) - affected
        external = {node_id: execution_list.blockCount[node_id] -
                    sum(node_id in blocked for blocked in execution_list.blocking.values())
                    for node_id in pending if node_id in execution_list.pendingNodes}
        callback = execution_list.output_link_callback
        ExecutionList.__init__(execution_list, dynprompt, self.cache, callback)
        for node_id in remaining | (set(self.outputs) & affected) | {target}:
            execution_list.add_node(node_id)
        for node_id, count in external.items():
            if node_id in execution_list.pendingNodes:
                execution_list.externalBlocks += count
                execution_list.blockCount[node_id] += count
        self.send("retry_restarted", {"node_id": target})


@PromptServer.instance.routes.post("/interactive-nodes/retry")
async def request_retry(request):
    try:
        payload = await request.json()
    except (ValueError, web.HTTPException):
        return web.json_response({"error": "Invalid JSON."}, status=400)
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str):
        return web.json_response({"error": "Expected a retry token."}, status=400)
    with _lock:
        checkpoint = _runs.get(token)
        if checkpoint is not None:
            run, node_id = checkpoint
            run.loop.call_soon_threadsafe(run.request, node_id)
            return web.json_response({"ok": True})
        saved = _completed.get(token)
        if saved is None:
            return web.json_response({"error": "Este retry não possui um ponto de retorno disponível."}, status=409)
        if saved["queued"] is not None:
            running, queued = PromptServer.instance.prompt_queue.get_current_queue_volatile()
            if saved["submitting"] or any(item[1] == saved["queued"] for item in running + queued):
                return web.json_response({"ok": True, "prompt_id": saved["queued"]})
            saved["queued"] = None
        prompt_id = str(uuid.uuid4())
        saved["queued"] = prompt_id
        saved["submitting"] = True
    prompt = copy.deepcopy(saved["prompt"])
    valid = await execution.validate_prompt(prompt_id, prompt, saved["outputs"])
    if not valid[0]:
        with _lock:
            saved["queued"] = None
            saved["submitting"] = False
        return web.json_response({"error": "Não foi possível validar o fluxo salvo.", "node_errors": valid[3]}, status=400)
    server = PromptServer.instance
    extra_data = copy.deepcopy(saved["extra_data"])
    extra_data["interactive_retry_token"] = token
    extra_data["create_time"] = int(time.time() * 1000)
    number = server.number
    server.number += 1
    server.prompt_queue.put((number, prompt_id, prompt, extra_data, valid[2], {}))
    with _lock:
        saved["submitting"] = False
    return web.json_response({"ok": True, "prompt_id": prompt_id, "number": number})



class Retry:
    CATEGORY = "workflow/control"
    FUNCTION = "passthrough"
    RETURN_TYPES = (ANY_TYPE,)
    RETURN_NAMES = ("value",)
    INPUT_IS_LIST = True
    OUTPUT_IS_LIST = (True,)
    OUTPUT_NODE = True
    DESCRIPTION = "Retry retorna a este ponto durante a execução ou inicia um novo fluxo a partir dele após o término."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": (ANY_TYPE,)}}

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        return True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def passthrough(self, value):
        return (value,)


_original_execute_async = execution.PromptExecutor.execute_async
_original_execute_node = execution.execute
_original_stage = ExecutionList.stage_node_execution


@functools.wraps(_original_execute_async)
async def execute_with_retry(self, prompt, prompt_id, extra_data=None, execute_outputs=None):
    if not any(node["class_type"] == "ComfyUIRetry" for node in prompt.values()):
        return await _original_execute_async(self, prompt, prompt_id, extra_data or {}, execute_outputs or [])
    prompt = copy.deepcopy(prompt)
    for node in prompt.values():
        if node["class_type"] == "ComfyUIRetry":
            node.pop("is_changed", None)
    extra_data = dict(extra_data or {})
    resume_token = extra_data.pop("interactive_retry_token", None)
    with _lock:
        saved = _completed.pop(resume_token, None) if resume_token else None
        if resume_token and saved is None:
            raise RuntimeError("O ponto de retorno deste retry não está mais disponível.")
        if not resume_token:
            client_id = extra_data.get("client_id")
            for token, checkpoint in list(_completed.items()):
                if checkpoint["client_id"] == client_id and checkpoint["queued"] is None:
                    _completed.pop(token)
                    self.server.send_sync("retry_closed", {"node_id": checkpoint["node_id"], "token": token}, client_id)
    cache = self.caches.outputs
    overlay = RetryCache(cache)
    run = RetryRun(self.server, prompt_id, list(execute_outputs or []), overlay)
    run.client_id = extra_data.get("client_id")
    run.prompt = copy.deepcopy(prompt)
    run.extra_data = {key: copy.deepcopy(value) for key, value in extra_data.items()
                      if key not in execution.SENSITIVE_EXTRA_DATA_KEYS}
    if saved is not None:
        overlay.invalid.update(saved["affected"])
        overlay.pinned.update(saved["sources"])
    self.caches.outputs = overlay
    self.caches.all[0] = overlay
    context = _current_run.set(run)
    finished = False
    try:
        result = await _original_execute_async(self, prompt, prompt_id, extra_data, execute_outputs or [])
        finished = True
        return result
    finally:
        if run.frame is not None:
            tasks = [task for results in run.frame[2].values() for task in results
                     if isinstance(task, asyncio.Task) and not task.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        if finished:
            run.finish()
        else:
            for node_id in list(run.checkpoints):
                run.close_checkpoint(node_id)
        overlay.pinned.clear()
        _current_run.reset(context)
        self.caches.outputs = cache
        self.caches.all[0] = cache


@functools.wraps(_original_execute_node)
async def execute_node_with_retry(server, dynprompt, caches, current_item, extra_data, executed,
                                  prompt_id, execution_list, pending_subgraph_results,
                                  pending_async_nodes, ui_outputs, asset_manager):
    run = _current_run.get()
    if run is not None:
        run.frame = (executed, pending_subgraph_results, pending_async_nodes, ui_outputs)
        run.capture_inputs(current_item, execution_list)
    result = await _original_execute_node(server, dynprompt, caches, current_item, extra_data, executed,
                                         prompt_id, execution_list, pending_subgraph_results,
                                         pending_async_nodes, ui_outputs, asset_manager)
    if (run is not None and result[0] == execution.ExecutionResult.SUCCESS and
            dynprompt.get_node(current_item)["class_type"] == "ComfyUIRetry"):
        run.checkpoint(current_item, execution_list)
    if run is not None and result[0] != execution.ExecutionResult.FAILURE:
        await asyncio.sleep(0)
        if run.requested is not None:
            if result[0] == execution.ExecutionResult.SUCCESS:
                execution_list.complete_node_execution()
            await run.rewind(execution_list)
            execution_list.staged_node_id = current_item
            return (execution.ExecutionResult.PENDING, None, None)
    return result


@functools.wraps(_original_stage)
async def stage_with_retry(self):
    run = _current_run.get()
    if run is None:
        return await _original_stage(self)
    while True:
        if run.requested is not None:
            await run.rewind(self)
        stage = asyncio.create_task(_original_stage(self))
        retry = asyncio.create_task(run.event.wait())
        try:
            await asyncio.wait((stage, retry), return_when=asyncio.FIRST_COMPLETED)
            if run.requested is not None:
                stage.cancel()
                await asyncio.gather(stage, return_exceptions=True)
                continue
            return await stage
        finally:
            retry.cancel()
            if not stage.done():
                stage.cancel()
            await asyncio.gather(stage, retry, return_exceptions=True)


execution.PromptExecutor.execute_async = execute_with_retry
execution.execute = execute_node_with_retry
ExecutionList.stage_node_execution = stage_with_retry
