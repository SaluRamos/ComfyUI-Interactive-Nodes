import asyncio
import threading
import uuid

from aiohttp import web
from comfy_execution.utils import get_executing_context
import comfy.model_management
from server import PromptServer


class AnyType(str):
    def __ne__(self, other):
        return False


ANY_TYPE = AnyType("*")
_pending = {}
_pending_lock = threading.Lock()


@PromptServer.instance.routes.post("/proceed-btn/decision")
async def submit_decision(request):
    try:
        payload = await request.json()
    except (ValueError, web.HTTPException):
        return web.json_response({"error": "Invalid JSON."}, status=400)

    if not isinstance(payload, dict):
        return web.json_response({"error": "Expected a decision object."}, status=400)
    token = payload.get("token")
    decision = payload.get("decision")
    if not isinstance(token, str) or decision not in ("continue", "cancel"):
        return web.json_response({"error": "Decision must be continue or cancel."}, status=400)

    with _pending_lock:
        waiter = _pending.get(token)
        if waiter is None:
            return web.json_response({"error": "This workflow is no longer waiting."}, status=404)
        waiter["decision"] = decision
        waiter["event"].set()

    return web.json_response({"ok": True})


class ProceedButton:
    CATEGORY = "workflow/control"
    FUNCTION = "wait_for_decision"
    RETURN_TYPES = (ANY_TYPE,)
    RETURN_NAMES = ("value",)
    OUTPUT_NODE = True
    DESCRIPTION = "Pause this workflow until you choose Continue or Cancel."

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "value": (ANY_TYPE,),
                "title": ("STRING", {"default": "Aguardando sua decisão", "multiline": False}),
                "description": ("STRING", {
                    "default": "O workflow está pausado. Escolha continuar ou cancelar.",
                    "multiline": True,
                }),
            }
        }

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        return True

    async def wait_for_decision(self, value, title, description):
        context = get_executing_context()
        if context is None:
            raise RuntimeError("Proceed Button must run inside a ComfyUI workflow.")

        token = uuid.uuid4().hex
        waiter = {"event": threading.Event(), "decision": None}
        with _pending_lock:
            _pending[token] = waiter

        server = PromptServer.instance
        server.send_sync("proceed_btn_waiting", {
            "token": token,
            "prompt_id": context.prompt_id,
            "node_id": context.node_id,
            "title": title,
            "description": description,
        }, server.client_id)

        decision = None
        try:
            while not waiter["event"].is_set():
                comfy.model_management.throw_exception_if_processing_interrupted()
                await asyncio.sleep(0.15)
            decision = waiter["decision"]
            if decision == "cancel":
                raise comfy.model_management.InterruptProcessingException()
            return (value,)
        finally:
            with _pending_lock:
                _pending.pop(token, None)
            server.send_sync("proceed_btn_resolved", {
                "token": token,
                "prompt_id": context.prompt_id,
                "decision": decision,
            }, server.client_id)


NODE_CLASS_MAPPINGS = {"ComfyUIProceedButton": ProceedButton}
NODE_DISPLAY_NAME_MAPPINGS = {"ComfyUIProceedButton": "Continuar / Cancelar"}
WEB_DIRECTORY = "./web"
