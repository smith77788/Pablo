// ── Массовое управление ресурсами: сортировка, виды, диапазон, весь срез ──
// Продукт — быстрое управление десятками, сотнями и тысячами аккаунтов,
// ботов, каналов и прокси. Здесь общее для четырёх списков, чтобы они вели
// себя одинаково (те же слова и места на экране):
//   * сортировка — на сервере, по всему набору (services/list_sort.py);
//     выбор запоминается на устройстве;
//   * сохранённые виды — фильтр, поиск, тег и сортировка под именем, на
//     сервере (services/mini_app_saved_views.py), то есть на всех устройствах;
//   * выбор диапазона — Shift+клик выделяет всё между двумя строками;
//   * «Весь срез» у ботов, каналов и прокси — догрузить все страницы и
//     выбрать всё под фильтром (у аккаунтов срез выбирает сервер);
//   * таблица аккаунтов на широком экране и теги аккаунтов.
//
// Файл подключается после инлайн-скрипта: внутри только объявления, общие
// помощники (api, toast, esc, askPrompt, askConfirm) вызываются по клику.

// Ключи обязаны совпадать с services/list_sort.py (tests/test_fleet_tools.py).
const FLEET_SORTS = {
  accounts: [['default','Сначала рабочие'], ['status','Сначала проблемные'], ['trust','Траст-скор'],
             ['added','Дата добавления'], ['last_used','Последняя работа'], ['phone','Телефон'],
             ['name','Имя'], ['geo','Страна'], ['pool','Пул'], ['stage','Статус CRM']],
  bots: [['default','Подписчики'], ['users','Все пользователи'], ['name','Имя'],
         ['status','Сначала со сбоями'], ['added','Сначала новые']],
  channels: [['default','Участники'], ['title','Название'], ['added','Дата добавления'],
             ['role','Сначала мои']],
  proxies: [['default','Сначала новые'], ['alive','Сначала мёртвые'], ['latency','Задержка'],
            ['accounts','Сколько аккаунтов'], ['label','Метка']],
};
// Потолок выбора за раз — как MASS_CAP на сервере.
const FLEET_MASS_CAP = 20000;

const FLEET_SORT = {};
function _fxSortState(kind) {
  if (!FLEET_SORT[kind]) {
    let st = null;
    try { st = JSON.parse(localStorage.getItem('fx_sort_' + kind) || 'null'); } catch (_) {}
    const ok = st && FLEET_SORTS[kind].some(([k]) => k === st.sort);
    FLEET_SORT[kind] = ok ? {sort: st.sort, dir: st.dir === 'asc' ? 'asc' : (st.dir === 'desc' ? 'desc' : '')}
                          : {sort: 'default', dir: ''};
  }
  return FLEET_SORT[kind];
}
// Хвост адреса списка: «&sort=…&dir=…» или пусто для порядка по умолчанию.
function fleetSortQS(kind) {
  const st = _fxSortState(kind);
  if (!st.sort || st.sort === 'default') return '';
  return '&sort=' + encodeURIComponent(st.sort) + (st.dir ? '&dir=' + st.dir : '');
}
function _fxReload(kind) {
  const fn = {accounts: 'reloadAccounts', bots: 'loadBots', channels: 'reloadChannels',
              proxies: 'loadProxyPool'}[kind];
  if (fn && typeof window[fn] === 'function') window[fn]();
}
function _fxSaveSort(kind) {
  try { localStorage.setItem('fx_sort_' + kind, JSON.stringify(FLEET_SORT[kind])); } catch (_) {}
}
function fleetSetSort(kind, key, dir) {
  const st = _fxSortState(kind);
  if (!FLEET_SORTS[kind].some(([k]) => k === key)) key = 'default';
  st.sort = key; st.dir = key === 'default' ? '' : (dir || '');
  _fxSaveSort(kind);
  fleetToolsRender(kind);
  _fxReload(kind);
}
function fleetFlipDir(kind) {
  const st = _fxSortState(kind);
  if (st.sort === 'default') return;
  st.dir = st.dir === 'asc' ? 'desc' : 'asc';
  _fxSaveSort(kind);
  fleetToolsRender(kind);
  _fxReload(kind);
}
// Клик по заголовку колонки: та же колонка — сменить направление.
function fleetSortByColumn(kind, key) {
  const st = _fxSortState(kind);
  if (st.sort === key) fleetFlipDir(kind); else fleetSetSort(kind, key, '');
}

// ── Сохранённые виды ────────────────────────────────────────────────
const FLEET_VIEWS = {};
// Что входит в вид и как его применить — у каждого списка своё.
const FLEET_VIEW_IO = {
  accounts: {
    get: () => ({filter: ACC_FILTER, stage: ACC_STAGE_FILTER, pool: ACC_POOL_FILTER,
                 geo: ACC_GEO_FILTER, q: ACC_SEARCH, tag: ACC_TAG_FILTER}),
    apply: p => {
      ACC_FILTER = p.filter || 'all'; ACC_STAGE_FILTER = p.stage || '';
      ACC_POOL_FILTER = p.pool || ''; ACC_GEO_FILTER = p.geo || ''; ACC_GEO_TITLE = '';
      ACC_SEARCH = p.q || ''; ACC_TAG_FILTER = p.tag || '';
      const i = document.getElementById('accSearch'); if (i) i.value = ACC_SEARCH;
      document.querySelectorAll('#accKpi .acc-kpi-card').forEach(x =>
        x.classList.toggle('on', x.getAttribute('data-filter') === ACC_FILTER));
      if (typeof accPoolBanner === 'function') accPoolBanner();
    },
  },
  bots: {
    get: () => ({filter: BOT_FILTER, q: BOT_SEARCH}),
    apply: p => {
      BOT_FILTER = p.filter || 'all'; BOT_SEARCH = p.q || '';
      const i = document.getElementById('botSearch'); if (i) i.value = BOT_SEARCH;
    },
  },
  channels: {
    get: () => ({filter: CH_FILTER, q: CH_SEARCH}),
    apply: p => {
      CH_FILTER = p.filter || 'all'; CH_SEARCH = p.q || '';
      const i = document.getElementById('chSearch'); if (i) i.value = CH_SEARCH;
    },
  },
  proxies: {
    get: () => ({filter: PP_FILTER, q: PP_SEARCH}),
    apply: p => {
      PP_FILTER = p.filter || 'all'; PP_SEARCH = p.q || '';
      const i = document.getElementById('ppSearch'); if (i) i.value = PP_SEARCH;
      const order = ['all', 'alive', 'dead', 'backup'];
      document.querySelectorAll('#proxyPoolSeg .seg-opt').forEach((o, n) =>
        o.classList.toggle('on', order[n] === PP_FILTER));
    },
  },
};
async function fleetViewsLoad(kind) {
  try {
    const d = await api('/api/miniapp/views?kind=' + kind);
    FLEET_VIEWS[kind] = d.views || [];
  } catch (_) { FLEET_VIEWS[kind] = FLEET_VIEWS[kind] || []; }
  fleetToolsRender(kind);
}
async function fleetViewSave(kind) {
  const name = await askPrompt('Название вида', {placeholder: 'Например: Мёртвые RU'});
  if (!name || !name.trim()) return;
  const st = _fxSortState(kind);
  const params = Object.assign({}, FLEET_VIEW_IO[kind].get(), {sort: st.sort, dir: st.dir});
  try {
    await api('/api/miniapp/views', {method: 'POST',
      body: JSON.stringify({kind, name: name.trim(), params})});
    toast('⭐ Вид сохранён: ' + name.trim());
    fleetViewsLoad(kind);
  } catch (e) { toast('⚠️ ' + errRu(e, 'Не удалось сохранить вид')); }
}
function fleetViewApply(kind, id) {
  const v = (FLEET_VIEWS[kind] || []).find(x => x.id === id);
  if (!v) return;
  const p = v.params || {};
  FLEET_VIEW_IO[kind].apply(p);
  const st = _fxSortState(kind);
  st.sort = FLEET_SORTS[kind].some(([k]) => k === p.sort) ? p.sort : 'default';
  st.dir = (p.dir === 'asc' || p.dir === 'desc') ? p.dir : '';
  _fxSaveSort(kind);
  fleetToolsRender(kind);
  _fxReload(kind);
}
async function fleetViewDelete(kind, id) {
  const v = (FLEET_VIEWS[kind] || []).find(x => x.id === id);
  if (!v || !await askConfirm('Удалить вид «' + v.name + '»? Сами ресурсы не меняются.')) return;
  try {
    await api('/api/miniapp/views/' + id, {method: 'DELETE'});
    fleetViewsLoad(kind);
  } catch (e) { toast('⚠️ ' + errRu(e, 'Не удалось удалить')); }
}

// Строка инструментов над списком: сортировка, направление, виды.
function fleetToolsRender(kind) {
  const box = document.getElementById('fx-' + kind);
  if (!box) return;
  const st = _fxSortState(kind);
  const opts = FLEET_SORTS[kind].map(([k, l]) =>
    `<option value="${k}"${k === st.sort ? ' selected' : ''}>${esc(l)}</option>`).join('');
  const dirBtn = st.sort === 'default' ? '' :
    `<button class="btn btn-s fx-dir" onclick="fleetFlipDir('${kind}')" aria-label="Сменить направление сортировки">${st.dir === 'asc' ? '↑' : st.dir === 'desc' ? '↓' : '⇅'}</button>`;
  const views = (FLEET_VIEWS[kind] || []).map(v =>
    `<span class="lf-chip fx-view" role="button" tabindex="0" onclick="fleetViewApply('${kind}',${v.id})">⭐ ${esc(v.name)}`
    + `<span class="fx-view-x" role="button" aria-label="Удалить вид" onclick="event.stopPropagation();fleetViewDelete('${kind}',${v.id})">✕</span></span>`).join('');
  box.innerHTML = `<div class="fx-row">
      <label class="fx-sort"><span>Сортировка</span>
        <select class="sel" onchange="fleetSetSort('${kind}',this.value)" aria-label="Сортировка списка">${opts}</select></label>
      ${dirBtn}
      <button class="btn btn-s fx-save" onclick="fleetViewSave('${kind}')" title="Сохранить фильтр, поиск и сортировку под именем">⭐ Сохранить вид</button>
    </div>
    ${views ? `<div class="lf-chips fx-views" role="group" aria-label="Сохранённые виды">${views}</div>` : ''}`;
}
// Первый показ экрана: инструменты и виды с сервера (один раз за сессию).
const _FX_INIT = {};
function fleetToolsInit(kind) {
  fleetToolsRender(kind);
  if (!_FX_INIT[kind]) { _FX_INIT[kind] = true; fleetViewsLoad(kind); }
}

// ── Выбор диапазона ────────────────────────────────────────────────
const FX_LAST = {};
// Shift+клик: выделить всё между прошлым и этим кликом. true — диапазон
// обработан, обычное переключение не нужно.
function fleetRangePick(kind, ids, sel, id, ev) {
  const last = FX_LAST[kind];
  FX_LAST[kind] = id;
  if (!ev || !ev.shiftKey || last == null) return false;
  const a = ids.indexOf(last), b = ids.indexOf(id);
  if (a < 0 || b < 0) return false;
  const lo = Math.min(a, b), hi = Math.max(a, b);
  for (let n = lo; n <= hi; n++) sel.add(ids[n]);
  return true;
}

// ── «Весь срез» для списков, которые фильтруются на устройстве ─────
// Догружаем страницы, пока не придёт всё (или потолок), и выбираем всё под
// фильтром. Без этого «Все» брал только загруженное.
async function _fxLoadAll(loaded, total, more) {
  let guard = 0;
  while (loaded() < Math.min(total(), FLEET_MASS_CAP) && guard++ < 100) {
    const before = loaded();
    toast(`Загружаю весь список: ${num(before)} из ${num(total())}…`);
    await more();
    if (loaded() <= before) break;
  }
}
async function botSelWholeFilter() {
  await _fxLoadAll(() => BOTS.length, () => BOT_TOTAL, loadMoreBots);
  botSelAll();
}
async function chSelWholeFilter() {
  await _fxLoadAll(() => CUR_CHANNELS.length, () => CH_TOTAL, loadMoreChannels);
  chSelAll();
}
async function ppSelWholeFilter() {
  await _fxLoadAll(() => _proxyPoolData.length, () => PP_TOTAL, loadMoreProxies);
  ppSelAll();
}

// ── Выбор прокси и массовые действия ───────────────────────────────
let PP_SEL_MODE = false;
const PP_SEL = new Set();
function togglePpSelMode() {
  PP_SEL_MODE = !PP_SEL_MODE;
  PP_SEL.clear();
  const btn = document.getElementById('ppSelToggle');
  if (btn) btn.textContent = PP_SEL_MODE ? '✖️ Отмена' : '☑️ Выбрать';
  const bar = document.getElementById('ppSelBar');
  if (bar) bar.style.display = PP_SEL_MODE ? '' : 'none';
  if (typeof syncSelBarGap === 'function') syncSelBarGap('ppSelBar');
  renderProxyPoolList(_ppFiltered());
  updatePpSelBar();
}
function togglePpSel(id, ev) {
  if (!fleetRangePick('proxies', _ppFiltered().map(p => p.id), PP_SEL, id, ev)) {
    if (PP_SEL.has(id)) PP_SEL.delete(id); else PP_SEL.add(id);
  }
  renderProxyPoolList(_ppFiltered());
  updatePpSelBar();
}
function ppSelAll() {
  _ppFiltered().slice(0, FLEET_MASS_CAP).forEach(p => PP_SEL.add(p.id));
  renderProxyPoolList(_ppFiltered()); updatePpSelBar();
}
function ppSelClear() { PP_SEL.clear(); renderProxyPoolList(_ppFiltered()); updatePpSelBar(); }
function updatePpSelBar() {
  const el = document.getElementById('ppSelCount');
  if (el) el.textContent = 'Выбрано: ' + PP_SEL.size;
}
async function runPpMass(op) {
  const n = PP_SEL.size;
  if (!n) { toast('❗ Выберите хотя бы один прокси'); return; }
  const body = {op, proxy_ids: [...PP_SEL]};
  if (op === 'label') {
    const v = await askPrompt('Метка для ' + n + ' прокси', {placeholder: 'Например: DE-резерв'});
    if (v === null) return;
    body.value = v;
  }
  if (op === 'delete' && !await askConfirm(`Удалить ${n} прокси? Назначенные аккаунтам не удаляются — их пропустим и посчитаем.`)) return;
  try {
    const r = await api('/api/miniapp/proxies/bulk', {method: 'POST', body: JSON.stringify(body)});
    const cnt = Number(r.count || 0);
    if (op === 'delete') toast(`🗑 Удалено: ${cnt}` + (r.skipped_assigned ? ` · пропущено назначенных: ${r.skipped_assigned}` : ''));
    else toast(`✅ Изменено прокси: ${cnt}`);
    togglePpSelMode();
    loadProxyPool();
  } catch (e) { toast('⚠️ ' + errRu(e, 'Ошибка')); }
}

// ── Теги аккаунтов ─────────────────────────────────────────────────
let ACC_TAG_FILTER = '';
let ACC_TAGS = [];
async function loadAccTags() {
  try { ACC_TAGS = (await api('/api/miniapp/accounts/tags')).tags || []; } catch (_) { ACC_TAGS = []; }
  renderAccTagChips();
}
function renderAccTagChips() {
  const box = document.getElementById('accTagChips');
  if (!box) return;
  if (!ACC_TAGS.length && !ACC_TAG_FILTER) { box.style.display = 'none'; box.innerHTML = ''; return; }
  box.style.display = 'flex';
  box.innerHTML = ACC_TAGS.map(t =>
    `<span class="lf-chip${ACC_TAG_FILTER === t.tag ? ' on' : ''}" role="button" tabindex="0" `
    + `aria-pressed="${ACC_TAG_FILTER === t.tag}" onclick="filterAccTag('${escJs(t.tag)}')">#${esc(t.tag)} ${num(t.count)}</span>`).join('');
}
function filterAccTag(tag) {
  ACC_TAG_FILTER = ACC_TAG_FILTER === tag ? '' : tag;
  renderAccTagChips();
  reloadAccounts();
}
async function runAccTag(op) {
  const n = _massSelCount();
  if (!n) { toast('❗ Выберите хотя бы один аккаунт'); return; }
  const hint = ACC_TAGS.slice(0, 5).map(t => t.tag).join(', ');
  const tag = await askPrompt(op === 'add_tag' ? `Тег для ${n} аккаунтов` : `Снять тег с ${n} аккаунтов`,
    {placeholder: hint ? 'Например: ' + hint : 'Например: прогрев'});
  if (!tag || !tag.trim()) return;
  try {
    const r = await api('/api/miniapp/accounts/mass', {method: 'POST',
      body: JSON.stringify(Object.assign({op, value: tag.trim()}, _massSel()))});
    toast((op === 'add_tag' ? '🏷 Тег поставлен: ' : '🏷 Тег снят: ') + num(r.count || 0)
      + (r.capped ? ` · не вошло сверх ${num(FLEET_MASS_CAP)}: ${num(r.capped)}` : ''));
    toggleAccSelMode();
    loadAccTags();
    reloadAccounts();
  } catch (e) { toast('⚠️ ' + errRu(e, 'Ошибка')); }
}
function accTagsHtml(a) {
  return (a.tags || []).map(t => `<span class="fx-tag">#${esc(t)}</span>`).join('');
}

// ── Таблица аккаунтов на широком экране ────────────────────────────
const FX_WIDE = window.matchMedia ? window.matchMedia('(min-width: 900px)') : null;
function fleetIsWide() { return !!(FX_WIDE && FX_WIDE.matches); }
if (FX_WIDE && FX_WIDE.addEventListener) {
  FX_WIDE.addEventListener('change', () => {
    if (typeof CUR_ACCS !== 'undefined' && document.getElementById('accList')) renderAccounts(CUR_ACCS);
  });
}
function _fxTh(kind, key, label) {
  const st = _fxSortState(kind);
  const mark = st.sort === key ? (st.dir === 'asc' ? ' ↑' : st.dir === 'desc' ? ' ↓' : ' ⇅') : '';
  return `<th class="fx-th" role="button" tabindex="0" onclick="fleetSortByColumn('${kind}','${key}')">${label}${mark}</th>`;
}
function accTableHtml(list) {
  const head = `<tr>${ACC_SEL_MODE ? '<th class="fx-chk"></th>' : ''}`
    + _fxTh('accounts', 'name', 'Аккаунт') + _fxTh('accounts', 'phone', 'Телефон')
    + _fxTh('accounts', 'status', 'Состояние') + _fxTh('accounts', 'trust', 'Траст')
    + _fxTh('accounts', 'geo', 'Прокси') + _fxTh('accounts', 'pool', 'Пул')
    + _fxTh('accounts', 'stage', 'Статус') + '<th>Теги</th>'
    + _fxTh('accounts', 'last_used', 'Работал') + '</tr>';
  const rows = list.map(a => {
    const s = accStatusOf(a);
    const sel = ACC_SEL.has(a.id);
    const name = a.first_name || (a.username ? '@' + a.username : '#' + a.id);
    const trust = a.trust_score != null ? Math.round(a.trust_score) : 100;
    const proxy = !a.has_proxy ? '<span class="fx-dim">напрямую</span>'
      : (a.proxy_alive === false ? '<span class="fx-bad">🔌 ' : '<span>') + esc(a.proxy_label || 'прокси') + '</span>';
    const onclick = ACC_SEL_MODE ? `toggleAccSel(${a.id},event)` : `openAccount(${a.id})`;
    return `<tr class="fx-tr${ACC_SEL_MODE && sel ? ' fx-on' : ''}" onclick="${onclick}">`
      + (ACC_SEL_MODE ? `<td class="fx-chk"><span class="acc-check ${sel ? 'on' : ''}">${sel ? '✓' : ''}</span></td>` : '')
      + `<td>${phoneFlag(a.phone || '')} ${esc(name)}</td><td class="fx-mono">${esc(a.phone || '')}</td>`
      + `<td><span class="badge ${s.cls}">${s.status}</span>${a.health_reason && (a.health_status === 'quarantine' || a.health_status === 'at_risk') ? `<div class="fx-dim">${esc(a.health_reason)}</div>` : ''}</td>`
      + `<td>${trust}%</td><td>${proxy}</td><td>${esc(a.pool || '')}</td>`
      + `<td>${stageBadge(a.stage)}</td><td>${accTagsHtml(a)}</td>`
      + `<td class="fx-dim">${a.last_used ? ago(a.last_used) : '—'}</td></tr>`;
  }).join('');
  return `<div class="fx-table-wrap"><table class="fx-table"><thead>${head}</thead><tbody>${rows}</tbody></table></div>`;
}
