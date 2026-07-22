(() => {
  const $ = (id) => document.getElementById(id);
  const t = (key, fallback, params = {}) => window.UmaI18n?.t(key, fallback, params) ?? fallback;
  let csrfToken = "";
  let currentConversation = "";

  function cookie(name) {
    return document.cookie.split("; ").find((part) => part.startsWith(`${name}=`))?.split("=")[1] || "";
  }
  async function api(path, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = new Headers(options.headers || {});
    if (method !== "GET") {
      headers.set("Content-Type", "application/json");
      headers.set("X-CSRF-Token", csrfToken || cookie("uma_csrf"));
    }
    const res = await fetch(path, {...options, method, headers, credentials: "same-origin"});
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) {
      const detail = data?.detail;
      const msg = typeof detail === "string" ? detail : (detail?.message || data?.message || `HTTP ${res.status}`);
      throw new Error(msg);
    }
    return data;
  }
  function credits(value) { return `${Math.max(0, Number(value || 0))} credits`; }
  function setStatus(text, type = "") {
    const node = $("status");
    node.textContent = text || "";
    node.className = `status ${type}`;
  }
  function appendMessage(role, text) {
    const row = document.createElement("div");
    row.className = `msg ${role}`;
    row.textContent = text;
    $("messages").append(row);
    $("messages").scrollTop = $("messages").scrollHeight;
  }
  function renderMessages(messages) {
    $("messages").textContent = "";
    if (!messages.length) {
      appendMessage("assistant", t("ai_support.welcome", "你好，我是小击击 AI 客服。你可以直接描述遇到的问题，也可以提供任务号让我帮你查询。"));
      return;
    }
    for (const msg of messages) appendMessage(msg.role === "user" ? "user" : "assistant", msg.safe_content || "");
  }
  async function loadMe() {
    const me = await api("/api/me");
    csrfToken = cookie("uma_csrf");
    $("balance").textContent = credits(me.balance_fen);
  }
  async function createConversation() {
    const data = await api("/api/ai-support/conversations", {method: "POST", body: "{}"});
    currentConversation = data.conversation.conversation_code;
    renderMessages([]);
    await loadHistory();
  }
  async function loadConversation(code) {
    const data = await api(`/api/ai-support/conversations/${encodeURIComponent(code)}`);
    currentConversation = data.conversation.conversation_code;
    renderMessages(data.messages || []);
  }
  async function loadHistory() {
    const data = await api("/api/ai-support/conversations");
    const panel = $("historyPanel");
    panel.textContent = "";
    for (const item of data.conversations || []) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "history-item";
      btn.textContent = `${item.conversation_code} · ${new Date(Number(item.updated_at || 0) * 1000).toLocaleString()}`;
      btn.addEventListener("click", async () => {
        panel.classList.add("hidden");
        await loadConversation(item.conversation_code);
      });
      panel.append(btn);
    }
  }
  async function sendMessage(text) {
    const content = String(text || "").trim();
    if (!content) return;
    if (!currentConversation) await createConversation();
    $("messageInput").value = "";
    appendMessage("user", content);
    appendMessage("assistant", t("ai_support.thinking", "正在查询和整理…"));
    setStatus("");
    try {
      const data = await api(`/api/ai-support/conversations/${encodeURIComponent(currentConversation)}/messages`, {
        method: "POST",
        body: JSON.stringify({message: content}),
      });
      const nodes = $("messages").querySelectorAll(".msg.assistant");
      const last = nodes[nodes.length - 1];
      if (last) last.textContent = data.assistant_message.safe_content || "";
      await loadHistory();
    } catch (err) {
      const nodes = $("messages").querySelectorAll(".msg.assistant");
      const last = nodes[nodes.length - 1];
      if (last) last.textContent = t("ai_support.failed", "暂时无法回复，请稍后重试。");
      setStatus(err.message, "error");
    }
  }

  $("backBtn")?.addEventListener("click", () => { window.location.href = "/"; });
  $("newConversationBtn")?.addEventListener("click", createConversation);
  $("historyBtn")?.addEventListener("click", async () => {
    await loadHistory();
    $("historyPanel").classList.toggle("hidden");
  });
  $("clearConversationBtn")?.addEventListener("click", async () => {
    if (!currentConversation) return;
    await api(`/api/ai-support/conversations/${encodeURIComponent(currentConversation)}/clear`, {method: "POST", body: "{}"});
    renderMessages([]);
  });
  $("composer")?.addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage($("messageInput").value);
  });
  document.querySelectorAll("[data-question]").forEach((btn) => {
    btn.addEventListener("click", () => sendMessage(btn.dataset.question || ""));
  });

  document.addEventListener("DOMContentLoaded", async () => {
    try {
      await loadMe();
      await loadHistory();
      const history = await api("/api/ai-support/conversations");
      if (history.conversations?.length) await loadConversation(history.conversations[0].conversation_code);
      else await createConversation();
    } catch (err) {
      if (/401|登录/.test(err.message)) window.location.href = "/login";
      else setStatus(err.message, "error");
    }
  });
})();

