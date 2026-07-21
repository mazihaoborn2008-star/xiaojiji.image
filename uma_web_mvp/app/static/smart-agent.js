const $ = (id) => document.getElementById(id);

let me = null;
let config = null;
let convCode = null;
let lastEventId = 0;
let pollingTimer = null;
let idlePollsAfterTerminal = 0;
let renderedEventIds = new Set();
let renderedImageKeys = new Set();
let renderedTaskSubmitKeys = new Set();
let generationCards = new Map();
let activeJobCodes = new Set();
let viewportFrame = 0;
let viewportHandlersInstalled = false;
let inputFrame = 0;
let isComposing = false;
let composerFocused = false;
let lastViewportHeight = 0;
let lastViewportTop = 0;
let activePromptVersion = 0;
let sendingLock = false;
let confirmingLock = false;
let turnInFlight = false;

const AVATAR_SRC = "/assets/branding/favicon-32x32.png?v=mizuhara";
const POLL_VISIBLE_MS = 1000;
const POLL_HIDDEN_MS = 5000;
const PROGRESS_EVENT_TYPES = new Set([
  "thinking",
  "searching_character_tags",
  "searching_prompt_library",
  "selecting_workflow",
  "selecting_lora",
  "selecting_resolution",
  "building_prompt",
  "validating_plan",
  "submitting_task",
  "generating",
]);

const JOB_EVENT_TYPES = new Set([
  "queued",
  "processing",
  "done",
  "failed",
  "refunded",
  "error",
  "image_generated",
]);

function t(key, fallback, params) {
  return window.UmaI18n?.t(key, fallback, params) ?? (fallback ?? key);
}

function lang() {
  return window.UmaI18n?.getLang?.() ?? "zh";
}

function updateSmartAgentViewport() {
  if (viewportFrame) {
    window.cancelAnimationFrame(viewportFrame);
  }
  viewportFrame = window.requestAnimationFrame(() => {
    viewportFrame = 0;
    const viewport = window.visualViewport;
    const height = Math.round(viewport?.height || window.innerHeight);
    const offsetTop = Math.round(viewport?.offsetTop || 0);
    if (
      lastViewportHeight &&
      Math.abs(height - lastViewportHeight) < 2 &&
      Math.abs(offsetTop - lastViewportTop) < 2
    ) {
      return;
    }
    lastViewportHeight = height;
    lastViewportTop = offsetTop;
    document.documentElement.style.setProperty("--smart-agent-viewport-height", `${height}px`);
    document.documentElement.style.setProperty("--smart-agent-viewport-top", `${offsetTop}px`);
  });
}

function installViewportHeightHandlers() {
  if (viewportHandlersInstalled) return;
  viewportHandlersInstalled = true;
  updateSmartAgentViewport();
  window.addEventListener("resize", updateSmartAgentViewport, { passive: true });
  window.visualViewport?.addEventListener("resize", updateSmartAgentViewport, { passive: true });
  window.visualViewport?.addEventListener("scroll", updateSmartAgentViewport, { passive: true });
}

function removeViewportHeightHandlers() {
  if (!viewportHandlersInstalled) return;
  viewportHandlersInstalled = false;
  window.removeEventListener("resize", updateSmartAgentViewport);
  window.visualViewport?.removeEventListener("resize", updateSmartAgentViewport);
  window.visualViewport?.removeEventListener("scroll", updateSmartAgentViewport);
  if (viewportFrame) {
    window.cancelAnimationFrame(viewportFrame);
    viewportFrame = 0;
  }
}

function isMobileSmartAgent() {
  return window.matchMedia("(max-width: 640px)").matches;
}

function applyComposerInputSize() {
  if (isMobileSmartAgent() || isComposing) return;
  const input = $("chatInput");
  if (!input) return;
  input.style.height = "auto";
  const nextHeight = Math.min(Math.max(input.scrollHeight, 48), 120);
  input.style.height = `${nextHeight}px`;
}

function resizeComposerInput(event) {
  if (isMobileSmartAgent() || isComposing || event?.isComposing) return;
  if (inputFrame) {
    window.cancelAnimationFrame(inputFrame);
  }
  inputFrame = window.requestAnimationFrame(() => {
    inputFrame = 0;
    applyComposerInputSize();
  });
}

function cancelComposerInputResize() {
  if (inputFrame) {
    window.cancelAnimationFrame(inputFrame);
    inputFrame = 0;
  }
}

function getCookie(name) {
  const prefix = `${name}=`;
  return document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix))
    ?.slice(prefix.length) || "";
}

function withCsrf(options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  if (["GET", "HEAD", "OPTIONS"].includes(method)) return options;
  const headers = new Headers(options.headers || {});
  const token = getCookie("uma_csrf");
  if (token) headers.set("X-CSRF-Token", decodeURIComponent(token));
  return { ...options, headers };
}

async function api(url, options = {}) {
  const res = await fetch(url, { credentials: "same-origin", ...withCsrf(options) });
  let data = null;
  try {
    data = await res.json();
  } catch (_) {}
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    let msg;
    if (typeof getApiErrorMessage === 'function') {
      msg = getApiErrorMessage(data, `HTTP ${res.status}`);
    } else {
      msg = data?.detail || `HTTP ${res.status}`;
    }
    // Attach retry_after from header or response body
    const retryAfter = res.headers.get("Retry-After") || (data?.retry_after);
    if (res.status === 429 && retryAfter) {
      const retrySec = parseInt(retryAfter, 10);
      if (retrySec > 0) {
        msg = JSON.stringify({ retry_after: retrySec, message: String(msg) });
      }
    }
    throw new Error(msg);
  }
  return data;
}

function creditsBalance(fen) {
  if (fen === null || fen === undefined || Number.isNaN(Number(fen))) {
    return "-- credits";
  }
  return `${Math.trunc(Number(fen || 0))} credits`;
}

function friendlyError(errMsg) {
  const raw = String(errMsg || "");
  if (/页面安全验证已过期|csrf/i.test(raw)) {
    return t("common.security_expired", "页面安全验证已过期，请刷新后重试。");
  }
  if (/not found/i.test(raw) || /^HTTP\s+\d+/.test(raw) || raw === "Not Found") {
    return lang() === "en"
      ? "Smart Agent is temporarily unavailable. Please refresh or try again later."
      : "智能 Agent 暂时无法连接，请刷新页面或稍后再试。";
  }
  // Parse structured 429 error
  if (/rate.*limit|过于频繁|429/i.test(raw) || /请稍后重试/.test(raw)) {
    let retrySec = 0;
    try {
      const parsed = JSON.parse(raw);
      if (parsed && parsed.retry_after) retrySec = parseInt(parsed.retry_after, 10);
    } catch (_) {
      const match = raw.match(/retry.*?(\d+)/i);
      if (match) retrySec = parseInt(match[1], 10);
    }
    if (retrySec > 0) {
      return `提交过于频繁，请在 ${retrySec} 秒后重试。`;
    }
    return "提交过于频繁，请稍后重试。";
  }
  return raw;
}

function showErrorBanner(msg) {
  const banner = $("smartErrorBanner");
  if (!banner) return;
  banner.textContent = msg;
  banner.classList.add("visible");
}

function hideErrorBanner() {
  $("smartErrorBanner")?.classList.remove("visible");
}

function refreshLabels() {
  window.UmaI18n?.apply(document);
  const balanceEl = $("balance");
  if (balanceEl) {
    balanceEl.textContent = me ? creditsBalance(me.balance_fen) : "-- credits";
  }
}

function openAboutDialog() {
  const dialog = $("aboutDialog");
  if (!dialog) return;
  dialog.classList.remove("hidden");
  document.body.classList.add("modal-open");
  $("aboutOkBtn")?.focus({ preventScroll: true });
}

function closeAboutDialog() {
  const dialog = $("aboutDialog");
  if (!dialog) return;
  dialog.classList.add("hidden");
  document.body.classList.remove("modal-open");
  $("aboutBtn")?.focus({ preventScroll: true });
}

async function refreshMe() {
  try {
    me = await api("/api/me");
  } catch (_) {
    me = null;
  }
  // 同时查询当前 pending_disambiguation 状态
  // 三态：undefined（未加载）| true（active）| false（inactive）| "error"（API失败）
  if (convCode) {
    try {
      const state = await api(`/api/smart-agent/conversations/${encodeURIComponent(convCode)}/state`);
      window._pendingDisambiguationActive = !!state.pending_disambiguation;
    } catch (_) {
      // API 失败时保持上一次状态，不随意设为 false
      if (window._pendingDisambiguationActive === undefined) {
        window._pendingDisambiguationActive = "error";
      }
    }
  }
  updateDisambiguationCardStates();
  refreshLabels();
}

function updateDisambiguationCardStates() {
  const pendingState = window._pendingDisambiguationActive;
  // unknown：尚未加载，不改变卡片状态
  if (pendingState === undefined) return;
  // error：API 加载失败，不把卡片永久禁用
  if (pendingState === "error") return;
  const active = !!pendingState;
  const cards = document.querySelectorAll('.disambiguation-card');
  cards.forEach(card => {
    if (active) {
      card.classList.remove('resolved');
      card.querySelectorAll('button').forEach(b => b.disabled = false);
    } else {
      card.classList.add('resolved');
      card.querySelectorAll('button').forEach(b => b.disabled = true);
    }
  });
}

function isNearMessageBottom(container) {
  if (!container) return true;
  return container.scrollHeight - container.scrollTop - container.clientHeight < 120;
}

function scrollMessagesToBottom(container) {
  if (!container) return;
  container.scrollTo({
    top: container.scrollHeight,
    behavior: isMobileSmartAgent() ? "auto" : "smooth",
  });
}

function appendMessageNode(container, node, forceScroll = false) {
  if (!container || !node) return;
  const shouldScroll = forceScroll || isNearMessageBottom(container);
  container.appendChild(node);
  if (shouldScroll) scrollMessagesToBottom(container);
}

function bindAvatarFallback(image) {
  image.addEventListener("error", () => {
    const parent = image.parentElement;
    image.remove();
    if (parent) parent.textContent = "击";
  }, { once: true });
}

function createAvatar() {
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  const img = document.createElement("img");
  img.src = AVATAR_SRC;
  img.alt = "";
  bindAvatarFallback(img);
  avatar.appendChild(img);
  return avatar;
}

function createAgentRow(bubbleClass = "bubble") {
  const row = document.createElement("div");
  row.className = "msg agent";
  const stack = document.createElement("div");
  stack.className = "agent-stack";
  const name = document.createElement("div");
  name.className = "agent-name";
  name.textContent = t("smart.xiaojiji", "小击击");
  const bubble = document.createElement("div");
  bubble.className = bubbleClass;
  stack.append(name, bubble);
  row.append(createAvatar(), stack);
  return { row, bubble };
}

function messageStatusText(status) {
  switch (String(status || "")) {
    case "pending":
      return t("smart.message_pending", "等待处理");
    case "processing":
      return t("smart.message_processing", "正在处理");
    case "failed":
      return t("smart.message_failed", "失败");
    case "done":
      return t("smart.message_done", "已完成");
    default:
      return t("smart.message_sent", "已发送");
  }
}

function addAssistantBubble(text) {
  const container = $("chatMessages");
  if (!container) return;
  const { row, bubble } = createAgentRow("bubble");
  bubble.textContent = text || "";
  appendMessageNode(container, row);
}

function extractJobCode(text) {
  const match = String(text || "").match(/\bGEN-[A-Z0-9]+\b/i);
  return match ? match[0].toUpperCase() : "";
}

function addAssistantBubbleOnce(text, options = {}) {
  const normalized = String(text || "").trim();
  if (!normalized) return false;
  const jobCode = normalizeJobCode(options.jobCode || extractJobCode(normalized));
  if (jobCode && /任务已加入队列|已提交生成任务|已提交|submitted|queued/i.test(normalized)) {
    if (renderedTaskSubmitKeys.has(jobCode)) return false;
    renderedTaskSubmitKeys.add(jobCode);
  }
  addAssistantBubble(normalized);
  return true;
}

function parsePromptReadyPayload(message) {
  try {
    const data = JSON.parse(message || "{}");
    return data && typeof data === "object" ? data : {};
  } catch (_) {
    return { message: message || "" };
  }
}

async function confirmPromptDraft(button, promptVersion) {
  if (!convCode) return;
  if (confirmingLock) return;
  confirmingLock = true;
  const card = button?.closest(".prompt-ready-card");
  const buttons = card?.querySelectorAll("button") || [];
  buttons.forEach((btn) => { btn.disabled = true; });
  const status = card?.querySelector(".prompt-ready-status");
  if (status) status.textContent = t("smart.prompt_confirming", "正在提交生成任务……");
  addOrUpdateEvent("generating", t("smart.prompt_confirming", "正在提交生成任务……"));
  try {
    const requestId = `prompt-confirm-${convCode}-${promptVersion || activePromptVersion || 1}`;
    const data = await api(`/api/smart-agent/conversations/${encodeURIComponent(convCode)}/prompt-draft/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Client-Request-Id": requestId },
      body: JSON.stringify({ message: "generate" }),
    });
    if (data.job_code) {
      activeJobCodes.add(data.job_code);
      markPromptReadyCardsSubmitted(data.job_code);
    }
    startPolling();
    await refreshMe();
  } catch (error) {
    buttons.forEach((btn) => { btn.disabled = false; });
    if (status) status.textContent = "";
    addErrorBubble(friendlyError(error.message) || t("smart.prompt_confirm_failed", "提交生成失败，请稍后重试。"));
  } finally {
    confirmingLock = false;
  }
}

function renderPromptReadyCard(payload) {
  const container = $("chatMessages");
  if (!container) return;
  const promptVersion = Number(payload.prompt_version || 1);
  if (container.querySelector(`.prompt-ready-card[data-prompt-version="${promptVersion}"]`)) return;
  activePromptVersion = promptVersion;
  const { row, bubble } = createAgentRow("bubble prompt-ready-card");
  bubble.dataset.promptVersion = String(promptVersion);
  const message = document.createElement("p");
  message.className = "prompt-ready-message";
  message.textContent = payload.message || t("smart.prompt_ready_message", "提示词已整理完成，是否现在开始生成？");

  const actions = document.createElement("div");
  actions.className = "prompt-ready-actions";
  const generateBtn = document.createElement("button");
  generateBtn.type = "button";
  generateBtn.className = "primary";
  generateBtn.textContent = t("smart.prompt_start_generate", "开始生成");
  generateBtn.addEventListener("click", () => confirmPromptDraft(generateBtn, promptVersion));
  const reviseBtn = document.createElement("button");
  reviseBtn.type = "button";
  reviseBtn.className = "ghost";
  reviseBtn.textContent = t("smart.prompt_continue_modify", "继续修改");
  reviseBtn.addEventListener("click", () => {
    const input = $("chatInput");
    input?.focus({ preventScroll: true });
  });
  actions.append(generateBtn, reviseBtn);

  const status = document.createElement("div");
  status.className = "prompt-ready-status";
  bubble.append(message, actions, status);
  appendMessageNode(container, row);
}

function markPromptReadyCardsSubmitted(jobCode) {
  const label = jobCode
    ? `${t("smart.task_submitted", "已提交生成任务")}：${normalizeJobCode(jobCode)}`
    : t("smart.task_submitted", "已提交生成任务");
  document.querySelectorAll(".prompt-ready-card").forEach((card) => {
    card.classList.add("submitted");
    card.querySelectorAll("button").forEach((btn) => { btn.disabled = true; });
    const status = card.querySelector(".prompt-ready-status");
    if (status) status.textContent = label;
  });
}

function renderDisambiguationCard(payloadRaw) {
  const container = $("chatMessages");
  if (!container) return;
  let payload;
  try {
    payload = typeof payloadRaw === "string" ? JSON.parse(payloadRaw) : payloadRaw;
  } catch (_) {
    payload = { term: "", candidates: [], group_id: "" };
  }
  const candidates = Array.isArray(payload.candidates) ? payload.candidates : [];
  if (!candidates.length) return;
  const term = payload.term || "";
  const groupId = payload.group_id || "";

  // Dedup: 如果已有相同 group_id 的卡片，跳过
  if (groupId) {
    const existing = container.querySelector(`.disambiguation-card[data-group-id="${CSS.escape(groupId)}"]`);
    if (existing) return;
  }

  const { row, bubble } = createAgentRow("bubble disambiguation-card");
  if (groupId) bubble.dataset.groupId = groupId;
  // 根据全局 pending 状态设置卡片是否可交互
  // 三态：undefined=未加载 → 默认可交互；false=明确 inactive → resolved
  //       "error"=API失败 → 默认可交互（不永久禁用）
  const pendingState = window._pendingDisambiguationActive;
  if (pendingState === false) {
    bubble.classList.add('resolved');
    // 禁用所有按钮
    const buttons = bubble.querySelectorAll('button');
    buttons.forEach(b => b.disabled = true);
  }

  // 只显示干净的提示文本，不显示 JSON
  const message = document.createElement("p");
  message.className = "disambiguation-message";
  message.textContent = term
    ? `检测到"${term}"对应多个角色，请选择：`
    : t("smart.disambiguation_message", "检测到多个可能角色，请选择：");

  const actions = document.createElement("div");
  actions.className = "disambiguation-actions";
  candidates.forEach((candidate) => {
    const charKey = candidate.character_key || "";
    const displayName = candidate.display_name || "";
    const displayNameEn = candidate.display_name_en || "";
    const franchise = candidate.franchise || "";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "disambiguation-btn";
    btn.dataset.action = "select-character-candidate";
    btn.dataset.characterKey = charKey;

    // 只显示中文名 + 作品名
    const nameLine = document.createElement("div");
    nameLine.className = "disambiguation-name";
    nameLine.textContent = displayName || displayNameEn;
    btn.appendChild(nameLine);

    if (displayNameEn && displayName && displayName !== displayNameEn) {
      const enLine = document.createElement("div");
      enLine.className = "disambiguation-name-en";
      enLine.textContent = displayNameEn;
      btn.appendChild(enLine);
    }

    if (franchise) {
      const franchiseLine = document.createElement("div");
      franchiseLine.className = "disambiguation-franchise";
      franchiseLine.textContent = `《${franchise}》`;
      btn.appendChild(franchiseLine);
    }

    btn.addEventListener("click", () => {
      sendDisambiguationChoice(btn, actions, charKey, displayName, franchise, groupId);
    });
    actions.appendChild(btn);
  });

  bubble.append(message, actions);
  appendMessageNode(container, row);
}

async function sendDisambiguationChoice(clickedBtn, allButtons, characterKey, displayName, franchise, groupId) {
  if (!convCode || !characterKey) return;
  const buttons = allButtons?.querySelectorAll("button") || [];
  // 临时禁用防止重复点击，但不加 resolved 类
  buttons.forEach((btn) => { btn.disabled = true; });
  clickedBtn?.classList.add("selected");

  // 显示用户选择（干净文本，不是 JSON）
  const userDisplay = displayName || characterKey;
  addUserBubble(userDisplay, "sent");
  showTyping();
  turnInFlight = true;
  setComposerBusy(true);

  try {
    const requestId = `smart-disambig-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const data = await api(`/api/smart-agent/conversations/${encodeURIComponent(convCode)}/resolve-disambiguation`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Client-Request-Id": requestId },
      body: JSON.stringify({ character_key: characterKey, group_id: groupId || "" }),
    });
    if (data.processing || data.job_code) {
      startPolling();
    } else {
      finishTurnUi();
    }
    // 成功后刷新 pending 状态，卡片将变为 resolved
    await refreshMe();
  } catch (e) {
    finishTurnUi();
    hideTyping();
    // 失败时恢复按钮，不把卡片永久变灰
    buttons.forEach((btn) => { btn.disabled = false; });
    clickedBtn?.classList.remove("selected");
    const errMsg = (e && e.message) || "";
    // 显示友好错误（不显示 409 的 "当前没有待确认" 等后端已处理的情况）
    if (errMsg && errMsg.includes("409")) {
      addErrorBubble("该候选已失效，请刷新页面或重新请求。");
    } else {
      addErrorBubble(friendlyError(errMsg) || "选择失败，请重试");
    }
  }
}

function addUserBubble(text, status = "sent") {
  const container = $("chatMessages");
  if (!container) return null;
  const row = document.createElement("div");
  row.className = "msg user";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text || "";
  const statusNode = document.createElement("div");
  statusNode.className = "message-status";
  statusNode.dataset.status = status;
  statusNode.textContent = messageStatusText(status);
  row.append(bubble, statusNode);
  appendMessageNode(container, row);
  return statusNode;
}

function setStatusNode(statusNode, status) {
  if (!statusNode) return;
  statusNode.dataset.status = status;
  statusNode.textContent = messageStatusText(status);
}

function updateFirstUserMessageStatus(fromStatuses, toStatus) {
  const wanted = new Set(fromStatuses);
  const nodes = $("chatMessages")?.querySelectorAll(".msg.user .message-status") || [];
  for (const statusNode of nodes) {
    if (wanted.has(statusNode.dataset.status || "")) {
      setStatusNode(statusNode, toStatus);
      return true;
    }
  }
  return false;
}

function addEventBubble(text, options = {}) {
  if (!text) return;
  const container = $("chatMessages");
  if (!container) return;
  const { row, bubble } = createAgentRow("bubble event");
  bubble.textContent = text;
  if (options.progress) bubble.dataset.progress = "agent";
  appendMessageNode(container, row);
}

function addOrUpdateEvent(eventType, text) {
  if (!text) return;
  const container = $("chatMessages");
  if (!container) return;
  if (PROGRESS_EVENT_TYPES.has(eventType)) {
    const progressBubble = container.querySelector('.bubble.event[data-progress="agent"]');
    if (progressBubble) {
      const shouldScroll = isNearMessageBottom(container);
      progressBubble.textContent = text;
      if (shouldScroll) scrollMessagesToBottom(container);
      return;
    }
    addEventBubble(text, { progress: true });
    return;
  }
  for (const bubble of container.querySelectorAll(".bubble.event")) {
    if (bubble.textContent === text) return;
  }
  addEventBubble(text);
}

function addErrorBubble(text) {
  const container = $("chatMessages");
  if (!container) return;
  const { row, bubble } = createAgentRow("bubble error");
  bubble.textContent = text || t("smart.failed", "暂时无法创建 Smart Agent 任务。");
  appendMessageNode(container, row);
}

function resetGenerationCards() {
  generationCards = new Map();
}

function resetConversationUiState() {
  lastEventId = 0;
  renderedEventIds = new Set();
  renderedImageKeys = new Set();
  renderedTaskSubmitKeys = new Set();
  resetGenerationCards();
  activeJobCodes = new Set();
  hideTyping();
  confirmingLock = false;
  sendingLock = false;
  turnInFlight = false;
  setComposerBusy(false);
  window._pendingDisambiguationActive = undefined;
}

function normalizeJobCode(jobCode) {
  return String(jobCode || "").trim();
}

function generationStatusLabel(status) {
  switch (String(status || "")) {
    case "queued":
      return t("smart.job_queued", "任务已加入队列。");
    case "processing":
      return t("smart.job_processing", "正在生成图片……");
    case "done":
      return t("smart.job_done", "生成完成。");
    case "failed":
    case "failed_refunded":
      return t("smart.job_failed", "生成失败，费用已退回。");
    case "cancelled":
    case "cancelled_refunded":
      return t("smart.job_cancelled", "任务已取消。");
    default:
      return "";
  }
}

function getOrCreateGenerationCard(jobCode, options = {}) {
  const container = $("chatMessages");
  const codeText = normalizeJobCode(jobCode);
  if (!container || !codeText) return null;
  const existing = generationCards.get(codeText);
  if (existing) return existing;

  const { row, bubble } = createAgentRow("bubble image-card");
  bubble.classList.add("generation-card");
  bubble.dataset.jobCode = codeText;

  const status = document.createElement("div");
  status.className = "generation-status";
  status.textContent = options.statusText || t("smart.job_queued", "任务已加入队列。");

  const outputsEl = document.createElement("div");
  outputsEl.className = "generation-outputs";

  const code = document.createElement("div");
  code.className = "job-code";
  code.textContent = `Smart Agent · ${t("smart.job_code", "任务号")}：${codeText}`;

  bubble.append(status, outputsEl, code);
  const state = {
    row,
    bubble,
    status,
    outputsEl,
    outputs: new Map(),
  };
  generationCards.set(codeText, state);
  appendMessageNode(container, row);
  return state;
}

function updateGenerationCardStatus(jobCode, statusText, options = {}) {
  const codeText = normalizeJobCode(jobCode);
  if (!codeText) return;
  const card = getOrCreateGenerationCard(codeText, { statusText });
  if (!card) return;
  if (statusText) card.status.textContent = statusText;
  card.bubble.classList.toggle("generation-error", Boolean(options.error));
}

function upsertGenerationOutput(jobCode, output) {
  const codeText = normalizeJobCode(jobCode);
  const imgUrl = output?.url || "";
  if (!codeText || !imgUrl) return;
  const imageKey = `${codeText}:${imgUrl}`;
  if (renderedImageKeys.has(imageKey)) return;
  renderedImageKeys.add(imageKey);

  const container = $("chatMessages");
  const card = getOrCreateGenerationCard(codeText, { statusText: generationStatusLabel("processing") });
  if (!container || !card) return;
  const shouldScrollAfterImageLoad = isNearMessageBottom(container);

  const item = document.createElement("figure");
  item.className = "generation-output";
  const img = document.createElement("img");
  img.className = "generated-img";
  img.src = imgUrl;
  img.alt = codeText || "Smart Agent output";
  img.loading = "lazy";
  img.decoding = "async";
  img.addEventListener("error", () => {
    const shouldScroll = isNearMessageBottom(container);
    img.remove();
    const note = document.createElement("div");
    note.className = "job-code";
    note.textContent = t("smart.image_load_failed", "图片暂时无法加载，请稍后在任务列表查看。");
    item.appendChild(note);
    if (shouldScroll) scrollMessagesToBottom(container);
  }, { once: true });
  img.addEventListener("load", () => {
    if (shouldScrollAfterImageLoad) scrollMessagesToBottom(container);
  }, { once: true });
  item.appendChild(img);

  const caption = output.caption || "";
  if (caption) {
    const figcaption = document.createElement("figcaption");
    figcaption.className = "image-caption";
    figcaption.textContent = caption;
    item.appendChild(figcaption);
  }

  // PC端操作按钮：保存图片 + 复制图片
  if (typeof isDesktopWithPointer === "function" && isDesktopWithPointer() && typeof copyImageToClipboard === "function") {
    const actions = document.createElement("div");
    actions.className = "sa-output-actions";
    const saveBtn = document.createElement("button");
    saveBtn.className = "copy-image-button";
    saveBtn.textContent = t("task.save", "保存图片");
    saveBtn.onclick = async () => {
      saveBtn.disabled = true;
      saveBtn.textContent = t("task.saving", "保存中…");
      try { await downloadImageSimple(imgUrl, "UMA_" + codeText + ".png"); }
      catch (e) { alert(e.message || t("task.download_failed", "下载失败")); }
      finally { saveBtn.disabled = false; saveBtn.textContent = t("task.save", "保存图片"); }
    };
    const copyBtn = document.createElement("button");
    copyBtn.className = "copy-image-button";
    copyBtn.textContent = t("task.copy_image", "复制图片");
    copyBtn.onclick = async () => {
      copyBtn.disabled = true;
      copyBtn.textContent = t("task.copying", "正在复制……");
      try {
        await copyImageToClipboard(imgUrl);
        copyBtn.disabled = false;
        copyBtn.textContent = t("task.copied", "已复制");
        setTimeout(() => { copyBtn.textContent = t("task.copy_image", "复制图片"); }, 1500);
      } catch (e) {
        copyBtn.disabled = false;
        copyBtn.textContent = t("task.copy_failed", "复制失败");
        setTimeout(() => { copyBtn.textContent = t("task.copy_image", "复制图片"); }, 2000);
        alert(e.message || t("task.copy_failed", "复制图片失败，请使用保存图片功能。"));
      }
    };
    actions.append(saveBtn, copyBtn);
    item.appendChild(actions);
  }

  card.outputs.set(imageKey, item);
  card.outputsEl.appendChild(item);
  card.status.textContent = generationStatusLabel("done");
}

function addImageCard(jobCode, imgUrl, caption) {
  upsertGenerationOutput(jobCode, { url: imgUrl, caption });
}

function renderGeneratedOutputs(jobCode, imgData) {
  const codeText = normalizeJobCode(imgData?.job_code || jobCode);
  if (!codeText) return;
  activeJobCodes.add(codeText);
  if (Array.isArray(imgData?.outputs) && imgData.outputs.length) {
    imgData.outputs.forEach((out, idx) => {
      if (out?.url) {
        upsertGenerationOutput(codeText, {
          url: out.url,
          caption: out.caption || imgData.caption || (imgData.outputs.length > 1 ? `${idx + 1}/${imgData.outputs.length}` : ""),
        });
      }
    });
    updateGenerationCardStatus(codeText, generationStatusLabel("done"));
  } else if (imgData?.url) {
    upsertGenerationOutput(codeText, { url: imgData.url, caption: imgData.caption || "" });
    updateGenerationCardStatus(codeText, generationStatusLabel("done"));
  }
}

function showTyping() {
  const container = $("chatMessages");
  if (!container || $("typingRow")) return;
  const { row, bubble } = createAgentRow("bubble");
  row.id = "typingRow";
  const dots = document.createElement("span");
  dots.className = "typing-dots";
  dots.append(document.createElement("span"), document.createElement("span"), document.createElement("span"));
  bubble.appendChild(dots);
  appendMessageNode(container, row);
}

function hideTyping() {
  $("typingRow")?.remove();
}

function setComposerBusy(isBusy) {
  const busy = Boolean(isBusy);
  const input = $("chatInput");
  const button = $("sendBtn");
  if (input) input.disabled = busy;
  if (button) button.disabled = busy;
}

function finishTurnUi() {
  turnInFlight = false;
  sendingLock = false;
  setComposerBusy(false);
  const input = $("chatInput");
  if (input && !isMobileSmartAgent()) input.focus({ preventScroll: true });
}

function resetComposerState() {
  setComposerBusy(false);
  autoResizeInput();
}

function showWelcome() {
  const container = $("chatMessages");
  if (!container) return;
  container.innerHTML = "";
  renderedImageKeys = new Set();
  renderedTaskSubmitKeys = new Set();
  resetGenerationCards();
  const text = lang() === "en"
    ? "Tell me what image you want. I can help choose character tags, prompt style, workflow, LoRA, and resolution."
    : "击击，告诉我你想生成什么图。我可以帮你匹配角色 Tag、提示词风格、工作流、LoRA 和画幅。";
  addAssistantBubble(text);
}

function formatHistoryTime(ts) {
  const num = Number(ts || 0);
  if (!num) return "";
  try {
    return new Date(num * 1000).toLocaleString(lang() === "en" ? "en-NZ" : "zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch (_) {
    return "";
  }
}

function closeHistory() {
  $("historyPanel")?.classList.remove("open");
}

async function loadHistoryList() {
  const panel = $("historyPanel");
  if (!panel) return;
  try {
    const data = await api("/api/smart-agent/conversations");
    const convs = data.conversations || [];
    panel.innerHTML = "";
    if (!convs.length) {
      const empty = document.createElement("div");
      empty.className = "history-empty";
      empty.textContent = t("smart.history_empty", "暂无历史聊天。");
      panel.appendChild(empty);
      return;
    }
    for (const item of convs) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "history-item";
      if (item.conversation_code === convCode) btn.classList.add("active");
      btn.dataset.code = item.conversation_code;

      const title = document.createElement("div");
      title.className = "history-title";
      title.textContent = item.title || t("smart.untitled_chat", "未命名聊天");
      const time = document.createElement("div");
      time.className = "history-time";
      time.textContent = formatHistoryTime(item.updated_at);
      btn.append(title, time);
      btn.addEventListener("click", () => selectConversation(item.conversation_code));
      panel.appendChild(btn);
    }
  } catch (e) {
    panel.innerHTML = "";
    const error = document.createElement("div");
    error.className = "history-empty";
    error.textContent = friendlyError(e.message);
    panel.appendChild(error);
  }
}

async function toggleHistory() {
  const panel = $("historyPanel");
  if (!panel) return;
  const willOpen = !panel.classList.contains("open");
  if (willOpen) {
    await loadHistoryList();
    panel.classList.add("open");
  } else {
    panel.classList.remove("open");
  }
}

async function selectConversation(code) {
  if (!code || code === convCode) {
    closeHistory();
    return;
  }
  convCode = code;
  resetConversationUiState();
  stopPolling();
  closeHistory();
  await loadConvHistory();
  await syncLatestEvents(false);
  resetComposerState();
  if (activeJobCodes.size) startPolling();
}

async function getOrCreateConv() {
  const data = await api("/api/smart-agent/conversations");
  const convs = data.conversations || [];
  if (convs.length > 0) return convs[0].conversation_code;
  const created = await api("/api/smart-agent/conversations", { method: "POST" });
  return created.conversation_code;
}

async function loadConvHistory() {
  if (!convCode) return;
  try {
    const data = await api(`/api/smart-agent/conversations/${encodeURIComponent(convCode)}`);
    const container = $("chatMessages");
    if (!container) return;
    container.innerHTML = "";
    resetConversationUiState();
    const messages = data.messages || [];
    for (const msg of messages) {
      const content = msg.content || "";
      if (msg.role === "user") {
        addUserBubble(content, msg.status || "done");
      } else if (msg.role === "assistant") {
        addAssistantBubbleOnce(content);
      } else if (msg.role === "image" || msg.role === "output") {
        renderImageMessage(content);
      } else if (msg.role === "system_event" || msg.role === "tool_event") {
        if (content && content !== "system_event") addEventBubble(content);
      }
    }
    if (container.children.length === 0) showWelcome();
    resetComposerState();
  } catch (e) {
    showWelcome();
    addErrorBubble(friendlyError(e.message));
    resetComposerState();
  }
}

function renderImageMessage(content) {
  try {
    const imgData = JSON.parse(content || "{}");
    renderGeneratedOutputs(imgData.job_code, imgData);
  } catch (_) {
    addAssistantBubble(content);
  }
}

async function newChat() {
  try {
    const created = await api("/api/smart-agent/conversations", { method: "POST" });
    convCode = created.conversation_code;
    resetConversationUiState();
    stopPolling();
    closeHistory();
    showWelcome();
    addEventBubble(t("smart.new_chat_started", "已开启新聊天。"));
    loadHistoryList();
    resetComposerState();
  } catch (e) {
    addErrorBubble(friendlyError(e.message) || "创建新聊天失败");
    resetComposerState();
  }
}

async function clearMemory() {
  if (!convCode) return;
  try {
    await api(`/api/smart-agent/conversations/${encodeURIComponent(convCode)}/clear`, { method: "POST" });
    resetConversationUiState();
    stopPolling();
    closeHistory();
    showWelcome();
    addEventBubble(t("smart.memory_cleared", "当前聊天记忆已清空。"));
    resetComposerState();
  } catch (e) {
    addErrorBubble(friendlyError(e.message) || "清空记忆失败");
    resetComposerState();
  }
}

function getPollDelay() {
  return document.visibilityState === "hidden" ? POLL_HIDDEN_MS : POLL_VISIBLE_MS;
}

function startPolling() {
  if (pollingTimer) return;
  idlePollsAfterTerminal = 0;
  pollEvents();
  pollingTimer = setInterval(pollEvents, getPollDelay());
}

function restartPollingIfNeeded() {
  if (!pollingTimer) return;
  stopPolling();
  startPolling();
}

function stopPolling() {
  if (pollingTimer) {
    clearInterval(pollingTimer);
    pollingTimer = null;
  }
}

async function syncLatestEvents(renderMessages = true) {
  if (!convCode) return [];
  const data = await api(`/api/smart-agent/conversations/${encodeURIComponent(convCode)}/events?after_id=0`);
  const events = data.events || [];
  let latestPromptReady = null;
  let generatedAfterLatestPrompt = false;
  let latestDisambiguation = null;
  let disambiguationResolved = false;
  for (const ev of events) {
    if (ev.id > lastEventId) lastEventId = ev.id;
    if (ev.event_type === "queued" && ev.job_code) activeJobCodes.add(ev.job_code);
    if (ev.event_type === "prompt_ready") {
      latestPromptReady = ev;
      generatedAfterLatestPrompt = false;
    } else if (ev.event_type === "generated" && latestPromptReady) {
      generatedAfterLatestPrompt = true;
    }
    if (ev.event_type === "character_disambiguation") {
      latestDisambiguation = ev;
      disambiguationResolved = false;
    } else if (ev.event_type === "assistant_message" && latestDisambiguation) {
      // An assistant message after disambiguation likely means resolution
      disambiguationResolved = true;
    }
  }
  if (!renderMessages) {
    // 恢复模式：渲染所有关键历史事件（不依赖 polling）
    for (const ev of events) {
      try {
        if (ev.event_type === "assistant_message" && ev.public_message) {
          addAssistantBubbleOnce(ev.public_message, { jobCode: ev.job_code });
        } else if (ev.event_type === "generated" && ev.public_message) {
          addAssistantBubbleOnce(ev.public_message, { jobCode: ev.job_code });
        } else if (ev.event_type === "message_processing" || ev.event_type === "message_pending") {
          if (ev.public_message) addOrUpdateEvent(ev.event_type, ev.public_message);
        } else if (ev.event_type === "character_disambiguation") {
          // 恢复歧义候选卡（仅当 pending 仍有效时）
          const pendingState = window._pendingDisambiguationActive;
          if (pendingState && pendingState !== false && pendingState !== "error") {
            renderDisambiguationCard(ev.public_message);
          }
        } else if (ev.event_type === "prompt_ready") {
          if (!config?.v2_enabled && !generatedAfterLatestPrompt && ev === latestPromptReady) {
            renderPromptReadyCard(parsePromptReadyPayload(ev.public_message));
          }
        }
      } catch (renderErr) {
        console.warn("smart-agent: failed to restore event", ev.event_type, ev.id, renderErr);
      }
    }
  }
  if (renderMessages) return events;
  resetComposerState();
  return [];
}

async function pollEvents() {
  if (!convCode) return;
  try {
    const data = await api(`/api/smart-agent/conversations/${encodeURIComponent(convCode)}/events?after_id=${lastEventId}`);
    const events = data.events || [];
    let terminalEvent = false;
    let hasDisambiguation = false;
    if (events.length) idlePollsAfterTerminal = 0;
    for (const ev of events) {
      if (ev.id > lastEventId) lastEventId = ev.id;
      if (renderedEventIds.has(ev.id)) continue;
      renderedEventIds.add(ev.id);
      try {
        renderEvent(ev);
      } catch (renderErr) {
        console.warn("smart-agent: failed to render event", ev.event_type, ev.id, renderErr);
        // continue rendering other events
      }
      if (ev.event_type === "queued" && ev.job_code) {
        activeJobCodes.add(ev.job_code);
        try { sessionStorage.setItem("uma_current_job", ev.job_code); } catch (_) {}
      }
      if (ev.event_type === "character_disambiguation") {
        hasDisambiguation = true;
      }
      if (["done", "failed", "refunded", "error"].includes(ev.event_type)) {
        terminalEvent = true;
      }
    }

    // 收到歧义事件后刷新 pending 状态（防止卡片被误认为 resolved）
    if (hasDisambiguation) {
      await refreshMe();
    }

    const jobCodes = Array.from(activeJobCodes);
    for (const jobCode of jobCodes) {
      const done = await pollTaskForImages(jobCode);
      if (done) activeJobCodes.delete(jobCode);
    }

    if (terminalEvent && activeJobCodes.size === 0) {
      idlePollsAfterTerminal = 1;
    } else if (!events.length && activeJobCodes.size === 0 && idlePollsAfterTerminal > 0) {
      idlePollsAfterTerminal += 1;
    }

    if (
      (idlePollsAfterTerminal >= 10 && activeJobCodes.size === 0) ||
      (!events.length && activeJobCodes.size === 0 && document.visibilityState === "hidden")
    ) {
      if (terminalEvent) await refreshMe();
      stopPolling();
    }
  } catch (_) {
    // Polling errors should not interrupt user typing.
  }
}

function renderEvent(ev) {
  if (ev.event_type === "message_pending" || ev.event_type === "message_processing") {
    if (ev.event_type === "message_processing") {
      updateFirstUserMessageStatus(["sent", "pending"], "processing");
    }
    if (ev.public_message) addOrUpdateEvent(ev.event_type, ev.public_message);
    return;
  }
  if (ev.event_type === "done") {
    updateFirstUserMessageStatus(["processing", "pending", "sent"], "done");
    hideTyping();
    finishTurnUi();
    return;
  }
  if (ev.event_type === "assistant_message") {
    hideTyping();
    if (ev.public_message) addAssistantBubbleOnce(ev.public_message, { jobCode: ev.job_code });
    return;
  }
  if (ev.event_type === "prompt_ready") {
    hideTyping();
    updateFirstUserMessageStatus(["processing", "pending", "sent"], "done");
    if (!config?.v2_enabled) {
      renderPromptReadyCard(parsePromptReadyPayload(ev.public_message));
    }
    finishTurnUi();
    return;
  }
  if (ev.event_type === "character_disambiguation") {
    hideTyping();
    updateFirstUserMessageStatus(["processing", "pending", "sent"], "done");
    renderDisambiguationCard(ev.public_message);
    finishTurnUi();
    return;
  }
  if (ev.event_type === "generated") {
    hideTyping();
    updateFirstUserMessageStatus(["processing", "pending", "sent"], "done");
    finishTurnUi();
    if (ev.public_message) addAssistantBubbleOnce(ev.public_message, { jobCode: ev.job_code });
    if (ev.job_code) {
      activeJobCodes.add(ev.job_code);
      markPromptReadyCardsSubmitted(ev.job_code);
      try { sessionStorage.setItem("uma_current_job", ev.job_code); } catch (_) {}
    }
    return;
  }
  if (ev.event_type === "image_generated" && ev.public_message) {
    try {
      const imgData = JSON.parse(ev.public_message);
      renderGeneratedOutputs(ev.job_code, imgData);
    } catch (_) {}
    return;
  }
  if (ev.job_code && JOB_EVENT_TYPES.has(ev.event_type)) {
    hideTyping();
    if (ev.event_type === "queued") {
      updateFirstUserMessageStatus(["processing", "pending", "sent"], "done");
      finishTurnUi();
    }
    const message = ev.public_message || generationStatusLabel(ev.event_type);
    updateGenerationCardStatus(ev.job_code, message, { error: ["failed", "refunded", "error"].includes(ev.event_type) });
    return;
  }
  if (ev.event_type === "failed" || ev.event_type === "error") {
    hideTyping();
    updateFirstUserMessageStatus(["processing", "pending", "sent"], "failed");
    addErrorBubble(ev.public_message || t("smart.failed", "暂时无法创建 Smart Agent 任务。"));
    finishTurnUi();
    return;
  }
  if (ev.public_message) addOrUpdateEvent(ev.event_type, ev.public_message);
}

async function pollTaskForImages(jobCode) {
  try {
    const data = await api(`/api/tasks/${encodeURIComponent(jobCode)}`);
    const item = data.item || {};
    const outputs = item.outputs || [];
    if (item.status === "done" && outputs.length) {
      renderGeneratedOutputs(jobCode, {
        job_code: jobCode,
        outputs: outputs.map((out, index) => ({
          url: out.url || (out.id ? `/api/outputs/${out.id}` : ""),
          caption: out.label || (outputs.length > 1 ? `${index + 1}/${outputs.length}` : ""),
        })),
      });
      await refreshMe();
      return true;
    }
    if (["failed_refunded", "cancelled_refunded", "cancelled"].includes(item.status)) {
      updateGenerationCardStatus(
        jobCode,
        item.error || item.error_message || generationStatusLabel(item.status) || t("smart.failed", "暂时无法创建 Smart Agent 任务。"),
        { error: true },
      );
      await refreshMe();
      return true;
    }
  } catch (_) {}
  return false;
}

async function sendMessage() {
  if (sendingLock || turnInFlight) return;
  const input = $("chatInput");
  const button = $("sendBtn");
  if (!input || !button || !convCode) return;
  const text = input.value.trim();
  if (!text) return;

  sendingLock = true;
  turnInFlight = true;
  setComposerBusy(true);
  let keepLocked = true;
  input.value = "";
  autoResizeInput();
  const localStatus = addUserBubble(text, "sent");
  showTyping();

  try {
    const requestId = `smart-msg-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const data = await api(`/api/smart-agent/conversations/${encodeURIComponent(convCode)}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Client-Request-Id": requestId },
      body: JSON.stringify({ message: text }),
    });
    setStatusNode(localStatus, data.duplicate ? "sent" : "pending");
    if (data.assistant_message) {
      hideTyping();
      addAssistantBubbleOnce(data.assistant_message, { jobCode: data.job_code });
    }
    if (data.job_code) activeJobCodes.add(data.job_code);
    if (data.processing || data.job_code || data.draft_ready) {
      startPolling();
    } else {
      keepLocked = false;
    }
    await refreshMe();
  } catch (e) {
    keepLocked = false;
    setStatusNode(localStatus, "failed");
    addErrorBubble(friendlyError(e.message) || "发送失败");
  } finally {
    sendingLock = false;
    if (!keepLocked) finishTurnUi();
  }
}

function autoResizeInput() {
  resizeComposerInput();
}

async function init() {
  installViewportHeightHandlers();
  window.UmaI18n?.apply(document);
  $("balance").textContent = "-- credits";
  resetComposerState();

  try {
    me = await api("/api/me");
    refreshLabels();
  } catch (e) {
    showErrorBanner(friendlyError(e.message) || "请先登录。");
    return;
  }

  try {
    config = await api("/api/smart-agent/config");
  } catch (_) {
    config = { enabled: true, cost_credits: 5 };
  }

  try {
    convCode = await getOrCreateConv();
    // 重置 cursor 和渲染状态（页面首次加载/刷新）
    resetConversationUiState();
    stopPolling();
    // 1. 加载聊天历史（用户/助手消息 + 事件）
    await loadConvHistory();
    // 2. 加载服务端当前状态（pending、prompt_ready 等）
    await refreshMe();
    // 3. 同步事件并恢复待处理卡片（如 disambiguation / prompt_ready）
    await syncLatestEvents(false);
    resetComposerState();
    if (activeJobCodes.size) startPolling();
  } catch (e) {
    showErrorBanner(friendlyError(e.message));
    resetComposerState();
    return;
  }

  if (!config.enabled) {
    addEventBubble(t("smart.disabled", "Smart Agent 暂未开放。"));
  }
  hideErrorBanner();
  resetComposerState();
}

$("composer")?.addEventListener("submit", (e) => {
  e.preventDefault();
  sendMessage();
});

$("chatInput")?.addEventListener("compositionstart", () => {
  isComposing = true;
});

$("chatInput")?.addEventListener("compositionend", () => {
  isComposing = false;
  resizeComposerInput();
});

$("chatInput")?.addEventListener("focus", () => {
  composerFocused = true;
});

$("chatInput")?.addEventListener("blur", () => {
  composerFocused = false;
});

$("chatInput")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && !isComposing) {
    e.preventDefault();
    $("composer")?.requestSubmit();
  }
});

$("chatInput")?.addEventListener("input", resizeComposerInput, { passive: true });
$("newChatBtn")?.addEventListener("click", newChat);
$("clearMemBtn")?.addEventListener("click", clearMemory);
$("historyBtn")?.addEventListener("click", toggleHistory);
$("aboutBtn")?.addEventListener("click", openAboutDialog);
$("aboutCloseBtn")?.addEventListener("click", closeAboutDialog);
$("aboutOkBtn")?.addEventListener("click", closeAboutDialog);
$("aboutDialog")?.addEventListener("click", (e) => {
  if (e.target === $("aboutDialog")) closeAboutDialog();
});
$("backBtn")?.addEventListener("click", () => {
  window.location.href = "/";
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("aboutDialog")?.classList.contains("hidden")) {
    closeAboutDialog();
  }
});

document.addEventListener("click", (e) => {
  const panel = $("historyPanel");
  const button = $("historyBtn");
  if (!panel || !button) return;
  if (!panel.contains(e.target) && e.target !== button) closeHistory();
});

document.addEventListener("visibilitychange", restartPollingIfNeeded);
window.addEventListener("uma:langchange", refreshLabels);
window.addEventListener("pageshow", () => {
  installViewportHeightHandlers();
  resetComposerState();
});
window.addEventListener("pagehide", () => {
  removeViewportHeightHandlers();
  cancelComposerInputResize();
});
window.addEventListener("DOMContentLoaded", init);
