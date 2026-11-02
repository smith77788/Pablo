// Когорты и LTV: сколько в итоге приносит привлечённый человек и откуда
// приходят самые доходные.
//
// Сервер (services/cohorts.py) ставит человека в когорту по первому касанию с
// владельцем (подписка на бота, доставленная рассылка, сам написал на
// аккаунт) и по каждому периоду от прихода считает долю активных, долю
// купивших и накопленную выручку на человека. Выручка пересчитана в основную
// валюту владельца (services/owner_currency.py); сумма в валюте без курса не
// прячется — экран показывает её отдельно.

let _coGrain = 'month';
let _coMetric = 'ltv';
let _coData = null;
let _msData = null;
const CO_METRICS = [['ltv', 'LTV'], ['active', 'Активны'], ['buyers', 'Покупали']];

function _coScreen(id, title, refresh) {
  let el = document.getElementById(id);
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = id;
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="back" onclick="back()" role="button" tabindex="0" aria-label="Назад">←</div>' +
      '<div class="hdr-title">' + title + '</div>' +
      (refresh ? '<div class="icon-btn" role="button" tabindex="0" aria-label="Обновить" ' +
        'style="margin-left:auto" onclick="' + refresh + '">🔄</div>' : '') +
    '</div>' +
    '<div class="sb"><div id="' + id + '-body" style="padding:6px 0">' +
      '<div class="spin-wrap"><div class="spin"></div></div>' +
    '</div></div>';
  document.body.appendChild(el);
  return el;
}

function _coMoney(v, sym) {
  if (v == null) return '—';
  const r = Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 100) / 100;
  return num(r) + ' ' + esc(sym || '');
}

function _coPct(v) {
  return v == null ? '—' : (Math.round(v * 1000) / 10) + '%';
}

function _coCell(v, max) {
  if (v == null) return '<td style="color:var(--hint);text-align:center">·</td>';
  const sym = (_coData && _coData.money && _coData.money.symbol) || '';
  const txt = _coMetric === 'ltv' ? _coMoney(v, sym) : _coPct(v);
  const a = max > 0 ? Math.min(1, v / max) : 0;
  return '<td style="text-align:center;padding:6px 4px;white-space:nowrap;' +
    'background:rgba(59,130,246,' + (0.08 + a * 0.5).toFixed(2) + ')">' + txt + '</td>';
}

function coSetMetric(m) {
  _coMetric = m;
  _coRender();
}

function _coRender() {
  const body = document.getElementById('s-cohorts-body');
  const d = _coData;
  if (!body || !d) return;
  const sym = (d.money && d.money.symbol) || '';
  const t = d.totals || {};
  let out = '<div class="seg" style="margin:8px 16px">' +
    [['month', 'По месяцам'], ['week', 'По неделям']].map(function (g) {
      return '<div class="seg-opt' + (g[0] === _coGrain ? ' on' : '') + '" role="button" tabindex="0" ' +
        'onclick="openCohorts(\'' + g[0] + '\')">' + g[1] + '</div>';
    }).join('') + '</div>';

  out += '<div class="kpi" style="padding:0 16px">' +
    '<div class="kpi-card"><div class="kpi-val">' + _coMoney(t.ltv, sym) + '</div><div class="kpi-lbl">LTV на человека</div></div>' +
    '<div class="kpi-card"><div class="kpi-val">' + _coPct(t.paying_share) + '</div><div class="kpi-lbl">Купили</div></div>' +
    '<div class="kpi-card"><div class="kpi-val">' + _coMoney(t.arppu, sym) + '</div><div class="kpi-lbl">На покупателя</div></div>' +
    '</div>';
  out += '<div style="margin:2px 16px 0;font-size:12px;color:var(--hint)">' +
    num(t.people || 0) + ' человек пришли за период · ' + num(t.buyers || 0) + ' купили · выручка ' +
    _coMoney(t.revenue, sym) + '</div>';

  (d.unconverted || []).forEach(function (u) {
    out += '<div class="card" style="margin:8px 16px;font-size:12px;border-left:3px solid var(--orange,#f59e0b)">' +
      '⚠️ ' + num(Math.round(u.amount)) + ' ' + esc(u.currency) + ' не пересчитано: нет курса. ' +
      '<span style="color:var(--accent,#3b82f6);cursor:pointer" role="button" tabindex="0" ' +
      'onclick="openMoneySettings()">Указать курс</span></div>';
  });

  if (!t.people) {
    out += empty('📈', 'Пока некого считать',
      'Когорта — люди, впервые пришедшие в один месяц: подписались на бота, получили рассылку или сами написали. Как только такие появятся, здесь будет видно, сколько они приносят.',
      {label: '🤖 К ботам', fn: "goTab('bots')"});
    body.innerHTML = out;
    return;
  }

  // Кривая LTV: сколько в среднем приносит человек к N-му периоду.
  const curve = d.ltv_curve || [];
  const cmax = Math.max.apply(null, curve.map(function (v) { return v || 0; }).concat([0]));
  out += '<div class="sec">Как растёт LTV</div><div style="margin:0 16px">';
  out += curve.map(function (v, k) {
    if (v == null) return '';
    const w = cmax ? Math.max(3, Math.round(v / cmax * 100)) : 3;
    return '<div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;font-size:12px">' +
      '<span style="width:64px;color:var(--hint)">' + (k === 0 ? 'приход' : '+' + k + (_coGrain === 'week' ? ' нед' : ' мес')) + '</span>' +
      '<div style="flex:1;height:8px;background:var(--bg2);border-radius:4px">' +
        '<div style="height:8px;width:' + w + '%;background:var(--accent,#3b82f6);border-radius:4px"></div></div>' +
      '<span style="width:76px;text-align:right"><b>' + _coMoney(v, sym) + '</b></span></div>';
  }).join('');
  out += '</div>';

  // Таблица когорт.
  out += '<div class="sec">Когорты</div>' +
    '<div class="seg" style="margin:0 16px 8px">' + CO_METRICS.map(function (m) {
      return '<div class="seg-opt' + (m[0] === _coMetric ? ' on' : '') + '" role="button" tabindex="0" ' +
        'onclick="coSetMetric(\'' + m[0] + '\')">' + m[1] + '</div>';
    }).join('') + '</div>';
  const rows = d.cohorts || [];
  let max = 0;
  rows.forEach(function (c) {
    (c[_coMetric] || []).forEach(function (v) { if (v != null && v > max) max = v; });
  });
  out += '<div style="margin:0 16px;overflow-x:auto;-webkit-overflow-scrolling:touch">' +
    '<table style="border-collapse:collapse;font-size:12px;min-width:100%">' +
    '<thead><tr><th style="text-align:left;padding:4px 6px">Пришли</th><th style="padding:4px">Людей</th>';
  for (let k = 0; k < (d.periods || 0); k++) {
    out += '<th style="padding:4px;color:var(--hint);font-weight:500">' + (k === 0 ? '0' : '+' + k) + '</th>';
  }
  out += '</tr></thead><tbody>';
  out += rows.map(function (c) {
    return '<tr><td style="padding:6px;white-space:nowrap">' + esc(c.label) + '</td>' +
      '<td style="text-align:center">' + num(c.size) + '</td>' +
      (c[_coMetric] || []).map(function (v) { return _coCell(v, max); }).join('') + '</tr>';
  }).join('');
  out += '</tbody></table></div>';
  out += '<div style="margin:6px 16px 0;font-size:11px;color:var(--hint);line-height:1.45">' +
    (_coMetric === 'ltv' ? 'Накопленная выручка на одного человека когорты к этому периоду.'
      : _coMetric === 'active' ? 'Доля когорты, которая в этом периоде написала или купила.'
      : 'Доля когорты, купившая в этом периоде.') +
    ' 0 — ' + (_coGrain === 'week' ? 'неделя' : 'месяц') + ' прихода.</div>';

  // LTV по источникам.
  const src = d.sources || [];
  out += '<div class="sec">LTV по источникам</div><div class="lst" style="margin:0 16px">';
  out += src.map(function (s) {
    return '<div class="row" style="display:block">' +
      '<div class="row-body"><div class="row-name">' + esc(s.label) + '</div>' +
        '<div class="row-val">' + _coMoney(s.ltv, sym) + '</div></div>' +
      '<div style="font-size:12px;margin-top:4px;color:var(--hint);line-height:1.5">' +
        num(s.people) + ' чел. · купили ' + _coPct(s.paying_share) +
        ' · выручка ' + _coMoney(s.revenue, sym) +
        (s.arppu != null ? ' · на покупателя ' + _coMoney(s.arppu, sym) : '') +
        (s.median_days_to_buy != null ? ' · до покупки ~' + num(s.median_days_to_buy) + ' дн.' : '') +
      '</div></div>';
  }).join('');
  out += '</div>';
  out += '<div style="margin:8px 16px 20px;font-size:11px;color:var(--hint);line-height:1.45">' +
    'Источник — канал первого касания человека с вами. LTV — выручка оплаченных заказов ботов-продавцов на одного пришедшего, ' +
    'в валюте ' + esc((d.money && d.money.currency) || '') + '. ' +
    'Курс: ' + (d.money && d.money.rates_at ? 'ЦБ РФ на ' + esc(String(d.money.rates_at).slice(0, 10)) : 'ЦБ РФ недоступен, только ручные курсы') +
    (d.money && d.money.manual_rates && Object.keys(d.money.manual_rates).length ? ', есть ваши курсы' : '') + '.</div>';
  body.innerHTML = out;
}

async function openCohorts(grain) {
  _coGrain = grain === 'week' ? 'week' : (grain === 'month' ? 'month' : _coGrain);
  const el = _coScreen('s-cohorts', '📈 Когорты и LTV', 'openCohorts(_coGrain)');
  if (!el.querySelector('.co-cur')) {
    const btn = document.createElement('div');
    btn.className = 'icon-btn co-cur';
    btn.setAttribute('role', 'button');
    btn.setAttribute('tabindex', '0');
    btn.setAttribute('aria-label', 'Валюта отчётов');
    btn.textContent = '💱';
    btn.onclick = function () { openMoneySettings(); };
    el.querySelector('.hdr').appendChild(btn);
  }
  push('s-cohorts');
  await _coLoad();
}

async function _coLoad() {
  const body = document.getElementById('s-cohorts-body');
  if (!body) return;
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    _coData = await api('/api/miniapp/cohorts?grain=' + _coGrain);
    _coRender();
  } catch (e) {
    body.innerHTML = errHtml(errRu(e), 'openCohorts(_coGrain)');
  }
}

async function openMoneySettings() {
  _coScreen('s-moneyset', '💱 Валюта отчётов', '');
  push('s-moneyset');
  const body = document.getElementById('s-moneyset-body');
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    _msData = await api('/api/miniapp/money_settings');
    _msRender();
  } catch (e) {
    body.innerHTML = errHtml(errRu(e), 'openMoneySettings()');
  }
}

function _msRender() {
  const body = document.getElementById('s-moneyset-body');
  const d = _msData || {};
  const main = d.currency || 'RUB';
  const manual = d.manual_rates || {};
  const eff = d.effective_rates || {};
  let out = '<div style="margin:8px 16px;font-size:13px;line-height:1.45">' +
    'В этой валюте считаются LTV, выручка и окупаемость. Заказы в других валютах пересчитываются по курсу ЦБ РФ; ' +
    'если вы выводите деньги по другому курсу — укажите свой, он важнее.</div>';
  out += '<div class="sec">Основная валюта</div><div style="margin:0 16px">' +
    '<select id="ms-cur" class="inp" style="width:100%" onchange="msChangeMain(this.value)">' +
    (d.choices || ['RUB']).map(function (c) {
      return '<option value="' + esc(c) + '"' + (c === main ? ' selected' : '') + '>' + esc(c) + '</option>';
    }).join('') + '</select></div>';
  out += '<div class="sec">Свои курсы (1 единица = сколько ' + esc(main) + ')</div><div style="margin:0 16px">';
  const codes = (d.choices || []).filter(function (c) { return c !== main; }).concat(['XTR']);
  out += codes.map(function (c) {
    const ph = eff[c] != null ? 'по ЦБ: ' + (Math.round(eff[c] * 10000) / 10000) : 'курса нет';
    return '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">' +
      '<span style="width:64px;font-size:13px">' + (c === 'XTR' ? '⭐ звезда' : esc(c)) + '</span>' +
      '<input class="inp ms-rate" data-cur="' + esc(c) + '" inputmode="decimal" style="flex:1" ' +
        'placeholder="' + esc(ph) + '" value="' + (manual[c] != null ? esc(String(manual[c])) : '') + '"></div>';
  }).join('');
  out += '</div><div style="margin:4px 16px;font-size:11px;color:var(--hint)">Пустое поле — курс ЦБ. ' +
    'Звёзды по умолчанию — по выплатному курсу Telegram через доллар.</div>';
  out += '<div style="margin:12px 16px 20px"><button class="btn" style="width:100%" onclick="msSave()">💾 Сохранить</button></div>';
  body.innerHTML = out;
}

function msChangeMain(v) {
  // Свои курсы заданы в старой основной валюте — при смене они теряют смысл.
  if (!_msData) return;
  if (Object.keys(_msData.manual_rates || {}).length) toast('Свои курсы сброшены: они были в ' + _msData.currency);
  _msData.currency = v;
  _msData.manual_rates = {};
  _msData.effective_rates = {};
  _msRender();
}

async function msSave() {
  const cur = (document.getElementById('ms-cur') || {}).value || 'RUB';
  const rates = {};
  let bad = '';
  document.querySelectorAll('#s-moneyset .ms-rate').forEach(function (inp) {
    const v = String(inp.value || '').trim().replace(',', '.').replace(/\s/g, '');
    if (!v) return;
    const n = Number(v);
    if (!isFinite(n) || n <= 0) bad = inp.dataset.cur;
    else rates[inp.dataset.cur] = n;
  });
  if (bad) { toast('Курс ' + bad + ' — положительное число'); return; }
  try {
    await api('/api/miniapp/money_settings', {method: 'POST',
      body: JSON.stringify({currency: cur, manual_rates: rates})});
    toast('✅ Валюта отчётов: ' + cur);
    back();
    if (_coData) _coLoad();
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось сохранить'));
  }
}
