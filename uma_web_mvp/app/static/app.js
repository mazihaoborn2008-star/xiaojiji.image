const $ = (id) => document.getElementById(id);
let catalog = {styles: [], control_characters: []};
let me = null;
let countdownTimer = null;
let pollTimer = null;
let pollingActive = false;
let currentTaskData = null;
let currentTaskPosition = null;
let queueStatusData = null;
let queueStatusTimer = null;
let queueStatusPollingActive = false;
let taskSummaryData = {active_count: 0, smart_planning_count: 0, queued_count: 0, translating_count: 0, processing_count: 0};
const outputFileCache = new Map();
const balanceRefreshedTerminalTasks = new Set();
let historyOffset = 0;
let historyLoadInFlight = false;
let currentTaskLoadInFlight = false;
let lastRenderedTaskState = {jobCode: null, status: null, outputCount: null};
let latestSubmittedJobCode = null;
let currentTaskRenderSignature = '';
let lastHistorySignature = '';
let queueStatusLoadInFlight = false;
let taskSummaryLoadInFlight = false;
let promptInputFocused = false;
let promptIsComposing = false;
let promptDraftSaveTimer = null;
let draftPrompt = '';
let promptEditingSince = 0;
let supportUnreadTimer = null;
let topupSubmitReminderTimer = null;
let activeTopupSubmitReminder = null;
let referralCampaignData = null;
let pendingCharacterResolution = null;
let characterResolutionSubmitting = false;
const promptDetailsOpenByJobCode = new Map();
const HISTORY_PAGE_SIZE = 20;
const API_TIMEOUT_MS = 12000;
const ACTIVE_POLL_MS = 5000;
const IDLE_POLL_MS = 20000;
const PROMPT_EDITING_POLL_MS = 15000;
const ACTIVE_QUEUE_POLL_MS = 10000;
const IDLE_QUEUE_POLL_MS = 20000;
const PROMPT_EDITING_QUEUE_POLL_MS = 15000;
const PROMPT_DRAFT_KEY = 'uma_prompt_draft';
const PROMPT_DRAFT_SAVE_MS = 1200;
const SMART_AGENT_LAUNCH_KEY = 'uma_smart_agent_launch_seen_v1';
// --- active job management (with validation & user-scoping) ---
const JOB_CODE_RE = /^GEN-[A-Z0-9]{12}$/;
function isValidJobCode(code) { return JOB_CODE_RE.test(code); }
function clearActiveJob() {
  if (activeJobCode) console.log('[UI] selected job changed', activeJobCode, '→', null);
  activeJobCode = null;
  sessionStorage.removeItem('activeJobCode');
}
function setActiveJob(code, userId) {
  if (activeJobCode !== code) console.log('[UI] selected job changed', activeJobCode, '→', code);
  activeJobCode = code;
  sessionStorage.setItem('activeJobCode', code);
  if (userId !== undefined) sessionStorage.setItem('activeUserId', String(userId));
}
let activeJobCode = (() => {
  const raw = sessionStorage.getItem('activeJobCode');
  if (!raw) return null;
  if (!isValidJobCode(raw)) {
    console.warn('[UI] invalid activeJobCode format, clearing:', raw);
    sessionStorage.removeItem('activeJobCode');
    return null;
  }
  return raw;
})();

// =====================
// API helper
// =====================
function getCookie(name) {
  const prefix = `${name}=`;
  return document.cookie
    .split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix))
    ?.slice(prefix.length) || '';
}

function withCsrf(options = {}) {
  const method = String(options.method || 'GET').toUpperCase();
  if (['GET', 'HEAD', 'OPTIONS'].includes(method)) return options;
  const headers = new Headers(options.headers || {});
  const token = getCookie('uma_csrf');
  if (token) headers.set('X-CSRF-Token', decodeURIComponent(token));
  return {...options, headers};
}

function t(key, fallback, params) {
  return window.UmaI18n?.t(key, fallback, params) ?? (fallback ?? key);
}

function translateMessage(text) {
  return window.UmaI18n?.translateMessage(text) ?? text;
}

/** ── Unified API error extraction ────────────────────────────────
 *  Never returns "[object Object]". Handles:
 *  – string detail      (FastAPI 400/403 with str(detail))
 *  – object detail      (FastAPI 400 with dict detail)
 *  – array  detail      (FastAPI 422 validation errors)
 *  – payload.message    (alternative shape)
 *  – payload.error      (alternative shape)
 *  Falls back to fallbackMessage for truly unknown shapes.
 */
function getApiErrorMessage(payload, fallbackMessage) {
  if (!payload) return String(fallbackMessage || '提交任务失败，请稍后重试');

  // Already a plain string (most common: FastAPI HTTPException(str))
  if (typeof payload === 'string') return payload;

  // { detail: "some string" }
  if (typeof payload.detail === 'string') return payload.detail;

  // { message: "some string" }
  if (typeof payload.message === 'string') return payload.message;

  // { error: "some string" }
  if (typeof payload.error === 'string') return payload.error;

  // { detail: { message: "...", code: "..." } }
  if (payload.detail && typeof payload.detail === 'object' && !Array.isArray(payload.detail)) {
    if (typeof payload.detail.message === 'string') return payload.detail.message;
    if (typeof payload.detail.msg === 'string') return payload.detail.msg;
  }

  // FastAPI 422: { detail: [{ loc: [...], msg: "...", type: "..." }] }
  if (Array.isArray(payload.detail)) {
    var msgs = [];
    for (var i = 0; i < payload.detail.length; i++) {
      var item = payload.detail[i];
      if (!item) continue;
      if (typeof item.msg === 'string') msgs.push(item.msg);
      else if (typeof item === 'string') msgs.push(item);
    }
    if (msgs.length > 0) return msgs.join('；');
  }

  return String(fallbackMessage || '提交任务失败，请稍后重试');
}

/** ── Internal error-code → user-friendly message ───────────────── */
var ERROR_CODE_MAP = {
  'character_ambiguous':                       '检测到无法确定的人物名称，请确认你指的是哪位角色',
  'character_prompt_validation_failed':        '人物 Prompt 校验失败，请检查人物名称或重新选择人物 Tag',
  'agent_unavailable':                          'Agent 服务暂时不可用，请稍后重试，或关闭 Agent 润色后提交',
  'insufficient_credits':                       'Credits 不足，请充值后重试',
  'queue_full':                                 '当前生成队列已满，请稍后重试',
  'validation_error':                           '提交内容格式有误，请检查后重试',
  'prompt_required':                            '请填写描述',
  'prompt_contains_invalid_characters':         '描述中包含非法字符，请检查后重试',
  'prompt_too_long':                            '描述过长，请控制在 3000 字符以内',
};

function mapErrorCode(rawMessage) {
  var msg = String(rawMessage || '').trim();
  // Try exact match first, then prefix match
  var mapped = ERROR_CODE_MAP[msg];
  if (mapped) return mapped;
  for (var code in ERROR_CODE_MAP) {
    if (msg.indexOf(code) === 0) return ERROR_CODE_MAP[code];
  }
  return msg;
}

/** Translate a raw backend message (may contain error_code prefix). */
function translateApiMessage(raw) {
  var msg = String(raw || '');
  // Handle rate_limited prefix with retry_after
  var rateMatch = msg.match(/^rate_limited:(\d+):(.*)/);
  if (rateMatch) {
    var retrySec = parseInt(rateMatch[1], 10);
    return t('app.rate_limited', `提交过于频繁，请在 ${retrySec} 秒后重试。`, {seconds: retrySec});
  }
  var translated = translateMessage(msg);
  if (translated === msg) translated = mapErrorCode(msg);
  return translated;
}

function styleDisplayName(style) {
  if (!style) return '';
  return t(`style.${style.key}`, style.name || style.key);
}
function normalizeStyleKey(key) {
  return key === 'anima' ? 'anima_owner' : key;
}

function outputDisplayLabel(rawLabel, outputIndex, outputCount) {
  const label = String(rawLabel || '').trim();
  if (label === '一次采样') return t('output.first_sample', '一次采样');
  if (label === '二次采样') return t('output.second_sample', '二次采样');
  if (!label && outputCount > 1) return t('output.generic', `输出 ${outputIndex}`, {index: outputIndex});
  return label;
}

async function api(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
  try {
    const res = await fetch(url, {credentials: 'same-origin', ...withCsrf(options), signal: controller.signal});
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) {
      const fallback = `HTTP ${res.status}`;
      let msg = getApiErrorMessage(data, fallback);
      // Attach retry_after from header or response body for 429
      if (res.status === 429) {
        const retryAfter = res.headers.get("Retry-After");
        const retrySec = retryAfter ? parseInt(retryAfter, 10) : (data && data.retry_after ? parseInt(data.retry_after, 10) : 0);
        if (retrySec > 0) {
          msg = `rate_limited:${retrySec}:${msg}`;
        }
      }
      console.log('[API] error', res.status, '→', msg.slice(0, 120));
      const error = new Error(msg);
      error.status = res.status;
      error.data = data;
      throw error;
    }
    return data;
  } catch (err) {
    if (err?.name === 'AbortError') throw new Error(t('common.request_timeout', '请求超时，请稍后重试'));
    throw err;
  } finally {
    clearTimeout(timer);
  }
}
function credits(fen){ return `${Math.trunc(Number(fen || 0))} credits`; }
function setMessage(elId, text, type=''){ const el=$(elId); if(!el) return; el.textContent=translateMessage(text); el.className=`message ${type}`; }
function maybeShowWelcomeBonus(user) {
  const el = $('welcomeBonusBanner');
  if (!el || !user?.welcome_bonus_granted) return;
  const key = `uma_welcome_bonus_seen:${user.account_id || user.user_id}`;
  if (sessionStorage.getItem(key)) return;
  el.textContent = t('app.welcome_bonus', '欢迎！已赠送 10 credits，可用于体验生图。');
  el.classList.remove('hidden');
  sessionStorage.setItem(key, '1');
  window.setTimeout(() => el.classList.add('hidden'), 9000);
}
function supportDialogKey(item) {
  const account = me?.account_id || me?.user_id || 'current';
  const sessionId = me?.session_public_id || 'session';
  return `support_important_seen:${account}:${sessionId}:${item.thread_code}:${item.message_id}`;
}
async function refreshSupportUnread() {
  if (!me) return;
  try {
    const data = await api('/api/support/unread-count');
    const count = Number(data.unread_count || 0);
    const badge = $('supportUnreadBadge');
    if (badge) {
      badge.textContent = String(count);
      badge.classList.toggle('hidden', count <= 0);
    }
    if (data.important) maybeShowImportantSupportMessage(data.important);
  } catch (_) {}
}
function maybeShowImportantSupportMessage(item) {
  const dialog = $('supportImportantDialog');
  if (!dialog || !item?.thread_code) return;
  const key = supportDialogKey(item);
  if (sessionStorage.getItem(key)) return;
  closeSmartAgentLaunchDialog(false);
  closeReferralCampaignDialog(false);
  $('supportImportantSubject').textContent = item.subject || t('support.important_title', '管理员消息');
  $('supportImportantBody').textContent = item.body_preview || '';
  activeSupportThreadCode = item.thread_code;
  sessionStorage.setItem(key, '1');
  dialog.classList.remove('hidden');
}
let activeSupportThreadCode = null;
function closeImportantSupportDialog() {
  $('supportImportantDialog')?.classList.add('hidden');
  maybeShowReferralCampaignDialog();
}
function topupReminderKey(item) {
  const account = me?.account_id || me?.user_id || 'current';
  const sessionId = me?.session_public_id || 'session';
  return `topup_submit_reminder_seen:${account}:${sessionId}:${item.reminder_id}`;
}
async function checkTopupSubmitReminder() {
  if (!me) return;
  try {
    const data = await api('/api/topup/pending-submit-reminder');
    if (!data.show) return;
    const key = topupReminderKey(data);
    if (sessionStorage.getItem(key)) return;
    activeTopupSubmitReminder = data;
    closeSmartAgentLaunchDialog(false);
    closeReferralCampaignDialog(false);
    const body = $('topupSubmitReminderBody');
    if (body) {
      const countText = Number(data.count || 1) > 1
        ? t('topup.submit_reminder.body_multiple', '你有 {count} 个充值订单已经超过 10 分钟，但目前还没有点击“我已付款，提交审核”。\n请返回充值页面完成提交。订单进入审核队列后，管理员才能确认到账并将 credits 添加到你的账户。', {count: data.count})
        : t('topup.submit_reminder.body', '你创建的充值订单已经超过 10 分钟，但目前还没有点击“我已付款，提交审核”。\n请返回充值页面完成提交。订单进入审核队列后，管理员才能确认到账并将 credits 添加到你的账户。');
      body.textContent = countText;
    }
    sessionStorage.setItem(key, '1');
    $('topupSubmitReminderDialog')?.classList.remove('hidden');
  } catch (_) {}
}
function closeTopupSubmitReminder() {
  $('topupSubmitReminderDialog')?.classList.add('hidden');
  maybeShowReferralCampaignDialog();
}
function dialogVisible(id) {
  const el = $(id);
  return Boolean(el && !el.classList.contains('hidden'));
}
function smartAgentLaunchBlockedByHigherPriority() {
  return dialogVisible('supportImportantDialog') || dialogVisible('topupSubmitReminderDialog') || dialogVisible('referralCampaignDialog');
}
function referralCampaignBlockedByHigherPriority() {
  return dialogVisible('supportImportantDialog') || dialogVisible('topupSubmitReminderDialog');
}
async function maybeShowReferralCampaignDialog() {
  const dialog = $('referralCampaignDialog');
  if (!dialog || !me) {
    maybeShowSmartAgentLaunchDialog();
    return;
  }
  if (referralCampaignBlockedByHigherPriority()) return;
  try {
    if (!referralCampaignData) referralCampaignData = await api('/api/referral-campaign');
    if (!referralCampaignData.show) {
      maybeShowSmartAgentLaunchDialog();
      return;
    }
    closeSmartAgentLaunchDialog(false);
    window.UmaI18n?.apply(dialog);
    dialog.classList.remove('hidden');
  } catch (_) {
    maybeShowSmartAgentLaunchDialog();
  }
}
async function markReferralCampaignSeen() {
  try { await api('/api/referral-campaign/seen', {method:'POST'}); } catch (_) {}
  if (referralCampaignData) referralCampaignData.show = false;
}
function closeReferralCampaignDialog(markSeen = true) {
  const dialog = $('referralCampaignDialog');
  if (!dialog) return;
  if (markSeen) markReferralCampaignSeen();
  dialog.classList.add('hidden');
  maybeShowSmartAgentLaunchDialog();
}
function maybeShowSmartAgentLaunchDialog() {
  const dialog = $('smartAgentLaunchDialog');
  if (!dialog || !me) return;
  if (localStorage.getItem(SMART_AGENT_LAUNCH_KEY)) return;
  if (smartAgentLaunchBlockedByHigherPriority()) return;
  window.UmaI18n?.apply(dialog);
  dialog.classList.remove('hidden');
}
function closeSmartAgentLaunchDialog(markSeen = true) {
  const dialog = $('smartAgentLaunchDialog');
  if (!dialog) return;
  if (markSeen) localStorage.setItem(SMART_AGENT_LAUNCH_KEY, '1');
  dialog.classList.add('hidden');
}
async function runInitialHomeDialogs() {
  await refreshSupportUnread();
  await checkTopupSubmitReminder();
  await maybeShowReferralCampaignDialog();
}
function startSupportAndTopupChecks() {
  runInitialHomeDialogs();
  if (!supportUnreadTimer) supportUnreadTimer = window.setInterval(refreshSupportUnread, 30000);
  if (!topupSubmitReminderTimer) topupSubmitReminderTimer = window.setInterval(checkTopupSubmitReminder, 30000);
}
function approxTime(seconds) {
  const value = Math.max(0, Number(seconds || 0));
  if (value <= 3) return t('time.soon', '即将完成');
  if (value < 60) return t('time.seconds', `约 ${Math.ceil(value)} 秒`, {value: Math.ceil(value)});
  if (value < 3600) return t('time.minutes', `约 ${Math.ceil(value / 60)} 分钟`, {value: Math.ceil(value / 60)});
  return t('time.hours', `约 ${Math.ceil(value / 3600)} 小时`, {value: Math.ceil(value / 3600)});
}
function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || seconds === '') return '';
  const total = Math.max(0, Math.floor(Number(seconds)));
  if (!Number.isFinite(total)) return '';
  if (total < 60) return `${total}秒`;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}小时${String(m).padStart(2, '0')}分${String(s).padStart(2, '0')}秒`;
  return `${m}分${String(s).padStart(2, '0')}秒`;
}
function isAgentChecked() {
  return getTranslationMode() === 'normal';
}
function getTranslationMode() {
  const toggle = $('agentToggle');
  const fast = $('fastTranslatorToggle');
  if (me?.fast_translator_enabled && fast && fast.checked && !fast.disabled) return 'fast';
  if (me?.agent_enabled && toggle && toggle.checked && !toggle.disabled) return 'normal';
  return 'none';
}
function setTranslationMode(mode) {
  const normal = $('agentToggle');
  const fast = $('fastTranslatorToggle');
  if (normal) normal.checked = mode === 'normal';
  if (fast) fast.checked = mode === 'fast';
  renderQueueStatus();
}
function isPromptEditing() {
  const promptEl = $('prompt');
  return Boolean(promptInputFocused || promptIsComposing || (promptEl && document.activeElement === promptEl));
}
function markPromptEditing() {
  promptEditingSince = Date.now();
}
function shouldDeferPollingForPromptInput() {
  return isPromptEditing()
    && isMobileViewport()
    && promptEditingSince
    && Date.now() - promptEditingSince < PROMPT_EDITING_POLL_MS;
}
function hasActiveTasks() {
  const activeCount = Number(taskSummaryData?.active_count || 0);
  const currentStatus = String(currentTaskData?.status || '');
  return Boolean(
    activeJobCode
    || activeCount > 0
    || ['smart_planning', 'queued', 'translating', 'processing'].includes(currentStatus)
  );
}
function currentTaskPollInterval() {
  if (isPromptEditing() && isMobileViewport()) return PROMPT_EDITING_POLL_MS;
  return hasActiveTasks() ? ACTIVE_POLL_MS : IDLE_POLL_MS;
}
function queuePollInterval() {
  if (isPromptEditing() && isMobileViewport()) return PROMPT_EDITING_QUEUE_POLL_MS;
  return hasActiveTasks() ? ACTIVE_QUEUE_POLL_MS : IDLE_QUEUE_POLL_MS;
}
function restartPollingForPromptState() {
  if (pollingActive) startPolling();
  if (queueStatusPollingActive) startQueueStatusPolling({immediate: false});
}
function getAgentEstimateSeconds() {
  return Number(queueStatusData?.estimated_agent_seconds || currentTaskPosition?.estimated_agent_seconds || 51);
}
function getGenerationEstimateSeconds() {
  return Number(queueStatusData?.estimated_generation_seconds || currentTaskPosition?.estimated_generation_seconds || 18);
}
function appendTextLine(parent, text, className = '') {
  const line = document.createElement('div');
  if (className) line.className = className;
  line.textContent = text;
  parent.append(line);
}

function outputsSignature(outputs) {
  return (outputs || []).map((item) => `${item.id || ''}:${item.label || ''}`).join('|');
}

function currentTaskSignature(task) {
  if (!task || !task.job_code) return 'empty';
  const position = currentTaskPosition || {};
  return JSON.stringify({
    job_code: task.job_code,
    status: task.status || '',
    original_prompt: task.original_prompt || task.prompt || '',
    effective_prompt: task.effective_prompt || '',
    use_agent: Number(task.use_agent || 0),
    agent_mode: task.agent_mode || '',
    smart_agent_request: task.smart_agent_request || '',
    workflow_key: task.workflow_key || '',
    loras_json: task.loras_json || '',
    prompt_source: task.prompt_source || '',
    mode: task.generation_mode || '',
    style: task.style_key || '',
    width: task.width || '',
    height: task.height || '',
    error: task.error || '',
    active_duration_seconds: task.active_duration_seconds ?? null,
    outputs: outputsSignature(task.outputs),
    eta_status: position.status || '',
    eta_total: position.estimated_total_seconds ?? null,
    eta_ahead: position.jobs_ahead ?? null,
  });
}

function historySignature(activeItems, completedItems) {
  const summarize = (items) => (items || []).map((task) => ({
    job_code: task.job_code,
    status: task.status,
    active_duration_seconds: task.active_duration_seconds ?? null,
    outputs: outputsSignature(task.outputs),
    effective_prompt: task.effective_prompt || '',
    agent_mode: task.agent_mode || '',
    smart_agent_request: task.smart_agent_request || '',
    workflow_key: task.workflow_key || '',
    error: task.error || '',
  }));
  return JSON.stringify({
    selected: activeJobCode || '',
    active: summarize(activeItems),
    completed: summarize(completedItems),
  });
}

function outputImageUrl(output) {
  if (!output) return '';
  return output.url || `/api/outputs/${output.id}`;
}

function isLongRunningTask(task, startField = 'started_at') {
  const startedAt = Number(task?.[startField] || task?.created_at || 0);
  if (!startedAt) return false;
  return (Date.now() / 1000) - startedAt > 180;
}

function renderQueueStatus() {
  const el = $('queueStatus');
  if (!el) return;
  el.replaceChildren();
  if (!queueStatusData) {
    appendTextLine(el, t('queue.loading', '正在读取队列状态…'), 'muted-line');
    return;
  }
  const queued = Number(queueStatusData.queued_total || 0);
  const translating = Number(queueStatusData.translating_count || 0);
  const processing = Number(queueStatusData.processing_count || 0);
  const waitingTasks = queued + translating + processing;
  const wait = Number(queueStatusData.estimated_wait_seconds || 0);
  const generation = getGenerationEstimateSeconds();
  const translationMode = getTranslationMode();
  const agentExtra = translationMode === 'normal' ? getAgentEstimateSeconds() : 0;
  const total = wait + generation + agentExtra;

  if (waitingTasks > 0) {
    appendTextLine(el, t('queue.ahead', `前方任务：${waitingTasks} 个（翻译中 ${translating} / 出图中 ${processing} / 排队中 ${queued}）`, {
      count: waitingTasks, translating, processing, queued,
    }));
  } else {
    appendTextLine(el, t('queue.none', '当前无需排队'));
  }
  if (waitingTasks > 0) {
    appendTextLine(el, t('queue.wait', `预计等待：${approxTime(wait)}`, {time: approxTime(wait)}), 'muted-line');
  }
  if (translationMode === 'fast') {
    appendTextLine(el, t('queue.fast_translate', '极速翻译：无需等待翻译时间'), 'muted-line');
    appendTextLine(el, t('queue.generation', `图片生成：${approxTime(generation)}`, {time: approxTime(generation)}), 'muted-line');
    appendTextLine(el, t('queue.total', `预计总时间：${approxTime(total)}`, {time: approxTime(total)}));
  } else if (agentExtra > 0) {
    appendTextLine(el, t('queue.agent', `Agent 翻译：${approxTime(agentExtra)}`, {time: approxTime(agentExtra)}), 'muted-line');
    appendTextLine(el, t('queue.generation', `图片生成：${approxTime(generation)}`, {time: approxTime(generation)}), 'muted-line');
    appendTextLine(el, t('queue.total', `预计总时间：${approxTime(total)}`, {time: approxTime(total)}));
  } else if (waitingTasks > 0) {
    appendTextLine(el, t('queue.total', `预计总时间：${approxTime(total)}`, {time: approxTime(total)}));
  } else {
    appendTextLine(el, t('queue.estimate_generation', `预计生成：${approxTime(generation)}`, {time: approxTime(generation)}), 'muted-line');
  }
}

async function loadQueueStatus() {
  if (!me) return;
  if (queueStatusLoadInFlight) return;
  queueStatusLoadInFlight = true;
  try {
    queueStatusData = await api('/api/queue/status');
    renderQueueStatus();
  } catch(e) {
    const el = $('queueStatus');
    if (el) {
      el.replaceChildren();
      appendTextLine(el, t('queue.unavailable', '队列状态暂时无法读取'), 'muted-line');
    }
  } finally {
    queueStatusLoadInFlight = false;
  }
}

function renderTaskBadge() {
  const badge = $('taskBadge');
  if (!badge) return;
  const count = Number(taskSummaryData?.active_count || 0);
  if (count <= 0) {
    badge.classList.add('hidden');
    badge.textContent = '0';
    return;
  }
  badge.textContent = count > 99 ? '99+' : String(count);
  badge.classList.remove('hidden');
}

async function loadTaskSummary() {
  if (!me) return;
  if (taskSummaryLoadInFlight) return;
  taskSummaryLoadInFlight = true;
  try {
    taskSummaryData = await api('/api/tasks/summary');
    renderTaskBadge();
  } catch (_) {
  } finally {
    taskSummaryLoadInFlight = false;
  }
}

function startQueueStatusPolling(options = {}) {
  const immediate = options.immediate !== false;
  stopQueueStatusPolling();
  queueStatusPollingActive = true;
  if (immediate) {
    loadQueueStatus();
    loadTaskSummary();
  }
  const tick = async () => {
    queueStatusTimer = null;
    if (!queueStatusPollingActive) return;
    if (!document.hidden) {
      if (!shouldDeferPollingForPromptInput()) {
        await loadQueueStatus();
        await loadTaskSummary();
        if (isDrawerOpen() && !isPromptEditing()) await loadHistory(true, {preserveScroll: true});
      }
    }
    if (queueStatusPollingActive) queueStatusTimer = setTimeout(tick, queuePollInterval());
  };
  queueStatusTimer = setTimeout(tick, queuePollInterval());
}

function stopQueueStatusPolling() {
  queueStatusPollingActive = false;
  if (queueStatusTimer) { clearTimeout(queueStatusTimer); queueStatusTimer = null; }
}

async function loadTaskPosition(jobCode) {
  if (!jobCode || !isValidJobCode(jobCode)) return null;
  try {
    return await api(`/api/tasks/${jobCode}/position`);
  } catch(e) {
    return null;
  }
}

// =====================
// Dimension presets
// =====================
const PRESETS = {
  '1024x1536': {w:1024, h:1536},
  '1536x1356': {w:1536, h:1356},
  '1536x1024': {w:1536, h:1024},
  '1024x1024': {w:1024, h:1024},
};

function getSelectedDimensions() {
  const preset = $('dimensionPreset').value;
  if (preset === 'custom') {
    return {w: parseInt($('width').value, 10), h: parseInt($('height').value, 10)};
  }
  return PRESETS[preset];
}

function setPresetFromDimensions(w, h) {
  const key = `${w}x${h}`;
  if (PRESETS[key]) {
    $('dimensionPreset').value = key;
    $('customDimensions').classList.add('hidden');
  } else {
    $('dimensionPreset').value = 'custom';
    $('customDimensions').classList.remove('hidden');
    $('width').value = w;
    $('height').value = h;
  }
}

function validateResolutionFrontend(w, h) {
  if (!Number.isInteger(w) || !Number.isInteger(h)) return '宽和高必须是整数';
  if (w < 512 || h < 512) return '宽和高不能低于 512';
  if (w > 2048 || h > 2048) return '宽和高不能超过 2048';
  if (w * h > 2359296) return '总像素过大，请减小画幅';
  if (w % 4 || h % 4) return '宽和高必须是 4 的倍数';
  return null;
}

// =====================
// Mode / Style rendering
// =====================
function renderStyleOptions(){
  const mode=$('mode').value;
  const current=normalizeStyleKey($('styleKey').value);
  const items=catalog.styles.filter(s=>s.modes.includes(mode));
  $('styleKey').replaceChildren(...items.map(s=>new Option(styleDisplayName(s),s.key)));
  if(items.some(s=>s.key===current)) $('styleKey').value=current;
  if(mode==='controlnet') $('styleKey').value='controlnet';
}

function updateMode(){
  const mode=$('mode').value;
  $('imageOptions').classList.toggle('hidden', mode==='txt2img');
  $('controlOptions').classList.toggle('hidden', mode!=='controlnet');
  $('denoise').value = mode==='controlnet' ? '0.6' : '0.5';
  renderStyleOptions();
}

// =====================
// Current task panel
// =====================
function renderCurrentTask(task) {
  const container = $('currentTask');
  if (!container) return;
  const signature = currentTaskSignature(task);
  if (signature === currentTaskRenderSignature) {
    currentTaskData = task || null;
    return;
  }
  currentTaskRenderSignature = signature;
  container.replaceChildren();

  if (!task || !task.job_code) {
    const empty = document.createElement('div');
    empty.className = 'current-task-empty';
    empty.textContent = t('task.empty', '还没有当前任务，请先创建一张图片。');
    container.append(empty);
    return;
  }

  currentTaskData = task;
  const status = task.status || 'queued';
  const outputCount = Array.isArray(task.outputs) ? task.outputs.length : 0;
  if (lastRenderedTaskState.jobCode !== task.job_code || lastRenderedTaskState.status !== status) {
    console.log('[UI] task status changed', {job_code: task.job_code, status});
  }
  if (lastRenderedTaskState.jobCode !== task.job_code || lastRenderedTaskState.outputCount !== outputCount) {
    console.log('[UI] outputs received count', {job_code: task.job_code, count: outputCount});
  }
  lastRenderedTaskState = {jobCode: task.job_code, status, outputCount};

  // Build common header elements
  function makeHeader(code, statusEl) {
    const codeEl = document.createElement('div');
    codeEl.className = 'ct-code';
    codeEl.textContent = code;
    container.append(codeEl);
    container.append(statusEl);
  }

  function makeMeta() {
    const isSmartAgent = task.agent_mode === 'smart_agent';
    const originalPrompt = isSmartAgent ? (task.smart_agent_request || task.original_prompt || '') : (task.original_prompt || task.prompt || '');
    const effectivePrompt = task.effective_prompt || '';
    const usesAgent = Boolean(Number(task.use_agent || 0));
    const promptEl = document.createElement('div');
    promptEl.className = 'ct-prompt';
    if (originalPrompt) {
      const title = document.createElement('div');
      title.className = 'ct-prompt-title';
      title.textContent = isSmartAgent
        ? t('task.smart_request', '用户原始需求：')
        : (usesAgent ? t('task.original_prompt', '原始描述：') : t('task.prompt', 'Prompt：'));
      const body = document.createElement('div');
      body.textContent = originalPrompt;
      promptEl.append(title, body);
    }
    if (usesAgent || isSmartAgent) {
      const details = document.createElement('details');
      details.dataset.jobCode = task.job_code;
      details.dataset.panel = 'effective-prompt';
      details.open = Boolean(promptDetailsOpenByJobCode.get(task.job_code));
      details.addEventListener('toggle', () => {
        promptDetailsOpenByJobCode.set(task.job_code, details.open);
      });
      const summary = document.createElement('summary');
      summary.textContent = isSmartAgent ? t('task.smart_final_prompt', 'Smart Agent 生图 Prompt') : t('task.effective_prompt', '实际生图 Prompt');
      summary.addEventListener('click', (event) => event.stopPropagation());
      const body = document.createElement('div');
      body.textContent = effectivePrompt || (isSmartAgent ? t('task.smart_planning_hint', 'Smart Agent 正在规划画面……') : t('task.agent_translating', 'Agent 正在翻译……'));
      details.append(summary, body);
      promptEl.append(details);
    }
    if (promptEl.childNodes.length) container.append(promptEl);
    const metaEl = document.createElement('div');
    metaEl.className = 'ct-meta';
    const mode = task.generation_mode || 'txt2img';
    const sk = task.style_key || 'style_a';
    const w = task.width || 1024;
    const h = task.height || 1536;
    const agentLabel = taskTranslationLabel(task);
    const workflow = isSmartAgent && task.workflow_key ? ` · ${t('task.workflow', '工作流')} ${task.workflow_key}` : '';
    metaEl.textContent = `${mode} · ${sk} · ${w}×${h} · ${agentLabel}${workflow}`;
    container.append(metaEl);
  }

  const statusMap = {
    smart_planning: [t('task.smart_planning', '智能 Agent 规划中'), 'smart_planning'],
    queued: [t('task.queued', '排队中'), 'queued'],
    translating: [t('task.translating', '翻译中'), 'translating'],
    processing: [t('task.processing', '出图中'), 'processing'],
    done: [t('task.done', '已完成'), 'done'],
    failed_refunded: [t('task.failed', '生成失败'), 'failed_refunded'],
    cancelled_refunded: [t('task.cancelled', '已取消，费用已退回'), 'cancelled_refunded'],
  };
  const [statusText, statusClass] = statusMap[status] || [status, ''];

  const statusEl = document.createElement('span');
  statusEl.className = 'ct-status ' + statusClass;
  statusEl.textContent = statusText;

  function appendCurrentEta() {
    const data = currentTaskPosition;
    if (!data || (status !== 'smart_planning' && status !== 'queued' && status !== 'translating' && status !== 'processing')) return;
    const eta = document.createElement('div');
    eta.className = 'ct-eta';
    if (status === 'smart_planning') {
      const total = Number(data.estimated_total_seconds || 0);
      appendTextLine(eta, t('task.smart_planning_hint', 'Smart Agent 正在规划画面……'));
      appendTextLine(eta, total <= 3 ? t('queue.enter_processing', '即将进入出图') : t('queue.complete_after', `预计 ${approxTime(total)} 后完成`, {time: approxTime(total)}), 'muted-line');
    } else if (status === 'queued') {
      const ahead = Number(data.jobs_ahead || 0);
      appendTextLine(eta, ahead > 0 ? t('queue.ahead_count', `前方还有 ${ahead} 个任务`, {count: ahead}) : t('queue.no_queued_ahead', '前方暂无 queued 任务'));
      const total = Number(data.estimated_total_seconds || 0);
      appendTextLine(eta, total <= 3 ? t('queue.starting', '即将开始生成') : t('queue.complete_after', `预计 ${approxTime(total)} 后完成`, {time: approxTime(total)}), 'muted-line');
    } else if (status === 'translating') {
      const total = Number(data.estimated_total_seconds || 0);
      appendTextLine(eta, total <= 3 ? t('queue.enter_processing', '即将进入出图') : t('queue.complete_after', `预计 ${approxTime(total)} 后完成`, {time: approxTime(total)}));
    } else if (status === 'processing') {
      const total = Number(data.estimated_total_seconds || 0);
      appendTextLine(eta, total <= 3 ? t('queue.finishing', '即将完成') : t('queue.remaining', `预计剩余：${approxTime(total)}`, {time: approxTime(total)}));
    }
    container.append(eta);
  }

  if (status === 'smart_planning') {
    makeHeader(task.job_code, statusEl);
    makeMeta();
    const hint = document.createElement('div');
    hint.className = 'ct-hint';
    hint.textContent = isLongRunningTask(task, 'created_at')
      ? t('task.long_wait', '处理时间较长，请继续等待…')
      : t('task.smart_planning_hint', 'Smart Agent 正在规划画面……');
    container.append(hint);
    appendCurrentEta();
  }
  else if (status === 'queued') {
    makeHeader(task.job_code, statusEl);
    makeMeta();
    appendCurrentEta();

    const actions = document.createElement('div');
    actions.className = 'ct-actions';
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'ghost';
    cancelBtn.textContent = t('task.cancel_refund', '取消并退款');
    cancelBtn.onclick = async () => {
      if (!confirm(`取消 ${task.job_code}？`)) return;
      try {
        await api(`/api/tasks/${task.job_code}/cancel`, {method:'POST'});
        await loadCurrentTask();
        await loadQueueStatus();
        await loadTaskSummary();
      } catch(e) { alert(e.message); }
    };
    actions.append(cancelBtn);
    container.append(actions);
  }
  else if (status === 'translating') {
    makeHeader(task.job_code, statusEl);
    makeMeta();

    const hint = document.createElement('div');
    hint.className = 'ct-hint';
    hint.textContent = isLongRunningTask(task, 'translating_started_at')
      ? t('task.long_wait', '处理时间较长，请继续等待…')
      : t('task.translating_hint', 'Agent 正在翻译 Prompt…');
    container.append(hint);
    appendCurrentEta();
  }
  else if (status === 'processing') {
    makeHeader(task.job_code, statusEl);
    makeMeta();

    const hint = document.createElement('div');
    hint.className = 'ct-hint';
    hint.textContent = isLongRunningTask(task, 'started_at')
      ? t('task.long_wait', '处理时间较长，请继续等待…')
      : t('task.processing_hint', '正在生成中，请耐心等待…');
    container.append(hint);
    appendCurrentEta();
  }
  else if (status === 'done') {
    makeHeader(task.job_code, statusEl);
    makeMeta();
    const durationText = formatDuration(task.active_duration_seconds);
    const doneHint = document.createElement('div');
    doneHint.className = 'ct-hint';
    appendTextLine(doneHint, t('task.done_hint', '生成完成'));
    if (durationText) appendTextLine(doneHint, t('task.duration', `用时：${durationText}`, {duration: durationText}));
    container.append(doneHint);

    if (task.outputs && task.outputs.length > 0) {
      const outputGrid = document.createElement('div');
      outputGrid.className = task.outputs.length > 1 ? 'ct-output-grid' : 'ct-output-grid single';
      task.outputs.forEach((out, idx) => {
        outputGrid.append(renderOutputCard(task, out, idx + 1, task.outputs.length));
      });
      container.append(outputGrid);
    } else {
      const hint = document.createElement('div');
      hint.className = 'ct-hint';
      hint.textContent = isLongRunningTask(task, 'finished_at')
        ? t('task.sync_long', '结果同步时间较长，请继续等待。')
        : t('task.syncing', '结果图正在同步，请稍候…');
      container.append(hint);
    }
  }
  else if (status === 'failed_refunded') {
    makeHeader(task.job_code, statusEl);
    makeMeta();

    if (task.error) {
      const errEl = document.createElement('div');
      errEl.className = 'ct-error';
      errEl.textContent = sanitizeError(task.error);
      container.append(errEl);
    }
  }
  else if (status === 'cancelled_refunded') {
    makeHeader(task.job_code, statusEl);
    makeMeta();
  }
  else {
    // Unknown status fallback
    makeHeader(task.job_code, statusEl);
    makeMeta();
  }
}

function sanitizeError(err) {
  if (!err) return '';
  let s = String(err);
  // Map well-known HTTP / framework errors to Chinese
  if (/^(?:HTTP\s*)?404$|^Not\s*Found$/i.test(s.trim())) return '任务不存在或已被清理';
  if (/^(?:HTTP\s*)?401$/i.test(s.trim())) return '请重新登录';
  if (/^(?:HTTP\s*)?403$/i.test(s.trim())) return '无权访问此任务';
  if (/^(?:HTTP\s*)?5\d\d$/i.test(s.trim())) return '服务器暂时无法读取当前任务';
  // Strip file paths (Windows and Unix)
  s = s.replace(/[A-Za-z]:\\[^\s'"]+/g, '').replace(/\/[^\s'"]+\.(?:py|js|ts|log)/g, '');
  // Strip stack trace lines
  s = s.replace(/\s*(?:at|File|Traceback).*$/gm, '');
  // Collapse whitespace
  s = s.replace(/\s+/g, ' ').trim();
  // Limit length
  if (s.length > 200) s = s.slice(0, 200) + '…';
  return s || '未知错误';
}

function taskFromResponse(data) {
  if (!data) return null;
  if (Object.prototype.hasOwnProperty.call(data, 'task')) return data.task;
  return data.item || null;
}

function isRecoverableTaskNotFound(message) {
  const msg = String(message || '');
  return msg.includes('404')
    || msg.includes('不存在')
    || msg.includes('Not Found')
    || msg.includes('not found');
}

function renderCurrentTaskError(errorMessage) {
  const container = $('currentTask');
  if (!container) return;
  container.replaceChildren();
  currentTaskRenderSignature = `error:${String(errorMessage || '')}`;
  const wrapper = document.createElement('div');
  wrapper.className = 'current-task-empty current-task-error-state';
  const errDiv = document.createElement('div');
  const errMsg = sanitizeError(errorMessage);
  errDiv.textContent = errMsg === '未知错误'
    ? t('task.load_failed_temp', '当前任务暂时无法加载，请稍后重试')
    : t('task.load_failed', `当前任务加载失败：${translateMessage(errMsg)}`, {error: translateMessage(errMsg)});
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.className = 'secondary current-task-retry';
  retry.textContent = t('app.reload', '重新加载');
  retry.addEventListener('click', () => {
    currentTaskRenderSignature = '';
    loadCurrentTask();
  }, { once: true });
  wrapper.append(errDiv, retry);
  container.append(wrapper);
}

async function loadCurrentTask() {
  if (!me) return;
  if (currentTaskLoadInFlight) return;
  currentTaskLoadInFlight = true;
  try {
    // --- User change detection ---
    const prevUserId = sessionStorage.getItem('activeUserId');
    if (prevUserId && me.user_id && String(prevUserId) !== String(me.user_id)) {
      console.warn('[UI] user changed, clearing activeJobCode:', prevUserId, '→', me.user_id);
      clearActiveJob();
      sessionStorage.removeItem('activeUserId');
    } else if (activeJobCode && me.user_id) {
      // Same user: ensure activeUserId is synced for future sessions
      sessionStorage.setItem('activeUserId', String(me.user_id));
    }

    let task = null;
    let fallbackToLatest = !activeJobCode;

    if (activeJobCode) {
      try {
        const data = await api(`/api/tasks/${activeJobCode}`);
        task = taskFromResponse(data);
        // Terminal states: keep displaying, don't clear activeJobCode
        if (!task) {
          // null task means deleted / no longer exists
          clearActiveJob();
          fallbackToLatest = true;
        }
      } catch (e) {
        const msg = String(e.message || '');
        // 404 or Not Found → task gone, fallback to latest
        if (isRecoverableTaskNotFound(msg)) {
          clearActiveJob();
          fallbackToLatest = true;
          task = null;
        } else if (/^HTTP\s*401$/.test(msg.trim()) || msg === '请先登录') {
          window.location.href = '/login';
          return;
        } else {
          throw e; // re-throw: network / 500 / parse errors
        }
      }
    }

    if (fallbackToLatest) {
      try {
        const data = await api('/api/tasks/latest');
        task = taskFromResponse(data);
        if (task && task.job_code) {
          setActiveJob(task.job_code, me.user_id);
        }
      } catch (e) {
        const msg = String(e.message || '');
        // If latest also fails with 404, just show empty state
        if (isRecoverableTaskNotFound(msg)) {
          console.log('[UI] /api/tasks/latest returned 404, showing empty');
          task = null;
        } else {
          throw e;
        }
      }
    }

    currentTaskPosition = null;
    if (task && task.job_code && (task.status === 'smart_planning' || task.status === 'queued' || task.status === 'translating' || task.status === 'processing')) {
      currentTaskPosition = await loadTaskPosition(task.job_code);
    }
    // Refresh balance once when task enters a refund terminal state
    if (task && task.job_code && (task.status === 'cancelled_refunded' || task.status === 'failed_refunded')) {
      if (!balanceRefreshedTerminalTasks.has(task.job_code)) {
        balanceRefreshedTerminalTasks.add(task.job_code);
        refreshMeAndBalance();
      }
    }
    renderCurrentTask(task);
  } catch(e) {
    console.error('[UI] loadCurrentTask failed:', e.message);
    renderCurrentTaskError(e.message);
  } finally {
    currentTaskLoadInFlight = false;
  }
}

function startPolling() {
  stopPolling();
  pollingActive = true;
  const tick = async () => {
    pollTimer = null;
    if (!pollingActive) return;
    if (!document.hidden && !shouldDeferPollingForPromptInput()) {
      await loadCurrentTask();
      await loadTaskSummary();
      if (isDrawerOpen() && !isPromptEditing()) await loadHistory(true, {preserveScroll: true});
    }
    if (pollingActive) pollTimer = setTimeout(tick, currentTaskPollInterval());
  };
  pollTimer = setTimeout(tick, currentTaskPollInterval());
}

function stopPolling() {
  pollingActive = false;
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopPolling();
    stopQueueStatusPolling();
  } else {
    loadCurrentTask();
    loadQueueStatus();
    startPolling();
    startQueueStatusPolling();
  }
});

window.addEventListener('beforeunload', () => {
  stopPolling();
  stopQueueStatusPolling();
});

// =====================
// Save / download output
// =====================
function extensionForMime(type) {
  const mime = String(type || '').split(';')[0].trim().toLowerCase();
  if (mime === 'image/jpeg' || mime === 'image/jpg') return {mime: 'image/jpeg', ext: '.jpg'};
  if (mime === 'image/webp') return {mime: 'image/webp', ext: '.webp'};
  return {mime: 'image/png', ext: '.png'};
}

function outputFileName(jobCode, mime, outputIndex = null, outputCount = 1) {
  const info = extensionForMime(mime);
  const suffix = outputCount > 1 && outputIndex ? `_${outputIndex}` : '';
  return `UMA_${jobCode}${suffix}${info.ext}`;
}

function shouldUseMobileSaveUi() {
  return window.matchMedia('(max-width: 640px), (pointer: coarse)').matches;
}

function setCopyButtonState(button, phase) {
  if (phase === 'copying') {
    button.disabled = true;
    button.textContent = t('task.copying', '正在复制……');
  } else if (phase === 'done') {
    button.disabled = false;
    button.textContent = t('task.copied', '已复制');
  } else if (phase === 'restore') {
    button.disabled = false;
    button.textContent = t('task.copy_image', '复制图片');
  } else {
    button.disabled = false;
    button.textContent = t('task.copy_failed', '复制失败');
  }
}

function isMobileViewport() {
  return window.matchMedia('(max-width: 640px)').matches;
}

function scrollToCurrentTaskPanel() {
  document.getElementById('currentTaskPanel')?.scrollIntoView({
    behavior: 'smooth',
    block: 'start',
  });
}

function syncHistoryViewingState() {
  document.querySelectorAll('.ht-card').forEach((card) => {
    const isActive = card.dataset.jobCode === activeJobCode;
    card.classList.toggle('active', isActive);
    let viewing = card.querySelector('.ht-viewing');
    if (isActive && !viewing) {
      viewing = document.createElement('div');
      viewing.className = 'ht-viewing';
      viewing.textContent = t('task.viewing', '正在查看');
      const head = card.querySelector('.ht-head');
      if (head) head.insertAdjacentElement('afterend', viewing);
      else card.prepend(viewing);
    } else if (!isActive && viewing) {
      viewing.remove();
    }
  });
}

async function fetchOutputBlob(outputId) {
  const res = await fetch(`/api/outputs/${outputId}/download`, {credentials: 'same-origin'});
  if (!res.ok) {
    let msg = t('task.download_failed', '下载失败');
    try { const d = await res.json(); msg = d.detail || msg; } catch(_) {}
    throw new Error(msg);
  }
  const blob = await res.blob();
  const type = res.headers.get('Content-Type') || blob.type || 'image/png';
  return {blob, type};
}

function canShareFile(file) {
  return Boolean(
    navigator.share &&
    navigator.canShare &&
    file &&
    navigator.canShare({files: [file]})
  );
}

function prefetchOutputFile(outputId, jobCode, outputIndex = null, outputCount = 1) {
  const key = String(outputId);
  const existing = outputFileCache.get(key);
  if (existing?.status === 'ready' || existing?.status === 'loading') return existing;

  const entry = {status: 'loading', file: null, blob: null, type: null, error: null, promise: null};
  entry.promise = fetchOutputBlob(outputId).then(({blob, type}) => {
    const info = extensionForMime(type);
    const normalizedBlob = blob.type === info.mime ? blob : blob.slice(0, blob.size, info.mime);
    const file = new File([normalizedBlob], outputFileName(jobCode, info.mime, outputIndex, outputCount), {type: info.mime});
    Object.assign(entry, {status: 'ready', blob: normalizedBlob, file, type: info.mime});
    return entry;
  }).catch((error) => {
    Object.assign(entry, {status: 'error', error});
    throw error;
  });
  outputFileCache.set(key, entry);
  return entry;
}

function triggerBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.append(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function downloadOutput(outputId, jobCode, button = null, outputIndex = null, outputCount = 1) {
  const originalText = button ? button.textContent : '';
  try {
    if (button) {
      button.disabled = true;
      button.textContent = t('task.saving', '保存中…');
    }
    const {blob, type} = await fetchOutputBlob(outputId);
    triggerBlobDownload(blob, outputFileName(jobCode, type, outputIndex, outputCount));
  } catch(e) {
    alert(e.message);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

async function shareOutputToAlbum(outputId, jobCode, button = null, outputIndex = null, outputCount = 1) {
  const key = String(outputId);
  const originalText = button ? button.textContent : '';
  let restoreButton = true;
  try {
    let entry = outputFileCache.get(key) || prefetchOutputFile(outputId, jobCode, outputIndex, outputCount);
    if (entry.status === 'loading') {
      if (button) {
        button.disabled = true;
        button.textContent = t('task.preparing_image', '图片准备中…');
        restoreButton = false;
        entry.promise?.then(() => {
          button.disabled = false;
          button.textContent = originalText;
        }).catch(() => {
          button.disabled = false;
          button.textContent = originalText;
        });
      }
      return;
    }
    if (entry.status !== 'ready' || !canShareFile(entry.file)) {
      await downloadOutput(outputId, jobCode, null, outputIndex, outputCount);
      return;
    }
    if (button) {
      button.disabled = true;
      button.textContent = t('task.saving', '保存中…');
    }
    await navigator.share({
      files: [entry.file],
      title: '小击击生图',
    });
  } catch(e) {
    if (e && (e.name === 'AbortError' || e.name === 'NotAllowedError')) {
      return;
    }
    setMessage('message', t('task.share_failed', '无法打开相册保存，请长按图片保存或使用下载功能。'), 'error');
  } finally {
    if (button && restoreButton) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

function renderOutputCard(task, out, outputIndex, outputCount) {
  const card = document.createElement('div');
  card.className = 'ct-output-card';
  const rawLabel = out.output_label || out.label || '';
  const labelText = outputDisplayLabel(rawLabel, outputIndex, outputCount);
  if (outputCount > 1 && labelText) {
    const label = document.createElement('div');
    label.className = 'ct-output-label';
    label.textContent = labelText;
    card.append(label);
  }

  const imgWrap = document.createElement('div');
  imgWrap.className = 'ct-image-wrap';
  const img = document.createElement('img');
  img.src = outputImageUrl(out);
  img.alt = `${task.job_code} ${labelText}`.trim();
  img.onerror = () => console.warn('[UI] output image failed to load', {job_code: task.job_code, output_id: out.id});
  imgWrap.append(img);
  card.append(imgWrap);

  const actions = document.createElement('div');
  actions.className = 'ct-actions';
  const cachedFile = prefetchOutputFile(out.id, task.job_code, outputIndex, outputCount);
  if (shouldUseMobileSaveUi()) {
    actions.classList.add('mobile-save-actions');
    const albumBtn = document.createElement('button');
    albumBtn.className = 'primary';
    albumBtn.textContent = cachedFile.status === 'loading' ? t('task.preparing_image', '图片准备中…') : t('task.save_album', '保存到相册');
    albumBtn.disabled = cachedFile.status === 'loading';
    cachedFile.promise?.then(() => {
      albumBtn.disabled = false;
      albumBtn.textContent = t('task.save_album', '保存到相册');
    }).catch(() => {
      albumBtn.disabled = false;
      albumBtn.textContent = t('task.save_album', '保存到相册');
    });
    albumBtn.onclick = () => shareOutputToAlbum(out.id, task.job_code, albumBtn, outputIndex, outputCount);

    const downloadBtn = document.createElement('button');
    downloadBtn.className = 'ghost';
    downloadBtn.textContent = t('task.download_file', '下载到文件');
    downloadBtn.onclick = () => downloadOutput(out.id, task.job_code, downloadBtn, outputIndex, outputCount);
    actions.append(albumBtn, downloadBtn);
  } else {
    const saveBtn = document.createElement('button');
    saveBtn.className = 'primary';
    saveBtn.textContent = outputCount > 1 ? `${t('task.save', '保存图片')} - ${labelText || outputIndex}` : t('task.save', '保存图片');
    saveBtn.onclick = () => downloadOutput(out.id, task.job_code, saveBtn, outputIndex, outputCount);
    actions.append(saveBtn);

    if (typeof isDesktopWithPointer === 'function' && isDesktopWithPointer() && typeof copyImageToClipboard === 'function') {
      const copyBtn = document.createElement('button');
      copyBtn.className = 'copy-image-button';
      copyBtn.textContent = t('task.copy_image', '复制图片');
      copyBtn.onclick = async () => {
        setCopyButtonState(copyBtn, 'copying');
        try {
          await copyImageToClipboard(outputImageUrl(out));
          setCopyButtonState(copyBtn, 'done');
          setTimeout(() => setCopyButtonState(copyBtn, 'restore'), 1500);
        } catch (e) {
          setCopyButtonState(copyBtn, 'error');
          setTimeout(() => setCopyButtonState(copyBtn, 'restore'), 2000);
          alert(e.message || t('task.copy_failed', '复制图片失败，请使用保存图片功能。'));
        }
      };
      actions.append(copyBtn);
    }
  }
  card.append(actions);
  return card;
}

// =====================
// History drawer
// =====================
function openDrawer() {
  $('historyOverlay').classList.remove('hidden');
  $('historyDrawer').classList.remove('hidden');
  // Force reflow before adding open class for transition
  $('historyDrawer').offsetHeight;
  $('historyDrawer').classList.add('open');
  document.body.style.overflow = 'hidden';
  historyOffset = 0;
  loadHistory(true, {force: true});
}

function closeDrawer() {
  $('historyDrawer').classList.remove('open');
  $('historyOverlay').classList.add('hidden');
  document.body.style.overflow = '';
  setTimeout(() => {
    if (!$('historyDrawer').classList.contains('open')) {
      $('historyDrawer').classList.add('hidden');
    }
  }, 300);
}

function isDrawerOpen() {
  return $('historyDrawer').classList.contains('open');
}

async function loadHistory(reset, options = {}) {
  if (historyLoadInFlight) return;
  historyLoadInFlight = true;
  const listEl = $('historyList');
  const previousScrollTop = options.preserveScroll ? listEl.scrollTop : 0;
  try {
    if (reset) {
      const active = await api('/api/tasks?status=active&limit=50&offset=0');
      const data = await api(`/api/tasks?status=completed&limit=${HISTORY_PAGE_SIZE}&offset=0`);
      const signature = historySignature(active.items, data.items);
      if (!options.force && signature === lastHistorySignature) {
        syncHistoryViewingState();
        if (options.preserveScroll) listEl.scrollTop = previousScrollTop;
        $('loadMoreBtn').classList.toggle('hidden', !data.has_more);
        return;
      }
      lastHistorySignature = signature;
      historyOffset = 0;
      listEl.replaceChildren();
      if (active.items.length) {
        const title = document.createElement('div');
        title.className = 'ht-section-title';
        title.textContent = `${t('task.active_section', '进行中')} ${active.items.length}`;
        listEl.append(title);
        const activeFragment = document.createDocumentFragment();
        for (const task of active.items) activeFragment.append(historyCard(task));
        listEl.append(activeFragment);
      }
      const doneTitle = document.createElement('div');
      doneTitle.className = 'ht-section-title';
      doneTitle.textContent = t('task.completed_section', '已完成');
      listEl.append(doneTitle);
      const fragment = document.createDocumentFragment();
      for (const task of data.items) {
        fragment.append(historyCard(task));
      }
      listEl.append(fragment);
      $('loadMoreBtn').classList.toggle('hidden', !data.has_more);
      historyOffset = data.items.length;
      if (options.preserveScroll) listEl.scrollTop = previousScrollTop;
    } else {
      const data = await api(`/api/tasks?status=completed&limit=${HISTORY_PAGE_SIZE}&offset=${historyOffset}`);
      const fragment = document.createDocumentFragment();
      for (const task of data.items) {
        fragment.append(historyCard(task));
      }
      listEl.append(fragment);
      $('loadMoreBtn').classList.toggle('hidden', !data.has_more);
      if (data.items.length > 0) {
        historyOffset += data.items.length;
      }
    }
  } catch(e) {
    if (reset) {
      listEl.textContent = e.message;
    }
  } finally {
    historyLoadInFlight = false;
  }
}

function historyCard(task) {
  const card = document.createElement('div');
  card.className = 'ht-card';
  card.dataset.jobCode = task.job_code;

  const head = document.createElement('div');
  head.className = 'ht-head';

  const code = document.createElement('span');
  code.className = 'ht-code';
  code.textContent = task.job_code;

  const status = document.createElement('span');
  status.className = 'ht-status';
  const statusMap = {
    smart_planning: t('task.smart_planning', '智能 Agent 规划中'),
    queued: '排队中',
    translating: t('task.translating', '翻译中'),
    processing: t('task.processing', '出图中'),
    done: t('task.done', '已完成'),
    failed_refunded: t('task.failed', '生成失败'),
    cancelled_refunded: t('task.cancelled', '已取消'),
  };
  statusMap.queued = t('task.queued', '排队中');
  status.textContent = statusMap[task.status] || task.status;
  const durationText = formatDuration(task.active_duration_seconds);
  if (task.status === 'done' && durationText) {
    status.textContent = `${t('task.done', '已完成')} · ${durationText}`;
  }
  // Color the status badge
  const statusColors = {
    smart_planning: {bg:'#2f3a66', fg:'#dbe6ff'},
    queued: {bg:'#27375e', fg:'#9aa8c7'},
    translating: {bg:'#3a2f66', fg:'#d8c7ff'},
    processing: {bg:'#1e3a5f', fg:'#7dd3fc'},
    done: {bg:'#1a3d2e', fg:'#8ee3a0'},
    failed_refunded: {bg:'#3d1a1a', fg:'#ff7b86'},
    cancelled_refunded: {bg:'#2d2d1a', fg:'#fbbf24'},
  };
  const sc = statusColors[task.status] || {bg:'#27375e', fg:'#9aa8c7'};
  status.style.background = sc.bg;
  status.style.color = sc.fg;

  head.append(code, status);
  card.append(head);
  if (task.job_code === activeJobCode) {
    const viewing = document.createElement('div');
    viewing.className = 'ht-viewing';
    viewing.textContent = t('task.viewing', '正在查看');
    card.append(viewing);
  }

  const isSmartAgent = task.agent_mode === 'smart_agent';
  const promptText = isSmartAgent ? (task.smart_agent_request || task.original_prompt || '') : (task.original_prompt || task.prompt || '');
  if (promptText) {
    const promptEl = document.createElement('div');
    promptEl.className = 'ht-prompt';
    promptEl.textContent = promptText;
    card.append(promptEl);
  }

  const meta = document.createElement('div');
  meta.className = 'ht-meta';
  meta.textContent = `${task.generation_mode} · ${task.style_key} · ${task.width}×${task.height} · ${taskTranslationLabel(task)} · ${new Date(task.created_at*1000).toLocaleString()}`;
  card.append(meta);
  if (task.job_code === activeJobCode) card.classList.add('active');
  card.addEventListener('click', (event) => {
    if (event.target.closest('button')) return;
    setActiveJob(task.job_code, me?.user_id);
    syncHistoryViewingState();
    loadCurrentTask().then(() => {
      if (isMobileViewport()) {
        closeDrawer();
        scrollToCurrentTaskPanel();
      }
    }).catch((error) => console.error('[UI] task card select failed:', error));
  });

  // Cancel button for queued tasks
  if (task.status === 'queued') {
    const actions = document.createElement('div');
    actions.className = 'ct-actions';
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'ghost';
    cancelBtn.textContent = t('task.cancel_refund', '取消并退款');
    cancelBtn.style.marginTop = '6px';
    cancelBtn.onclick = async (event) => {
      event.stopPropagation();
      if (!confirm(`取消 ${task.job_code}？`)) return;
      try {
        await api(`/api/tasks/${task.job_code}/cancel`, {method:'POST'});
        // Refresh both history and current task
        historyOffset = 0;
        await loadHistory(true, {force: true, preserveScroll: true});
        await loadCurrentTask();
        await loadQueueStatus();
        await loadTaskSummary();
      } catch(e) { alert(e.message); }
    };
    actions.append(cancelBtn);
    card.append(actions);
  }

  // Thumbnail for completed tasks
  if (task.outputs && task.outputs.length > 0) {
    const thumb = document.createElement('div');
    thumb.className = 'ht-thumb';
    const img = document.createElement('img');
    img.src = outputImageUrl(task.outputs[0]);
    img.alt = task.job_code;
    img.onerror = () => console.warn('[UI] thumbnail failed to load', {job_code: task.job_code, output_id: task.outputs[0].id});
    thumb.append(img);
    card.append(thumb);
  }

  return card;
}

// =====================
// Load user info
// =====================
async function refreshMeAndBalance() {
  try {
    const latestMe = await api('/api/me');
    me = latestMe;
    const balanceEl = $('balance');
    if (balanceEl) balanceEl.textContent = credits(latestMe.balance_fen);
    return latestMe;
  } catch(e) {
    console.warn('[UI] refreshMeAndBalance failed:', e.message);
    return null;
  }
}

function taskTranslationLabel(task) {
  const source = String(task?.prompt_source || '');
  if (source === 'fast_translate' || source.startsWith('fast_translate:')) {
    return t('task.fast_translate_used', '极速翻译已使用');
  }
  if (source.startsWith('agent_')) {
    return t('task.agent_on', '普通翻译已使用');
  }
  const isSmartAgent = task?.agent_mode === 'smart_agent';
  if (isSmartAgent) {
    return t('task.smart_agent_badge', '智能 Agent');
  }
  const usesAgent = Boolean(Number(task?.use_agent || 0));
  if (usesAgent) {
    return t('task.agent_on', '普通翻译已使用');
  }
  return t('task.agent_off', '未使用翻译');
}

async function loadMe(){
  try {
    // App init: load user identity, catalog, and current task
    me=await api('/api/me');
    $('userName').textContent=me.username;
    $('balance').textContent=credits(me.balance_fen);
    $('adminImageRefundLink')?.classList.toggle('hidden', !me.is_admin);
    const smartLink = $('smartAgentNavLink');
    if (smartLink) {
      if (me.ai_support_enabled) {
        smartLink.dataset.i18n = 'nav.ai_support';
        smartLink.textContent = t('nav.ai_support', 'AI 客服');
        smartLink.classList.remove('hidden');
      } else if (me.smart_agent_enabled) {
        smartLink.dataset.i18n = 'nav.smart_agent';
        smartLink.textContent = t('nav.smart_agent', '智能 Agent');
        smartLink.classList.remove('hidden');
      } else {
        smartLink.classList.add('hidden');
      }
    }
    maybeShowWelcomeBonus(me);
    startSupportAndTopupChecks();

    // Agent toggle state
    if (me.agent_enabled) {
      $('agentHint').textContent = '';
      $('agentToggle').disabled = false;
    } else {
      $('agentHint').textContent = t('app.agent_disabled', 'Agent 当前未启用');
      $('agentToggle').disabled = true;
      $('agentToggle').checked = false;
    }
    if ($('fastTranslatorToggle')) {
      $('fastTranslatorToggle').disabled = !me.fast_translator_enabled;
    }
    if (!me.fast_translator_enabled && !me.agent_enabled) {
      $('agentHint').textContent = t('app.translation_all_disabled', '翻译功能当前未启用');
    }
    // Populate translation cost labels
    var normalCostEl = $('normalTranslatorCost');
    if (normalCostEl) {
      var normalCost = Number(me.normal_translator_cost_credits || me.agent_surcharge_credits || 1);
      normalCostEl.textContent = t('app.translation_normal_cost', `额外 ${normalCost} credit`, {credits: normalCost});
    }
    var fastCostEl = $('fastTranslatorCost');
    if (fastCostEl) {
      var fastCost = Number(me.fast_translator_cost_credits || 2);
      fastCostEl.textContent = t('app.translation_fast_cost', `额外 ${fastCost} credits`, {credits: fastCost});
    }
    startQueueStatusPolling();

    // Dev mode badge and logout visibility
    if (me.dev_auth_bypass) {
      $('devBadge').classList.remove('hidden');
      $('logoutBtn').classList.add('hidden');
      $('logoutMenuBtn')?.classList.add('hidden');
    } else {
      $('devBadge').classList.add('hidden');
      $('logoutBtn').classList.remove('hidden');
      $('logoutMenuBtn')?.classList.remove('hidden');
    }

    catalog=await api('/api/catalog');
    $('controlCharacter').replaceChildren(...catalog.control_characters.map(x=>new Option(x.name,x.key)));
    renderStyleOptions();

    if(me.settings){
      if(me.settings.last_width && me.settings.last_height) {
        setPresetFromDimensions(me.settings.last_width, me.settings.last_height);
      }
      if(me.settings.lora_weight!=null) $('loraWeight').value=me.settings.lora_weight;
      const savedStyleKey = normalizeStyleKey(me.settings.style_key);
      if(catalog.styles.some(s=>s.key===savedStyleKey && s.modes.includes($('mode').value))) $('styleKey').value=savedStyleKey;
    }

    await loadCurrentTask();
    startPolling();
  } catch(e) {
    setMessage('message', e.message, 'error');
  }
}

// =====================
// Agent refine (internal, used by submit)
// =====================
// =====================
// Dimension preset change
// =====================
$('dimensionPreset').addEventListener('change', () => {
  const val = $('dimensionPreset').value;
  if (val === 'custom') {
    $('customDimensions').classList.remove('hidden');
  } else {
    $('customDimensions').classList.add('hidden');
    const p = PRESETS[val];
    $('width').value = p.w;
    $('height').value = p.h;
  }
});

// =====================
// Mode change
// =====================
$('mode').addEventListener('change', updateMode);
$('agentToggle').addEventListener('change', () => {
  if ($('agentToggle').checked) setTranslationMode('normal');
  else renderQueueStatus();
});
$('fastTranslatorToggle')?.addEventListener('change', () => {
  if ($('fastTranslatorToggle').checked) setTranslationMode('fast');
  else renderQueueStatus();
});

function schedulePromptDraftSave() {
  if (promptDraftSaveTimer) clearTimeout(promptDraftSaveTimer);
  promptDraftSaveTimer = setTimeout(() => {
    promptDraftSaveTimer = null;
    if (promptIsComposing) return;
    try { localStorage.setItem(PROMPT_DRAFT_KEY, draftPrompt || ''); } catch (_) {}
  }, PROMPT_DRAFT_SAVE_MS);
}

function restorePromptDraft() {
  if (!promptInput || promptInput.value) return;
  try {
    const saved = localStorage.getItem(PROMPT_DRAFT_KEY);
    if (saved) {
      draftPrompt = saved;
      promptInput.value = saved;
    }
  } catch (_) {}
}

function splitPromptTags(text) {
  return String(text || '')
    .replace(/，/g, ',')
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function promptTagKey(tag) {
  return String(tag || '').trim().toLowerCase().replace(/[_-]+/g, ' ').replace(/\s+/g, ' ');
}

function stripLeadingKnownCharacterTags(tags) {
  const horseTailIndex = tags.slice(0, 10).findIndex((tag) => promptTagKey(tag) === 'horse tail');
  if (horseTailIndex >= 0 && tags.slice(0, horseTailIndex + 1).some((tag) => promptTagKey(tag) === 'umamusume')) {
    return tags.slice(horseTailIndex + 1);
  }
  return tags.filter((tag) => {
    const key = promptTagKey(tag);
    return !(key === 'umamusume' || key === 'horse ears' || key === 'horse tail' || key.includes('(umamusume)'));
  });
}

function applyPendingCharacterTagsToPrompt() {
  const raw = sessionStorage.getItem('pending_prompt_character_tags');
  if (!raw || !promptInput) return;
  let payload = null;
  try { payload = JSON.parse(raw); } catch (_) { payload = {tags: raw}; }
  const incoming = splitPromptTags(payload?.tags || '');
  if (!incoming.length) {
    sessionStorage.removeItem('pending_prompt_character_tags');
    return;
  }
  let existing = splitPromptTags(promptInput.value);
  const hasOtherUmaCharacter = existing.some((tag) => promptTagKey(tag) === 'umamusume')
    && !incoming.every((tag) => existing.some((oldTag) => promptTagKey(oldTag) === promptTagKey(tag)));
  if (hasOtherUmaCharacter) {
    const name = payload?.name_zh || payload?.name_en || t('app.character', '当前人物');
    const ok = window.confirm(t('tags.replace_character_confirm', 'Prompt 已包含其他人物，是否替换为“{name}”？', {name}));
    if (!ok) {
      sessionStorage.removeItem('pending_prompt_character_tags');
      return;
    }
    existing = stripLeadingKnownCharacterTags(existing);
  }
  const merged = [];
  const seen = new Set();
  for (const tag of [...incoming, ...existing]) {
    const key = promptTagKey(tag);
    if (!key || seen.has(key)) continue;
    merged.push(tag);
    seen.add(key);
  }
  promptInput.value = merged.join(', ');
  draftPrompt = promptInput.value;
  try { localStorage.setItem(PROMPT_DRAFT_KEY, draftPrompt); } catch (_) {}
  promptInput.dispatchEvent(new Event('input', {bubbles: true}));
  promptInput.dispatchEvent(new Event('change', {bubbles: true}));
  sessionStorage.removeItem('pending_prompt_character_tags');
  setMessage('message', t('tags.inserted_home', '已插入到首页 Prompt。'), 'ok');
}

function updateFileInputUi(input) {
  if (!input) return;
  const wrap = input.nextElementSibling?.classList?.contains('custom-file')
    ? input.nextElementSibling
    : null;
  if (!wrap) return;
  const button = wrap.querySelector('.custom-file-button');
  const name = wrap.querySelector('.custom-file-name');
  if (button) button.textContent = t('file.choose', '选择文件');
  if (name) name.textContent = input.files?.[0]?.name || t('file.none', '未选择任何文件');
}

function setupCustomFileInputs() {
  document.querySelectorAll('input[type="file"]').forEach((input) => {
    if (input.dataset.customFileReady === '1') {
      updateFileInputUi(input);
      return;
    }
    input.dataset.customFileReady = '1';
    input.classList.add('native-file-input');
    const wrap = document.createElement('div');
    wrap.className = 'custom-file';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'ghost custom-file-button';
    const name = document.createElement('span');
    name.className = 'custom-file-name';
    button.addEventListener('click', () => input.click());
    input.addEventListener('change', () => updateFileInputUi(input));
    input.insertAdjacentElement('afterend', wrap);
    wrap.append(button, name);
    updateFileInputUi(input);
  });
}

// Keep Prompt typing extremely light, especially for mobile Chinese IME.
const promptInput = $('prompt');
restorePromptDraft();
applyPendingCharacterTagsToPrompt();
promptInput?.addEventListener('focus', () => {
  promptInputFocused = true;
  markPromptEditing();
  restartPollingForPromptState();
});
promptInput?.addEventListener('blur', () => {
  promptInputFocused = false;
  promptIsComposing = false;
  promptEditingSince = 0;
  restartPollingForPromptState();
});
promptInput?.addEventListener('compositionstart', () => {
  promptIsComposing = true;
  markPromptEditing();
  if (promptDraftSaveTimer) {
    clearTimeout(promptDraftSaveTimer);
    promptDraftSaveTimer = null;
  }
  restartPollingForPromptState();
});
promptInput?.addEventListener('compositionend', () => {
  promptIsComposing = false;
  markPromptEditing();
  draftPrompt = promptInput.value;
  schedulePromptDraftSave();
  restartPollingForPromptState();
});
promptInput?.addEventListener('input', () => {
  markPromptEditing();
  draftPrompt = promptInput.value;
  if (promptIsComposing) return;
  schedulePromptDraftSave();
});

// =====================
// History drawer events
// =====================
$('historyBtn').addEventListener('click', () => {
  if (isDrawerOpen()) { closeDrawer(); } else { openDrawer(); }
});
$('menuBtn')?.addEventListener('click', (event) => {
  event.stopPropagation();
  const menu = $('navMenu');
  const btn = $('menuBtn');
  if (!menu || !btn) return;
  const willOpen = menu.classList.contains('hidden');
  menu.classList.toggle('hidden', !willOpen);
  btn.setAttribute('aria-expanded', String(willOpen));
});
document.addEventListener('click', (event) => {
  const menu = $('navMenu');
  const btn = $('menuBtn');
  if (!menu || !btn || menu.classList.contains('hidden')) return;
  if (event.target.closest('#navMenu') || event.target.closest('#menuBtn')) return;
  menu.classList.add('hidden');
  btn.setAttribute('aria-expanded', 'false');
});
$('drawerCloseBtn').addEventListener('click', closeDrawer);
$('historyOverlay').addEventListener('click', closeDrawer);
$('loadMoreBtn').addEventListener('click', () => loadHistory(false));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (isDrawerOpen()) { closeDrawer(); return; }
    const menu = $('navMenu');
    const btn = $('menuBtn');
    if (menu && !menu.classList.contains('hidden')) {
      menu.classList.add('hidden');
      if (btn) btn.setAttribute('aria-expanded', 'false');
    }
  }
});

function getCharacterResolutionDetail(error) {
  if (error?.status !== 409) return null;
  const payload = error?.data || {};
  const detail = payload.detail || payload;
  const isSelectionRequired =
    detail?.code === 'character_resolution_required'
    || detail?.requiresCharacterSelection === true
    || payload?.requiresCharacterSelection === true;
  if (!isSelectionRequired) return null;
  const resolution =
    detail?.resolution
    || detail?.characterResolution
    || payload?.resolution
    || payload?.characterResolution
    || null;
  if (!resolution || !Array.isArray(resolution.mentions)) return null;
  return resolution;
}

function closeCharacterResolutionDialog(messageKey = '') {
  const dialog = $('characterResolutionDialog');
  if (dialog) dialog.classList.add('hidden');
  document.body.classList.remove('dialog-open');
  pendingCharacterResolution = null;
  characterResolutionSubmitting = false;
  if ($('submitBtn')) $('submitBtn').disabled = false;
  if (messageKey) setMessage('message', t(messageKey, '已取消，Prompt 已保留，未创建任务。'));
}

function updateCharacterResolutionConfirmState() {
  const groups = Array.from(document.querySelectorAll('.character-resolution-group'));
  const ready = groups.length > 0 && groups.every((group) => group.querySelector('input[type="radio"]:checked'));
  const confirmBtn = $('characterResolutionConfirmBtn');
  if (confirmBtn) confirmBtn.disabled = !ready || characterResolutionSubmitting;
}

function renderCharacterResolutionDialog(resolution, submitContext) {
  const groups = Array.isArray(resolution?.mentions)
    ? resolution.mentions.filter((item) => item && item.status === 'ambiguous')
    : [];
  if (!groups.length) return false;
  pendingCharacterResolution = {
    resolution,
    submitContext,
    requestId: submitContext.clientRequestId,
  };
  const dialog = $('characterResolutionDialog');
  const intro = $('characterResolutionIntro');
  const groupRoot = $('characterResolutionGroups');
  const errorBox = $('characterResolutionError');
  if (!dialog || !intro || !groupRoot || !errorBox) return false;

  const mentionSummary = groups.map((group) => `“${group.rawText || ''}”`).join('、');
  const isSingleFuzzy = groups.length === 1
    && groups[0].candidates
    && groups[0].candidates.length === 1
    && groups[0].matchType
    && !['exact_zh', 'exact_en', 'tag'].includes(groups[0].matchType);
  intro.textContent = isSingleFuzzy
    ? t('character_resolution.fuzzy_intro', '已自动为你匹配到人物库中可能的角色，请确认：')
    : groups.length === 1
      ? t('character_resolution.intro', `“${groups[0].rawText || ''}”匹配到多个角色，请选择你想生成的人物。`, {mention: groups[0].rawText || ''})
      : `${t('character_resolution.pick_all', '请为每个存在歧义的人物选择一个选项。')} ${mentionSummary}`;
  errorBox.classList.add('hidden');
  errorBox.textContent = '';
  groupRoot.textContent = '';

  groups.forEach((group, groupIndex) => {
    const section = document.createElement('section');
    section.className = 'character-resolution-group';
    section.dataset.mentionId = group.mentionId || '';
    section.dataset.rawText = group.rawText || '';

    const title = document.createElement('p');
    title.className = 'character-resolution-group-title';
    const isGroupFuzzy = group.candidates && group.candidates.length === 1
      && group.matchType && !['exact_zh', 'exact_en', 'tag'].includes(group.matchType);
    title.textContent = isGroupFuzzy
      ? t('character_resolution.fuzzy_intro', '已自动为你匹配到人物库中可能的角色，请确认：')
      : t('character_resolution.intro', `“${group.rawText || ''}”匹配到多个角色，请选择你想生成的人物。`, {mention: group.rawText || ''});
    section.appendChild(title);

    const options = document.createElement('div');
    options.className = 'character-resolution-options';
    const radioName = `character-resolution-${groupIndex}`;
    (group.candidates || []).forEach((candidate, optionIndex) => {
      const label = document.createElement('label');
      label.className = 'character-resolution-option';

      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = radioName;
      radio.value = candidate.characterId || candidate.character_key || '';
      radio.dataset.mentionId = group.mentionId || '';
      radio.dataset.rawText = group.rawText || '';

      const textWrap = document.createElement('span');
      const name = document.createElement('span');
      name.className = 'character-resolution-name';
      name.textContent = candidate.name || candidate.name_zh || candidate.name_en || radio.value;
      const series = document.createElement('span');
      series.className = 'character-resolution-series';
      series.textContent = candidate.series || candidate.franchise || '';
      textWrap.appendChild(name);
      if (series.textContent) textWrap.appendChild(series);

      label.appendChild(radio);
      label.appendChild(textWrap);
      options.appendChild(label);
    });

    const noLibrary = document.createElement('label');
    noLibrary.className = 'character-resolution-option';
    const noLibraryRadio = document.createElement('input');
    noLibraryRadio.type = 'radio';
    noLibraryRadio.name = radioName;
    noLibraryRadio.value = '__no_library_character__';
    noLibraryRadio.dataset.mentionId = group.mentionId || '';
    noLibraryRadio.dataset.rawText = group.rawText || '';
    const noLibraryText = document.createElement('span');
    const noLibraryName = document.createElement('span');
    noLibraryName.className = 'character-resolution-name';
    noLibraryName.textContent = t('character_resolution.no_library', '不使用人物库人物');
    const noLibraryHint = document.createElement('span');
    noLibraryHint.className = 'character-resolution-hint';
    noLibraryHint.textContent = t('character_resolution.no_library_hint', `将“${group.rawText || ''}”作为普通文本交给 Agent 处理`, {mention: group.rawText || ''});
    noLibraryText.appendChild(noLibraryName);
    noLibraryText.appendChild(noLibraryHint);
    noLibrary.appendChild(noLibraryRadio);
    noLibrary.appendChild(noLibraryText);
    options.appendChild(noLibrary);
    options.addEventListener('change', () => {
      options.querySelectorAll('.character-resolution-option').forEach((option) => {
        const input = option.querySelector('input[type="radio"]');
        option.classList.toggle('selected', Boolean(input?.checked));
      });
      updateCharacterResolutionConfirmState();
    });

    section.appendChild(options);
    groupRoot.appendChild(section);
  });

  dialog.classList.remove('hidden');
  document.body.classList.add('dialog-open');
  updateCharacterResolutionConfirmState();
  return true;
}

async function confirmCharacterResolution() {
  if (!pendingCharacterResolution || characterResolutionSubmitting) return;
  const groups = Array.from(document.querySelectorAll('.character-resolution-group'));
  const selections = [];
  for (const group of groups) {
    const selected = group.querySelector('input[type="radio"]:checked');
    if (!selected) {
      const errorBox = $('characterResolutionError');
      if (errorBox) {
        errorBox.textContent = t('character_resolution.pick_all', '请为每个存在歧义的人物选择一个选项。');
        errorBox.classList.remove('hidden');
      }
      updateCharacterResolutionConfirmState();
      return;
    }
    const skipCharacterLibrary = selected.value === '__no_library_character__';
    selections.push({
      mentionId: selected.dataset.mentionId || group.dataset.mentionId || '',
      rawText: selected.dataset.rawText || group.dataset.rawText || '',
      characterId: skipCharacterLibrary ? null : selected.value,
      skipCharacterLibrary,
    });
  }
  characterResolutionSubmitting = true;
  $('characterResolutionConfirmBtn').disabled = true;
  $('characterResolutionCancelBtn').disabled = true;
  setMessage('message', t('character_resolution.loading', '正在继续提交…'));
  const ctx = pendingCharacterResolution.submitContext;
  const payload = {status: 'resolved', selections};
  try {
    await doSubmit(ctx.promptText, ctx.w, ctx.h, payload, ctx.clientRequestId);
    $('characterResolutionDialog')?.classList.add('hidden');
    document.body.classList.remove('dialog-open');
    pendingCharacterResolution = null;
  } finally {
    characterResolutionSubmitting = false;
    if ($('characterResolutionConfirmBtn')) $('characterResolutionConfirmBtn').disabled = false;
    if ($('characterResolutionCancelBtn')) $('characterResolutionCancelBtn').disabled = false;
  }
}

$('characterResolutionCancelBtn')?.addEventListener('click', () => closeCharacterResolutionDialog('character_resolution.cancelled'));
$('characterResolutionConfirmBtn')?.addEventListener('click', confirmCharacterResolution);
$('characterResolutionDialog')?.addEventListener('click', (event) => {
  if (event.target === $('characterResolutionDialog')) closeCharacterResolutionDialog('character_resolution.cancelled');
});

// =====================
// Form submission
// =====================
$('generateForm').addEventListener('submit', async(e) => {
  e.preventDefault();
  if (window._submitLock) return;
  window._submitLock = true;
  $('submitBtn').disabled = true;
  setMessage('message', t('topup.submitting', '正在提交…'));

  // Get dimensions
  const dims = getSelectedDimensions();
  const w = dims.w;
  const h = dims.h;

  // Validate dimensions
  const dimError = validateResolutionFrontend(w, h);
  if (dimError) {
    setMessage('message', dimError, 'error');
    $('submitBtn').disabled = false;
    window._submitLock = false;
    return;
  }

  const promptText = $('prompt').value.trim();
  if (!promptText && $('mode').value !== 'controlnet') {
    setMessage('message', t('app.prompt_required', '请填写描述'), 'error');
    $('submitBtn').disabled = false;
    window._submitLock = false;
    return;
  }

  try {
    await doSubmit(promptText, w, h);
  } finally {
    window._submitLock = false;
  }
});

function makeClientRequestId() {
  return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
}

async function doSubmit(promptText, w, h, characterResolution = null, clientRequestId = null) {
  $('submitBtn').disabled = true;
  setMessage('message', t('topup.submitting', '正在提交…'));
  const requestId = clientRequestId || makeClientRequestId();
  const translationMode = getTranslationMode();
  let promptForTask = promptText;
  let effectivePromptForPreview = translationMode === 'normal' ? null : promptText;
  let promptSource = '';

  if (translationMode === 'fast') {
    try {
      setMessage('message', t('app.fast_translating', '正在极速翻译…'));
      const fastPayload = {
        text: promptText,
        client_request_id: requestId,
      };
      if (characterResolution) fastPayload.character_resolution = characterResolution;
      const fastData = await api('/api/prompt/fast-refine', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(fastPayload),
      });
      promptForTask = fastData.prompt || promptText;
      effectivePromptForPreview = promptForTask;
      promptSource = `fast_translate:${fastData.request_code || ''}`;
      window._lastFastTranslationCode = fastData.request_code || '';
    } catch (err) {
      const resolution = getCharacterResolutionDetail(err);
      if (resolution && !characterResolution) {
        const opened = renderCharacterResolutionDialog(resolution, {promptText, w, h, clientRequestId: requestId});
        if (opened) {
          setMessage('message', '');
          return;
        }
      }
      setMessage('message', translateApiMessage(err.message), 'error');
      return;
    }
  }

  const fd = new FormData();
  fd.set('mode', $('mode').value);
  const selectedStyleKey = normalizeStyleKey($('styleKey').value);
  fd.set('style_key', selectedStyleKey);
  fd.set('prompt', promptForTask);
  if (translationMode === 'fast') fd.set('original_prompt', promptText);
  fd.set('prompt_source', translationMode === 'fast' ? 'fast_translate' : '');
  fd.set('width', String(w));
  fd.set('height', String(h));
  fd.set('lora_weight', $('loraWeight').value);
  fd.set('denoise', $('denoise').value);
  fd.set('control_type', $('controlType').value);
  fd.set('control_character', $('controlCharacter').value);
  fd.set('auto_tagger', $('autoTagger').checked ? 'true' : 'false');
  fd.set('use_agent', translationMode === 'normal' ? 'true' : 'false');
  fd.set('client_request_id', requestId);
  if (translationMode === 'fast' && window._lastFastTranslationCode) {
    fd.set('fast_translation_request_code', window._lastFastTranslationCode);
  }
  if (characterResolution) fd.set('character_resolution', JSON.stringify(characterResolution));
  if ($('inputImage').files[0]) fd.set('input_image', $('inputImage').files[0]);

  try {
    const shouldSelectSubmittedJob = !activeJobCode;
    const data = await api('/api/tasks', {method:'POST', body:fd});
    latestSubmittedJobCode = data.job_code;
    setMessage('message', t('app.queued_success', `已加入任务队列：${data.job_code}`, {job: data.job_code}), 'ok');
    await loadQueueStatus();
    await loadTaskSummary();

    if (shouldSelectSubmittedJob) {
      setActiveJob(data.job_code, me.user_id);
      console.log(`[UI] selected job=${activeJobCode} status=queued`);
      renderCurrentTask({
        job_code: activeJobCode,
        status: 'queued',
        prompt: promptText,
        original_prompt: promptText,
        effective_prompt: effectivePromptForPreview,
        use_agent: translationMode === 'normal' ? 1 : 0,
        generation_mode: $('mode').value,
        style_key: selectedStyleKey,
        width: w,
        height: h,
        outputs: [],
      });
    } else {
      console.log(`[UI] submitted job=${data.job_code}; keeping selected job=${activeJobCode}`);
    }

    // Then start polling by specific job_code
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; pollingActive = false; }
    startPolling();
    await loadCurrentTask();
    await loadQueueStatus();
    await loadTaskSummary();
    if (isDrawerOpen()) await loadHistory(true, {preserveScroll: true});

    // Reload me for balance update
    try {
      me = await api('/api/me');
      $('balance').textContent = credits(me.balance_fen);
    } catch(_) {}
  } catch(err) {
    const resolution = getCharacterResolutionDetail(err);
    if (resolution && translationMode === 'normal' && !characterResolution) {
      const opened = renderCharacterResolutionDialog(resolution, {promptText, w, h, clientRequestId: requestId});
      if (opened) {
        setMessage('message', '');
        return;
      }
    }
    setMessage('message', translateApiMessage(err.message), 'error');
  } finally {
    $('submitBtn').disabled = false;
  }
}

// =====================
// Logout
// =====================
async function performLogout() {
  clearActiveJob();
  sessionStorage.removeItem('activeUserId');
  if (supportUnreadTimer) window.clearInterval(supportUnreadTimer);
  if (topupSubmitReminderTimer) window.clearInterval(topupSubmitReminderTimer);
  await fetch('/auth/logout', {method: 'POST', credentials: 'same-origin', ...withCsrf({method: 'POST'})});
  window.location.href='/login';
}
$('logoutBtn')?.addEventListener('click', performLogout);
$('logoutMenuBtn')?.addEventListener('click', (event) => {
  event.preventDefault();
  performLogout();
});

$('supportLaterBtn')?.addEventListener('click', closeImportantSupportDialog);
$('supportViewBtn')?.addEventListener('click', () => {
  const code = activeSupportThreadCode ? `?thread=${encodeURIComponent(activeSupportThreadCode)}` : '';
  window.location.href = `/messages${code}`;
});
$('topupSubmitLaterBtn')?.addEventListener('click', closeTopupSubmitReminder);
$('topupSubmitGoBtn')?.addEventListener('click', () => {
  const code = activeTopupSubmitReminder?.topup_code ? `?topup=${encodeURIComponent(activeTopupSubmitReminder.topup_code)}` : '';
  window.location.href = `/topup${code}`;
});
$('smartAgentLaunchLaterBtn')?.addEventListener('click', () => closeSmartAgentLaunchDialog(true));
$('smartAgentLaunchGoBtn')?.addEventListener('click', () => {
  closeSmartAgentLaunchDialog(true);
  window.location.href = '/smart-agent';
});
$('smartAgentLaunchDialog')?.addEventListener('click', (event) => {
  if (event.target === $('smartAgentLaunchDialog')) closeSmartAgentLaunchDialog(true);
});
$('referralCampaignLaterBtn')?.addEventListener('click', () => closeReferralCampaignDialog(true));
$('referralCampaignGoBtn')?.addEventListener('click', async () => {
  await markReferralCampaignSeen();
  window.location.href = '/profile#referralSection';
});
$('referralCampaignDialog')?.addEventListener('click', (event) => {
  if (event.target === $('referralCampaignDialog')) closeReferralCampaignDialog(true);
});

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && me) {
    refreshSupportUnread();
    checkTopupSubmitReminder();
  }
});

window.addEventListener('uma:langchange', () => {
  window.UmaI18n?.apply(document);
  renderStyleOptions();
  setupCustomFileInputs();
  currentTaskRenderSignature = '';
  lastHistorySignature = '';
  renderQueueStatus();
  if (currentTaskData) renderCurrentTask(currentTaskData);
  else renderCurrentTask(null);
  if (isDrawerOpen()) loadHistory(true, {force: true, preserveScroll: true});
});

// =====================
// Init
// =====================
window.UmaI18n?.apply(document);
setupCustomFileInputs();
updateMode();
loadMe();
