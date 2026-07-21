const $ = (id) => document.getElementById(id);
let me = null;
const PASSWORD_RULE_MESSAGE = '密码至少 8 位，并包含字母、数字或符号中的至少两类。';

function t(key, fallback, params) {
  return window.UmaI18n?.t(key, fallback, params) ?? (fallback ?? key);
}

function translateMessage(text) {
  return window.UmaI18n?.translateMessage(text) ?? text;
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
    const _msg = (typeof getApiErrorMessage === 'function')
      ? getApiErrorMessage(data, `HTTP ${res.status}`)
      : (data?.detail || `HTTP ${res.status}`);
    const error = new Error(translateMessage(_msg));
    error.status = res.status;
    throw error;
  }
  return data;
}

function credits(fen){ return `${Math.trunc(Number(fen || 0))} credits`; }
function setMessage(elId, text, type=''){ const el=$(elId); if(!el) return; el.textContent=translateMessage(text); el.className=`message ${type}`; }

function passwordLooksValid(password) {
  if (!password || password.length < 8 || password.length > 128 || !password.trim()) return false;
  const lowered = password.trim().toLowerCase();
  if (['12345678', 'password', 'qwerty123', '11111111', '123456789', 'password123'].includes(lowered)) return false;
  let classes = 0;
  if (/[A-Za-z]/.test(password)) classes += 1;
  if (/\d/.test(password)) classes += 1;
  if (/[^\w\s]/.test(password)) classes += 1;
  return classes >= 2;
}

function goBackOrHome() {
  if (window.history.length > 1) {
    window.history.back();
  } else {
    window.location.href = '/';
  }
}

function renderPasswordPanel() {
  if (!me) return;
  if (me.provider !== 'email') {
    $('passwordPanel')?.classList.add('hidden');
    $('notEmailMessage')?.classList.remove('hidden');
    return;
  }
  $('passwordPanel')?.classList.remove('hidden');
  $('notEmailMessage')?.classList.add('hidden');
  const hasPassword = Boolean(me.has_email_password);
  $('passwordHint').textContent = hasPassword
    ? t('settings.change_hint', '修改密码需要输入当前密码。')
    : t('settings.set_hint', '已验证邮箱。设置密码后，下次可直接使用邮箱和密码登录。');
  $('oldPasswordWrap').classList.toggle('hidden', !hasPassword);
  $('passwordSaveBtn').textContent = hasPassword
    ? t('settings.change_password', '修改登录密码')
    : t('settings.set_password', '保存登录密码');
}

async function init() {
  try {
    me = await api('/api/me');
    $('userName').textContent = me.username;
    $('balance').textContent = credits(me.balance_fen);
    renderPasswordPanel();
  } catch(e) {
    if (e.message.includes('登录') || e.message.includes('401')) {
      window.location.href = '/login';
      return;
    }
    setMessage('passwordMessage', e.message, 'error');
  }
}

$('passwordSaveBtn')?.addEventListener('click', async (e) => {
  e.preventDefault();
  if (!me || me.provider !== 'email') return;
  const hasPassword = Boolean(me.has_email_password);
  const oldPassword = $('oldPassword').value;
  const password = $('newPassword').value;
  const confirm = $('confirmPassword').value;
  if (hasPassword && !oldPassword) return setMessage('passwordMessage', t('settings.old_required', '请输入当前密码。'), 'error');
  if (!passwordLooksValid(password)) return setMessage('passwordMessage', t('common.password_rule', PASSWORD_RULE_MESSAGE), 'error');
  if (password !== confirm) return setMessage('passwordMessage', t('common.password_mismatch', '两次输入的密码不一致。'), 'error');
  $('passwordSaveBtn').disabled = true;
  setMessage('passwordMessage', t('settings.saving', '保存中...'));
  try {
    await api('/auth/email/password/set', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        old_password: hasPassword ? oldPassword : null,
        password,
        confirm_password: confirm,
        revoke_other_sessions: $('revokeOtherSessions').checked,
      }),
    });
    $('oldPassword').value = '';
    $('newPassword').value = '';
    $('confirmPassword').value = '';
    me.has_email_password = true;
    renderPasswordPanel();
    setMessage('passwordMessage', t('settings.password_saved', '登录密码已保存 ✓'), 'ok');
  } catch(err) {
    setMessage('passwordMessage', err.message || t('settings.password_save_failed', '暂时无法保存密码，请稍后再试'), 'error');
  } finally {
    $('passwordSaveBtn').disabled = false;
  }
});

// Menu toggle
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

window.addEventListener('uma:langchange', () => {
  window.UmaI18n?.apply(document);
  renderPasswordPanel();
});

init();
