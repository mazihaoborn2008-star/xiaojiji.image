const $ = (id) => document.getElementById(id);
let me = null;
let activeCode = null;
let paymentStep = 'choose';
let topupItems = [];
let _createLock = false;          // anti-double-submit
const PAYMENT_WECHAT = 'wechat_qr';
const PAYMENT_ASB = 'asb_bank_transfer';
const TOPUP_NOTICE_TZ = 'Pacific/Auckland';
const BEIJING_TZ = 'Asia/Shanghai';

function t(key, fallback, params) {
  return window.UmaI18n?.t(key, fallback, params) ?? (fallback ?? key);
}

function translateMessage(text) {
  return window.UmaI18n?.translateMessage(text) ?? text;
}

function friendlyError(errMsg) {
  const raw = String(errMsg || '');
  const lang = window.UmaI18n?.getLang?.() ?? 'zh';
  if (/401|登录.*失效|unauthorized/i.test(raw)) {
    return lang === 'en'
      ? 'Your login session has expired. Please log in again.'
      : '登录状态已失效，请重新登录。';
  }
  if (/403|验证失败|forbidden/i.test(raw)) {
    return lang === 'en'
      ? 'Request validation failed. Please refresh the page and try again.'
      : '请求验证失败，请刷新页面后重试。';
  }
  if (/网络|network|fetch|timeout|ECONNREFUSED/i.test(raw)) {
    return lang === 'en'
      ? 'The top-up page is temporarily unavailable. Please try again later.'
      : '充值页面暂时无法打开，请稍后重试。';
  }
  return raw;
}

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

async function api(url, options = {}) {
  const res = await fetch(url, {credentials: 'same-origin', ...withCsrf(options)});
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    let msg;
    if (typeof getApiErrorMessage === 'function') {
      msg = getApiErrorMessage(data, `HTTP ${res.status}`);
    } else {
      msg = data?.detail || `HTTP ${res.status}`;
    }
    throw new Error(translateMessage(msg));
  }
  return data;
}

function money(fen) { return `¥${(Number(fen || 0) / 100).toFixed(2)}`; }
function creditsBalance(fen) { return `${Math.trunc(Number(fen || 0))} credits`; }
function refreshPaymentQr() {
  const img = $('paymentQr');
  if (img) img.src = `/api/payment-qr?v=${Date.now()}`;
}
function zonedParts(date, timeZone) {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(date);
  const map = Object.fromEntries(parts.filter((p) => p.type !== 'literal').map((p) => [p.type, p.value]));
  return {
    year: Number(map.year),
    month: Number(map.month),
    day: Number(map.day),
    hour: Number(map.hour) % 24,
    minute: Number(map.minute),
  };
}
function civilMinutes(parts) {
  return Math.floor(Date.UTC(parts.year, parts.month - 1, parts.day) / 60000) + parts.hour * 60 + parts.minute;
}
function addCivilDays(parts, days) {
  const d = new Date(Date.UTC(parts.year, parts.month - 1, parts.day + days, parts.hour || 0, parts.minute || 0));
  return {year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate(), hour: parts.hour || 0, minute: parts.minute || 0};
}
function zonedLocalToDate(timeZone, parts) {
  const target = {...parts, minute: parts.minute || 0};
  let utcMs = Date.UTC(target.year, target.month - 1, target.day, target.hour, target.minute);
  for (let i = 0; i < 4; i += 1) {
    const actual = zonedParts(new Date(utcMs), timeZone);
    const diff = civilMinutes(actual) - civilMinutes(target);
    if (diff === 0) break;
    utcMs -= diff * 60000;
  }
  return new Date(utcMs);
}
function formatBeijingTime(date) {
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: BEIJING_TZ,
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date).replace(/^24:/, '00:');
}
function topupNoticeWindow(now = new Date()) {
  const nz = zonedParts(now, TOPUP_NOTICE_TZ);
  const shouldShow = nz.hour >= 22 || nz.hour < 9;
  const startLocal = {...nz, hour: 22, minute: 0};
  const endBase = addCivilDays(nz, 1);
  const endLocal = {...endBase, hour: 9, minute: 0};
  return {
    shouldShow,
    start: formatBeijingTime(zonedLocalToDate(TOPUP_NOTICE_TZ, startLocal)),
    end: formatBeijingTime(zonedLocalToDate(TOPUP_NOTICE_TZ, endLocal)),
  };
}
function topupNoticeStorageKey(user) {
  const account = user?.account_id || user?.user_id || 'anonymous';
  const sessionId = user?.session_public_id || 'current_login';
  return `topup_warning_shown:${account}:${sessionId}`;
}
function topupNoticeStorage() {
  return window.localStorage || window.sessionStorage || sessionStorage;
}
function closeTopupTimeNotice() {
  $('topupTimeNotice')?.classList.add('hidden');
  if (me) topupNoticeStorage().setItem(topupNoticeStorageKey(me), '1');
}
function maybeShowTopupTimeNotice(now = new Date()) {
  const modal = $('topupTimeNotice');
  if (!modal || !me) return false;
  const key = topupNoticeStorageKey(me);
  if (topupNoticeStorage().getItem(key)) return false;
  const windowInfo = topupNoticeWindow(now);
  if (!windowInfo.shouldShow) return false;
  const body = $('topupTimeNoticeBody');
  if (body) {
    body.textContent = t(
      'topup.time_notice.body',
      '北京时间 {start} - 次日 {end} 不建议充值操作。\n如已付款，请耐心等待管理员确认。',
      {start: windowInfo.start, end: windowInfo.end},
    );
  }
  modal.classList.remove('hidden');
  $('topupTimeNoticeOk')?.focus();
  return true;
}
function clearTopupNoticeSessionFlags() {
  for (const storage of [window.localStorage, window.sessionStorage, sessionStorage]) {
    if (!storage) continue;
    for (let i = storage.length - 1; i >= 0; i -= 1) {
      const key = storage.key(i);
      if (key && key.startsWith('topup_warning_shown:')) storage.removeItem(key);
    }
  }
}
function selectedPaymentMethod() {
  return document.querySelector('input[name="payment_method"]:checked')?.value || PAYMENT_WECHAT;
}
function isAsb(order) { return order?.payment_method === PAYMENT_ASB; }
function stepMethod() {
  if (paymentStep === 'asb') return PAYMENT_ASB;
  if (paymentStep === 'wechat') return PAYMENT_WECHAT;
  return null;
}
function stepForMethod(method) {
  return method === PAYMENT_ASB ? 'asb' : 'wechat';
}
function currentActiveOrder(items = topupItems) {
  const requested = new URLSearchParams(window.location.search).get('topup');
  if (requested) {
    const match = (items || []).find((item) => item.code === requested && ['created', 'paid'].includes(item.status));
    if (match) {
      paymentStep = stepForMethod(match.payment_method);
      renderMethodPanels();
      return match;
    }
  }
  const method = stepMethod();
  if (!method) return null;
  return (items || []).find((item) => item.payment_method === method && ['created', 'paid'].includes(item.status));
}
function nzdPackageLabel(credits) {
  const map = {100: 'NZD $1.00', 200: 'NZD $2.00', 500: 'NZD $5.00', 1000: 'NZD $10.00'};
  return map[Number(credits)] || '';
}
function setMessage(text, type = '') {
  const el = $('message');
  el.textContent = translateMessage(text);
  el.className = `message ${type}`;
}

async function copyText(text) {
  const value = String(text || '');
  if (!value) return false;
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch (_) {}
  }
  const input = document.createElement('textarea');
  input.value = value;
  input.setAttribute('readonly', '');
  input.className = 'clipboard-fallback';
  document.body.append(input);
  input.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (_) {
    ok = false;
  }
  input.remove();
  return ok;
}

function copyButton(label, text) {
  const btn = document.createElement('button');
  btn.className = 'ghost compact';
  btn.type = 'button';
  btn.textContent = label;
  btn.onclick = async () => {
    const ok = await copyText(text);
    setMessage(ok ? t('topup.copy_ok', '已复制') : t('topup.copy_failed', '复制失败，请手动复制。'), ok ? 'ok' : 'error');
  };
  return btn;
}

function renderWechatPaymentNotice(order) {
  if (!order || !order.code) return null;
  const section = document.createElement('div');
  section.className = 'wechat-payment-notice';

  const warnTitle = document.createElement('div');
  warnTitle.className = 'wpn-title';
  warnTitle.textContent = t('topup.wpn_title', '⚠️ 付款备注必须填写充值单号');

  const explain = document.createElement('p');
  explain.className = 'wpn-explain';
  explain.textContent = t('topup.wpn_explain', '请在微信付款备注 / 付款单号中填写下方充值单号，否则管理员可能无法确认你的付款。');

  const codeLabel = document.createElement('div');
  codeLabel.className = 'wpn-code-label';
  codeLabel.textContent = t('topup.wpn_code_label', '充值单号：');
  const codeValue = document.createElement('div');
  codeValue.className = 'wpn-code-value';
  codeValue.textContent = order.code;

  const afterPay = document.createElement('p');
  afterPay.className = 'wpn-after-pay';
  afterPay.textContent = t('topup.wpn_after_pay', '付款完成后，请回到本页面点击「我已付款，提交审核」。管理员确认收款后，credits 才会到账。');

  const warnings = document.createElement('div');
  warnings.className = 'wpn-warnings';
  const w1 = document.createElement('span');
  w1.textContent = t('topup.wpn_w1', '❌ 请勿只填写昵称');
  const w2 = document.createElement('span');
  w2.textContent = t('topup.wpn_w2', '❌ 请勿付款后直接关闭页面');
  const w3 = document.createElement('span');
  w3.textContent = t('topup.wpn_w3', '❌ 未点击「我已付款」的订单不会进入审核');
  warnings.append(w1, w2, w3);

  const actions = document.createElement('div');
  actions.className = 'wpn-actions';
  actions.append(copyButton(t('topup.copy_order_code', '复制充值单号'), order.code));

  section.append(warnTitle, explain, codeLabel, codeValue, afterPay, warnings, actions);
  return section;
}

function statusText(status, method = PAYMENT_WECHAT) {
  if (method === PAYMENT_ASB) {
    return {
      created: 'Awaiting payment',
      paid: 'Waiting for admin confirmation',
      approved: 'Credits added',
      rejected: 'Rejected',
      expired: 'Expired',
    }[status] || status;
  }
  const zh = {
    created: '待付款',
    paid: '已提交审核',
    approved: '已到账',
    rejected: '已驳回',
    expired: '已过期',
  };
  return zh[status] || status;
}

function updateFlowCopy() {
  const title = document.querySelector('.form-panel h1');
  const subtitle = document.querySelector('[data-i18n="topup.subtitle"]');
  const historyTitle = document.querySelector('#topupHistoryPanel h2');
  if (paymentStep === 'wechat') {
    if (title) title.textContent = '发起微信充值';
    if (subtitle) subtitle.textContent = '充值后由管理员人工确认到账。';
    if (historyTitle) historyTitle.textContent = '充值记录';
    return;
  }
  if (paymentStep === 'asb') {
    if (title) title.textContent = 'ASB Bank Transfer Top-up';
    if (subtitle) subtitle.textContent = 'Top-ups are manually confirmed by the admin.';
    if (historyTitle) historyTitle.textContent = 'Top-up History';
    return;
  }
  if (title) title.textContent = t('topup.title', '发起充值');
  if (subtitle) subtitle.textContent = t('topup.subtitle', '充值后由群主人工确认到账。');
  if (historyTitle) historyTitle.textContent = t('topup.history', '充值记录');
}

function renderMethodPanels() {
  const isChoose = paymentStep === 'choose';
  updateFlowCopy();
  $('paymentChoicePanel')?.classList.toggle('hidden', !isChoose);
  $('wechatCreatePanel')?.classList.toggle('hidden', paymentStep !== 'wechat');
  $('asbCreatePanel')?.classList.toggle('hidden', paymentStep !== 'asb');
  $('topupHistoryPanel')?.classList.toggle('hidden', isChoose);
  const btn = $('createTopupBtn');
  if (btn) {
    if (isChoose) btn.textContent = window.UmaI18n?.getLang?.() === 'en' ? 'Continue' : '确认付款方式';
    else btn.textContent = paymentStep === 'asb' ? 'Create order' : '生成充值单';
  }
  renderActive(isChoose ? null : currentActiveOrder());
  setMessage('');
  // Ensure create button is enabled
  if (btn) btn.disabled = false;
}

function appendLine(parent, label, value) {
  const line = document.createElement('div');
  line.className = 'payment-detail-line';
  const k = document.createElement('span');
  k.textContent = label;
  const v = document.createElement('strong');
  v.textContent = value || '-';
  line.append(k, v);
  parent.append(line);
}

function renderActive(order) {
  const box = $('activeOrder');
  const qrPanel = $('wechatQrPanel');
  box.replaceChildren();
  if (!order || !order.code || !['created', 'paid'].includes(order.status)) {
    box.classList.add('hidden');
    qrPanel?.classList.add('hidden');
    activeCode = null;
    return;
  }
  activeCode = order.code;
  box.classList.remove('hidden');
  box.classList.toggle('asb-active', isAsb(order));
  box.classList.toggle('wechat-active', !isAsb(order));
  qrPanel?.classList.toggle('hidden', isAsb(order));
  if (!isAsb(order)) refreshPaymentQr();

  const title = document.createElement('h2');
  title.textContent = isAsb(order) ? 'Current ASB top-up order' : '当前充值单';
  const code = document.createElement('div');
  code.className = 'topup-code';
  code.textContent = order.code;
  const amount = document.createElement('div');
  amount.className = 'topup-amount';
  amount.textContent = order.amount_text || money(order.amount_fen);
  const state = document.createElement('div');
  state.className = `topup-status ${order.status}`;
  state.textContent = statusText(order.status, order.payment_method);
  box.append(title, code, amount, state);

  if (isAsb(order)) {
    const info = document.createElement('div');
    info.className = 'payment-rules asb-rules active-payment-rules';
    const intro = document.createElement('div');
    intro.className = 'payment-instructions';
    intro.innerHTML = [
      '<strong>Payment method: ASB Bank Transfer</strong>',
      '<p>Please transfer the exact amount to the ASB account below.</p>',
      '<p>Use the order reference as your bank transfer reference.</p>',
      '<p>Your credits will be added after the admin confirms the payment has arrived.</p>',
      '<p>Bank transfers may take some time to arrive.</p>',
      '<p>Please click "I have paid" after completing the transfer.</p>',
    ].join('');
    info.append(intro);
    if (!order.asb?.configured) {
      const unavailable = document.createElement('div');
      unavailable.className = 'ct-error';
      unavailable.textContent = 'ASB payment is temporarily unavailable. Please contact the admin.';
      info.append(unavailable);
    } else {
      appendLine(info, 'Payee:', order.asb.payee_name);
      appendLine(info, 'Bank:', order.asb.bank_name || 'ASB');
      appendLine(info, 'Account number:', order.asb.account_number);
      appendLine(info, 'Amount:', order.amount_text || nzdPackageLabel(order.credits));
      appendLine(info, 'Reference:', order.payment_reference || order.code);
      const pricing = document.createElement('div');
      pricing.className = 'asb-pricing';
      pricing.innerHTML = [
        '<strong>ASB pricing:</strong>',
        '<span>100 credits = NZD $1.00</span>',
        '<span>200 credits = NZD $2.00</span>',
        '<span>500 credits = NZD $5.00</span>',
        '<span>1000 credits = NZD $10.00</span>',
        '<span>Normal generation: 1 credit</span>',
        '<span>Anima Double Sample: 2 credits</span>',
      ].join('');
      info.append(pricing);
    }
    box.append(info);
  } else {
    const info = document.createElement('div');
    info.className = 'payment-rules wechat-rules active-payment-rules';
    info.innerHTML = '<strong>支付方式：微信扫码</strong><p>请使用微信扫码支付。</p><p>付款完成后点击"我已付款"。</p><p>管理员确认到账后会为你加 credits。</p><p>1 元 = 100 credits</p><p>普通生成消耗 1 credit。Anima 双采样消耗 2 credits。</p>';
    box.append(info);
    const paymentNotice = renderWechatPaymentNotice(order);
    if (paymentNotice) box.append(paymentNotice);
  }

  if (order.status === 'created') {
    const btn = document.createElement('button');
    btn.className = 'primary';
    btn.type = 'button';
    btn.textContent = isAsb(order) ? 'I have paid' : '我已付款，提交审核';
    btn.onclick = async () => {
      btn.disabled = true;
      setMessage(isAsb(order) ? 'Submitting for admin review...' : t('topup.submitting', '正在提交审核…'));
      try {
        const data = await api(`/api/topups/${order.code}/paid`, {method: 'POST'});
        setMessage(data.message || (isAsb(order) ? 'Submitted for admin review.' : t('topup.submitted', '已提交给群主审核。')), 'ok');
        renderActive(data.item);
        await loadTopups();
      } catch (e) {
        setMessage(friendlyError(e.message), 'error');
      } finally {
        btn.disabled = false;
      }
    };
    box.append(btn);
  }
}

function renderList(items) {
  const list = $('topupList');
  list.replaceChildren();
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'muted';
    empty.textContent = t('topup.no_records', '暂无充值记录。');
    list.append(empty);
    return;
  }
  for (const item of items) {
    const row = document.createElement('div');
    row.className = 'topup-row';
    const left = document.createElement('div');
    const code = document.createElement('div');
    code.className = 'topup-code small';
    code.textContent = item.code;
    const time = document.createElement('div');
    time.className = 'muted';
    time.textContent = new Date(item.created_at * 1000).toLocaleString();
    left.append(code, time);
    const right = document.createElement('div');
    right.className = 'topup-row-right';
    const amount = document.createElement('strong');
    amount.textContent = item.amount_text || money(item.amount_fen);
    const status = document.createElement('span');
    status.className = `topup-status ${item.status}`;
    status.textContent = statusText(item.status, item.payment_method);
    right.append(amount, status);
    row.append(left, right);
    list.append(row);
  }
}

async function loadTopups() {
  try {
    const data = await api('/api/topups');
    topupItems = data.items || [];
    renderList(topupItems);
    renderActive(currentActiveOrder(topupItems));
  } catch (e) {
    setMessage(friendlyError(e.message), 'error');
  }
}

async function init() {
  try {
    me = await api('/api/me');
    $('userName').textContent = me.username;
    $('balance').textContent = creditsBalance(me.balance_fen);
    maybeShowTopupTimeNotice();
    await loadTopups();
  } catch (e) {
    if (/401|登录/.test(e.message)) window.location.href = '/login';
    else setMessage(friendlyError(e.message), 'error');
  }
}

// ── Form submit handler (event delegation on form) ──
const topupForm = $('topupForm');
if (topupForm) {
  topupForm.addEventListener('submit', async (e) => {
    e.preventDefault();

    // Step 1: choose payment method → advance to detail panel
    if (paymentStep === 'choose') {
      paymentStep = stepForMethod(selectedPaymentMethod());
      renderMethodPanels();
      return;
    }

    // Step 2: create order
    if (_createLock) return;
    _createLock = true;

    const method = stepMethod();
    const amount = $('amountInput')?.value.trim() || '';
    const credits = Number($('asbCredits')?.value || 100);
    const btn = $('createTopupBtn');
    if (btn) btn.disabled = true;

    setMessage(method === PAYMENT_ASB ? 'Creating ASB top-up order...' : t('topup.creating', '正在生成充值单…'));

    try {
      const payload = method === PAYMENT_ASB
        ? {payment_method: PAYMENT_ASB, credits}
        : {payment_method: PAYMENT_WECHAT, amount_rmb: amount};
      const data = await api('/api/topups', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });

      if (data.status === 'existing_pending') {
        // Backend returned an existing pending order
        setMessage(data.message || t('topup.existing_pending', '你有一笔待处理充值订单。'), 'ok');
        paymentStep = stepForMethod(data.item.payment_method);
        renderActive(data.item);
        await loadTopups();
      } else {
        // New order created
        paymentStep = stepForMethod(data.item.payment_method);
        setMessage(
          method === PAYMENT_ASB
            ? `ASB top-up order created: ${data.item.code}`
            : t('topup.created', `充值单已生成：${data.item.code}`, {code: data.item.code}),
          'ok',
        );
        renderActive(data.item);
        await loadTopups();
      }
    } catch (e) {
      setMessage(friendlyError(e.message), 'error');
    } finally {
      _createLock = false;
      if (btn) btn.disabled = false;
    }
  });
}

// ── Cancel existing order ──
async function cancelAndRecreate(orderCode, method) {
  if (!confirm(t('topup.cancel_confirm', '确定要取消该订单并重新创建吗？'))) return;
  try {
    const res = await api(`/api/topups/${orderCode}/cancel`, {method: 'POST'});
    setMessage(res.message || '订单已取消。', 'ok');
    // Reset to choose step so user can create new order
    paymentStep = 'choose';
    renderMethodPanels();
    await loadTopups();
  } catch (e) {
    setMessage(friendlyError(e.message), 'error');
  }
}

// ── Back buttons ──
function backToPaymentMethods() {
  paymentStep = 'choose';
  renderMethodPanels();
}

$('wechatBackBtn')?.addEventListener('click', backToPaymentMethods);
$('asbBackBtn')?.addEventListener('click', backToPaymentMethods);

// ── Time notice modal ──
$('topupTimeNoticeClose')?.addEventListener('click', closeTopupTimeNotice);
$('topupTimeNoticeOk')?.addEventListener('click', closeTopupTimeNotice);

$('paymentQr')?.addEventListener('error', () => {
  const box = $('paymentQr')?.parentElement;
  if (!box) return;
  box.replaceChildren();
  const msg = document.createElement('div');
  msg.className = 'muted';
  msg.textContent = t('topup.qr_missing', '收款码暂未配置。');
  box.append(msg);
});

document.querySelectorAll('input[name="payment_method"]').forEach((input) => {
  input.addEventListener('change', () => {
    if (paymentStep === 'choose') setMessage('');
  });
});

// ── Init ──
renderMethodPanels();
init();

// ── Lang change ──
window.addEventListener('uma:langchange', async () => {
  window.UmaI18n?.apply(document);
  renderMethodPanels();
  if (!$('topupTimeNotice')?.classList.contains('hidden')) {
    const key = topupNoticeStorageKey(me);
    topupNoticeStorage().removeItem(key);
    maybeShowTopupTimeNotice();
  }
  try { await loadTopups(); } catch (_) {}
});

// ── Test export ──
window.UmaTopupNoticeTest = {
  topupNoticeWindow,
  formatBeijingTime,
  zonedParts,
  zonedLocalToDate,
};

// ── Menu toggle ──
$('menuBtn')?.addEventListener('click', (event) => {
  event.stopPropagation();
  const menu = document.getElementById('navMenu');
  const btn = document.getElementById('menuBtn');
  if (!menu || !btn) return;
  const willOpen = menu.classList.contains('hidden');
  menu.classList.toggle('hidden', !willOpen);
  btn.setAttribute('aria-expanded', String(willOpen));
});
document.addEventListener('click', (event) => {
  const menu = document.getElementById('navMenu');
  const btn = document.getElementById('menuBtn');
  if (!menu || !btn || menu.classList.contains('hidden')) return;
  if (event.target.closest('#navMenu') || event.target.closest('#menuBtn')) return;
  menu.classList.add('hidden');
  btn.setAttribute('aria-expanded', 'false');
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const menu = document.getElementById('navMenu');
    const btn = document.getElementById('menuBtn');
    if (menu && !menu.classList.contains('hidden')) {
      menu.classList.add('hidden');
      if (btn) btn.setAttribute('aria-expanded', 'false');
    }
  }
});
