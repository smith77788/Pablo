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
const CC_STATUS = {
  active:           { c: '#2dd4bf', label: 'Активные' },
  warming:          { c: '#38bdf8', label: 'Прогрев' },
  cooldown:         { c: '#f59e0b', label: 'Кулдаун' },
  spamblock:        { c: '#fb923c', label: 'Спамблок' },
  limited:          { c: '#fbbf24', label: 'Ограничены' },
  banned:           { c: '#ef4444', label: 'Забанены' },
  deactivated:      { c: '#94a3b8', label: 'Деактив.' },
  session_expired:  { c: '#a78bfa', label: 'Сессия ист.' },
};
function _ccStatus(st) {
  return CC_STATUS[st] || { c: '#64748b', label: st };
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
    if (body) body.innerHTML = errHtml((e && e.message) || 'Ошибка загрузки', 'openCmdCenter()');
    return;
  }
  if (body) body.innerHTML = _ccRender(d);
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

// Горизонтальный бар: доля value от total.
function _ccBar(label, value, total, color) {
  const pct = total ? Math.round(value / total * 100) : 0;
  return '<div style="margin:7px 0">' +
    '<div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px">' +
    `<span style="color:var(--hint)">${esc(label)}</span>` +
    `<span style="font-weight:700;color:var(--fg)">${value}</span></div>` +
    '<div style="height:8px;border-radius:6px;background:rgba(128,128,128,.15);overflow:hidden">' +
    `<div style="height:100%;width:${pct}%;background:${color};border-radius:6px"></div></div></div>`;
}

function _ccRender(d) {
  const accounts = d.accounts || [];
  const prx = d.proxies || {};
  const hl = d.health || {};
  const ops = d.ops7d || [];
  const now = d.ops_now || {};

  // ── Аккаунты: кольцо + легенда ──
  const accTotal = accounts.reduce((s, a) => s + a.count, 0);
  const donutParts = accounts.map(a => ({ value: a.count, color: _ccStatus(a.status).c }));
  const legend = accounts.length
    ? accounts.map(a => {
        const st = _ccStatus(a.status);
        return '<div style="display:flex;align-items:center;gap:6px;font-size:12px;margin:3px 0">' +
          `<span style="width:10px;height:10px;border-radius:3px;background:${st.c};flex:0 0 auto"></span>` +
          `<span style="color:var(--hint);flex:1">${esc(st.label)}</span>` +
          `<span style="font-weight:700;color:var(--fg)">${a.count}</span></div>`;
      }).join('')
    : '<div style="color:var(--hint);font-size:12px">Нет аккаунтов</div>';
  const accCard = _ccCard('👤 Аккаунты по статусам',
    '<div style="display:flex;align-items:center;gap:12px">' +
    _ccDonut(donutParts, String(accTotal), 'всего') +
    `<div style="flex:1;min-width:0">${legend}</div></div>`);

  // ── Прокси-пул ──
  const pt = prx.total || 0;
  const prxCard = _ccCard('🌐 Прокси-пул',
    _ccBar('Живые', prx.alive || 0, pt, '#2dd4bf') +
    _ccBar('Мёртвые', prx.dead || 0, pt, '#ef4444') +
    _ccBar('Назначены аккаунтам', prx.assigned || 0, pt, '#38bdf8') +
    _ccBar('Резервные', prx.backup || 0, pt, '#f59e0b') +
    `<div style="font-size:11px;color:var(--hint);margin-top:6px">Всего прокси: <b>${pt}</b></div>`);

  // ── Health-гейдж (trust_score 0–100) ──
  const avg = hl.avg || 0;
  const gcol = avg >= 70 ? '#2dd4bf' : avg >= 40 ? '#f59e0b' : '#ef4444';
  const gauge = _ccDonut([{ value: avg, color: gcol }, { value: 100 - avg, color: 'rgba(0,0,0,0)' }],
                         String(avg), 'trust');
  const healthCard = _ccCard('❤️ Здоровье сетки',
    '<div style="display:flex;align-items:center;gap:12px">' + gauge +
    '<div style="flex:1">' +
    _ccBar('Здоровы (≥70)', hl.good || 0, (hl.good || 0) + (hl.warn || 0) + (hl.bad || 0), '#2dd4bf') +
    _ccBar('Внимание (40–69)', hl.warn || 0, (hl.good || 0) + (hl.warn || 0) + (hl.bad || 0), '#f59e0b') +
    _ccBar('Риск (<40)', hl.bad || 0, (hl.good || 0) + (hl.warn || 0) + (hl.bad || 0), '#ef4444') +
    '</div></div>');

  // ── Операции за 7 дней (спарклайн-бары) ──
  const maxOps = Math.max(1, ...ops.map(o => o.done + o.failed));
  const bars = ops.length
    ? ops.map(o => {
        const hDone = Math.round((o.done) / maxOps * 60);
        const hFail = Math.round((o.failed) / maxOps * 60);
        return '<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:3px">' +
          '<div style="display:flex;flex-direction:column;justify-content:flex-end;height:64px;width:60%">' +
          `<div style="background:#ef4444;height:${hFail}px;border-radius:3px 3px 0 0" title="ошибок ${o.failed}"></div>` +
          `<div style="background:#2dd4bf;height:${hDone}px;border-radius:${hFail?0:'3px 3px 0 0'}" title="успешно ${o.done}"></div>` +
          '</div>' +
          `<div style="font-size:10px;color:var(--hint)">${esc(o.day)}</div></div>`;
      }).join('')
    : '<div style="color:var(--hint);font-size:12px">Нет операций за 7 дней</div>';
  const opsCard = _ccCard('⚙️ Операции за 7 дней',
    `<div style="display:flex;align-items:flex-end;gap:4px;min-height:76px">${bars}</div>` +
    '<div style="display:flex;gap:14px;margin-top:8px;font-size:11px;color:var(--hint)">' +
    '<span><span style="color:#2dd4bf">■</span> успешно</span>' +
    '<span><span style="color:#ef4444">■</span> ошибки</span>' +
    `<span style="margin-left:auto">▶ выполняется: <b style="color:var(--fg)">${now.running || 0}</b> · ` +
    `⏳ в очереди: <b style="color:var(--fg)">${now.pending || 0}</b></span></div>`);

  return accCard + healthCard + prxCard + opsCard;
}
