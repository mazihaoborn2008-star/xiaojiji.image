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
  const el = $('supportMessage');
  if (!el) return;
  el.textContent = text ? translateMessage(text) : '';
  el.className = `message ${type}`;
}
function formatTime(sec) {
  if (!sec) return '';
  return new Date(Number(sec) * 1000).toLocaleString();
}
function labelForThread(thread) {
  return thread.subject || `${thread.category} · ${thread.thread_code}`;
}
function renderThreads() {
  const list = $('threadList');
  list.replaceChildren();
  $('unreadBadge').classList.toggle('hidden', !threads.some((item) => item.unread_user_count > 0));
  $('unreadBadge').textContent = String(threads.reduce((sum, item) => sum + Number(item.unread_user_count || 0), 0));
  if (!threads.length) {
    const empty = document.createElement('div');
    empty.className = 'muted support-empty';
    empty.textContent = t('messages.empty', '暂无消息。');
    list.append(empty);
    return;
  }
  for (const thread of threads) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = `support-thread-card ${thread.thread_code === activeThreadCode ? 'active' : ''}`;
    const title = document.createElement('strong');
    title.textContent = labelForThread(thread);
    const meta = document.createElement('span');
    meta.textContent = `${thread.category} · ${thread.status} · ${formatTime(thread.updated_at)}`;
    const flags = document.createElement('span');
    flags.className = 'support-thread-flags';
    if (thread.priority !== 'normal') {
      const priority = document.createElement('em');
      priority.textContent = thread.priority;
      flags.append(priority);
    }
    if (thread.unread_user_count > 0) {
      const unread = document.createElement('em');
      unread.className = 'unread';
      unread.textContent = `${thread.unread_user_count}`;
      flags.append(unread);
    }
    card.append(title, meta, flags);
    card.addEventListener('click', () => selectThread(thread.thread_code));
    list.append(card);
  }
}
function renderMessages(thread, messages) {
  activeThreadCode = thread.thread_code;
  renderThreads();
  $('threadMeta').textContent = `${labelForThread(thread)} · ${thread.category} · ${thread.status}`;
  const list = $('messageList');
  list.replaceChildren();
  for (const msg of messages) {
    const item = document.createElement('div');
    item.className = `support-message ${msg.sender_type === 'user' ? 'mine' : 'theirs'}`;
    const body = document.createElement('div');
    body.className = 'support-message-body';
    body.textContent = msg.body;
    const meta = document.createElement('div');
    meta.className = 'support-message-meta';
    meta.textContent = `${msg.sender_type === 'user' ? t('messages.me', '我') : t('messages.admin', '管理员')} · ${formatTime(msg.created_at)}`;
    item.append(body, meta);
    list.append(item);
  }
  $('replyForm').classList.toggle('hidden', thread.status !== 'open');
  $('closedNotice').classList.toggle('hidden', thread.status === 'open');
  $('threadListPane').classList.add('mobile-hidden');
  $('threadDetailPane').classList.add('mobile-active');
  list.scrollTop = list.scrollHeight;
}
async function selectThread(code) {
  try {
    setStatus('');
    const data = await api(`/api/support/threads/${encodeURIComponent(code)}`);
    renderMessages(data.thread, data.messages || []);
    await api(`/api/support/threads/${encodeURIComponent(code)}/read`, {method: 'POST'});
    await loadThreads(false);
  } catch (e) {
    setStatus(e.message, 'error');
  }
}
async function loadThreads(autoSelect = true) {
  const data = await api('/api/support/threads');
  threads = data.items || [];
  renderThreads();
  if (autoSelect && !activeThreadCode && threads[0]) await selectThread(threads[0].thread_code);
}
async function init() {
  try {
    me = await api('/api/me');
    $('userName').textContent = me.username || '';
    $('balance').textContent = credits(me.balance_fen);
    const requested = new URLSearchParams(window.location.search).get('thread');
    await loadThreads(!requested);
    if (requested) await selectThread(requested);
  } catch (e) {
    if (e.message.includes('401') || e.message.includes('登录')) window.location.href = '/login';
    else setStatus(e.message, 'error');
  }
}

$('replyForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const text = $('replyInput').value.trim();
  if (!text || !activeThreadCode) return;
  $('replyBtn').disabled = true;
  try {
    await api(`/api/support/threads/${encodeURIComponent(activeThreadCode)}/messages`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text}),
    });
    $('replyInput').value = '';
    await selectThread(activeThreadCode);
  } catch (e) {
    setStatus(e.message, 'error');
  } finally {
    $('replyBtn').disabled = false;
  }
});

$('backToThreadsBtn').addEventListener('click', () => {
  $('threadListPane').classList.remove('mobile-hidden');
  $('threadDetailPane').classList.remove('mobile-active');
});
document.querySelector('[data-back-home]')?.addEventListener('click', () => { window.location.href = '/'; });
window.addEventListener('DOMContentLoaded', init);
