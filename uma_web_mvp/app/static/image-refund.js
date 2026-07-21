const $ = (id) => document.getElementById(id);
let me = null;
let selectedJobCode = "";
let expandedJobCode = "";
let eligibleItems = [];
let reviewItems = [];
let reviewsExpanded = false;
const submittingJobs = new Set();

function t(key, fallback, params) {
  return window.UmaI18n?.t(key, fallback, params) ?? (fallback ?? key);
}

function translateMessage(text) {
  return window.UmaI18n?.translateMessage(text) ?? text;
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
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  return { ...options, headers };
}

async function api(url, options = {}) {
  const res = await fetch(url, { credentials: "same-origin", ...withCsrf(options) });
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const _msg = (typeof getApiErrorMessage === 'function')
      ? getApiErrorMessage(data, `HTTP ${res.status}`)
      : (data?.detail || `HTTP ${res.status}`);
    const error = new Error(translateMessage(_msg));
    error.status = res.status;
    throw error;
  }
  return data;
}

function credits(value) {
  return `${Math.trunc(Number(value || 0))} credits`;
}

function setMessage(text, type = "") {
  const node = $("refundMessage");
  if (!node) return;
  node.textContent = translateMessage(text);
  node.className = `message ${type}`;
}

function statusLabel(status) {
  return t(`refund.status.${status || "pending"}`, status || "");
}

function fmtTime(seconds) {
  if (!seconds) return "";
  return new Date(Number(seconds) * 1000).toLocaleString();
}

function node(tag, className = "", text = "") {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text) el.textContent = text;
  return el;
}

function outputCountText(item) {
  return t("refund.output_count", "{value} 张输出", { value: (item.outputs || []).length });
}

function findEligible(jobCode) {
  return eligibleItems.find((item) => item.job_code === jobCode) || null;
}

function openPreview(src, alt) {
  const modal = $("refundPreviewModal");
  const img = $("refundPreviewImage");
  if (!modal || !img || !src) return;
  img.src = src;
  img.alt = alt || "";
  modal.classList.remove("hidden");
  document.body.classList.add("modal-open");
}

function closePreview() {
  const modal = $("refundPreviewModal");
  const img = $("refundPreviewImage");
  if (img) img.removeAttribute("src");
  modal?.classList.add("hidden");
  document.body.classList.remove("modal-open");
}

function renderOutputs(parent, outputs, jobCode) {
  const grid = node("div", "refund-output-grid");
  (outputs || []).forEach((output, index) => {
    const card = node("button", "refund-output-card", "");
    card.type = "button";
    const img = document.createElement("img");
    img.src = output.url;
    img.alt = `${jobCode || "output"} ${index + 1}`;
    img.loading = "lazy";
    img.decoding = "async";
    const label = node("span", "", output.label || `#${index + 1}`);
    card.append(img, label);
    card.addEventListener("click", (event) => {
      event.stopPropagation();
      openPreview(output.url, img.alt);
    });
    grid.append(card);
  });
  parent.append(grid);
}

/** @param {Object} item - task item from eligibleItems */
function renderDetail(parent, item) {
  const detail = node("div", "refund-task-detail");
  const prompt = node("p", "refund-prompt-full", item.original_prompt_preview || "");
  detail.append(prompt);
  renderOutputs(detail, item.outputs, item.job_code);

  const noteLabel = node("label", "", t("refund.note", "补充说明"));
  const note = document.createElement("textarea");
  note.rows = 4;
  note.maxLength = 500;
  note.setAttribute("data-refund-note", item.job_code);
  note.placeholder = t("refund.note_placeholder", "可简单说明哪里出现严重结构崩坏，不要填写隐私信息。");
  noteLabel.append(note);

  const confirmLabel = node("label", "check");
  const confirm = document.createElement("input");
  confirm.type = "checkbox";
  confirm.setAttribute("data-refund-confirm", item.job_code);
  confirmLabel.append(confirm, node("span", "", t("refund.confirm", "我确认该任务所有输出均存在严重畸形且不可使用。")));

  // 提交按钮：使用 data-action 属性，事件委托在 #eligibleList 上统一处理
  const submit = node("button", "primary", t("refund.submit", "提交审核"));
  submit.type = "button";
  submit.setAttribute("data-action", "submit-refund");
  submit.setAttribute("data-job-code", item.job_code);

  detail.append(noteLabel, confirmLabel, submit);
  parent.append(detail);
}

function renderEligible(items) {
  eligibleItems = items || [];
  const list = $("eligibleList");
  list.replaceChildren();
  if (!eligibleItems.length) {
    list.append(node("p", "muted", t("refund.empty_eligible", "暂无可申请退款的完成任务。")));
    return;
  }
  eligibleItems.forEach((item) => {
    const isExpanded = item.job_code === expandedJobCode;
    const card = node("article", "refund-task-card", "");
    if (item.job_code === selectedJobCode) card.classList.add("active");

    const header = node("div", "refund-task-summary");
    const title = node("strong", "refund-job-code", item.job_code || "");
    const meta = node("div", "refund-task-meta");
    meta.append(
      node("span", "", fmtTime(item.finished_at || item.created_at)),
      node("span", "", credits(item.charged_credits || 0)),
      node("span", "", outputCountText(item)),
    );
    const prompt = node("p", "refund-prompt-preview", item.original_prompt_preview || "");
    header.append(title, meta, prompt);

    const actions = node("div", "refund-task-actions");
    const selectBtn = node("button", "ghost compact", t("refund.select_this", "选择该任务"));
    selectBtn.type = "button";
    selectBtn.addEventListener("click", () => selectTask(item.job_code));
    const expandBtn = node("button", "ghost compact", isExpanded ? t("refund.collapse_details", "收起详情") : t("refund.expand_details", "展开详情"));
    expandBtn.type = "button";
    expandBtn.addEventListener("click", () => toggleTask(item.job_code));
    actions.append(selectBtn, expandBtn);

    card.append(header, actions);
    if (isExpanded) renderDetail(card, item);
    list.append(card);
  });
}

function renderReviews(items) {
  reviewItems = items || [];
  const list = $("reviewList");
  list.replaceChildren();
  const header = node("div", "refund-history-header");
  const title = node("strong", "", t("refund.history_count", "申请记录（{value}）", { value: reviewItems.length }));
  const toggle = node("button", "ghost compact", reviewsExpanded ? t("refund.history_collapse", "收起申请记录") : t("refund.history_expand", "展开申请记录"));
  toggle.type = "button";
  toggle.addEventListener("click", () => {
    reviewsExpanded = !reviewsExpanded;
    renderReviews(reviewItems);
  });
  header.append(title);
  if (reviewItems.length) header.append(toggle);
  list.append(header);
  if (!reviewItems.length) {
    list.append(node("p", "muted", t("refund.empty_history", "暂无退款申请记录。")));
    return;
  }
  if (!reviewsExpanded) {
    list.append(node("p", "muted", t("refund.history_folded_hint", "已折叠，展开后可查看审核进度。")));
    return;
  }
  reviewItems.forEach((item) => {
    const card = node("article", "refund-review-card");
    card.append(
      node("strong", "", `${item.job_code || ""} · ${statusLabel(item.status)}`),
      node("span", "muted", t("refund.review_code", "审核编号：{value}", { value: item.review_code || "" })),
      node("span", "muted", t("refund.charged", "扣费：{value} credits", { value: item.charged_credits || 0 })),
    );
    if (item.created_at) {
      card.append(node("span", "muted", fmtTime(item.created_at)));
    }
    if (item.refunded_at || item.status === "refunded") {
      card.append(node("span", "status-pill", t("refund.status.refunded", "已退款")));
    }
    if (item.public_reason && item.public_reason.includes("管理员")) {
      card.append(node("span", "muted", t("refund.manual_approved", "管理员人工批准退款")));
    } else if (item.decision) {
      card.append(node("span", "muted", t("refund.auto_result", "自动审核结果")));
    }
    if (item.public_reason) {
      card.append(node("p", "", `${t("refund.reason", "审核说明")}：${item.public_reason}`));
    }
    if (item.can_request_manual_review) {
      const manual = node("div", "refund-manual-review");
      const note = document.createElement("textarea");
      note.rows = 3;
      note.maxLength = 500;
      note.placeholder = t("refund.manual_note_placeholder", "可补充说明为什么需要人工复审。");
      const btn = node("button", "ghost compact", t("refund.request_manual_review", "提交人工复审"));
      btn.type = "button";
      btn.addEventListener("click", () => requestManualReview(item, note, btn));
      manual.append(
        node("p", "muted", t("refund.manual_available", "自动审核未通过或不确定时，可提交一次人工复审。")),
        note,
        btn,
      );
      card.append(manual);
    } else if (String(item.status || "") === "manual_rejected") {
      card.append(node("p", "muted", t("refund.manual_final_rejected", "该任务已人工复审未通过，不能再次申请退款。")));
    }
    list.append(card);
  });
}

function selectTask(jobCode) {
  selectedJobCode = jobCode;
  expandedJobCode = jobCode;
  setMessage("");
  renderEligible(eligibleItems);
  const card = document.querySelector(`.refund-task-card.active`);
  card?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function toggleTask(jobCode) {
  if (expandedJobCode === jobCode) {
    expandedJobCode = "";
    if (selectedJobCode === jobCode) selectedJobCode = "";
  } else {
    selectedJobCode = jobCode;
    expandedJobCode = jobCode;
  }
  setMessage("");
  renderEligible(eligibleItems);
}

async function loadData() {
  const [eligible, reviews] = await Promise.all([
    api("/api/image-refunds/eligible-tasks"),
    api("/api/image-refunds"),
  ]);
  const codes = new Set((eligible.items || []).map((item) => item.job_code));
  if (!codes.has(expandedJobCode)) expandedJobCode = "";
  if (!codes.has(selectedJobCode)) selectedJobCode = "";
  renderEligible(eligible.items || []);
  renderReviews(reviews.items || []);
}

/**
 * 事件委托方式触发退款提交。
 * 从当前 DOM 读取 job_code、textarea、checkbox，不依赖闭包引用。
 */
async function handleSubmitRefund(btn) {
  const jobCode = btn.getAttribute("data-job-code");
  if (!jobCode) {
    setMessage("请选择需要审核的任务。", "error");
    return;
  }
  if (submittingJobs.has(jobCode)) {
    setMessage(t("refund.submitting", "正在提交，请勿重复点击。"), "error");
    return;
  }

  const detail = btn.closest(".refund-task-detail");
  if (!detail) return;

  const note = detail.querySelector("[data-refund-note]");
  const confirm = detail.querySelector("[data-refund-confirm]");

  if (!confirm?.checked) {
    setMessage(t("refund.confirm", "请确认该问题属于整张图片严重结构崩坏。"), "error");
    return;
  }

  const item = findEligible(jobCode);
  if (!item?.job_code) {
    setMessage("该任务不可申请退款。", "error");
    return;
  }

  submittingJobs.add(jobCode);
  btn.disabled = true;
  btn.textContent = "正在提交……";
  setMessage("");

  try {
    await api("/api/image-refunds", {
      method: "POST",
      body: JSON.stringify({
        job_code: jobCode,
        user_note: note?.value || "",
        confirm_severe_only: true,
      }),
    });
    setMessage(t("refund.created", "审核申请已提交。"), "ok");
    selectedJobCode = "";
    expandedJobCode = "";
    await loadData();
  } catch (error) {
    setMessage(error.message || "提交失败", "error");
    btn.disabled = false;
    btn.textContent = t("refund.submit", "提交审核");
  } finally {
    submittingJobs.delete(jobCode);
  }
}

async function requestManualReview(item, note, btn) {
  if (!item?.review_code) return;
  btn.disabled = true;
  setMessage(t("refund.manual_submitting", "正在提交人工复审…"));
  try {
    await api(`/api/image-refunds/${encodeURIComponent(item.review_code)}/request-manual-review`, {
      method: "POST",
      body: JSON.stringify({ user_note: note.value || "" }),
    });
    reviewsExpanded = true;
    setMessage(t("refund.manual_requested", "已提交人工复审，等待管理员处理。"), "ok");
    await loadData();
  } catch (error) {
    setMessage(error.message || t("refund.manual_failed", "提交人工复审失败。"), "error");
  } finally {
    btn.disabled = false;
  }
}

async function init() {
  try {
    me = await api("/api/me");
    $("userName").textContent = me.username || "";
    $("balance").textContent = credits(me.balance_fen);
    window.UmaI18n?.apply(document);
    await loadData();
  } catch (error) {
    if (error.status === 401) window.location.href = "/login";
    else setMessage(error.message || "加载失败", "error");
  }
}

// ==== 事件监听 ====

$("refreshBtn")?.addEventListener("click", loadData);
$("refundPreviewClose")?.addEventListener("click", closePreview);
$("refundPreviewModal")?.addEventListener("click", (event) => {
  if (event.target === $("refundPreviewModal")) closePreview();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closePreview();
});

// 事件委托：在 #eligibleList 上统一处理"提交审核"按钮点击
// 这样无论 renderEligible 如何重建 DOM，事件都不会丢失
$("eligibleList")?.addEventListener("click", (event) => {
  const btn = event.target.closest("[data-action='submit-refund']");
  if (!btn) return;
  event.preventDefault();
  event.stopPropagation();
  handleSubmitRefund(btn);
});

window.addEventListener("uma:langchange", () => {
  window.UmaI18n?.apply(document);
  renderEligible(eligibleItems);
  renderReviews(reviewItems);
});

init();
