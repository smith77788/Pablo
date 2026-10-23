// Удержание приглашённых: сколько людей осталось в чатах и откуда уходят.
//
// Эндпоинт /api/miniapp/invite/retention существовал без экрана, а сама цифра
// была неправдой: уход человека из чата нигде не записывался, поэтому «ушло»
// всегда приходило нулём и удержание выходило ровно 100% при любом оттоке.
// Запись ухода добавлена в охране чатов (bot/handlers/chat_guard._record_leave).
//
// Честное ограничение, которое экран проговаривает вслух: отток виден только по
// чатам, где наш бот — администратор. Где его нет, уходы Telegram не покажет
// никому.

let _rtDays = 30;
const RT_PERIODS = [[7, '7 дней'], [30, '30 дней'], [90, '90 дней']];

function _rtScreen(id) {
  let el = document.getElementById(id);
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = id;
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="back" onclick="back()" role="button" tabindex="0" aria-label="Назад">←</div>' +
      '<div class="hdr-title">🧲 Удержание приглашённых</div>' +
      '<div class="icon-btn" role="button" tabindex="0" aria-label="Обновить" ' +
        'style="margin-left:auto" onclick="openRetention(_rtDays)">🔄</div>' +
    '</div>' +
    '<div class="sb"><div id="' + id + '-body" style="padding:6px 0">' +
      '<div class="spin-wrap"><div class="spin"></div></div>' +
    '</div></div>';
  document.body.appendChild(el);
  return el;
}

const RT_HEALTH = {
  green: {e: '🟢', t: 'Люди остаются — приглашать можно смелее.'},
  amber: {e: '🟡', t: 'Уходит заметная часть. Приветствие и правила чата стоит пересмотреть до того, как наращивать инвайт.'},
  red:   {e: '🔴', t: 'Уходит больше половины. Наращивать инвайт сейчас — лить людей в дырявое ведро: сначала разберитесь, почему уходят.'},
  unknown: {e: '⚪️', t: 'Приглашений за период не было — считать удержание не на чем.'},
};

function _rtPeriods() {
  return '<div class="seg" style="margin:8px 16px">' + RT_PERIODS.map(function (p) {
    return '<div class="seg-opt' + (p[0] === _rtDays ? ' on' : '') + '" role="button" tabindex="0" ' +
      'onclick="openRetention(' + p[0] + ')">' + p[1] + '</div>';
  }).join('') + '</div>';
}

async function openRetention(days) {
  _rtDays = Number(days) || 30;
  _rtScreen('s-retention');
  push('s-retention');
  const body = document.getElementById('s-retention-body');
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const d = await api('/api/miniapp/invite/retention?days=' + _rtDays);
    const h = RT_HEALTH[d.health] || RT_HEALTH.unknown;
    const pct = d.retention_pct;
    let out = _rtPeriods();
    out += '<div class="kpi" style="padding:0 16px">' +
      '<div class="kpi-card"><div class="kpi-val">' + num(d.joined || 0) + '</div><div class="kpi-lbl">Вступили</div></div>' +
      '<div class="kpi-card"><div class="kpi-val" style="' + kpiTone(d.left || 0, 'red') + '">' + num(d.left || 0) +
        '</div><div class="kpi-lbl">Ушли</div></div>' +
      '<div class="kpi-card"><div class="kpi-val">' + (pct == null ? '—' : pct + '%') +
        '</div><div class="kpi-lbl">Осталось</div></div></div>';
    out += '<div style="margin:8px 16px 0;background:var(--bg2);border-radius:10px;padding:10px 12px;' +
      'font-size:12px;line-height:1.45">' + h.e + ' ' + h.t + '</div>';
    const rows = d.by_chat || [];
    if (rows.length) {
      out += '<div class="sec">Откуда уходят</div><div class="lst" style="margin:0 16px">';
      out += rows.map(function (r) {
        const nm = r.title || ('Чат ' + r.chat_id);
        return '<div class="row"><div class="row-body">' +
          '<div class="row-name">' + esc(nm) + '</div>' +
          '<div class="row-val" style="color:var(--red)">ушло ' + num(r.left) + '</div>' +
          '</div></div>';
      }).join('');
      out += '</div>';
      out += '<div style="margin:8px 16px 0;font-size:11px;color:var(--hint);line-height:1.4">' +
        'Приветствие, правила и капчу для чата настраивает «Модератор чатов»: команда /guard в самом чате.</div>';
    } else if (d.joined) {
      out += '<div style="margin:8px 16px 0;font-size:12px;color:var(--hint);line-height:1.45">' +
        'Уходов за период не зафиксировано. Важно: отток виден только по чатам, где наш бот — ' +
        'администратор. Если бота в чате нет, Telegram об уходах не сообщает никому.</div>';
    }
    out += '<div style="display:flex;gap:8px;padding:12px 16px 20px">' +
      '<button class="btn btn-s" style="flex:1;font-size:12px;padding:10px 6px" onclick="openMassInvite()">👥 Масс-инвайт</button>' +
      '<button class="btn btn-s" style="flex:1;font-size:12px;padding:10px 6px" onclick="openDigest()">📈 Пульс-отчёт</button>' +
      '</div>';
    body.innerHTML = out;
  } catch (e) {
    body.innerHTML = errHtml(errRu(e), 'openRetention(' + _rtDays + ')');
  }
}
