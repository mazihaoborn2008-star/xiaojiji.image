const $ = (id) => document.getElementById(id);
let me = null;
let currentReviewCode = null;

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
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
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

function credits(value) {
  return `${Math.trunc(Number(value || 0))} credits`;
}

function setMessage(text, type = '') {
  const el = $('adminMessage');
  if (!el) return;
  el.textContent = translateMessage(text);
  el.className = `message ${type}`;
}

function node(tag, className = '', text = '') {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text) el.textContent = text;
  return el;
}

function openPreview(src, alt) {
  const modal = $('refundPreviewModal');
  const img = $('refundPreviewImage');
  if (!modal || !img || !src) return;
  img.src = src;
  img.alt = alt || '';
  modal.classList.remove('hidden');
  document.body.classList.add('modal-open');
}

function closePreview() {
  const modal = $('refundPreviewModal');
  const img = $('refundPreviewImage');
  if (img) img.removeAttribute('src');
  modal?.classList.add('hidden');
  document.body.classList.remove('modal-open');
}

function statusLabel(status) {
  return t(`refund.status.${status || 'pending'}`, status || '');
}

function safeJson(value) {
  try { return JSON.parse(value || '{}'); } catch (_) { return {}; }
}

function isRefundFinal(status) {
  return ['refunded', 'refund_completed'].includes(String(status || ''));
}

function isRejectFinal(status) {
  return ['manual_rejected', 'refund_rejected'].includes(String(status || ''));
}

function processingOnly(status) {
  return String(status || '') === 'refund_pending';
}

function reviewMethod(item) {
  const reason = String(item.public_reason || '');
  if (reason.includes('管理员')) return '管理员人工批准';
  if (item.decision) return '自动审核结果';
  return '-';
}

function renderList(items) {
  const list = $('adminRefundList');
  list.replaceChildren();
  if (!items.length) {
    list.append(node('p', 'muted', t('admin_refund.empty', '暂无审核记录。')));
    return;
  }
  items.forEach((item) => {
    const card = node('button', 'refund-task-card', '');
    card.type = 'button';
    if (item.review_code === currentReviewCode) card.classList.add('active');
    card.append(
      node('strong', '', `${item.review_code || ''}`),
      node('span', 'muted', `${item.job_code || ''} · ${statusLabel(item.status)}`),
      node('span', 'muted', `account ${item.account_id || ''}`),
      node('span', 'muted', credits(item.charged_credits)),
      node('span', 'muted', item.created_at ? new Date(Number(item.created_at) * 1000).toLocaleString() : '')
    );
    card.addEventListener('click', () => loadDetail(item.review_code));
    list.append(card);
  });
}

function renderAdminOutputs(parent, item) {
  const grid = node('div', 'refund-output-grid');
  let outputIds = [];
  try { outputIds = JSON.parse(item.output_ids_json || '[]').map((value) => Number(value)).filter(Boolean); } catch (_) {}
  outputIds.forEach((outputId, index) => {
    const card = node('button', 'refund-output-card');
    card.type = 'button';
    const img = document.createElement('img');
    img.src = `/api/admin/image-refunds/${encodeURIComponent(item.review_code)}/outputs/${outputId}`;
    img.alt = `${item.job_code || 'output'} ${index + 1}`;
    img.loading = 'lazy';
    img.decoding = 'async';
    card.append(img, node('span', '', `#${index + 1}`));
    card.addEventListener('click', () => openPreview(img.src, img.alt));
    grid.append(card);
  });
  parent.append(grid);
}

function addRow(parent, label, value) {
  const row = node('div', 'refund-detail-row');
  row.append(node('span', 'muted', label), node('strong', '', value || '-'));
  parent.append(row);
}

function renderDetail(item) {
  const detail = $('adminRefundDetail');
  detail.className = 'refund-admin-detail';
  detail.replaceChildren();

  const header = node('div', 'refund-detail-header');
  header.append(node('h2', '', `${item.review_code || ''}`), node('span', 'status-pill', statusLabel(item.status)));
  detail.append(header);

  addRow(detail, 'Job', item.job_code || '');
  addRow(detail, 'Account', item.account_id || '');
  addRow(detail, 'Charged', credits(item.charged_credits));
  addRow(detail, 'Created', item.created_at ? new Date(Number(item.created_at) * 1000).toLocaleString() : '');
  addRow(detail, 'Decision', item.decision || '');
  addRow(detail, 'Severity', String(item.severity_score ?? ''));
  addRow(detail, 'Confidence', item.confidence === null || item.confidence === undefined ? '' : String(item.confidence));
  addRow(detail, 'Model', item.reviewer_model || '');
  if (item.user_note) {
    const userNote = node('p', 'refund-public-reason', item.user_note);
    detail.append(node('span', 'muted', 'User note'));
    detail.append(userNote);
  }
  if (item.public_reason) detail.append(node('p', 'refund-public-reason', item.public_reason));

  renderAdminOutputs(detail, item);

  const result = safeJson(item.review_result_json);
  const resultBox = node('pre', 'refund-json');
  resultBox.textContent = JSON.stringify(result, null, 2);
  detail.append(resultBox);

  if (isRefundFinal(item.status) || isRejectFinal(item.status) || processingOnly(item.status)) {
    const readonly = node('div', 'refund-final-card');
    if (isRefundFinal(item.status)) {
      readonly.append(
        node('strong', '', '该任务已退款完成'),
        node('span', '', `已退还：${credits(item.charged_credits)}`),
        node('span', '', `处理方式：${reviewMethod(item)}`),
        node('span', '', `完成时间：${item.refunded_at ? new Date(Number(item.refunded_at) * 1000).toLocaleString() : '-'}`),
      );
    } else if (isRejectFinal(item.status)) {
      readonly.append(
        node('strong', '', '该任务已人工复审未通过'),
        node('span', '', `审核说明：${item.public_reason || '未达到严重结构崩坏标准。'}`),
        node('span', '', '处理方式：人工复审终审'),
      );
    } else {
      readonly.append(
        node('strong', '', '退款处理中'),
        node('span', '', '后台将按幂等逻辑处理退款，请勿重复批准。'),
      );
    }
    detail.append(readonly);
    return;
  }

  const noteLabel = node('label', '', t('admin_refund.note', '管理员备注'));
  const note = document.createElement('textarea');
  note.id = 'adminActionNote';
  note.rows = 3;
  note.maxLength = 500;
  noteLabel.append(note);
  detail.append(noteLabel);

  const actions = node('div', 'refund-admin-actions');
  const actionDefs = [
    ['approve', 'admin_refund.approve', '批准退款'],
    ['reject', 'admin_refund.reject', '拒绝'],
  ];
  if (!['manual_review_requested', 'manual_reviewing'].includes(String(item.status || ''))) {
    actionDefs.push(
      ['manual-review', 'admin_refund.manual', '标记可人工复审'],
      ['retry', 'admin_refund.retry', '重新自动审核'],
    );
  }
  actionDefs.forEach(([action, key, fallback]) => {
    const btn = node('button', action === 'approve' ? 'primary' : 'ghost', t(key, fallback));
    btn.type = 'button';
    btn.addEventListener('click', () => runAction(action));
    actions.append(btn);
  });
  detail.append(actions);
}

async function loadList() {
  setMessage('');
  const status = $('statusFilter').value;
  const data = await api(`/api/admin/image-refunds?status=${encodeURIComponent(status)}`);
  renderList(data.items || []);
}

async function loadDetail(reviewCode) {
  if (!reviewCode) return;
  currentReviewCode = reviewCode;
  setMessage('');
  const data = await api(`/api/admin/image-refunds/${encodeURIComponent(reviewCode)}`);
  renderDetail(data.item);
  await loadList();
}

async function runAction(action) {
  if (!currentReviewCode) return;
  const note = $('adminActionNote')?.value || '';
  try {
    const data = await api(`/api/admin/image-refunds/${encodeURIComponent(currentReviewCode)}/${action}`, {
      method: 'POST',
      body: JSON.stringify({note}),
    });
    setMessage('操作完成', 'ok');
    renderDetail(data.item);
    await loadList();
  } catch (error) {
    setMessage(error.message || '操作失败', 'error');
  }
}

async function init() {
  try {
    me = await api('/api/me');
    if (!me.is_admin) {
      window.location.href = '/';
      return;
    }
    $('userName').textContent = me.username || '';
    $('balance').textContent = credits(me.balance_fen);
    window.UmaI18n?.apply(document);
    await loadList();
  } catch (error) {
    if (error.status === 401) window.location.href = '/login';
    else setMessage(error.message || '加载失败', 'error');
  }
}

$('refreshBtn')?.addEventListener('click', loadList);
$('statusFilter')?.addEventListener('change', loadList);
$('refundPreviewClose')?.addEventListener('click', closePreview);
$('refundPreviewModal')?.addEventListener('click', (event) => {
  if (event.target === $('refundPreviewModal')) closePreview();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closePreview();
});
window.addEventListener('uma:langchange', () => {
  window.UmaI18n?.apply(document);
  loadList();
  if (currentReviewCode) loadDetail(currentReviewCode);
});

init();
