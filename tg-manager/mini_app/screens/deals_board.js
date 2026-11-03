// ── Доска сделок (screens/deals_board.js) ──────────────────────────────────────
// Все сделки по стадиям: колонка на стадию. В колонке два вида карточек —
// контакт в воронке (contact_crm.stage, тот же список, что у чипов хаба) и
// простая сделка (crm_deals: её создают кнопка «➕ Сделка», сценарии,
// автоответчик и коннекторы CRM). Раньше у простых сделок был свой экран
// «CRM — Сделки» со своими числами и «$» перед любой суммой; теперь доска одна,
// и сумма колонки складывается по валютам из обеих систем.
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
let _DK_BUSY = {};       // contact_id / 'd'+deal_id → идёт смена стадии (двойной тап не шлёт два запроса)

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
      '<div class="hdr-title">💼 Сделки</div>' +
      '<div class="hdr-meta" style="margin-left:auto;display:flex;gap:6px;align-items:center">' +
        '<button class="btn btn-p" style="padding:7px 12px;font-size:13px" onclick="openDealModal()">➕ Сделка</button>' +
        '<div class="icon-btn" role="button" tabindex="0" aria-label="Обновить" onclick="loadDealsBoard()">🔄</div>' +
      '</div>' +
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

// Карточка простой сделки. Ключ — 'd'+id: id сделки целое и не пересекается
// с uuid контакта, но и смешивать их в одном словаре занятости нельзя.
function _dkDealCard(d) {
  const id = parseInt(d.deal_id, 10);
  if (!(id > 0)) return '';
  const nxt = DK_NEXT[d.stage];
  const lbl = nxt ? _dkLabel(nxt) : '';
  return '<div class="dk-card' + (_DK_BUSY['d' + id] ? ' busy' : '') + '" id="dkd-' + id + '" ' +
      'role="button" tabindex="0" onclick="dkOpenDeal(' + id + ')">' +
    (nxt ? '<div class="dk-next" style="min-height:32px" role="button" tabindex="0" aria-label="На стадию ' + esc(lbl) + '" ' +
      'title="→ ' + esc(lbl) + '" onclick="event.stopPropagation();dkMoveDeal(' + id + ',\'' + nxt + '\')">→</div>' : '') +
    '<div class="dk-name">💼 ' + esc(d.title) + '</div>' +
    (d.contact ? '<div class="dk-sub">' + esc(d.contact) + '</div>' : '') +
    (d.value ? '<div class="dk-val">' + num(Math.round(d.value)) + ' ' + esc(d.currency) + '</div>' : '') +
    (d.updated_at ? '<div class="dk-rem">' + esc(ago(d.updated_at)) + '</div>' : '') +
  '</div>';
}

function _dkCount(col) { return (col.count || 0) + (col.deal_count || 0); }

function _dkLabel(stage) {
  const col = (_DK && _DK.columns || []).find(function (c) { return c.stage === stage; });
  return col ? col.label : stage;
}

function _dkRender() {
  const body = document.getElementById('dkBody');
  const top = document.getElementById('dkTop');
  if (!body || !_DK) return;
  const cols = _DK.columns || [];
  const all = (_DK.total || 0) + (_DK.deals_total || 0);
  if (!all) {
    top.innerHTML = '';
    // Пустая доска — не тупик: сделку можно завести кнопкой, а контакт
    // появляется здесь, когда ему задана стадия.
    body.innerHTML = empty('💼', 'Сделок пока нет',
      'Заведите сделку кнопкой «➕ Сделка» или откройте контакт и задайте ему стадию — он появится в колонке этой стадии.',
      {label: '➕ Новая сделка', fn: 'openDealModal()'}) +
      '<div style="padding:0 16px 16px"><button class="btn btn-s" style="width:100%" onclick="openContacts()">📇 Открыть контакты</button></div>';
    return;
  }
  const open = cols.filter(function (c) { return c.stage !== 'won' && c.stage !== 'lost'; })
    .reduce(function (s, c) { return s + _dkCount(c); }, 0);
  top.innerHTML =
    '<div class="kpi-row">' +
      '<div class="kpi-card"><div class="kpi-val">' + num(all) + '</div><div class="kpi-lbl">Всего</div></div>' +
      '<div class="kpi-card"><div class="kpi-val">' + num(open) + '</div><div class="kpi-lbl">В работе</div></div>' +
      '<div class="kpi-card"><div class="kpi-val" style="' + (_DK.reminders_7d ? 'color:var(--orange)' : '') + '">' +
        num(_DK.reminders_7d || 0) + '</div><div class="kpi-lbl">Напоминаний 7 дн</div></div>' +
    '</div>';
  body.innerHTML = '<div class="dk-board">' + cols.map(function (col) {
    col.deals = col.deals || [];
    const shown = col.cards.length + col.deals.length;
    const total = _dkCount(col);
    return '<div class="dk-col" data-stage="' + esc(col.stage) + '">' +
      '<div class="dk-colhdr" style="background:' + (DK_TONE[col.stage] || 'transparent') + ';border-radius:var(--radius-lg) var(--radius-lg) 0 0">' +
        '<div class="dk-colname">' + esc(col.label) + ' <span class="dk-cnt">' + num(total) + '</span></div>' +
        '<div class="dk-sum">' + _dkMoney(col.by_currency) + '</div>' +
      '</div>' +
      '<div class="dk-cards">' +
        (shown ? col.cards.map(_dkCard).join('') + col.deals.map(_dkDealCard).join('')
               : '<div class="dk-empty">Пусто</div>') +
        ((col.has_more || col.deals_has_more) ? '<button class="btn btn-s" style="width:100%" onclick="dkMore(\'' + esc(col.stage) + '\')">' +
          'Показать ещё · ' + num(shown) + ' из ' + num(total) + '</button>' : '') +
      '</div>' +
    '</div>';
  }).join('') + '</div>';
}

// «Показать ещё» в колонке добирает оба вида карточек: у контактов и у
// простых сделок своё смещение, и каждая половина листается до своего конца.
async function dkMore(stage) {
  const col = (_DK && _DK.columns || []).find(function (c) { return c.stage === stage; });
  if (!col) return;
  const per = _DK.per_stage || 30;
  try {
    const [d, dd] = await Promise.all([
      col.has_more ? api('/api/miniapp/crm/board?stage=' + encodeURIComponent(stage) +
        '&offset=' + col.cards.length + '&limit=' + per) : null,
      col.deals_has_more ? api('/api/miniapp/crm/deals?stage=' + encodeURIComponent(stage) +
        '&offset=' + (col.deals || []).length + '&limit=' + per) : null,
    ]);
    if (d) {
      const seen = {};
      col.cards.forEach(function (c) { seen[c.contact_id] = 1; });
      (d.cards || []).forEach(function (c) { if (!seen[c.contact_id]) col.cards.push(c); });
      col.count = d.total != null ? d.total : col.count;
      col.has_more = !!d.has_more;
    }
    if (dd) {
      col.deals = col.deals || [];
      const seenD = {};
      col.deals.forEach(function (x) { seenD[x.deal_id] = 1; });
      (dd.deals || []).forEach(function (x) { if (!seenD[x.deal_id]) col.deals.push(x); });
      col.deal_count = dd.total != null ? dd.total : col.deal_count;
      col.deals_has_more = !!(dd.page && dd.page.has_more);
    }
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

// ── Простые сделки на доске ────────────────────────────────────────────────────

function _dkFindDeal(id) {
  for (const col of (_DK && _DK.columns) || []) {
    const i = (col.deals || []).findIndex(function (d) { return d.deal_id === id; });
    if (i >= 0) return {col: col, i: i, deal: col.deals[i]};
  }
  return null;
}

function dkOpenDeal(id) {
  id = parseInt(id, 10);
  const f = _dkFindDeal(id);
  if (!f) return;
  const d = f.deal;
  document.getElementById('dkCardTitle').textContent = '💼 ' + d.title;
  const facts = [
    d.contact ? '👤 ' + esc(d.contact) : '',
    d.value ? '💰 ' + num(Math.round(d.value)) + ' ' + esc(d.currency) : '',
    d.notes ? '📝 ' + esc(d.notes) : '',
  ].filter(Boolean).map(function (x) { return '<div style="font-size:13px;margin:0 0 4px">' + x + '</div>'; }).join('');
  const stages = (_DK.columns || []).map(function (col) {
    const cur = col.stage === d.stage;
    return '<button class="btn ' + (cur ? 'btn-p' : 'btn-s') + ' dk-stagebtn"' + (cur ? ' disabled' : '') +
      ' onclick="closeModal(\'mo-dk-card\');dkMoveDeal(' + id + ',\'' + esc(col.stage) + '\')">' +
      (cur ? '✓ ' : '→ ') + esc(col.label) + '</button>';
  }).join('');
  document.getElementById('dkCardBody').innerHTML =
    (facts ? '<div style="margin:0 0 12px">' + facts + '</div>' : '') +
    '<div style="font-size:12px;font-weight:600;color:var(--hint);margin:0 0 6px">Перевести на стадию</div>' +
    stages +
    '<button class="btn btn-d" style="width:100%;margin-top:6px" onclick="dkDeleteDeal(' + id + ', this)">🗑 Удалить сделку</button>';
  document.getElementById('mo-dk-card').classList.add('show');
}

// Перенос простой сделки — та же дверь, что у бота и коннекторов
// (PUT /crm/deal/{id}/stage → db.move_crm_deal_stage: событие для сценариев).
async function dkMoveDeal(id, stage) {
  id = parseInt(id, 10);
  const key = 'd' + id;
  if (!(id > 0) || _DK_BUSY[key]) return;
  const f = _dkFindDeal(id);
  if (!f || f.deal.stage === stage) return;
  const to = (_DK.columns || []).find(function (c) { return c.stage === stage; });
  if (!to) return;
  const from = f.col, deal = f.deal, prevStage = deal.stage;
  const amount = Number(deal.value) || 0, cur = deal.currency;
  const shift = function (src, dst) {
    src.deals = (src.deals || []).filter(function (x) { return x.deal_id !== id; });
    src.deal_count = Math.max(0, (src.deal_count || 0) - 1);
    dst.deals = dst.deals || [];
    dst.deals.unshift(deal);
    dst.deal_count = (dst.deal_count || 0) + 1;
    if (amount) {
      src.by_currency = src.by_currency || {}; dst.by_currency = dst.by_currency || {};
      src.by_currency[cur] = Math.max(0, (src.by_currency[cur] || 0) - amount);
      dst.by_currency[cur] = (dst.by_currency[cur] || 0) + amount;
    }
    src.deals_has_more = src.deal_count > src.deals.length;
  };
  deal.stage = stage;
  shift(from, to);
  _DK_BUSY[key] = 1;
  _dkRender();
  try {
    await api('/api/miniapp/crm/deal/' + id + '/stage', {method: 'PUT', body: JSON.stringify({stage: stage})});
    try { tg.HapticFeedback?.notificationOccurred(stage === 'lost' ? 'warning' : 'success'); } catch (_) {}
    toast((stage === 'won' ? '🎉 ' : '✅ ') + deal.title + ' → ' + to.label);
  } catch (e) {
    deal.stage = prevStage;
    shift(to, from);
    try { tg.HapticFeedback?.notificationOccurred('error'); } catch (_) {}
    toast('⚠️ ' + errRu(e, 'Стадия не сохранилась').substring(0, 60));
  } finally {
    delete _DK_BUSY[key];
    _dkRender();
  }
}

async function dkDeleteDeal(id, el) {
  id = parseInt(id, 10);
  if (!(id > 0)) return;
  if (!await confirmDelete('Удалить сделку', el)) return;
  try {
    await api('/api/miniapp/crm/deal/' + id, {method: 'DELETE'});
    closeModal('mo-dk-card');
    toast('✅ Сделка удалена');
    loadDealsBoard();
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось удалить').substring(0, 60));
  }
}
