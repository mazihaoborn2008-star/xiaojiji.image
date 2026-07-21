(() => {
  const $ = (id) => document.getElementById(id);
  let profile = null;

  function t(key, fallback, params) {
    return window.UmaI18n?.t ? window.UmaI18n.t(key, fallback, params) : (fallback || key);
  }

  function csrfHeaders() {
    const match = document.cookie.match(/(?:^|;\s*)uma_csrf=([^;]+)/);
    return match ? {'X-CSRF-Token': decodeURIComponent(match[1])} : {};
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      credentials: 'same-origin',
      cache: 'no-store',
      ...options,
      headers: {
        ...(options.body ? {'Content-Type': 'application/json'} : {}),
        ...(options.method && options.method !== 'GET' ? csrfHeaders() : {}),
        ...(options.headers || {}),
      },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const _msg = (typeof getApiErrorMessage === 'function')
        ? getApiErrorMessage(data, "暂时无法登录，请稍后再试")
        : (data.detail || "暂时无法登录，请稍后再试");
      throw new Error(_msg);
    }
    return data;
  }

  function credits(value) {
    return `${Math.max(0, Number(value || 0))} credits`;
  }

  function setMessage(id, text, kind = '') {
    const el = $(id);
    if (!el) return;
    el.textContent = text || '';
    el.className = `message ${kind}`.trim();
  }

  function render() {
    if (!profile) return;
    const code = profile.referral_code || '';
    $('referralCode').textContent = code || '-';
    $('copyReferralBtn').disabled = !code;
    $('claimReferralBtn').classList.toggle('hidden', Boolean(code) || !profile.referral_campaign_enabled);
    $('referralStatus').textContent = profile.referral_campaign_enabled
      ? ''
      : t('profile.referral_closed', '邀请活动暂未开放。');
    $('displayUsernameInput').value = profile.display_username || '';
    $('profileDisplayName').textContent = profile.display_label || profile.display_name || '-';
    $('profileProvider').textContent = profile.provider === 'email' ? 'Email' : 'Discord';
    $('profileBalance').textContent = credits(profile.balance_fen);
    const stats = profile.referral_stats || {};
    $('referralCount').textContent = t('profile.invited_count', '已邀请 {count} 人', {count: Number(stats.invited_count || 0)});
    $('referralReward').textContent = t('profile.reward_total', '累计获得 {credits} credits', {credits: Number(stats.inviter_reward_credits || 0)});
    window.UmaI18n?.apply(document);
  }

  async function loadProfile() {
    profile = await api('/api/profile');
    render();
  }

  async function copyText(text) {
    if (!text || text === '-') {
      setMessage('referralStatus', t('profile.claim_first', '请先领取邀请码'), 'error');
      return;
    }
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const input = document.createElement('textarea');
        input.value = text;
        input.setAttribute('readonly', 'readonly');
        input.style.position = 'fixed';
        input.style.left = '-9999px';
        document.body.appendChild(input);
        input.select();
        document.execCommand('copy');
        input.remove();
      }
      setMessage('referralStatus', t('profile.copied', '邀请码已复制'), 'ok');
    } catch (_) {
      setMessage('referralStatus', t('profile.copy_failed', '复制失败，请长按邀请码手动复制。'), 'error');
    }
  }

  $('claimReferralBtn')?.addEventListener('click', async () => {
    const btn = $('claimReferralBtn');
    if (btn.disabled) return;
    btn.disabled = true;
    btn.textContent = t('profile.generating', '正在生成……');
    try {
      const data = await api('/api/profile/referral-code', {method: 'POST', body: '{}'});
      profile.referral_code = data.referral_code;
      profile.referral_code_created_at = data.created_at;
      render();
      setMessage('referralStatus', t('profile.code_generated', '邀请码已生成'), 'ok');
    } catch (err) {
      const msg = err.message || '';
      if (msg.includes('登录') || msg.includes('401') || msg.includes('Unauthorized')) {
        setMessage('referralStatus', t('profile.session_expired', '登录状态已失效，请重新登录'), 'error');
      } else if (msg.includes('验证') || msg.includes('CSRF') || msg.includes('403')) {
        setMessage('referralStatus', t('profile.csrf_failed', '请求验证失败，请刷新页面后重试'), 'error');
      } else if (msg.includes('未开放')) {
        setMessage('referralStatus', t('profile.campaign_closed', '邀请活动当前未开放'), 'error');
      } else {
        setMessage('referralStatus', t('profile.generate_failed', '邀请码生成失败，请稍后重试'), 'error');
      }
    } finally {
      btn.disabled = false;
      btn.textContent = t('profile.claim_code', '领取邀请码');
    }
  });

  $('copyReferralBtn')?.addEventListener('click', () => copyText(profile?.referral_code || ''));

  $('saveUsernameBtn')?.addEventListener('click', async () => {
    const btn = $('saveUsernameBtn');
    const value = $('displayUsernameInput').value.trim();
    btn.disabled = true;
    setMessage('usernameMessage', '');
    try {
      const data = await api('/api/profile/username', {
        method: 'POST',
        body: JSON.stringify({display_username: value}),
      });
      profile.display_username = data.display_username;
      profile.display_label = data.display_username;
      setMessage('usernameMessage', t('profile.saved', '已保存。'), 'ok');
      render();
    } catch (err) {
      setMessage('usernameMessage', err.message, 'error');
    } finally {
      btn.disabled = false;
    }
  });

  window.addEventListener('uma:langchange', render);
  loadProfile().catch((err) => setMessage('usernameMessage', err.message, 'error'));
})();
