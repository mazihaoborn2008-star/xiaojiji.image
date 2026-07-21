const $ = (id) => document.getElementById(id);
let me = null;

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

function goBackOrHome() {
  if (window.history.length > 1) {
    window.history.back();
  } else {
    window.location.href = '/';
  }
}

// Hash redirect compatibility
(function handleHashRedirect() {
  const hash = window.location.hash;
  if (hash === '#character-tags') {
    window.location.replace('/character-tags');
    return;
  }
  if (hash === '#feedback') {
    window.location.replace('/feedback');
    return;
  }
  if (hash === '#change-password') {
    window.location.replace('/change-password');
    return;
  }
})();

async function init() {
  try {
    me = await api('/api/me');
    $('userName').textContent = me.username;
    $('balance').textContent = credits(me.balance_fen);
    window.UmaI18n?.apply(document);
  } catch(e) {
    if (e.message.includes('登录') || e.message.includes('401')) {
      window.location.href = '/login';
      return;
    }
    setMessage('message', e.message, 'error');
  }
}

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
});

init();
