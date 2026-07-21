let me = null;
let threads = [];
let activeThreadCode = null;

function $(id) { return document.getElementById(id); }
function t(key, fallback, params) { return window.UmaI18n?.t(key, fallback, params) ?? (fallback ?? key); }
function translateMessage(text) { return window.UmaI18n?.translateMessage(text) ?? text; }
function credits(fen) { return `${Math.trunc(Number(fen || 0))} credits`; }
function getCookie(name) {
  const prefix = `${name}=`;
  return document.cookie.split(';').map((part) => part.trim()).find((part) => part.startsWith(prefix))?.slice(prefix.length) || '';
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
  if (!res.ok) const _msg = (typeof getApiErrorMessage === 'function')
      ? getApiErrorMessage(data, `HTTP ${res.status}`)
      : (data?.detail || `HTTP ${res.status}`);
    throw new Error(translateMessage(_msg));
  return data;
}
function setStatus(text, type = '') {
  const el = $('adminSupportMessage');
  if (!el) return;
  el.textContent = text ? translateMessage(text) : '';
  el.className = `message ${type}`;
}
function formatTime(sec) { return sec ? new Date(Number(sec) * 1000).toLocaleString() : ''; }
function displayThread(thread) {
  const name = thread.display_name || thread.account_id;
  return `${thread.subject || thread.thread_code} · ${name}`;
}
function renderThreads() {
  const list = $('adminThreadList');
  list.replaceChildren();
  if (!threads.length) {
    const empty = document.createElement('div');
    empty.className = 'muted support-empty';
    empty.textContent = t('admin_support.empty', '暂无会话。');
    list.append(empty);
    return;
  }
  for (const thread of threads) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = `support-thread-card ${thread.thread_code === activeThreadCode ? 'active' : ''}`;
    const title = document.createElement('strong');
    title.textContent = displayThread(thread);
    const meta = document.createElement('span');
    meta.textContent = `${thread.category} · ${thread.status} · ${thread.balance_fen} credits · ${formatTime(thread.updated_at)}`;
    const flags = document.createElement('span');
    flags.className = 'support-thread-flags';
    if (thread.unread_admin_count > 0) {
      const unread = document.createElement('em');
      unread.className = 'unread';
      unread.textContent = `${thread.unread_admin_count}`;
      flags.append(unread);
    }
    if (thread.priority !== 'normal') {
      const priority = document.createElement('em');
      priority.textContent = thread.priority;
      flags.append(priority);
    }
    card.append(title, meta, flags);
    card.addEventListener('click', () => selectThread(thread.thread_code));
    list.append(card);
  }
}
function renderAccounts(items) {
  const list = $('accountPickerList');
  list.replaceChildren();
  const filter = $('accountPickerFilter').value.trim().toLowerCase();
  const filtered = (items || []).filter((item) => {
    const haystack = `${item.account_id} ${item.legacy_user_id || ''} ${item.display_name || ''} ${item.display_username || ''} ${item.referral_code || ''} ${item.email_masked || ''} ${item.provider || ''}`.toLowerCase();
    return !filter || haystack.includes(filter);
  }).slice(0, 40);
  if (!filtered.length) {
    const empty = document.createElement('div');
    empty.className = 'muted support-empty';
    empty.textContent = t('admin_support.no_accounts', '没有匹配账号。');
    list.append(empty);
    return;
  }
  for (const account of filtered) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'support-thread-card';
    const title = document.createElement('strong');
    title.textContent = account.display_name || account.email_masked || account.account_id;
    const meta = document.createElement('span');
    meta.textContent = `${account.provider} · ${account.balance_fen} credits · username: ${account.display_username || '未设置'}`;
    const id = document.createElement('span');
    id.textContent = `account_id: ${account.account_id}`;
    const referral = document.createElement('span');
    referral.textContent = `invite: ${account.referral_code || '-'} · referrals: ${account.referral_count || 0} · reward: ${account.referral_reward_credits || 0} credits`;
    card.append(title, meta, id, referral);
    card.addEventListener('click', () => {
      $('targetAccountId').value = account.account_id;
      $('accountSearch').value = account.account_id;
      loadThreads(true);
    });
    list.append(card);
  }
}
async function loadAccounts() {
  const params = new URLSearchParams({limit: '100'});
  const query = $('accountPickerFilter').value.trim();
  if (query) params.set('query', query);
  const data = await api(`/api/admin/accounts?${params.toString()}`);
  renderAccounts(data.items || []);
}
function renderDetail(thread, messages) {
  activeThreadCode = thread.thread_code;
  renderThreads();
  const pane = $('adminThreadDetail');
  pane.replaceChildren();
  const header = document.createElement('div');
  header.className = 'support-detail-header';
  const title = document.createElement('h2');
  title.textContent = thread.subject || thread.thread_code;
  const meta = document.createElement('p');
  meta.className = 'muted';
  meta.textContent = `${thread.thread_code} · ${thread.display_name || ''} · ${thread.provider || ''} · ${thread.status}`;
  header.append(title, meta);
  const list = document.createElement('div');
  list.className = 'support-message-list admin-message-list';
  for (const msg of messages) {
    const item = document.createElement('div');
    item.className = `support-message ${msg.sender_type === 'admin' ? 'mine' : 'theirs'}`;
    const body = document.createElement('div');
    body.className = 'support-message-body';
    body.textContent = msg.body;
    const m = document.createElement('div');
    m.className = 'support-message-meta';
    m.textContent = `${msg.sender_type} · ${formatTime(msg.created_at)} · user read: ${msg.read_by_user_at ? formatTime(msg.read_by_user_at) : '-'}`;
    item.append(body, m);
    list.append(item);
  }
  const form = document.createElement('form');
  form.className = 'support-reply';
  const input = document.createElement('textarea');
  input.maxLength = 2000;
  input.rows = 3;
  input.placeholder = t('admin_support.reply_placeholder', '回复用户...');
  const send = document.createElement('button');
  send.className = 'primary';
  send.type = 'submit';
  send.textContent = t('admin_support.send_reply', '发送回复');
  form.append(input, send);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    send.disabled = true;
    try {
      await api(`/api/admin/support/threads/${encodeURIComponent(thread.thread_code)}/messages`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: text}),
      });
      await selectThread(thread.thread_code);
    } catch (e) {
      setStatus(e.message, 'error');
    } finally {
      send.disabled = false;
    }
  });
  const actions = document.createElement('div');
  actions.className = 'dialog-actions support-admin-actions';
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'ghost';
  toggle.textContent = thread.status === 'open' ? t('admin_support.close', '关闭会话') : t('admin_support.reopen', '重新打开');
  toggle.addEventListener('click', async () => {
    try {
      const action = thread.status === 'open' ? 'close' : 'reopen';
      await api(`/api/admin/support/threads/${encodeURIComponent(thread.thread_code)}/${action}`, {method: 'POST'});
      await selectThread(thread.thread_code);
    } catch (e) {
      setStatus(e.message, 'error');
    }
  });
  actions.append(toggle);
  pane.append(header, list, form, actions);
  list.scrollTop = list.scrollHeight;
}
async function selectThread(code) {
  const data = await api(`/api/admin/support/threads/${encodeURIComponent(code)}`);
  renderDetail(data.thread, data.messages || []);
  await loadThreads(false);
}
async function loadThreads(clearActive = false) {
  const params = new URLSearchParams();
  const account = $('accountSearch').value.trim();
  if (account) params.set('account_id', account);
  const status = $('statusFilter').value;
  if (status) params.set('status', status);
  if ($('unreadOnly').checked) params.set('unread_only', 'true');
  const data = await api(`/api/admin/support/threads?${params.toString()}`);
  threads = data.items || [];
  if (clearActive) activeThreadCode = null;
  renderThreads();
}
function applyQueryPrefill() {
  const q = new URLSearchParams(window.location.search);
  if (q.get('account_id')) $('targetAccountId').value = q.get('account_id');
  if (q.get('feedback_id')) {
    $('relatedFeedbackId').value = q.get('feedback_id');
    $('threadCategory').value = 'feedback';
    $('threadSubject').value = t('admin_support.feedback_subject', '关于你的反馈');
  }
  if (q.get('topup_code')) {
    $('relatedTopupCode').value = q.get('topup_code');
    $('threadCategory').value = 'payment';
    $('threadSubject').value = t('admin_support.payment_subject', '关于你的充值订单');
  }
}
async function init() {
  try {
    me = await api('/api/me');
    if (!me.is_admin) throw new Error('Not found');
    $('userName').textContent = me.username || '';
    $('balance').textContent = credits(me.balance_fen);
    applyQueryPrefill();
    await loadAccounts();
    await loadThreads(false);
  } catch (e) {
    if (e.message.includes('401') || e.message.includes('登录')) window.location.href = '/login';
    else setStatus(e.message, 'error');
  }
}
$('createThreadForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('createThreadBtn').disabled = true;
  try {
    const feedbackRaw = $('relatedFeedbackId').value.trim();
    const payload = {
      account_id: $('targetAccountId').value.trim(),
      category: $('threadCategory').value,
      priority: $('threadPriority').value,
      subject: $('threadSubject').value.trim(),
      message: $('threadMessage').value.trim(),
      related_feedback_id: feedbackRaw ? Number(feedbackRaw) : null,
      related_topup_code: $('relatedTopupCode').value.trim() || null,
    };
    const data = await api('/api/admin/support/threads', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    setStatus(t('admin_support.sent', '已发送。'), 'ok');
    $('threadMessage').value = '';
    await loadThreads(false);
    if (data.thread?.thread_code) await selectThread(data.thread.thread_code);
  } catch (e) {
    setStatus(e.message, 'error');
  } finally {
    $('createThreadBtn').disabled = false;
  }
});
$('refreshThreadsBtn').addEventListener('click', () => loadThreads(false));
$('loadAccountsBtn').addEventListener('click', loadAccounts);
$('accountPickerFilter').addEventListener('input', loadAccounts);
$('statusFilter').addEventListener('change', () => loadThreads(true));
$('unreadOnly').addEventListener('change', () => loadThreads(true));
$('accountSearch').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') loadThreads(true);
});
document.querySelector('[data-back-home]')?.addEventListener('click', () => { window.location.href = '/'; });
window.addEventListener('DOMContentLoaded', init);
