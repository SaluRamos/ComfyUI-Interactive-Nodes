const { app } = window.comfyAPI.app;
const { api } = window.comfyAPI.api;
const waits = new Map();
const cards = new Map();

function showWaitingCard(detail) {
  const card = document.createElement("section");
  Object.assign(card.style, {
    position: "fixed", top: "18px", right: "18px", zIndex: "100000",
    width: "min(380px, calc(100vw - 36px))", padding: "18px",
    borderRadius: "10px", color: "#fff", background: "#282a32",
    boxShadow: "0 6px 28px #0008", font: "14px/1.45 sans-serif"
  });

  const heading = document.createElement("strong");
  heading.textContent = detail.title || "Aguardando sua decisão";
  heading.style.display = "block";
  heading.style.marginBottom = "8px";
  const message = document.createElement("div");
  message.textContent = detail.description || "O workflow está pausado.";
  message.style.whiteSpace = "pre-wrap";
  const actions = document.createElement("div");
  Object.assign(actions.style, { display: "flex", gap: "8px", marginTop: "14px" });

  for (const [decision, label] of [["continue", "Continuar"], ["cancel", "Cancelar"]]) {
    const button = document.createElement("button");
    button.textContent = label;
    button.style.cssText = "flex:1;padding:9px;border:0;border-radius:6px;cursor:pointer";
    button.addEventListener("click", () => choose(detail.token, decision));
    actions.append(button);
  }
  card.append(heading, message, actions);
  document.body.append(card);
  cards.set(detail.token, card);
}

async function choose(token, decision) {
  const state = waits.get(token);
  if (!state || state.sending) return;
  state.sending = true;
  const card = cards.get(token);
  card?.querySelectorAll("button").forEach(button => { button.disabled = true; });
  try {
    const response = await api.fetchApi("/proceed-btn/decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, decision })
    });
    if (!response.ok) {
      const result = await response.json();
      throw new Error(result.error || "Não foi possível enviar a decisão.");
    }
    state.decision = decision;
    card?.querySelectorAll("button").forEach(button => { button.textContent = "Aguarde…"; });
  } catch (error) {
    state.sending = false;
    card?.querySelectorAll("button").forEach(button => { button.disabled = false; });
    const message = card?.querySelector("div");
    if (message) message.textContent = error.message;
  }
}

function finish(token) {
  cards.get(token)?.remove();
  cards.delete(token);
  waits.delete(token);
}

api.addEventListener("proceed_btn_waiting", ({ detail }) => {
  waits.set(detail.token, {
    nodeId: String(detail.node_id), promptId: detail.prompt_id, sending: false
  });
  showWaitingCard(detail);
  app.graph?.getNodeById(Number(detail.node_id))?.setDirtyCanvas(true, true);
});

api.addEventListener("proceed_btn_resolved", ({ detail }) => {
  finish(detail.token);
});

api.addEventListener("execution_interrupted", ({ detail }) => {
  for (const [token, state] of waits) {
    if (state.promptId === detail.prompt_id) finish(token);
  }
});

app.registerExtension({
  name: "ComfyUI.ProceedButton",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== "ComfyUIProceedButton") return;
    const originalCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function (...args) {
      originalCreated?.apply(this, args);
      const node = this;
      this.addWidget("button", "Continuar", null, () => chooseForNode(node, "continue"));
      this.addWidget("button", "Cancelar", null, () => chooseForNode(node, "cancel"));
    };
  }
});

function chooseForNode(node, decision) {
  const token = [...waits].find(([, state]) => state.nodeId === String(node.id))?.[0];
  if (token) choose(token, decision);
}
