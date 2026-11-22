// ── Экран "Командный центр" (screens/cmdcenter.js) ─────────────────────────
// Визуальный дашборд оператора: аккаунты по статусам (кольцо), здоровье
// прокси-пула (бары), health-гейдж (trust_score), поток операций за 7 дней
// (спарклайн). Данные — один запрос /api/miniapp/dashboard/visual.
//
// Зависит от глобальных хелперов основного <script> index.html (push, api,
// esc, toast). Экран s-cmdcenter создаётся ДИНАМИЧЕСКИ (не трогаем index.html
// HTML — минимум конфликтов с параллельным UI-агентом). Всё по клику
// пользователя, поэтому хелперы к моменту вызова уже существуют.
'use strict';

// Цвета серий (не из CSS-vars — данные должны читаться в любой теме).
// `go` — куда ведёт строка легенды: [фильтр, этап] для списка аккаунтов.
// Пусто — вести некуда, и строка тогда не притворяется кнопкой.
// 'ok' здесь потому, что его пишут в acc_status наравне с 'active': без него
// в легенде вылезало сырое английское «ok».
const CC_STATUS = {
  active:           { c: '#3bb6a6', label: 'Активные',    go: ['active', ''] },
  ok:               { c: '#3bb6a6', label: 'Активные',    go: ['active', ''] },
  warming:          { c: '#38bdf8', label: 'Прогрев',     go: ['all', 'warming'] },
  cooldown:         { c: '#f59e0b', label: 'Кулдаун',     go: ['cooldown', ''] },
  spamblock:        { c: '#fb923c', label: 'Спамблок',    go: ['spamblock', ''] },
  limited:          { c: '#fbbf24', label: 'Ограничены',  go: null },
  banned:           { c: '#ef4444', label: 'Забанены',    go: ['banned', ''] },
  deactivated:      { c: '#94a3b8', label: 'Деактив.',    go: ['dead', ''] },
  session_expired:  { c: '#a78bfa', label: 'Сессия ист.', go: ['dead', ''] },
  // Эти статусы тоже пишутся в acc_status, а в легенде всплывали сырым
  // английским словом («frozen»).
  frozen:           { c: '#7dd3fc', label: 'Заморожены',  go: ['dead', ''] },
  deleted:          { c: '#64748b', label: 'Удалены',     go: ['dead', ''] },
};
function _ccStatus(st) {
  // Незнакомый статус — «Другие», а не служебное имя из базы.
  return CC_STATUS[st] || { c: '#64748b', label: 'Другие', go: null };
}

function _ccEnsureScreen() {
  let el = document.getElementById('s-cmdcenter');
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = 's-cmdcenter';
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="hdr-burger" onclick="back()">‹</div>' +
      '<div class="hdr-title">📊 Командный центр</div>' +
      '<div class="hdr-burger" onclick="openCmdCenter()" title="Обновить">⟳</div>' +
    '</div>' +
    '<div class="sb"><div id="ccBody" style="padding:4px 0">' +
      '<div class="spin-wrap"><div class="spin"></div></div>' +
    '</div></div>';
  // Тот же родитель, что и у остальных экранов — наследуем раскладку/позицию.
  const anchor = document.getElementById('s-more') || document.body;
  anchor.parentNode.appendChild(el);
  return el;
}

async function openCmdCenter() {
  _ccEnsureScreen();
  push('s-cmdcenter');
  const body = document.getElementById('ccBody');
  if (body) body.innerHTML =
    '<div class="spin-wrap"><div class="spin"></div></div>';
  let d;
  try {
    d = await api('/api/miniapp/dashboard/visual');
  } catch (e) {
    // Через общий errHtml: он всегда даёт кнопку выхода. Своя вёрстка ошибки
    // оставляла экран без единой кнопки, а таббар на подэкране скрыт.
    if (body) body.innerHTML = errHtml(errRu(e, 'Командный центр не загрузился'), 'openCmdCenter()');
    return;
  }
  if (body) body.innerHTML = _ccAutoRow() + _ccRender(d);
  _ccSchedule();
}

// ── Автообновление ──
// Командный центр держат открытым, пока идёт кампания, и жали ⟳ каждые
// пару минут. Галочка обновляет данные раз в минуту на месте (без
// спиннера и без шага в истории) и запоминается. Таймер живёт, только пока
// экран сверху и вкладка видна: ушли с экрана — запросы прекращаются.
const _CC_EVERY_MS = 60000;

function _ccAutoOn() {
  const p = (typeof loadFormPrefs === 'function') ? (loadFormPrefs('cmdcenter') || {}) : {};
  return !!p.auto;
}

function _ccOnTop() {
  return typeof STACK !== 'undefined' && STACK.length && STACK[STACK.length - 1] === 's-cmdcenter'
    && !(typeof document.visibilityState === 'string' && document.visibilityState === 'hidden');
}

function _ccAutoRow() {
  const t = new Date().toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'});
  return '<label style="display:flex;gap:8px;align-items:center;font-size:12px;color:var(--hint);margin:0 2px 10px">' +
    '<input type="checkbox" id="ccAuto"' + (_ccAutoOn() ? ' checked' : '') + ' onchange="ccSetAuto(this.checked)">' +
    '<span style="flex:1">Обновлять каждую минуту</span><span id="ccUpdated">обновлено в ' + t + '</span></label>';
}

function ccSetAuto(on) {
  if (typeof saveFormPrefs === 'function') saveFormPrefs('cmdcenter', {auto: !!on});
  _ccSchedule();
}

function _ccSchedule() {
  clearTimeout(_ccSchedule.timer);
  _ccSchedule.timer = null;
  if (!_ccAutoOn()) return;
  _ccSchedule.timer = setTimeout(_ccTick, _CC_EVERY_MS);
}

async function _ccTick() {
  _ccSchedule.timer = null;
  if (!_ccAutoOn() || !_ccOnTop()) return;
  const body = document.getElementById('ccBody');
  try {
    const d = await api('/api/miniapp/dashboard/visual');
    // Пока шёл запрос, могли уйти с экрана или выключить галочку.
    if (body && _ccOnTop() && _ccAutoOn()) body.innerHTML = _ccAutoRow() + _ccRender(d);
  } catch (_) {
    // Сбой фонового обновления не затирает экран: остаются прежние данные,
    // а подпись честно говорит, что они не свежие.
    const u = document.getElementById('ccUpdated');
    if (u) u.textContent = 'не обновилось — данные прежние';
  }
  _ccSchedule();
}

function _ccCard(title, inner) {
  return '<div style="background:var(--card,var(--bg-input));border-radius:14px;' +
    'padding:14px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,.06)">' +
    '<div style="font-weight:700;font-size:13px;margin-bottom:10px;color:var(--fg)">' +
    esc(title) + '</div>' + inner + '</div>';
}

// Кольцевая диаграмма (SVG) из [{value,color}], + подпись по центру.
function _ccDonut(parts, centerTop, centerBot) {
  const total = parts.reduce((s, p) => s + p.value, 0) || 1;
  const R = 52, C = 2 * Math.PI * R;
  let off = 0;
  const segs = parts.filter(p => p.value > 0).map(p => {
    const len = C * (p.value / total);
    const dash = `${len} ${C - len}`;
    const el = `<circle cx="70" cy="70" r="${R}" fill="none" stroke="${p.color}" ` +
      `stroke-width="16" stroke-dasharray="${dash}" stroke-dashoffset="${-off}" ` +
      `transform="rotate(-90 70 70)"/>`;
    off += len;
    return el;
  }).join('');
  return '<svg width="140" height="140" viewBox="0 0 140 140" style="flex:0 0 auto">' +
    `<circle cx="70" cy="70" r="${R}" fill="none" stroke="rgba(128,128,128,.15)" stroke-width="16"/>` +
    segs +
    `<text x="70" y="66" text-anchor="middle" font-size="26" font-weight="800" fill="var(--fg)">${esc(centerTop)}</text>` +
    `<text x="70" y="86" text-anchor="middle" font-size="11" fill="var(--hint)">${esc(centerBot)}</text>` +
    '</svg>';
}

// Горизонтальный бар: доля value от total. `onclick` необязателен — бар без
// перехода остаётся баром и кнопкой не притворяется.
function _ccBar(label, value, total, color, onclick) {
  const pct = total ? Math.round(value / total * 100) : 0;
  const tap = onclick ? ` onclick="${onclick}" role="button" tabindex="0"` : '';
  // Клик по полоске ведёт на разбор, а высота выходила 28 пикселей. Отступ
  // снаружи переносим внутрь: шаг между полосками остаётся тем же, а зона
  // нажатия дорастает до 44.
  return `<div${tap} style="${onclick ? 'cursor:pointer;' : ''}margin:0;` +
    `${onclick ? 'padding:8px 0;min-height:44px;box-sizing:border-box' : 'padding:7px 0'}">` +
    '<div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px">' +
    `<span style="color:var(--hint)">${esc(label)}${onclick ? ' \u203a' : ''}</span>` +
    `<span style="font-weight:700;color:var(--fg)">${Number(value) || 0}</span></div>` +
    '<div style="height:8px;border-radius:6px;background:rgba(128,128,128,.15);overflow:hidden">' +
    `<div style="height:100%;width:${pct}%;background:${color};border-radius:6px"></div></div></div>`;
}

// Полоска прокси открывает парк сразу на своём срезе («Мёртвые» — мёртвые),
// а не общий экран прокси, где срез ещё надо искать.
function _ccPool(filter) {
  return (typeof openProxyPool === 'function') ? "openProxyPool('" + filter + "')" : 'openProxies()';
}

function _ccRender(d) {
  const accounts = d.accounts || [];
  const prx = d.proxies || {};
  const hl = d.health || {};
  const ops = d.ops7d || [];
  const now = d.ops_now || {};

  // ── Аккаунты: кольцо + легенда ──
  // Пустое поле в ответе давало NaN в центре кольца и «undefined» в легенде:
  // для человека это неотличимо от поломки продукта.
  const _n = (x) => Number(x) || 0;
  const accTotal = accounts.reduce((s, a) => s + _n(a.count), 0);
  // 'ok' и 'active' — один и тот же статус в базе, и в легенде они давали две
  // строки «Активные» подряд. Сводим по подписи.
  const merged = [];
  accounts.forEach(a => {
    const st = _ccStatus(a.status);
    const same = merged.find(m => m.label === st.label);
    if (same) { same.count += _n(a.count); return; }
    merged.push({ status: a.status, label: st.label, c: st.c, go: st.go, count: _n(a.count) });
  });
  const donutParts = merged.map(a => ({ value: a.count, color: a.c }));
  // Число в легенде — это срез списка аккаунтов, а не просто цифра: строка
  // открывает именно его. Где среза нет (статус без фильтра), строка остаётся
  // строкой, а не притворяется кнопкой.
  const legend = merged.length
    ? merged.map(st => {
        const a = st;
        const tap = st.go
          ? ` onclick="healthGoAccounts('${st.go[0]}','${st.go[1]}')" role="button" tabindex="0"`
          : '';
        const cur = st.go ? 'cursor:pointer;' : '';
        // Зона нажатия, а не только вид: строка легенды открывает срез
        // списка аккаунтов, а выходила 23 пикселя высотой — на телефоне в
        // такую не попасть. Отступ снаружи переносим внутрь, чтобы шаг между
        // строками не изменился, и ставим min-height как у соседнего
        // композера («Вставить шаблон» в кампании).
        return `<div${tap} style="${cur}display:flex;align-items:center;gap:6px;font-size:12px;margin:0;padding:9px 0;min-height:34px;box-sizing:border-box">` +
          `<span style="width:10px;height:10px;border-radius:3px;background:${st.c};flex:0 0 auto"></span>` +
          `<span style="color:var(--hint);flex:1">${esc(st.label)}</span>` +
          `<span style="font-weight:700;color:var(--fg)">${a.count}</span>` +
          (st.go ? '<span style="color:var(--hint)">\u203a</span>' : '') + '</div>';
      }).join('')
    : '<div style="color:var(--hint);font-size:12px">Нет аккаунтов</div>';
  // Запрос считает только is_active=TRUE — подпись «всего» обещала больше,
  // чем показывает: отключённые аккаунты в кольцо не попадают вовсе.
  const accCard = _ccCard('👤 Аккаунты по статусам',
    '<div style="display:flex;align-items:center;gap:12px">' +
    `<div onclick="healthGoAccounts('all','')" role="button" tabindex="0" style="cursor:pointer">` +
    _ccDonut(donutParts, String(accTotal), 'в работе') + '</div>' +
    `<div style="flex:1;min-width:0">${legend}</div></div>` +
    '<div style="font-size:11px;color:var(--hint);margin-top:8px">Отключённые аккаунты в кольцо не входят. Нажмите на срез — откроется список.</div>');

  // ── Прокси-пул ──
  const pt = prx.total || 0;
  const prxCard = _ccCard('🌐 Прокси-пул',
    _ccBar('Живые', prx.alive || 0, pt, '#3bb6a6', _ccPool('alive')) +
    _ccBar('Мёртвые', prx.dead || 0, pt, '#ef4444', _ccPool('dead')) +
    _ccBar('Назначены аккаунтам', prx.assigned || 0, pt, '#38bdf8', _ccPool('all')) +
    _ccBar('Резервные', prx.backup || 0, pt, '#f59e0b', _ccPool('backup')) +
    '<div style="font-size:11px;color:var(--hint);margin-top:6px">Всего прокси: ' +
    `<b>${pt}</b> · нажмите на строку, чтобы открыть пул</div>`);

  // ── Health-гейдж (trust_score 0–100) ──
  const avg = hl.avg || 0;
  const hTotal = (hl.good || 0) + (hl.warn || 0) + (hl.bad || 0);
  const gcol = avg >= 70 ? '#3bb6a6' : avg >= 40 ? '#f59e0b' : '#ef4444';
  const gauge = _ccDonut([{ value: avg, color: gcol }, { value: 100 - avg, color: 'rgba(0,0,0,0)' }],
                         String(avg), 'доверие');
  const healthCard = _ccCard('❤️ Здоровье сетки',
    '<div style="display:flex;align-items:center;gap:12px">' +
    '<div onclick="openHealth()" role="button" tabindex="0" style="cursor:pointer">' + gauge + '</div>' +
    '<div style="flex:1">' +
    _ccBar('Здоровы (≥70)', hl.good || 0, hTotal, '#3bb6a6', 'openHealth()') +
    _ccBar('Внимание (40–69)', hl.warn || 0, hTotal, '#f59e0b', 'openHealth()') +
    _ccBar('Риск (<40)', hl.bad || 0, hTotal, '#ef4444', 'openHealth()') +
    '</div></div>');

  // ── Операции за 7 дней (спарклайн-бары) ──
  const maxOps = Math.max(1, ...ops.map(o => _n(o.done) + _n(o.failed)));
  const bars = ops.length
    ? ops.map(o => {
        const hDone = Math.round(_n(o.done) / maxOps * 60);
        const hFail = Math.round(_n(o.failed) / maxOps * 60);
        return '<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:3px">' +
          '<div style="display:flex;flex-direction:column;justify-content:flex-end;height:64px;width:60%">' +
          `<div style="background:#ef4444;height:${hFail}px;border-radius:3px 3px 0 0" title="ошибок ${_n(o.failed)}"></div>` +
          `<div style="background:#3bb6a6;height:${hDone}px;border-radius:${hFail?0:'3px 3px 0 0'}" title="успешно ${_n(o.done)}"></div>` +
          '</div>' +
          `<div style="font-size:10px;color:var(--hint)">${esc(o.day)}</div></div>`;
      }).join('')
    : '<div style="color:var(--hint);font-size:12px">Нет операций за 7 дней</div>';
  const opsCard = _ccCard('⚙️ Операции за 7 дней',
    '<div onclick="openOps()" role="button" tabindex="0" ' +
    `style="cursor:pointer;display:flex;align-items:flex-end;gap:4px;min-height:76px">${bars}</div>` +
    '<div style="display:flex;gap:14px;margin-top:8px;font-size:11px;color:var(--hint)">' +
    '<span><span style="color:#3bb6a6">■</span> успешно</span>' +
    '<span><span style="color:#ef4444">■</span> ошибки</span>' +
    '<span onclick="openOps()" role="button" tabindex="0" style="cursor:pointer;margin-left:auto;display:inline-flex;align-items:center;min-height:32px">' +
    `▶ выполняется: <b style="color:var(--fg)">${now.running || 0}</b> · ` +
    `⏳ в очереди: <b style="color:var(--fg)">${now.pending || 0}</b> \u203a</span></div>`);

  return accCard + healthCard + prxCard + opsCard;
}
