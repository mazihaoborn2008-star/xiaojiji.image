const $ = (id) => document.getElementById(id);
let me = null;
let tagCategories = [];
let selectedCategoryIndex = 0;
let tagSearchTimer = null;
const TAG_COLLAPSED_LIMIT = 3;
const expandedTagCategories = new Set();
const expandedTagTexts = new Set();

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
function currentLang() { return window.UmaI18n?.getLang?.() || 'zh'; }

function categoryName(category) {
  return currentLang() === 'en'
    ? (category.category_en || category.category_zh || '')
    : (category.category_zh || category.category_en || '');
}

function itemPrimaryName(item) {
  return currentLang() === 'en'
    ? (item.name_en || item.name_zh || item.tags || '')
    : (item.name_zh || item.name_en || item.tags || '');
}

function itemSecondaryName(item) {
  return currentLang() === 'en'
    ? (item.name_zh || item.name_en || '')
    : (item.name_en || item.name_zh || '');
}

function searchableText(category, item) {
  return [
    category.category_zh,
    category.category_en,
    item.name_zh,
    item.name_en,
    item.aliases,
    item.tags,
  ].filter(Boolean).join(' ').toLowerCase();
}

function categoryKey(category, index = selectedCategoryIndex) {
  return category?.category_en || category?.category_zh || `category-${index}`;
}

function tagItemKey(category, item) {
  return `${categoryKey(category)}::${item.name_en || item.name_zh || item.tags || ''}`;
}

function getVisibleTagEntries(query) {
  const normalized = query.trim().toLowerCase();
  if (normalized) {
    return tagCategories.flatMap((category) => (category.items || [])
      .filter((item) => searchableText(category, item).includes(normalized))
      .map((item) => ({category, item})));
  }
  const category = tagCategories[selectedCategoryIndex];
  if (!category) return [];
  return (category.items || []).map((item) => ({category, item}));
}

async function copyTags(tags, messageKey = 'tags.copied') {
  try {
    await navigator.clipboard.writeText(tags);
  } catch (_) {
    const ta = document.createElement('textarea');
    ta.value = tags;
    ta.setAttribute('readonly', '');
    ta.className = 'clipboard-helper';
    document.body.append(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  setMessage('tagStatus', t(messageKey, '已复制'), 'ok');
}

function insertTagsToHomePrompt(item) {
  const tags = String(item?.tags || '').trim();
  if (!tags) return;
  sessionStorage.setItem('pending_prompt_character_tags', JSON.stringify({
    tags,
    character_key: item.character_key || '',
    name_zh: item.name_zh || '',
    name_en: item.name_en || '',
  }));
  setMessage('tagStatus', t('tags.inserted_home', '已插入到首页 Prompt。'), 'ok');
  window.location.href = '/';
}

function renderTagCategories() {
  const wrap = $('tagCategories');
  if (!wrap) return;
  wrap.replaceChildren();
  tagCategories.forEach((category, index) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `tag-category${index === selectedCategoryIndex ? ' active' : ''}`;
    btn.textContent = categoryName(category);
    btn.addEventListener('click', () => {
      selectedCategoryIndex = index;
      renderTags();
    });
    wrap.append(btn);
  });
}

function renderTags() {
  renderTagCategories();
  const list = $('tagList');
  if (!list) return;
  list.replaceChildren();
  if (!tagCategories.length) {
    const empty = document.createElement('div');
    empty.className = 'muted';
    empty.textContent = t('tags.empty', '暂无 tag。');
    list.append(empty);
    return;
  }
  const query = $('tagSearch')?.value.trim().toLowerCase() || '';
  const entries = getVisibleTagEntries(query);
  const activeCategory = tagCategories[selectedCategoryIndex];
  const activeCategoryKey = categoryKey(activeCategory);
  const categoryExpanded = expandedTagCategories.has(activeCategoryKey);
  const shouldLimitCategory = !query && entries.length > TAG_COLLAPSED_LIMIT && !categoryExpanded;
  const visibleEntries = shouldLimitCategory ? entries.slice(0, TAG_COLLAPSED_LIMIT) : entries;
  if (!entries.length) {
    const empty = document.createElement('div');
    empty.className = 'muted';
    empty.textContent = t('tags.no_results', '没有找到匹配的角色。');
    list.append(empty);
    return;
  }
  for (const {category, item} of visibleEntries) {
    const itemKey = tagItemKey(category, item);
    const tagExpanded = expandedTagTexts.has(itemKey);
    const card = document.createElement('article');
    card.className = 'tag-card';

    const names = document.createElement('div');
    names.className = 'tag-card-names';
    const primary = document.createElement('strong');
    primary.textContent = itemPrimaryName(item);
    names.append(primary);
    const secondaryText = itemSecondaryName(item);
    if (secondaryText && secondaryText !== primary.textContent) {
      const secondary = document.createElement('span');
      secondary.textContent = secondaryText;
      names.append(secondary);
    }
    if (query) {
      const categoryLabel = document.createElement('span');
      categoryLabel.textContent = categoryName(category);
      names.append(categoryLabel);
    }

    const tags = document.createElement('p');
    tags.className = `tag-text${tagExpanded ? ' expanded' : ' clamped'}`;
    tags.dataset.tagKey = itemKey;
    tags.textContent = item.tags || '';

    const tagToggle = document.createElement('button');
    tagToggle.type = 'button';
    tagToggle.className = 'tag-text-toggle hidden';
    tagToggle.textContent = tagExpanded ? t('tags.hide_tags', '收起 tag') : t('tags.show_tags', '展开 tag');
    tagToggle.addEventListener('click', () => {
      if (expandedTagTexts.has(itemKey)) {
        expandedTagTexts.delete(itemKey);
      } else {
        expandedTagTexts.add(itemKey);
      }
      renderTags();
    });

    const actions = document.createElement('div');
    actions.className = 'tag-actions';
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'ghost';
    copy.textContent = t('tags.copy', '复制 tag');
    copy.addEventListener('click', () => copyTags(item.tags || ''));
    const insert = document.createElement('button');
    insert.type = 'button';
    insert.className = 'ghost';
    insert.textContent = t('tags.insert', '插入到 Prompt');
    insert.addEventListener('click', () => insertTagsToHomePrompt(item));
    actions.append(copy, insert);

    card.append(names, tags, tagToggle, actions);
    list.append(card);
  }
  if (!query && entries.length > TAG_COLLAPSED_LIMIT) {
    const moreWrap = document.createElement('div');
    moreWrap.className = 'tag-list-controls';
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'ghost tag-list-toggle';
    more.textContent = categoryExpanded
      ? t('tags.collapse_characters', '收起角色')
      : t('tags.show_more_characters', '展开更多角色（共 {count} 个）', {count: entries.length});
    more.addEventListener('click', () => {
      if (expandedTagCategories.has(activeCategoryKey)) {
        expandedTagCategories.delete(activeCategoryKey);
      } else {
        expandedTagCategories.add(activeCategoryKey);
      }
      renderTags();
    });
    moreWrap.append(more);
    list.append(moreWrap);
  }
  requestAnimationFrame(updateTagTextToggles);
}

function updateTagTextToggles() {
  document.querySelectorAll('.tag-card').forEach((card) => {
    const text = card.querySelector('.tag-text');
    const btn = card.querySelector('.tag-text-toggle');
    if (!text || !btn) return;
    const expanded = text.classList.contains('expanded');
    const isOverflowing = expanded || text.scrollHeight > text.clientHeight + 1;
    btn.classList.toggle('hidden', !isOverflowing);
  });
}

async function loadCharacterTags() {
  try {
    const data = await api('/api/character-tags');
    tagCategories = Array.isArray(data.categories) ? data.categories : [];
    selectedCategoryIndex = 0;
    renderTags();
    if (data.umamusume_desktop_found === false) {
      setMessage('tagStatus', t('tags.umamusume_missing', '未找到桌面 umamusume.txt，已显示内置 tag。'), '');
    }
  } catch (err) {
    setMessage('tagStatus', err.message || t('tags.load_failed', '暂时无法加载人物 tag。'), 'error');
  }
}

function goBackOrHome() {
  if (window.history.length > 1) {
    window.history.back();
  } else {
    window.location.href = '/';
  }
}

async function init() {
  try {
    me = await api('/api/me');
    $('userName').textContent = me.username;
    $('balance').textContent = credits(me.balance_fen);
    await loadCharacterTags();
  } catch(e) {
    if (e.message.includes('登录') || e.message.includes('401')) {
      window.location.href = '/login';
      return;
    }
    setMessage('tagStatus', e.message, 'error');
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
  renderTags();
});

$('tagSearch')?.addEventListener('input', () => {
  clearTimeout(tagSearchTimer);
  tagSearchTimer = setTimeout(renderTags, 120);
});

$('tagSearchForm')?.addEventListener('submit', (event) => {
  event.preventDefault();
  clearTimeout(tagSearchTimer);
  renderTags();
});

init();
