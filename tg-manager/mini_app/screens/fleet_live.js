// ── «Флот сейчас» — живая вкладка единого дашборда (screens/fleet_live.js) ──
// Первое, что видит владелец, открыв «📊 Дашборд»: сколько аккаунтов в строю,
// на прогреве, в кулдауне, в карантине и выбыло; что требует внимания; какие
// операции идут прямо сейчас и чем кончились за сутки; лента банов и
// предупреждений. Обновляется сама раз в 20 секунд, пока вкладка на экране,
// и подсвечивает цифры, которые изменились с прошлого раза.
//
// Данные — один запрос /api/miniapp/dashboard/live (services/mini_app_dashboard.
// live_snapshot): единый пульс, «требует внимания» и очередь операций — те же
// источники, что у остальных экранов, без второго расчёта.
//
// Кнопки — существующие двери: срезы списка аккаунтов (healthGoAccounts),
// экран здоровья, карточка операции, пауза/возобновление/повтор очереди (те же
// ручки, что на экране «Операции»), действия карточек «внимания» (_udAct из
// dashboard.js). Своих массовых действий здесь нет.
//
// Вёрстка — компоненты ui_kit.js и классы .fl-* в ui_kit.css: без style="…"
// и без своих цветов, полоски и кольцо рисуются SVG-атрибутами.
// Классический скрипт: грузится после dashboard.js и делит с ним область.
'use strict';

const _FL_EVERY_MS = 20000;
let _flPrev = null;          // прошлый снимок — чтобы подсветить изменения
let _flAt = 0;               // когда пришёл последний удачный снимок
let _flStale = false;        // последнее фоновое обновление не удалось

function _flAutoOn() {
  const p = (typeof loadFormPrefs === 'function') ? (loadFormPrefs('fleet_live') || {}) : {};
  return p.auto !== false;   // по умолчанию — живая
}
function flSetAuto(on) {
  if (typeof saveFormPrefs === 'function') saveFormPrefs('fleet_live', { auto: !!on });
  _flSchedule();
  _flTickClock();
}

// Вкладка видна: экран дашборда сверху, выбрана «Флот сейчас», окно не скрыто.
function _flVisible() {
  return typeof STACK !== 'undefined' && STACK.length && STACK[STACK.length - 1] === 's-uni-dash'
    && typeof _udTabCur !== 'undefined' && _udTabCur === 'live'
    && !(typeof document.visibilityState === 'string' && document.visibilityState === 'hidden');
}

function _flSchedule() {
  clearTimeout(_flSchedule.timer);
  _flSchedule.timer = null;
  if (!_flAutoOn()) return;
  _flSchedule.timer = setTimeout(_flTick, _FL_EVERY_MS);
}

async function _flTick() {
  _flSchedule.timer = null;
  if (!_flVisible()) {
    // Ушли на подэкран (карточка операции, список аккаунтов) — запросов нет,
    // но таймер ждёт возврата: back() вкладку заново не загружает. Закрыли
    // дашборд совсем — цикл кончается.
    if (typeof STACK !== 'undefined' && STACK.includes('s-uni-dash') && _udTabCur === 'live') _flSchedule();
    return;
  }
  await _flLoad(true);
}

// Первая загрузка — со скелетом; фоновая — на месте, без мигания экрана.
async function _flLoad(silent) {
  const body = document.getElementById('udBody');
  if (!body) return;
  if (!silent) body.innerHTML = UI.skeleton(6);
  try {
    const d = await api('/api/miniapp/dashboard/live');
    if (!_flVisible() && silent) return;
    const html = _flRender(d, _flPrev);
    if (typeof _udTabCur !== 'undefined' && _udTabCur !== 'live') return;
    body.innerHTML = html;
    _flPrev = d;
    _flAt = Date.now();
    _flStale = false;
  } catch (e) {
    if (!silent) {
      body.innerHTML = UI.empty('⚠️ ' + errRu(e, 'Не удалось загрузить состояние флота')) +
        '<div class="fl-center">' + UI.button('↺ Повторить', '_flLoad') + '</div>';
      return;
    }
    // Сбой фонового обновления не затирает экран: остаются прежние цифры,
    // а строка состояния честно говорит, что они не свежие.
    _flStale = true;
  } finally {
    _flTickClock();
    _flSchedule();
  }
}

// «Обновлено N сек назад» тикает раз в секунду, пока вкладка видна.
function _flTickClock() {
  clearTimeout(_flTickClock.timer);
  const el = document.getElementById('flClock');
  if (!el) return;
  if (!_flVisible()) {
    // Подэкран поверх дашборда — часы ждут возврата, не трогая разметку.
    if (typeof STACK !== 'undefined' && STACK.includes('s-uni-dash') && _udTabCur === 'live')
      _flTickClock.timer = setTimeout(_flTickClock, 1000);
    return;
  }
  const dot = document.getElementById('flDot');
  if (dot) dot.className = 'fl-dot' + (_flStale ? ' is-stale' : (_flAutoOn() ? ' is-live' : ''));
  if (_flStale) {
    el.textContent = 'не обновилось — данные прежние';
  } else if (_flAt) {
    const s = Math.max(0, Math.round((Date.now() - _flAt) / 1000));
    el.textContent = s < 5 ? 'обновлено только что'
      : s < 60 ? 'обновлено ' + s + ' сек назад'
      : 'обновлено ' + Math.floor(s / 60) + ' мин назад';
  }
  _flTickClock.timer = setTimeout(_flTickClock, 1000);
}

// ── Рендер ──────────────────────────────────────────────────────────────────
function _flN(v) { return UI.num(v || 0); }

// Число, которое изменилось с прошлого снимка, коротко подсвечивается и
// получает стрелку: глазу видно, что именно сдвинулось за эти 20 секунд.
function _flVal(cur, prev, worseUp) {
  const c = Number(cur) || 0;
  let cls = 'fl-v';
  let mark = '';
  if (prev !== undefined && prev !== null && Number(prev) !== c) {
    const up = c > Number(prev);
    const bad = worseUp ? up : !up;
    cls += ' fl-bump ' + (bad ? 'is-worse' : 'is-better');
    mark = '<span class="fl-arrow">' + (up ? '▲' : '▼') + '</span>';
  }
  return '<span class="' + cls + '">' + _flN(c) + '</span>' + mark;
}

function _flRing(score) {
  const has = score !== null && score !== undefined;
  const v = has ? Math.max(0, Math.min(100, Number(score) || 0)) : 0;
  const R = 34, C = 2 * Math.PI * R, len = C * v / 100;
  const tone = !has ? 'is-none' : v >= 70 ? 'is-good' : v >= 40 ? 'is-warn' : 'is-bad';
  return '<svg class="fl-ring ' + tone + '" viewBox="0 0 84 84" aria-hidden="true">' +
    '<circle class="fl-ring-bg" cx="42" cy="42" r="' + R + '"/>' +
    '<circle class="fl-ring-fg" cx="42" cy="42" r="' + R + '" stroke-dasharray="' +
      len.toFixed(1) + ' ' + (C - len).toFixed(1) + '" transform="rotate(-90 42 42)"/>' +
    '<text x="42" y="47" text-anchor="middle" class="fl-ring-txt">' + (has ? v : '—') + '</text></svg>';
}

// Полоска прогресса — SVG-атрибутами, без inline-стилей.
function _flBar(done, total) {
  const pct = total > 0 ? Math.max(0, Math.min(100, done / total * 100)) : 0;
  return '<svg class="fl-bar" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true">' +
    '<rect class="fl-bar-bg" width="100" height="6" rx="3"/>' +
    (total > 0 ? '<rect class="fl-bar-fg" width="' + pct.toFixed(1) + '" height="6" rx="3"/>'
               : '<rect class="fl-bar-run" width="30" height="6" rx="3"/>') + '</svg>';
}

// Состав флота одной полосой: в строю / кулдаун / ограничены / выбыли.
function _flMix(f) {
  const parts = [
    ['is-good', f.online], ['is-warn', f.cooldown],
    ['is-restr', f.restricted], ['is-bad', (f.lost || 0) + (f.session || 0)],
  ];
  const tot = parts.reduce((s, p) => s + (p[1] || 0), 0);
  if (!tot) return '';
  let x = 0;
  const rects = parts.filter(p => p[1] > 0).map(p => {
    const w = p[1] / tot * 100;
    const r = '<rect class="fl-mix-' + p[0] + '" x="' + x.toFixed(2) + '" width="' + w.toFixed(2) + '" height="8"/>';
    x += w;
    return r;
  }).join('');
  return '<svg class="fl-mix" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true">' + rects + '</svg>';
}

function _flTile(ico, label, val, prev, worseUp, tone, fn, ...args) {
  const oc = fn ? ' onclick="' + UI.call(fn, ...args) + '" role="button" tabindex="0"' : '';
  return '<div class="fl-tile ' + (tone || '') + '"' + oc + '>' +
    '<div class="fl-tile-lbl">' + UI.esc(ico) + ' ' + UI.esc(label) + '</div>' +
    '<div class="fl-tile-val">' + _flVal(val, prev, worseUp) + '</div></div>';
}

function _flVerdict(score, f, crit) {
  if (!f || !f.total) return 'Аккаунтов пока нет — добавьте первый, и здесь появится живая картина флота.';
  if (crit > 0) return 'За сутки ограничения получили аккаунтов: ' + crit + '. Посмотрите ленту ниже.';
  if (score === null || score === undefined) return 'Пульс ещё считается.';
  if (score >= 70) return 'Флот в форме: можно запускать работу.';
  if (score >= 40) return 'Есть риски: часть аккаунтов лучше поберечь.';
  return 'Флоту плохо: разберите карантин и ограничения прежде чем запускать новое.';
}

function _flTitle(o) {
  return o.label || ((typeof opRu === 'function') ? opRu(o.op_type) : '') || 'Операция';
}

function _flAgo(ts) { return (typeof ago === 'function') ? ago(ts) : ''; }

const _FL_OP_STATE = {
  running: '▶ идёт', pending: '⏳ в очереди', scheduled: '🕒 запустится позже',
  waiting_approval: '🔐 ждёт подтверждения', paused: '⏸ на паузе',
};
const _FL_OP_END = {
  done: '✅', partial: '⚠️', failed: '❌', cancelled: '🚫',
};
const _FL_OP_END_TXT = {
  done: 'завершена', partial: 'завершена частично', failed: 'упала', cancelled: 'отменена',
};

function _flRender(d, prev) {
  d = d || {};
  const p = prev || {};
  const f = d.fleet, pf = p.fleet || {};
  const pu = d.pulse || {}, ppu = p.pulse || {};
  const ops = d.ops || {}, pops = p.ops || {};
  const score = pu.score;

  // 1. Строка «в эфире»
  const live = '<div class="fl-live">' +
    '<span id="flDot" class="fl-dot"></span>' +
    '<span id="flClock" class="fl-clock">обновлено только что</span>' +
    '<label class="fl-auto"><input type="checkbox"' + (_flAutoOn() ? ' checked' : '') +
    ' onchange="flSetAuto(this.checked)"> авто</label>' +
    '<span class="ui-link" role="button" tabindex="0" onclick="_flLoad()">⟳</span></div>';

  // 2. Герой: здоровье + вердикт
  const total = f ? f.total : 0;
  const hero = '<div class="fl-hero">' +
    '<div class="fl-hero-ring" onclick="' + UI.call('openHealth') + '" role="button" tabindex="0">' + _flRing(score) + '</div>' +
    '<div class="fl-hero-body">' +
      '<div class="fl-hero-title">Здоровье флота</div>' +
      '<div class="fl-hero-sub">' + UI.esc(_flVerdict(score, f, d.critical_24h || 0)) + '</div>' +
      (f ? '<div class="fl-hero-meta">В строю ' + _flVal(f.online, pf.online, false) +
        ' из ' + _flN(total) + (d.critical_24h ? ' · <span class="fl-bad">🚨 ' + _flN(d.critical_24h) + ' за сутки</span>' : '') +
        '</div>' : '') +
      (f ? _flMix(f) : '') +
    '</div></div>';

  // 3. Плитки статусов — каждая открывает свой срез
  const tiles = f ? '<div class="fl-tiles">' +
    _flTile('🟢', 'В строю', f.online, pf.online, false, 'is-good', 'healthGoAccounts', 'active') +
    _flTile('🔥', 'Прогрев', f.warming, pf.warming, null, 'is-info', 'openWarmup') +
    _flTile('⏳', 'Кулдаун', f.cooldown, pf.cooldown, true, 'is-warn', 'healthGoAccounts', 'cooldown') +
    _flTile('🛡', 'Карантин', pu.quarantine || 0, ppu.quarantine, true, 'is-warn', 'openHealth') +
    _flTile('🚫', 'Спамблок', f.restricted, pf.restricted, true, 'is-restr', 'healthGoAccounts', 'spamblock') +
    _flTile('⛔', 'Бан и сессии', (f.lost || 0) + (f.session || 0),
            (pf.lost === undefined ? undefined : (pf.lost || 0) + (pf.session || 0)), true, 'is-bad',
            'healthGoAccounts', 'dead') +
    '</div>' : UI.empty('Состояние аккаунтов сейчас не прочиталось — остальное ниже.');

  // 4. Требует внимания: первые три карточки с их действиями
  const att = d.attention;
  let attHtml = '';
  if (att && att.total) {
    // Кнопки работают через _udAct (dashboard.js), он читает _UD_ITEMS.
    if (typeof _UD_ITEMS !== 'undefined') _UD_ITEMS = att.items || [];
    const cards = (att.items || []).map((it, i) => UI.alert({
      severity: it.severity, title: it.title, detail: it.detail,
      buttonHtml: it.action
        ? '<button id="udBtn' + i + '" class="ui-btn" onclick="' + UI.call('_udAct', i) + '">' +
          UI.esc(it.action.label || 'Действие') + '</button>' : '',
    })).join('');
    attHtml = UI.block('⚠️ Требует внимания' + (att.high ? ' · срочного ' + att.high : ''), cards,
      att.total > (att.items || []).length
        ? { label: 'Все ' + att.total + ' ›', fn: '_udTab', args: ['attention'] } : null);
  } else if (att) {
    attHtml = UI.block('⚠️ Требует внимания', UI.ok('Срочного нет: упавших операций, мёртвых прокси и молчащих ботов не видно.'));
  }

  // 5. Операции сейчас
  const act = d.active || [];
  const opsHead = '<div class="fl-ops-head">' +
    '<span>▶ ' + _flVal(ops.running, pops.running, null) + ' идут</span>' +
    '<span>⏳ ' + _flVal(ops.queued, pops.queued, null) + ' в очереди</span>' +
    (ops.paused ? '<span>⏸ ' + _flVal(ops.paused, pops.paused, null) + ' на паузе</span>' : '') +
    '<span>✅ ' + _flVal(ops.done_24h, pops.done_24h, false) + ' за сутки</span>' +
    (ops.failed_24h ? '<span class="fl-bad">❌ ' + _flVal(ops.failed_24h, pops.failed_24h, true) + ' не доведены</span>' : '') +
    '</div>';
  const opRows = act.length ? act.map(o => {
    const prog = o.total > 0 ? _flN(o.done) + ' / ' + _flN(o.total) : '';
    return '<div class="fl-op" onclick="' + UI.call('openOpDetail', o.id) + '" role="button" tabindex="0">' +
      '<div class="fl-op-top"><span class="fl-op-name">' + UI.esc(_flTitle(o)) + '</span>' +
      '<span class="fl-op-st">' + UI.esc(_FL_OP_STATE[o.status] || o.status) + '</span></div>' +
      (o.status === 'running' ? _flBar(o.done, o.total) : '') +
      '<div class="fl-op-sub">' + UI.esc([prog, o.since ? 'начало: ' + _flAgo(o.since) : ''].filter(Boolean).join(' · ')) + '</div>' +
      '</div>';
  }).join('') : UI.empty('Сейчас ничего не выполняется.');
  const opBtns = [];
  if ((ops.running || 0) + (ops.queued || 0) > 0) opBtns.push(UI.button('⏸ Пауза очереди', 'flQueue', 'pause'));
  if (ops.paused) opBtns.push(UI.button('▶ Возобновить', 'flQueue', 'resume'));
  if (ops.failed_24h) opBtns.push(UI.button('↺ Повторить недоведённые', 'flQueue', 'retry'));
  const opsHtml = UI.block('⚙️ Операции сейчас', opsHead + opRows +
    (opBtns.length ? '<div class="fl-btns">' + opBtns.join('') + '</div>' : ''),
    { label: 'Все операции ›', fn: 'openOps' });

  // 6. Лента событий за сутки
  const ev = d.events || [];
  const evHtml = UI.block('🕑 События за сутки', ev.length ? '<div class="fl-feed">' + ev.map(e => {
    let ico, text, fn, args;
    if (e.kind === 'op') {
      ico = _FL_OP_END[e.status] || '•';
      text = _flTitle(e) + ' — ' + (_FL_OP_END_TXT[e.status] || e.status) +
        (e.total > 0 ? ' · ' + _flN(e.done) + '/' + _flN(e.total) : '');
      fn = 'openOpDetail'; args = [e.op_id];
    } else {
      ico = e.severity === 'bad' ? '🚨' : e.severity === 'warn' ? '⚠️' : 'ℹ️';
      text = e.title + (e.account ? ' — ' + e.account : '');
      fn = 'openHealth'; args = [];
    }
    return '<div class="fl-ev is-' + UI.esc(e.severity || 'info') + '" onclick="' + UI.call(fn, ...args) + '" role="button" tabindex="0">' +
      '<span class="fl-ev-ico">' + ico + '</span><span class="fl-ev-txt">' + UI.esc(text) + '</span>' +
      '<span class="fl-ev-at">' + UI.esc(_flAgo(e.at)) + '</span></div>';
  }).join('') + '</div>' : UI.empty('За сутки событий не было: ни завершённых операций, ни ограничений.'));

  // 7. Быстрые действия
  const quick = UI.block('⚡ Быстрые действия', '<div class="fl-quick">' +
    _flQuick('➕', 'Аккаунт', 'openAccImportModal') +
    _flQuick('🔥', 'Прогрев', 'openWarmup') +
    _flQuick('🌐', 'Прокси', 'openProxies') +
    _flQuick('❤️', 'Здоровье', 'openHealth') +
    '</div>');

  return live + hero + tiles + attHtml + opsHtml + evHtml + quick;
}

function _flQuick(ico, label, fn) {
  if (typeof window !== 'undefined' && typeof window[fn] !== 'function') return '';
  return '<div class="fl-q" onclick="' + UI.call(fn) + '" role="button" tabindex="0">' +
    '<div class="fl-q-ico">' + UI.esc(ico) + '</div><div class="fl-q-lbl">' + UI.esc(label) + '</div></div>';
}

// Пауза / возобновление / повтор — те же ручки и те же формулировки, что у
// экрана «Операции», но после ответа обновляется эта вкладка, а не список
// операций, которого на экране нет.
async function flQueue(kind) {
  const ep = { pause: 'pause', resume: 'resume', retry: 'retry_failed' }[kind];
  if (!ep) return;
  if (kind === 'retry' && typeof askConfirm === 'function' &&
      !await askConfirm('Перезапустить все недоведённые операции за последнюю неделю?\nПовтор уважает дедуп: уже обработанные цели заново не берутся.')) return;
  try {
    const d = await api('/api/miniapp/operations/' + ep, { method: 'POST' });
    let t;
    if (kind === 'pause') {
      const n = d.paused || 0, m = d.pause_requested || 0;
      t = (n || m) ? '⏸ Приостановлено: ' + n + (m ? '; идущие (' + m + ') встанут после текущего прохода' : '')
                   : 'Нет операций, которые можно приостановить';
    } else if (kind === 'resume') {
      const n = d.resumed || 0;
      t = n ? '▶️ Возобновлено: ' + n : 'Нет приостановленных операций';
    } else {
      t = d.retried ? '↺ ' + (d.note || ('Перезапущено: ' + d.retried)) : (d.note || 'Нечего перезапускать');
    }
    toast(t);
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не получилось'));
  }
  _flLoad(true);
}

// Вернулись во вкладку браузера — сразу свежие цифры, а не через 20 секунд.
try {
  document.addEventListener('visibilitychange', () => {
    if (_flVisible() && _flAutoOn()) _flLoad(true);
  });
} catch (_) {}
