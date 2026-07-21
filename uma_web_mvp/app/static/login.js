// ============================================================
// Auth-compatible login.js — progressive enhancement
// Version: auth-android-webview-fallback1
//
// 设计原则：
// 1. 认证导航使用真实 <a href> 链接，JS 禁用时仍可工作
// 2. JS 正常时拦截链接实现原地切换，不刷新页面
// 3. 认证初始化独立在最前，不受其他模块异常影响
// 4. 兼容旧 Android WebView：不使用 optional chaining / ?? / top-level await
// 5. 安全日志：auth_navigation_loaded / initialized / view_selected / fallback_navigation
// ============================================================

var $ = function (id) { return document.getElementById(id); };

var emailTimer = null;
var resetTimer = null;
var emailMode = 'login';
var resetToken = '';
var EMAIL_RESEND_SECONDS = 45;
var EMAIL_FAILURE_RETRY_SECONDS = 15;
var LOGIN_FETCH_TIMEOUT_MS = 12000;
var SESSION_CONFIRM_TIMEOUT_MS = 5000;

function t(key, fallback, params) {
  var i18n = window.UmaI18n;
  return (i18n && i18n.t) ? i18n.t(key, fallback, params) : (fallback || key);
}

function translateMessage(text) {
  var i18n = window.UmaI18n;
  return (i18n && i18n.translateMessage) ? i18n.translateMessage(text) : text;
}

// =====================
// API helpers
// =====================

function apiRaw(url, options) {
  if (!options) options = {};
  var controller = new AbortController();
  var timer = setTimeout(function () { controller.abort(); }, LOGIN_FETCH_TIMEOUT_MS);
  options.credentials = 'same-origin';
  options.signal = controller.signal;
  try {
    return fetch(url, options).then(function (res) {
      var contentType = res.headers.get('Content-Type') || '';
      var dataPromise;
      if (contentType.indexOf('application/json') !== -1) {
        dataPromise = res.json().catch(function () { return null; });
      } else {
        dataPromise = res.text().catch(function () { return null; }).then(function (txt) { return {detail: txt}; });
      }
      return dataPromise.then(function (data) {
        if (!res.ok) {
          var fallback;
          if (res.status === 403) {
            fallback = t('common.security_expired', '页面安全验证已过期，请刷新后重试。');
          } else if (res.status === 409) {
            fallback = t('common.registered', '该邮箱已经注册，请使用邮箱登录。');
          } else if (res.status === 404) {
            fallback = t('common.not_registered', '该邮箱尚未注册，请先注册');
          } else if (res.status === 429) {
            fallback = t('common.too_many', '请求过于频繁，请稍后再试');
          } else if (res.status >= 500) {
            fallback = t('common.temp_send_error', '暂时无法发送验证码，请稍后重试');
          } else {
            fallback = t('common.invalid_code', '验证码无效或已过期');
          }
          const _msg = (typeof getApiErrorMessage === 'function')
      ? getApiErrorMessage(data, fallback)
      : ((data && data.detail) || fallback);
    throw new Error(translateMessage(_msg));
        }
        return data || {};
      });
    });
  } catch (err) {
    if (err && err.name === 'AbortError') {
      throw new Error(t('common.request_timeout', '请求超时，请稍后重试'));
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

function jsonPost(url, body) {
  return apiRaw(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
}

// =====================
// Safe redirect helper
// =====================

function getSafeInternalRedirect(redirectTo) {
  if (!redirectTo || typeof redirectTo !== 'string') return '/';
  if (redirectTo.charAt(0) === '/' && redirectTo.charAt(1) !== '/') {
    return redirectTo;
  }
  return '/';
}

// =====================
// Session confirmation
// =====================

function confirmSessionActive() {
  try {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, SESSION_CONFIRM_TIMEOUT_MS);
    return fetch('/api/me', {
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller.signal
    }).then(function (res) {
      clearTimeout(timer);
      return res.ok;
    }).catch(function () {
      return false;
    });
  } catch (_) {
    return Promise.resolve(false);
  }
}

// =====================
// Login success fallback UI
// =====================

function showLoginFallback(redirectTo) {
  var container = document.querySelector('.login-card');
  if (!container) {
    window.location.replace(redirectTo);
    return;
  }
  container.innerHTML = '<div style="text-align:center;padding:2.5rem 1.5rem;">'
    + '<p style="font-size:1.15rem;margin-bottom:1.8rem;color:var(--green,#16a34a);font-weight:600;">'
    + t('login.success', '登录成功！')
    + '</p>'
    + '<button id="loginFallbackBtn" class="primary" type="button" style="font-size:1rem;padding:0.75rem 2.5rem;border-radius:8px;cursor:pointer;">'
    + t('login.enter_home', '进入首页')
    + '</button></div>';
  var btn = document.getElementById('loginFallbackBtn');
  if (btn) {
    btn.addEventListener('click', function () {
      window.location.replace(redirectTo);
    });
  }
}

function handleEmailLoginSuccess(data) {
  var redirectTo = getSafeInternalRedirect((data && data.redirect) || '/');
  // 服务端已返回 200 + Set-Cookie = 登录成功，直接跳转
  // 不再依赖额外的 /api/me 验证（可能导致超时/fallback UI）
  window.location.replace(redirectTo);
}

// =====================
// Already-logged-in check
// =====================

function checkAlreadyLoggedIn() {
  // 防止重定向循环：如果刚刚从 login 跳转到 / 又被踢回 login，不再跳转
  if (sessionStorage.getItem('uma_login_redirect_guard')) {
    sessionStorage.removeItem('uma_login_redirect_guard');
    return Promise.resolve(false);
  }
  return fetch('/api/me', {
    credentials: 'same-origin',
    cache: 'no-store'
  }).then(function (res) {
    if (res.ok) {
      sessionStorage.setItem('uma_login_redirect_guard', '1');
      window.location.replace('/');
      return true;
    }
    return false;
  }).catch(function () {
    return false;
  });
}

// =====================
// UI helpers
// =====================

function setMessage(id, text, type) {
  var el = $(id);
  if (!el) return;
  el.textContent = translateMessage(text);
  el.className = 'message' + (type ? ' ' + type : '');
}

function setButton(btn, disabled, text) {
  if (!btn) return;
  btn.disabled = disabled;
  if (text !== undefined) btn.textContent = text;
}

function passwordLooksValid(password) {
  if (!password || password.length < 8 || password.length > 128 || !password.trim()) return false;
  var lowered = password.trim().toLowerCase();
  var weakPasswords = ['12345678', 'password', 'qwerty123', '11111111', '123456789', 'password123'];
  for (var i = 0; i < weakPasswords.length; i++) {
    if (lowered === weakPasswords[i]) return false;
  }
  var classes = 0;
  if (/[A-Za-z]/.test(password)) classes += 1;
  if (/\d/.test(password)) classes += 1;
  if (/[^\w\s]/.test(password)) classes += 1;
  return classes >= 2;
}

function setSendButton(disabled, text) {
  setButton($('sendCodeBtn'), disabled, text || t('login.send_code', '发送验证码'));
}

function resetEmailTimer() {
  if (emailTimer) {
    clearInterval(emailTimer);
    emailTimer = null;
  }
  setSendButton(false);
}

function startCountdown(timerName, buttonId, seconds, idleText) {
  if (timerName === 'reset' && resetTimer) clearInterval(resetTimer);
  if (timerName === 'email' && emailTimer) clearInterval(emailTimer);
  var remain = seconds;
  var btn = $(buttonId);
  var countdownText = t('login.resend', '重新发送') + '（' + remain + ' 秒）';
  setButton(btn, true, countdownText);
  var timer = setInterval(function () {
    remain -= 1;
    if (remain <= 0) {
      clearInterval(timer);
      if (timerName === 'reset') resetTimer = null;
      if (timerName === 'email') emailTimer = null;
      setButton(btn, false, idleText);
    } else {
      setButton(btn, true, t('login.resend', '重新发送') + '（' + remain + ' 秒）');
    }
  }, 1000);
  if (timerName === 'reset') resetTimer = timer;
  if (timerName === 'email') emailTimer = timer;
}

function setEmailMode(mode) {
  emailMode = mode;
  resetEmailTimer();
  var title = $('emailPanelTitle');
  if (title) {
    title.textContent = mode === 'register'
      ? t('login.register.title', '注册邮箱账户')
      : t('login.code.title', '邮箱验证码登录');
  }
  var verifyButton = $('verifyCodeBtn');
  if (verifyButton) {
    verifyButton.textContent = mode === 'register'
      ? t('login.register.button', '注册')
      : t('login.button', '登录');
  }
  var verifyStep = $('emailVerifyStep');
  var requestStep = $('emailRequestStep');
  var inviteField = $('inviteCodeField');
  if (inviteField) inviteField.classList.toggle('hidden', mode !== 'register');
  if (verifyStep) verifyStep.classList.add('hidden');
  if (requestStep) requestStep.classList.remove('hidden');
  setMessage('emailMessage', '');
  setMessage('verifyMessage', '');
  var codeInput = $('codeInput');
  if (codeInput) codeInput.value = '';
}

// ============================================================
// AUTH NAVIGATION — 独立安全入口，最先初始化
// ============================================================

var ALLOWED_AUTH_VIEWS = ['discord', 'email-otp', 'password', 'register', 'forgot-password'];
var DEFAULT_AUTH_VIEW = 'discord';

function isAllowedAuthView(view) {
  for (var i = 0; i < ALLOWED_AUTH_VIEWS.length; i++) {
    if (ALLOWED_AUTH_VIEWS[i] === view) return true;
  }
  return false;
}

function showAuthView(view) {
  // Safety log
  if (window.console && window.console.log) {
    window.console.log('auth_view_selected');
  }

  // Map view to internal tab name for tab highlighting
  var tabMap = {
    'discord': 'discord',
    'email-otp': 'email-otp',
    'password': 'password',
    'register': 'register',
    'forgot-password': 'password'
  };
  var internalTab = tabMap[view] || 'discord';

  // Update tab active states
  var tabs = document.querySelectorAll('.login-tab');
  for (var i = 0; i < tabs.length; i++) {
    var tabView = tabs[i].getAttribute('data-auth-view');
    if (tabView === internalTab) {
      tabs[i].classList.add('active');
    } else {
      tabs[i].classList.remove('active');
    }
  }

  // Show/hide panels
  var discordPanel = $('discordPanel');
  var passwordPanel = $('passwordPanel');
  var emailPanel = $('emailPanel');

  if (discordPanel) {
    if (view === 'discord') {
      discordPanel.classList.remove('hidden');
    } else {
      discordPanel.classList.add('hidden');
    }
  }
  if (passwordPanel) {
    if (view === 'password' || view === 'forgot-password') {
      passwordPanel.classList.remove('hidden');
    } else {
      passwordPanel.classList.add('hidden');
    }
  }
  if (emailPanel) {
    if (view === 'email-otp' || view === 'register') {
      emailPanel.classList.remove('hidden');
    } else {
      emailPanel.classList.add('hidden');
    }
  }

  // Email panel mode
  if (view === 'email-otp' || view === 'register') {
    setEmailMode(view === 'register' ? 'register' : 'login');
  }

  // Password panel sub-state
  if (view === 'password') {
    var loginStep = $('passwordLoginStep');
    var resetStep = $('passwordResetStep');
    if (loginStep) loginStep.classList.remove('hidden');
    if (resetStep) resetStep.classList.add('hidden');
    setMessage('passwordMessage', '');
    setMessage('resetMessage', '');
  }

  if (view === 'forgot-password') {
    resetToken = '';
    var loginStep2 = $('passwordLoginStep');
    var resetStep2 = $('passwordResetStep');
    if (loginStep2) loginStep2.classList.add('hidden');
    if (resetStep2) resetStep2.classList.remove('hidden');
    var resetFields = $('resetPasswordFields');
    if (resetFields) resetFields.classList.add('hidden');
    setMessage('resetMessage', '');
    var resetEmailInput = $('resetEmailInput');
    var passwordEmailInput = $('passwordEmailInput');
    if (resetEmailInput && passwordEmailInput) {
      resetEmailInput.value = passwordEmailInput.value.trim();
    }
  }

  // Update URL without navigation
  try {
    var url = new URL(window.location.href);
    url.searchParams.set('view', view);
    history.replaceState(null, '', url.pathname + url.search);
  } catch (_) {
    // URL update is best-effort, don't break switching
  }
}

function initAuthNavigation() {
  // Safety log
  if (window.console && window.console.log) {
    window.console.log('auth_navigation_loaded');
  }

  var authRoot = document.getElementById('authNav');
  if (!authRoot) {
    // Safety log: no auth navigation container found
    if (window.console && window.console.log) {
      window.console.log('auth_navigation_container_missing');
    }
    return;
  }

  // ── Read view from URL query parameter ──
  var view = DEFAULT_AUTH_VIEW;
  try {
    var params = new URLSearchParams(window.location.search);
    var rawView = params.get('view');
    if (rawView && isAllowedAuthView(rawView)) {
      view = rawView;
    }
  } catch (_) {
    // URLSearchParams not available — use default
  }

  // Show the correct panel
  showAuthView(view);

  // Safety log
  if (window.console && window.console.log) {
    window.console.log('auth_navigation_initialized');
  }

  // ── Click delegation: intercept auth links for in-place switching ──
  // Uses event delegation on a stable container (document.body or authRoot parent)
  // so that even if other JS fails, this handler is already registered.
  var delegateTarget = authRoot.parentNode || document.body;

  delegateTarget.addEventListener('click', function (event) {
    // Manual closest() — iterate up from target
    var el = event.target;
    while (el && el !== delegateTarget) {
      if (el.getAttribute && el.getAttribute('data-auth-view')) {
        break;
      }
      el = el.parentNode;
    }
    if (!el || !el.getAttribute) return;

    var linkView = el.getAttribute('data-auth-view');
    if (!linkView || !isAllowedAuthView(linkView)) return;

    // JS enabled — intercept and switch in-place
    event.preventDefault();
    showAuthView(linkView);
  });

  // Safety log
  if (window.console && window.console.log) {
    window.console.log('auth_navigation_click_delegation_bound');
  }
}

// ============================================================
// EMAIL CODE LOGIN — isolated binding
// ============================================================

function bindEmailCodeLogin() {
  var sendBtn = $('sendCodeBtn');
  var verifyBtn = $('verifyCodeBtn');
  var backBtn = $('backToEmailBtn');
  var codeInput = $('codeInput');
  var emailInput = $('emailInput');
  if (!sendBtn || !emailInput || !$('emailMessage')) return;

  emailInput.addEventListener('input', function () {
    resetEmailTimer();
    var ci = $('codeInput');
    if (ci) ci.value = '';
    setMessage('emailMessage', '');
    setMessage('verifyMessage', '');
  });

  sendBtn.addEventListener('click', function (event) {
    event.preventDefault();
    var email = emailInput.value.trim();
    if (!email) return setMessage('emailMessage', t('common.email_required', '请输入邮箱'), 'error');
    setSendButton(true, t('common.sending', '发送中'));
    setMessage('emailMessage', t('common.sending', '发送中'));
    jsonPost('/auth/email/' + emailMode + '/send-code', {email: email}).then(function (data) {
      setMessage('emailMessage', (data && data.message) || t('common.sent_code', '如果邮箱地址可用，验证码已经发送。'), 'ok');
      var hint = $('emailHint');
      if (hint) hint.textContent = t('common.code_sent_hint', '验证码已发送，请查看邮箱。');
      var reqStep = $('emailRequestStep');
      var verStep = $('emailVerifyStep');
      if (reqStep) reqStep.classList.add('hidden');
      if (verStep) verStep.classList.remove('hidden');
      startCountdown('email', 'sendCodeBtn', EMAIL_RESEND_SECONDS, t('login.send_code', '发送验证码'));
    }).catch(function (e) {
      setMessage('emailMessage', (e && e.message) || t('common.temp_send_error', '暂时无法发送验证码，请稍后重试'), 'error');
      startCountdown('email', 'sendCodeBtn', EMAIL_FAILURE_RETRY_SECONDS, t('login.send_code', '发送验证码'));
    });
  });

  if (verifyBtn) {
    verifyBtn.addEventListener('click', function (event) {
      event.preventDefault();
      var email = emailInput.value.trim();
      var codeInputEl = $('codeInput');
      var code = codeInputEl ? codeInputEl.value.trim() : '';
      var inviteCodeEl = $('inviteCodeInput');
      var inviteCode = inviteCodeEl ? inviteCodeEl.value.trim().toUpperCase() : '';
      if (!/^\d{6}$/.test(code)) return setMessage('verifyMessage', t('common.code_required', '请输入 6 位数字验证码'), 'error');
      var originalText = emailMode === 'register'
        ? t('login.register.button', '注册')
        : t('login.button', '登录');
      // 立即 disabled + 文案变化，防止重复点击
      verifyBtn.disabled = true;
      verifyBtn.textContent = emailMode === 'register'
        ? t('login.registering', '注册中...')
        : t('login.logging', '登录中...');
      setMessage('verifyMessage', verifyBtn.textContent);
      var payload = {email: email, code: code};
      if (emailMode === 'register' && inviteCode) payload.invite_code = inviteCode;
      jsonPost('/auth/email/' + emailMode + '/verify-code', payload).then(function (data) {
        // 成功跳转，无需恢复按钮
        handleEmailLoginSuccess(data);
      }).catch(function (e) {
        // 失败才恢复按钮
        setMessage('verifyMessage', (e && e.message) || t('common.temp_login_error', '暂时无法登录，请稍后再试'), 'error');
        verifyBtn.disabled = false;
        verifyBtn.textContent = originalText;
      });
    });
  }

  if (backBtn) {
    backBtn.addEventListener('click', function (event) {
      event.preventDefault();
      var verStep = $('emailVerifyStep');
      var reqStep = $('emailRequestStep');
      if (verStep) verStep.classList.add('hidden');
      if (reqStep) reqStep.classList.remove('hidden');
      setMessage('emailMessage', '');
      setMessage('verifyMessage', '');
    });
  }

  if (codeInput) {
    codeInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        if (verifyBtn) verifyBtn.click();
      }
    });
  }
}

// ============================================================
// PASSWORD LOGIN — isolated binding
// ============================================================

function bindPasswordLogin() {
  var loginBtn = $('passwordLoginBtn');
  var passwordInput = $('passwordInput');

  if (loginBtn) {
    loginBtn.addEventListener('click', function (event) {
      event.preventDefault();
      var emailInputEl = $('passwordEmailInput');
      var email = emailInputEl ? emailInputEl.value.trim() : '';
      var password = passwordInput ? passwordInput.value : '';
      if (!email) return setMessage('passwordMessage', t('common.email_required', '请输入邮箱'), 'error');
      if (!password) return setMessage('passwordMessage', t('common.password_required', '请输入密码'), 'error');
      // 立即 disabled + 文案变化，防止重复点击
      setButton(loginBtn, true, t('login.logging', '登录中...'));
      setMessage('passwordMessage', t('login.logging', '登录中...'));
      jsonPost('/auth/email/password/login', {email: email, password: password}).then(function (data) {
        // 成功跳转，无需恢复按钮
        handleEmailLoginSuccess(data);
      }).catch(function (e) {
        // 失败才恢复按钮
        setMessage('passwordMessage', (e && e.message) || t('common.temp_login_error', '暂时无法登录，请稍后再试'), 'error');
        setButton(loginBtn, false, t('login.button', '登录'));
      });
    });
  }

  if (passwordInput) {
    passwordInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        if (loginBtn) loginBtn.click();
      }
    });
  }

  // Note: forgotPassword + useCodeLogin are now <a> links handled by auth navigation delegation.
  // No separate click listeners needed.
}

// ============================================================
// PASSWORD RESET — isolated binding
// ============================================================

function bindPasswordReset() {
  var sendBtn = $('resetSendCodeBtn');
  var verifyBtn = $('resetVerifyCodeBtn');
  var completeBtn = $('resetCompleteBtn');

  if (sendBtn) {
    sendBtn.addEventListener('click', function (event) {
      event.preventDefault();
      var resetEmailEl = $('resetEmailInput');
      var email = resetEmailEl ? resetEmailEl.value.trim() : '';
      if (!email) return setMessage('resetMessage', t('common.email_required', '请输入邮箱'), 'error');
      setButton(sendBtn, true, t('common.sending', '发送中'));
      setMessage('resetMessage', t('common.sending', '发送中'));
      jsonPost('/auth/email/password/reset/send-code', {email: email}).then(function (data) {
        setMessage('resetMessage', (data && data.message) || t('common.sent_reset_code', '如果邮箱已注册，验证码已经发送。'), 'ok');
        startCountdown('reset', 'resetSendCodeBtn', EMAIL_RESEND_SECONDS, t('login.reset.send', '发送重置验证码'));
      }).catch(function (e) {
        setMessage('resetMessage', (e && e.message) || t('common.temp_send_error', '暂时无法发送验证码，请稍后重试'), 'error');
        startCountdown('reset', 'resetSendCodeBtn', EMAIL_FAILURE_RETRY_SECONDS, t('login.reset.send', '发送重置验证码'));
      });
    });
  }

  if (verifyBtn) {
    verifyBtn.addEventListener('click', function (event) {
      event.preventDefault();
      var resetEmailEl = $('resetEmailInput');
      var email = resetEmailEl ? resetEmailEl.value.trim() : '';
      var codeInputEl = $('resetCodeInput');
      var code = codeInputEl ? codeInputEl.value.trim() : '';
      if (!/^\d{6}$/.test(code)) return setMessage('resetMessage', t('common.code_required', '请输入 6 位数字验证码'), 'error');
      setButton(verifyBtn, true, t('common.verify_loading', '验证中...'));
      setMessage('resetMessage', t('common.verify_loading', '验证中...'));
      jsonPost('/auth/email/password/reset/verify-code', {email: email, code: code}).then(function (data) {
        resetToken = (data && data.reset_token) || '';
        var resetFields = $('resetPasswordFields');
        if (resetFields) resetFields.classList.remove('hidden');
        setMessage('resetMessage', t('common.reset_verified', '邮箱已验证，请设置新密码。'), 'ok');
      }).catch(function (e) {
        setMessage('resetMessage', (e && e.message) || t('common.invalid_code', '验证码无效或已过期'), 'error');
      }).then(function () {
        setButton(verifyBtn, false, t('login.reset.verify', '验证验证码'));
      });
    });
  }

  if (completeBtn) {
    completeBtn.addEventListener('click', function (event) {
      event.preventDefault();
      var newPassEl = $('resetNewPasswordInput');
      var confirmEl = $('resetConfirmPasswordInput');
      var password = newPassEl ? newPassEl.value : '';
      var confirm = confirmEl ? confirmEl.value : '';
      if (!passwordLooksValid(password)) return setMessage('resetMessage', t('common.password_rule'), 'error');
      if (password !== confirm) return setMessage('resetMessage', t('common.password_mismatch', '两次输入的密码不一致。'), 'error');
      if (!resetToken) return setMessage('resetMessage', t('common.reset_first', '请先验证邮箱验证码。'), 'error');
      setButton(completeBtn, true, t('login.resetting', '重置中...'));
      setMessage('resetMessage', t('login.resetting', '重置中...'));
      jsonPost('/auth/email/password/reset/complete', {
        reset_token: resetToken,
        password: password,
        confirm_password: confirm
      }).then(function (data) {
        resetToken = '';
        return handleEmailLoginSuccess(data);
      }).catch(function (e) {
        setMessage('resetMessage', (e && e.message) || t('common.reset_expired', '暂时无法重置密码，请稍后再试'), 'error');
        setButton(completeBtn, false, t('login.reset.complete', '重置密码并登录'));
      });
    });
  }

  // Note: backToPasswordLogin is now an <a> link handled by auth navigation delegation.
  // No separate click listener needed.
}

// ============================================================
// INIT — 分层初始化，认证导航最先执行
// ============================================================

window.addEventListener('uma:langchange', function () {
  setEmailMode(emailMode);
  var i18n = window.UmaI18n;
  if (i18n && i18n.apply) {
    i18n.apply(document);
  }
});

document.addEventListener('DOMContentLoaded', function () {
  // Step 0: Already logged in? Redirect immediately.
  checkAlreadyLoggedIn().then(function (alreadyLoggedIn) {
    if (alreadyLoggedIn) return;

    // i18n apply — safe, no DOM dependency
    try {
      var i18n = window.UmaI18n;
      if (i18n && i18n.apply) {
        i18n.apply(document);
      }
    } catch (e) {
      if (window.console && window.console.error) {
        window.console.error('i18n_init_failed');
      }
    }

    // ── Step 1: Auth navigation — MUST succeed independently ──
    try {
      initAuthNavigation();
    } catch (e) {
      // If auth navigation fails, fallback: rely on raw <a href> links
      if (window.console && window.console.error) {
        window.console.error('auth_navigation_init_failed, fallback to href links');
      }
      // Show default panel as best-effort
      try {
        var dp = $('discordPanel');
        if (dp) dp.classList.remove('hidden');
      } catch (_) {}
    }

    // ── Step 2+: Optional features — each in own try/catch ──

    try {
      bindEmailCodeLogin();
    } catch (e) {
      if (window.console && window.console.error) {
        window.console.error('email_code_login_init_failed');
      }
    }

    try {
      bindPasswordLogin();
    } catch (e) {
      if (window.console && window.console.error) {
        window.console.error('password_login_init_failed');
      }
    }

    try {
      bindPasswordReset();
    } catch (e) {
      if (window.console && window.console.error) {
        window.console.error('password_reset_init_failed');
      }
    }
  });
});
