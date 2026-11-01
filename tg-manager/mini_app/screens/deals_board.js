// ── Канбан сделок (screens/deals_board.js) ─────────────────────────────────────
// Воронка контактов по стадиям: колонка на стадию, карточка на контакт.
// Стадии — contact_crm.stage, тот же список, что у чипов хаба контактов.
//
// Зачем экран. Воронку можно было только отфильтровать: шесть чипов в хабе,
// каждый открывает свой срез. Ответа на главный вопрос продавца «где сейчас мои
// сделки и сколько в них денег» не было ни на одном экране. У amoCRM, Bitrix24 и
// Pipedrive это первый экран.
//
// Перетаскивания нет намеренно: внутри Telegram горизонтальная прокрутка доски и
// жест перетаскивания спорят за один и тот же палец. Карточка двигается тапом:
// «→» на карточке переводит на следующую стадию, тап по карточке открывает
// выбор любой стадии и карточку клиента.
//
// Данные: GET /api/miniapp/crm/board (вся доска, по странице в колонке) и
// GET /api/miniapp/crm/board?stage=…&offset=… (ещё страница одной колонки).
// Смена стадии — POST /api/miniapp/uch/contacts/{id}/crm, та же ручка, что
// у карточки клиента: одна проверка стадии и один журнал.
//
// Экран создаётся динамически; зависит от глобальных хелперов index.html
// (api, push, esc, num, ago, empty, errHtml, toast, closeModal).
'use strict';

const DK_NEXT = {lead: 'contact', contact: 'proposal', proposal: 'negotiation', negotiation: 'won'};
const DK_TONE = {
  lead: 'var(--bg-blue-16)', contact: 'var(--bg-violet-16)', proposal: 'var(--bg-yellow-16)',
  negotiation: 'var(--bg-orange-16)', won: 'var(--bg-green-16)', lost: 'var(--bg-red-16)',
};
const DK_UUID = /^[0-9a-f-]{36}$/i;

let _DK = null;          // ответ доски: {columns, total, reminders_7d, per_stage}
let _DK_BUSY = {};       // contact_id → идёт смена стадии (двойной тап не шлёт два запроса)

function _dkStyles() {
  if (document.getElementById('dk-style')) return;
  const st = document.createElement('style');
  st.id = 'dk-style';
  st.textContent =
    '.dk-board{display:flex;gap:10px;overflow-x:auto;padding:6px 16px 16px;' +
      'scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;overscroll-behavior-x:contain}' +
    '.dk-col{flex:0 0 82%;max-width:300px;scroll-snap-align:start;background:var(--bg2);' +
      'border:1px solid var(--border);border-radius:var(--radius-lg);display:flex;flex-direction:column;min-height:120px}' +
    '@media (min-width:720px){.dk-col{flex:0 0 260px}}' +
    '.dk-colhdr{padding:10px 12px 8px;border-bottom:1px solid var(--sep)}' +
    '.dk-colname{font-size:13px;font-weight:700;display:flex;align-items:center;gap:6px}' +
    '.dk-cnt{font-size:11px;font-weight:600;color:var(--hint);background:var(--bg-input);border-radius:10px;padding:1px 7px}' +
    '.dk-sum{font-size:11px;color:var(--hint);margin-top:3px;min-height:14px}' +
    '.dk-cards{padding:8px;display:flex;flex-direction:column;gap:8px}' +
    '.dk-card{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);padding:9px 10px;cursor:pointer;position:relative}' +
    '.dk-card:active{opacity:.75}' +
    '.dk-card.busy{opacity:.5;pointer-events:none}' +
    '.dk-name{font-size:14px;font-weight:600;padding-right:34px;overflow-wrap:anywhere}' +
    '.dk-sub{font-size:12px;color:var(--hint);margin-top:2px;overflow-wrap:anywhere}' +
    '.dk-val{font-size:13px;font-weight:600;margin-top:4px}' +
    '.dk-rem{font-size:11px;margin-top:4px;color:var(--hint)}' +
    '.dk-rem.due{color:var(--red);font-weight:600}' +
    '.dk-next{position:absolute;top:5px;right:5px;width:32px;height:32px;border-radius:50%;border:1px solid var(--border);' +
      'background:var(--bg2);color:var(--accent);display:flex;align-items:center;justify-content:center;font-size:14px;cursor:pointer}' +
    '.dk-empty{font-size:12px;color:var(--hint);text-align:center;padding:14px 6px}' +
    '.dk-stagebtn{display:block;width:100%;margin:0 0 8px;text-align:left}';
  document.head.appendChild(st);
}

function _dkScreen() {
  let el = document.getElementById('s-deals');
  if (el) return el;
  _dkStyles();
  el = document.createElement('div');
  el.className = 'screen';
  el.id = 's-deals';
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="back" onclick="back()" role="button" tabindex="0" aria-label="Назад">←</div>' +
      '<div class="hdr-title">🗂 Сделки</div>' +
      '<div class="icon-btn" role="button" tabindex="0" aria-label="Обновить" ' +
        'style="margin-left:auto" onclick="loadDealsBoard()">🔄</div>' +
    '</div>' +
    '<div class="sub-sb"><div id="dkTop"></div><div id="dkBody">' +
      '<div class="spin-wrap"><div class="spin"></div></div>' +
    '</div></div>';
  document.body.appendChild(el);
  // Окно выбора стадии — одно на экран, наполняется под карточку.
  const mo = document.createElement('div');
  mo.className = 'mbg';
  mo.id = 'mo-dk-card';
  mo.setAttribute('onclick', "if(event.target===this)closeModal('mo-dk-card')");
  mo.innerHTML =
    '<div class="modal">' +
      '<div class="modal-x" onclick="closeModal(\'mo-dk-card\')" role="button" tabindex="0" aria-label="Закрыть">✕</div>' +
      '<div class="modal-title" id="dkCardTitle">Сделка</div>' +
      '<div id="dkCardBody"></div>' +
    '</div>';
  document.body.appendChild(mo);
  return el;
}

async function openDeals() {
  _dkScreen();
  push('s-deals');
  await loadDealsBoard();
}

async function loadDealsBoard() {
  const body = document.getElementById('dkBody');
  if (!body) return;
  if (!_DK) body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    _DK = await api('/api/miniapp/crm/board');
    _dkRender();
  } catch (e) {
    body.innerHTML = errHtml(errRu(e), 'loadDealsBoard()');
  }
}

// Деньги колонки. Валюты не складываются: «итого» из рублей с долларами было бы
// красивым и неверным. Одна валюта — сумма, несколько — каждая отдельно.
function _dkMoney(byCur) {
  const parts = Object.entries(byCur || {}).filter(function (p) { return Number(p[1]) > 0; });
  return parts.map(function (p) { return num(Math.round(Number(p[1]))) + ' ' + esc(p[0]); }).join(' · ');
}

function _dkCard(c) {
  if (!DK_UUID.test(c.contact_id)) return '';
  const nxt = DK_NEXT[c.stage];
  const lbl = nxt ? _dkLabel(nxt) : '';
  const sub = [c.company, c.username ? '@' + c.username : ''].filter(Boolean).map(esc).join(' · ');
  let rem = '';
  if (c.next_reminder_at) {
    const due = new Date(c.next_reminder_at) <= new Date();
    rem = '<div class="dk-rem' + (due ? ' due' : '') + '">⏰ ' +
      (due ? 'Пора: ' : '') + esc(c.next_reminder_text || 'напоминание') + '</div>';
  } else if (c.last_message_preview) {
    rem = '<div class="dk-rem">💬 ' + esc(c.last_message_preview) + '</div>';
  }
  const when = c.last_interaction_at || c.updated_at;
  return '<div class="dk-card' + (_DK_BUSY[c.contact_id] ? ' busy' : '') + '" id="dk-' + c.contact_id + '" ' +
      'role="button" tabindex="0" onclick="dkOpenCard(\'' + c.contact_id + '\')">' +
    (nxt ? '<div class="dk-next" style="min-height:32px" role="button" tabindex="0" aria-label="На стадию ' + esc(lbl) + '" ' +
      'title="→ ' + esc(lbl) + '" onclick="event.stopPropagation();dkMove(\'' + c.contact_id + '\',\'' + nxt + '\')">→</div>' : '') +
    '<div class="dk-name">' + esc(c.name) + '</div>' +
    (sub ? '<div class="dk-sub">' + sub + '</div>' : '') +
    (c.deal_value ? '<div class="dk-val">' + num(Math.round(c.deal_value)) + ' ' + esc(c.currency) + '</div>' : '') +
    rem +
    (when ? '<div class="dk-rem">' + esc(ago(when)) + '</div>' : '') +
  '</div>';
}

function _dkLabel(stage) {
  const col = (_DK && _DK.columns || []).find(function (c) { return c.stage === stage; });
  return col ? col.label : stage;
}

function _dkRender() {
  const body = document.getElementById('dkBody');
  const top = document.getElementById('dkTop');
  if (!body || !_DK) return;
  const cols = _DK.columns || [];
  if (!_DK.total) {
    top.innerHTML = '';
    // Пустая доска — не тупик: сделка появляется, когда контакту задана стадия.
    body.innerHTML = empty('🗂', 'Сделок пока нет',
      'Откройте контакт и задайте ему стадию: «Лид», «Предложение» и дальше. Он появится в колонке этой стадии.',
      {label: '📇 Открыть контакты', fn: 'openContacts()'});
    return;
  }
  const open = cols.filter(function (c) { return c.stage !== 'won' && c.stage !== 'lost'; })
    .reduce(function (s, c) { return s + c.count; }, 0);
  top.innerHTML =
    '<div class="kpi-row">' +
      '<div class="kpi-card"><div class="kpi-val">' + num(_DK.total) + '</div><div class="kpi-lbl">Всего</div></div>' +
      '<div class="kpi-card"><div class="kpi-val">' + num(open) + '</div><div class="kpi-lbl">В работе</div></div>' +
      '<div class="kpi-card"><div class="kpi-val" style="' + (_DK.reminders_7d ? 'color:var(--orange)' : '') + '">' +
        num(_DK.reminders_7d || 0) + '</div><div class="kpi-lbl">Напоминаний 7 дн</div></div>' +
    '</div>';
  body.innerHTML = '<div class="dk-board">' + cols.map(function (col) {
    const shown = col.cards.length;
    return '<div class="dk-col" data-stage="' + esc(col.stage) + '">' +
      '<div class="dk-colhdr" style="background:' + (DK_TONE[col.stage] || 'transparent') + ';border-radius:var(--radius-lg) var(--radius-lg) 0 0">' +
        '<div class="dk-colname">' + esc(col.label) + ' <span class="dk-cnt">' + num(col.count) + '</span></div>' +
        '<div class="dk-sum">' + _dkMoney(col.by_currency) + '</div>' +
      '</div>' +
      '<div class="dk-cards">' +
        (shown ? col.cards.map(_dkCard).join('') : '<div class="dk-empty">Пусто</div>') +
        (col.has_more ? '<button class="btn btn-s" style="width:100%" onclick="dkMore(\'' + esc(col.stage) + '\')">' +
          'Показать ещё · ' + num(shown) + ' из ' + num(col.count) + '</button>' : '') +
      '</div>' +
    '</div>';
  }).join('') + '</div>';
}

async function dkMore(stage) {
  const col = (_DK && _DK.columns || []).find(function (c) { return c.stage === stage; });
  if (!col) return;
  try {
    const d = await api('/api/miniapp/crm/board?stage=' + encodeURIComponent(stage) +
      '&offset=' + col.cards.length + '&limit=' + (_DK.per_stage || 30));
    const seen = {};
    col.cards.forEach(function (c) { seen[c.contact_id] = 1; });
    (d.cards || []).forEach(function (c) { if (!seen[c.contact_id]) col.cards.push(c); });
    col.count = d.total != null ? d.total : col.count;
    col.has_more = !!d.has_more;
    _dkRender();
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось загрузить').substring(0, 60));
  }
}

function _dkFind(cid) {
  for (const col of (_DK && _DK.columns) || []) {
    const i = col.cards.findIndex(function (c) { return c.contact_id === cid; });
    if (i >= 0) return {col: col, i: i, card: col.cards[i]};
  }
  return null;
}

function dkOpenCard(cid) {
  if (!DK_UUID.test(cid)) return;
  const f = _dkFind(cid);
  if (!f) return;
  const c = f.card;
  document.getElementById('dkCardTitle').textContent = c.name;
  const stages = (_DK.columns || []).map(function (col) {
    const cur = col.stage === c.stage;
    return '<button class="btn ' + (cur ? 'btn-p' : 'btn-s') + ' dk-stagebtn"' + (cur ? ' disabled' : '') +
      ' onclick="closeModal(\'mo-dk-card\');dkMove(\'' + cid + '\',\'' + esc(col.stage) + '\')">' +
      (cur ? '✓ ' : '→ ') + esc(col.label) + '</button>';
  }).join('');
  document.getElementById('dkCardBody').innerHTML =
    '<button class="btn btn-p" style="width:100%;margin-bottom:12px" ' +
      'onclick="closeModal(\'mo-dk-card\');openContactDetail(\'' + cid + '\')">👤 Открыть карточку клиента</button>' +
    '<div style="font-size:12px;font-weight:600;color:var(--hint);margin:0 0 6px">Перевести на стадию</div>' +
    stages;
  document.getElementById('mo-dk-card').classList.add('show');
}

// Перенос карточки. Сразу на экране (иначе тап казался бы несработавшим),
// с откатом, если сервер не принял: доска не должна показывать то, чего нет.
async function dkMove(cid, stage) {
  if (!DK_UUID.test(cid) || _DK_BUSY[cid]) return;
  const f = _dkFind(cid);
  if (!f || f.card.stage === stage) return;
  const to = (_DK.columns || []).find(function (c) { return c.stage === stage; });
  if (!to) return;
  const from = f.col, card = f.card, prevStage = card.stage;
  const amount = Number(card.deal_value) || 0, cur = card.currency;
  const shift = function (src, dst) {
    src.cards = src.cards.filter(function (c) { return c.contact_id !== cid; });
    src.count = Math.max(0, src.count - 1);
    dst.cards.unshift(card);
    dst.count += 1;
    if (amount) {
      src.by_currency = src.by_currency || {}; dst.by_currency = dst.by_currency || {};
      src.by_currency[cur] = Math.max(0, (src.by_currency[cur] || 0) - amount);
      dst.by_currency[cur] = (dst.by_currency[cur] || 0) + amount;
    }
    src.has_more = src.count > src.cards.length;
  };
  card.stage = stage;
  shift(from, to);
  _DK_BUSY[cid] = 1;
  _dkRender();
  try {
    await api('/api/miniapp/uch/contacts/' + cid + '/crm', {method: 'POST', body: JSON.stringify({stage: stage})});
    try { tg.HapticFeedback?.notificationOccurred(stage === 'lost' ? 'warning' : 'success'); } catch (_) {}
    toast((stage === 'won' ? '🎉 ' : '✅ ') + card.name + ' → ' + to.label);
  } catch (e) {
    card.stage = prevStage;
    shift(to, from);
    try { tg.HapticFeedback?.notificationOccurred('error'); } catch (_) {}
    toast('⚠️ ' + errRu(e, 'Стадия не сохранилась').substring(0, 60));
  } finally {
    delete _DK_BUSY[cid];
    _dkRender();
  }
}
