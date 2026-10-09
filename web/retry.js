const { app } = window.comfyAPI.app;
const { api } = window.comfyAPI.api;
const checkpoints = new Map();

function redraw(nodeId) {
  app.graph?.getNodeById(Number(nodeId))?.setDirtyCanvas(true, true);
}

api.addEventListener("retry_ready", ({ detail }) => {
  checkpoints.set(String(detail.node_id), { ...detail, sending: false });
  redraw(detail.node_id);
});
api.addEventListener("retry_available", ({ detail }) => {
  checkpoints.set(String(detail.node_id), { ...detail, sending: false, completed: true });
  redraw(detail.node_id);
});
api.addEventListener("retry_closed", ({ detail }) => {
  const id = String(detail.node_id);
  if (checkpoints.get(id)?.token === detail.token) checkpoints.delete(id);
  redraw(id);
});
api.addEventListener("retry_restarted", ({ detail }) => {
  const state = checkpoints.get(String(detail.node_id));
  if (state?.prompt_id === detail.prompt_id) state.sending = false;
  redraw(detail.node_id);
});
for (const event of ["execution_success", "execution_error", "execution_interrupted"]) {
  api.addEventListener(event, ({ detail }) => {
    for (const [id, state] of checkpoints) {
      if (state.prompt_id === detail.prompt_id) {
        state.sending = false;
        redraw(id);
      }
    }
  });
}
api.addEventListener("reconnecting", () => checkpoints.clear());

app.registerExtension({
  name: "ComfyUI.InteractiveRetry",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== "ComfyUIRetry") return;
    const originalCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function (...args) {
      originalCreated?.apply(this, args);
      const node = this;
      const button = this.addWidget("button", "retry", null, async () => {
        const state = checkpoints.get(String(node.id));
        if (!state || state.sending) return;
        state.sending = true;
        try {
          const response = await api.fetchApi("/interactive-nodes/retry", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token: state.token })
          });
          if (!response.ok) {
            const result = await response.json();
            throw new Error(result.error || "Não foi possível retornar ao retry.");
          }
          state.sending = false;
        } catch (error) {
          state.sending = false;
          app.extensionManager.toast.add({ severity: "error", summary: "retry", detail: error.message });
        }
      });
      button.options.serialize = false;
    };
  }
});
