// ── Единый дашборд (screens/dashboard.js) ──────────────────────────────────
// Один экран-хаб со вкладками, объединяющий все дашборды проекта:
//   📊 Инфраструктура — аккаунты/прокси/операции/здоровье сетки (мой
//      /api/miniapp/dashboard/visual, рендер переиспользует _ccRender из
//      cmdcenter.js — грузится раньше);
//   📈 Аналитика — аудитория/каналы/посты/рост/история (/dashboard_realtime);
//   🗂 Все дашборды — запуск детальных экранов (здоровье, стата ботов,
//      админ-стата, аудитория и т.д.) — ничего не потеряно, всё из одной точки.
//
// Экран s-uni-dash создаётся ДИНАМИЧЕСКИ (в index.html только плитка +
// <script>). Зависит от глобалей основного скрипта (push, api, esc, num) и от
// cmdcenter.js (_ccRender/_ccCard/_ccBar). Всё по клику — хелперы уже готовы.
'use strict';

let _udTabCur = 'infra';

function _udEnsureScreen() {
  let el = document.getElementById('s-uni-dash');
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = 's-uni-dash';
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="hdr-burger" onclick="back()">‹</div>' +
      '<div class="hdr-title">📊 Дашборд</div>' +
      '<div class="hdr-burger" onclick="_udReload()" title="Обновить">⟳</div>' +
    '</div>' +
    '<div class="sb">' +
      '<div id="udTabs" style="display:flex;gap:6px;overflow-x:auto;padding:4px 0 10px;' +
        '-ms-overflow-style:none;scrollbar-width:none"></div>' +
      '<div id="udBody" style="padding:2px 0"></div>' +
    '</div>';
  const anchor = document.getElementById('s-more') || document.body;
  anchor.parentNode.appendChild(el);
  return el;
}

const _UD_TABS = [
  { key: 'infra',     label: '📊 Инфраструктура' },
  { key: 'analytics', label: '📈 Аналитика' },
  { key: 'more',      label: '🗂 Все дашборды' },
];

function _udRenderTabs() {
  const bar = document.getElementById('udTabs');
  if (!bar) return;
  bar.innerHTML = _UD_TABS.map(t => {
    const on = t.key === _udTabCur;
    return `<span onclick="_udTab('${t.key}')" style="white-space:nowrap;cursor:pointer;` +
      `padding:7px 13px;border-radius:20px;font-size:13px;font-weight:600;` +
      (on ? 'background:var(--accent,#2dd4bf);color:#04201c'
          : 'background:rgba(128,128,128,.15);color:var(--hint)') +
      `">${esc(t.label)}</span>`;
  }).join('');
}

async function openUnifiedDashboard() {
  _udEnsureScreen();
  push('s-uni-dash');
  _udTabCur = 'infra';
  _udRenderTabs();
  await _udLoad();
}

function _udTab(key) {
  _udTabCur = key;
  _udRenderTabs();
  _udLoad();
}

function _udReload() { _udLoad(); }

function _udLoading() {
  const b = document.getElementById('udBody');
  if (b) b.innerHTML = '<div style="text-align:center;color:var(--hint);padding:40px">Загрузка…</div>';
}
function _udError(msg) {
  const b = document.getElementById('udBody');
  if (b) b.innerHTML = '<div style="text-align:center;color:var(--orange);padding:40px">⚠️ ' +
    esc(msg || 'Ошибка загрузки') + '</div>';
}

async function _udLoad() {
  const body = document.getElementById('udBody');
  if (!body) return;
  if (_udTabCur === 'more') { body.innerHTML = _udRenderMore(); return; }
  _udLoading();
  try {
    if (_udTabCur === 'infra') {
      const d = await api('/api/miniapp/dashboard/visual');
      // переиспользуем рендер командного центра (cmdcenter.js)
      body.innerHTML = (typeof _ccRender === 'function')
        ? _ccRender(d)
        : '<div style="color:var(--hint);padding:20px">Модуль инфраструктуры не загружен</div>';
    } else if (_udTabCur === 'analytics') {
      const d = await api('/api/miniapp/dashboard_realtime?range=7d');
      body.innerHTML = _udRenderAnalytics(d);
    }
  } catch (e) {
    _udError((e && e.message) || 'Ошибка');
  }
}

// ── Вкладка «Аналитика» ─────────────────────────────────────────────────────
function _udNum(v) {
  return (typeof num === 'function') ? num(v || 0) : String(v || 0);
}
function _udChip(ico, val, lbl, color) {
  return '<div style="flex:0 0 auto;min-width:88px;background:var(--card,var(--bg-input));' +
    'border-radius:12px;padding:10px 12px;text-align:center">' +
    `<div style="font-size:19px;font-weight:800;color:${color||'var(--fg)'}">${esc(String(val))}</div>` +
    `<div style="font-size:11px;color:var(--hint);margin-top:2px">${esc(ico+' '+lbl)}</div></div>`;
}
// Мини-спарклайн из массива чисел / [{value}].
function _udLine(series, color) {
  const vals = (series || []).map(p => typeof p === 'object' ? (p.value || 0) : (p || 0));
  if (vals.length < 2) return '<div style="color:var(--hint);font-size:12px;padding:8px">Нет данных</div>';
  const W = 300, H = 54, max = Math.max(1, ...vals), min = Math.min(...vals);
  const rng = (max - min) || 1;
  const pts = vals.map((v, i) => {
    const x = (i / (vals.length - 1)) * W;
    const y = H - 4 - ((v - min) / rng) * (H - 8);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" preserveAspectRatio="none">` +
    `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2" ` +
    `stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

function _udRenderAnalytics(d) {
  d = d || {};
  const g = d.growth_7d || 0;
  const gcol = g > 0 ? 'var(--green,#2dd4bf)' : g < 0 ? 'var(--red,#ef4444)' : 'var(--fg)';
  const chips = '<div style="display:flex;gap:8px;overflow-x:auto;padding:2px 0 4px;' +
    '-ms-overflow-style:none;scrollbar-width:none">' +
    _udChip('👥', _udNum(d.total_subscribers), 'Подписчики', 'var(--blue,#38bdf8)') +
    _udChip('📡', _udNum(d.total_channels), 'Каналы', 'var(--purple,#a78bfa)') +
    _udChip('📝', _udNum(d.total_posts), 'Посты', 'var(--green,#2dd4bf)') +
    _udChip('📱', _udNum(d.total_accounts), 'Аккаунты', 'var(--orange,#f59e0b)') +
    _udChip('📈', (g > 0 ? '+' : '') + _udNum(g), 'Рост 7д', gcol) +
    '</div>';

  const card = (title, inner) => (typeof _ccCard === 'function')
    ? _ccCard(title, inner)
    : `<div style="background:var(--card,var(--bg-input));border-radius:14px;padding:14px;margin-bottom:12px">` +
      `<div style="font-weight:700;font-size:13px;margin-bottom:10px">${esc(title)}</div>${inner}</div>`;

  const charts = card('📈 Динамика (7 дней)',
    '<div style="font-size:12px;color:var(--hint);margin-bottom:2px">Подписчики</div>' +
    _udLine(d.subs_history, 'var(--blue,#38bdf8)') +
    '<div style="font-size:12px;color:var(--hint);margin:8px 0 2px">Просмотры</div>' +
    _udLine(d.views_history, 'var(--orange,#f59e0b)') +
    '<div style="font-size:12px;color:var(--hint);margin:8px 0 2px">Вовлечённость</div>' +
    _udLine(d.engagement_history, 'var(--green,#2dd4bf)'));

  const tc = d.top_channels || [];
  const topCard = card('🏆 Топ каналов', tc.length
    ? tc.slice(0, 5).map((c, i) =>
        '<div style="display:flex;align-items:center;gap:8px;font-size:13px;margin:5px 0">' +
        `<span style="color:var(--hint);width:16px">${i + 1}</span>` +
        `<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">` +
        `${esc(c.name || c.username || c.title || '—')}</span>` +
        `<span style="font-weight:700">${_udNum(c.subscribers || c.value || 0)}</span></div>`).join('')
    : '<div style="color:var(--hint);font-size:12px">Нет данных</div>');

  const acts = d.recent_activity || [];
  const actCard = card('🕑 Последние события', acts.length
    ? acts.slice(0, 8).map(a =>
        '<div style="display:flex;gap:8px;font-size:12px;margin:5px 0;color:var(--fg)">' +
        `<span style="flex:1;min-width:0">${esc(a.text || a.type || '—')}</span>` +
        (a.value != null ? `<span style="font-weight:700;color:var(--hint)">${esc(String(a.value))}</span>` : '') +
        '</div>').join('')
    : '<div style="color:var(--hint);font-size:12px">Нет событий</div>');

  return chips + charts + topCard + actCard;
}

// ── Вкладка «Все дашборды» — запуск детальных экранов ───────────────────────
const _UD_LAUNCH = [
  { fn: 'openHealth',            ico: '❤️', lbl: 'Дэшборд здоровья' },
  { fn: 'openInfraHealth',      ico: '🩺', lbl: 'Здоровье инфраструктуры' },
  { fn: 'openAnalyticsDashboard', ico: '📈', lbl: 'Дашборд метрик' },
  { fn: 'openAnalytics',        ico: '📊', lbl: 'Аналитика' },
  { fn: 'openAudienceAnalytics', ico: '👥', lbl: 'Аналитика аудитории' },
  { fn: 'openBotStats',         ico: '🤖', lbl: 'Статистика ботов' },
  { fn: 'openAdminStats',       ico: '🛠', lbl: 'Админ-статистика' },
  { fn: 'openContactStats',     ico: '📇', lbl: 'Статистика контактов' },
  { fn: 'openCmdCenter',        ico: '📟', lbl: 'Командный центр' },
];
function _udRenderMore() {
  const tiles = _UD_LAUNCH.filter(t => typeof window[t.fn] === 'function').map(t =>
    `<div onclick="${t.fn}()" style="cursor:pointer;background:var(--card,var(--bg-input));` +
    'border-radius:12px;padding:14px 10px;text-align:center">' +
    `<div style="font-size:24px">${t.ico}</div>` +
    `<div style="font-size:12px;margin-top:6px;color:var(--fg)">${esc(t.lbl)}</div></div>`).join('');
  return '<div style="font-size:12px;color:var(--hint);margin-bottom:10px">' +
    'Все детальные дашборды и статистики — в одном месте:</div>' +
    `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px">${tiles}</div>`;
}
