// Воронка денег: охват → подписка → ответ → покупка, по кампаниям.
//
// Аналитика продукта мерила систему (отправлено, живых аккаунтов), а не
// деньги. Сервер (services/money_funnel.py) сводит доставки рассылок, подписки
// на ботов, ответы со всех каналов и оплаченные заказы ботов-продавцов, и
// приписывает каждое событие ПОСЛЕДНЕЙ кампании, написавшей человеку в окне
// 30 дней. Расход вводит владелец — цену аккаунтов и прокси система не знает;
// из него считается стоимость лида (лид = ответил) и окупаемость.

let _mfDays = 30;
let _mfRows = [];
const MF_PERIODS = [[7, '7 дней'], [30, '30 дней'], [90, '90 дней']];

function _mfScreen() {
  const id = 's-moneyfunnel';
  let el = document.getElementById(id);
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = id;
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="back" onclick="back()" role="button" tabindex="0" aria-label="Назад">←</div>' +
      '<div class="hdr-title">💰 Воронка денег</div>' +
      '<div class="icon-btn" role="button" tabindex="0" aria-label="Обновить" ' +
        'style="margin-left:auto" onclick="openMoneyFunnel(_mfDays)">🔄</div>' +
    '</div>' +
    '<div class="sb"><div id="' + id + '-body" style="padding:6px 0">' +
      '<div class="spin-wrap"><div class="spin"></div></div>' +
    '</div></div>';
  document.body.appendChild(el);
  return el;
}

function _mfMoney(list) {
  // Суммы в разных валютах не складываем: «итого» из рублей с долларами было
  // бы красивым и неверным.
  if (!list || !list.length) return '—';
  return list.map(function (r) {
    const v = r.total_cents != null ? r.total_cents / 100 : r.amount;
    return num(Math.round(v)) + ' ' + esc(r.currency || '');
  }).join(' + ');
}

function _mfPct(v) {
  return v == null ? '—' : (Math.round(v * 1000) / 10) + '%';
}

async function openMoneyFunnel(days) {
  _mfDays = Number(days) || _mfDays || 30;
  _mfScreen();
  push('s-moneyfunnel');
  const body = document.getElementById('s-moneyfunnel-body');
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const d = await api('/api/miniapp/money_funnel?days=' + _mfDays);
    _mfRows = d.campaigns || [];
    let out = '<div class="seg" style="margin:8px 16px">' + MF_PERIODS.map(function (p) {
      return '<div class="seg-opt' + (p[0] === _mfDays ? ' on' : '') + '" role="button" tabindex="0" ' +
        'onclick="openMoneyFunnel(' + p[0] + ')">' + p[1] + '</div>';
    }).join('') + '</div>';

    const steps = d.steps || [];
    const top = steps.length ? (steps[0].count || 0) : 0;
    out += '<div style="margin:4px 16px 0">';
    out += steps.map(function (s) {
      const w = top ? Math.max(4, Math.round((s.count || 0) / top * 100)) : 4;
      return '<div style="margin-bottom:8px">' +
        '<div style="display:flex;justify-content:space-between;font-size:13px">' +
          '<span>' + esc(s.label) + '</span>' +
          '<span><b>' + num(s.count || 0) + '</b>' + (s.rate != null ? ' · ' + _mfPct(s.rate) : '') + '</span></div>' +
        '<div style="height:8px;background:var(--bg2);border-radius:4px;margin-top:3px">' +
          '<div style="height:8px;width:' + w + '%;background:var(--accent,#3b82f6);border-radius:4px"></div></div>' +
        '</div>';
    }).join('');
    out += '</div>';
    out += '<div class="kpi" style="padding:0 16px">' +
      '<div class="kpi-card"><div class="kpi-val">' + _mfMoney(d.revenue) + '</div><div class="kpi-lbl">Выручка</div></div>' +
      '<div class="kpi-card"><div class="kpi-val">' + _mfMoney(d.spend) + '</div><div class="kpi-lbl">Расход</div></div>' +
      '<div class="kpi-card"><div class="kpi-val">' + (d.cost_per_lead != null ? num(Math.round(d.cost_per_lead)) : '—') +
        '</div><div class="kpi-lbl">Цена лида</div></div></div>';

    if (!_mfRows.length) {
      out += empty('💰', 'Данных за период нет',
        'Воронка строится по личным рассылкам: кому доставлено, кто подписался на бота, ответил и купил у бота-продавца.',
        {label: '✉️ К рассылкам', fn: "goTab('broadcasts')"});
      body.innerHTML = out;
      return;
    }
    out += '<div class="sec">По кампаниям</div><div class="lst" style="margin:0 16px">';
    out += _mfRows.map(function (r, i) {
      const s = r.steps || {};
      let sig = '';
      if (r.vs_best) {
        if (r.vs_best.best) sig = '<span style="color:var(--green)">🏆 лучшая по ответам</span>';
        else if (r.vs_best.significant) sig = '<span style="color:var(--red)">хуже лучшей — разница значима (95%)</span>';
        else if (r.vs_best.enough_data) sig = '<span style="color:var(--hint)">разница с лучшей — в пределах случайности</span>';
        else sig = '<span style="color:var(--hint)">мало данных для сравнения</span>';
      }
      const isReal = r.campaign_id !== 0;
      return '<div class="row" style="display:block">' +
        '<div class="row-body"><div class="row-name">' + esc(r.name) + '</div>' +
          '<div class="row-val">' + _mfMoney(r.revenue) + '</div></div>' +
        '<div style="font-size:12px;margin-top:4px;line-height:1.5">' +
          (isReal ? '📨 ' + num(s.reached || 0) + ' → ' : '') +
          '🤖 ' + num(s.subscribed || 0) + ' → 💬 ' + num(s.replied || 0) + ' → 🛒 ' + num(s.bought || 0) +
          (isReal && r.rates ? ' · ответили ' + _mfPct(r.rates.replied) : '') + '</div>' +
        (isReal ? '<div style="font-size:12px;color:var(--hint);margin-top:2px">' +
          'Расход: ' + (r.spend ? num(r.spend.amount) + ' ' + esc(r.spend.currency || '') : 'не указан') +
          (r.cost_per_lead != null ? ' · лид ' + num(Math.round(r.cost_per_lead)) : '') +
          (r.cost_per_buyer != null ? ' · покупатель ' + num(Math.round(r.cost_per_buyer)) : '') +
          (r.romi != null ? ' · окупаемость ' + Math.round(r.romi * 100) + '%' : '') +
          ' <a href="#" onclick="mfEditSpend(' + i + ');return false">✏️</a></div>' : '') +
        (sig ? '<div style="font-size:11px;margin-top:2px">' + sig + '</div>' : '') +
      '</div>';
    }).join('');
    out += '</div>';
    out += '<div style="margin:8px 16px 20px;font-size:11px;color:var(--hint);line-height:1.45">' +
      'Событие засчитывается последней кампании, написавшей человеку не раньше чем за ' + num(d.window_days || 30) +
      ' дней до него. Лид — человек, который ответил. «Без рассылки» — подписки, ответы и покупки людей, которым кампании не писали.</div>';
    body.innerHTML = out;
  } catch (e) {
    body.innerHTML = errHtml(errRu(e), 'openMoneyFunnel(' + _mfDays + ')');
  }
}

async function mfEditSpend(i) {
  const r = _mfRows[i];
  if (!r || !r.campaign_id) return;
  const cur = r.spend ? r.spend.amount : '';
  const v = await askPrompt('Расход на «' + r.name + '», ₽',
    {type: 'number', value: cur === '' ? '' : String(cur), placeholder: 'например, 15000'});
  if (v === null || v === undefined || String(v).trim() === '') return;
  const amount = Number(String(v).replace(',', '.').replace(/\s/g, ''));
  if (!isFinite(amount) || amount < 0) { toast('Введите сумму числом'); return; }
  try {
    await api('/api/miniapp/money_funnel/spend', {method: 'POST',
      body: JSON.stringify({campaign_id: r.campaign_id, amount: amount,
                            currency: (r.spend && r.spend.currency) || 'RUB'})});
    toast('✅ Расход сохранён');
    openMoneyFunnel(_mfDays);
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось сохранить'));
  }
}
