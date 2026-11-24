// Аналитика продаж под аналитикой воронки (#dkIns, то же окно): сравнение с
// прошлым периодом, где теряем, аккаунты-источники, брошенные сделки.
// Всё считает сервер (services/crm_sales_insights.py). Код ужат: весь
// мини-апп стоит у потолка размера (tests/test_miniapp_minify.py).

function _ciM(o) {
  return Object.entries(o || {}).filter(function (p) { return p[1] > 0; })
    .map(function (p) { return num(Math.round(p[1])) + ' ' + esc(p[0]); }).join(' · ');
}

// Изменение к прошлому периоду; pp — в процентных пунктах.
function _ciD(v, pp) {
  if (v == null) return '';
  const p = Math.round(v * 100);
  return ' <small style="color:var(' + (p > 0 ? '--green' : p < 0 ? '--orange' : '--hint') + ')">' +
    (p > 0 ? '+' : p < 0 ? '−' : '±') + Math.abs(p) + (pp ? ' п.п.' : '%') + '</small>';
}

function _ciRow(name, sub, open) {
  return '<div class="li" style="margin-bottom:6px"' + (open ? ' role="button" tabindex="0" onclick="' + open + '"' : '') +
    '><div class="li-body"><div class="li-name">' + name + '</div><div class="li-sub">' + sub + '</div></div></div>';
}

function _ciSec(t, body, note) {
  return '<div class="sec-hdr" style="padding:4px 16px">' + t + '</div><div style="padding:0 16px 8px">' + body +
    (note ? '<div class="field-note">' + note + '</div>' : '') + '</div>';
}

async function dkInsights(days) {
  const box = document.getElementById('dkIns');
  if (!box) return;
  let a;
  try {
    a = await api('/api/miniapp/crm/sales_insights?days=' + days);
  } catch (e) {
    box.innerHTML = errHtml(errRu(e), 'dkInsights(' + days + ')');
    return;
  }
  const p = a.period, b = a.previous, c = a.compare;
  const kpi = function (v, d, l) {
    return '<div class="kpi-card"><div class="kpi-val">' + v + d + '</div><div class="kpi-lbl">' + l + '</div></div>';
  };
  let h = '<div class="sec-hdr" style="padding:4px 16px">📊 К прошлым ' + num(a.window_days) + ' дн</div><div class="kpi-row">' +
    kpi(num(p.new), _ciD(c.new), 'Новых · было ' + num(b.new)) +
    kpi(num(p.won), _ciD(c.won), 'Выиграно · было ' + num(b.won)) +
    kpi(_dkPct(p.win_rate), _ciD(c.win_rate_pp, 1), 'Доля выигранных · была ' + _dkPct(b.win_rate)) + '</div>';
  const rev = Object.keys(Object.assign({}, p.won_by_currency, b.won_by_currency));
  if (rev.length) {
    h += '<div style="padding:0 16px 8px">' + rev.map(function (k) {
      return '<div class="li-sub">Выручка: ' + num(Math.round(p.won_by_currency[k] || 0)) + ' ' + esc(k) +
        _ciD(c.won_by_currency[k]) + ' · было ' + num(Math.round(b.won_by_currency[k] || 0)) + '</div>';
    }).join('') + '</div>';
  }
  if (a.losses.length) {
    h += _ciSec('💔 Где теряем сделки', a.losses.map(function (r) {
      return _ciRow(esc(r.label) + ' · ' + _dkPct(r.share), num(r.count) + ' сделок' +
        (_ciM(r.value_by_currency) ? ' · упущено ' + _ciM(r.value_by_currency) : ''));
    }).join(''), a.worst_loss_label ? 'Чаще всего теряем на стадии «' + esc(a.worst_loss_label) + '».' : '');
  }
  if (a.sources.length) {
    h += _ciSec('📡 Какие аккаунты приносят сделки', a.sources.map(function (s) {
      return _ciRow(esc(s.label), 'Сделок ' + num(s.leads) + ' · выиграно ' + num(s.won) +
        (s.win_rate != null ? ' · ' + _dkPct(s.win_rate) : '') + (s.open ? ' · в работе ' + num(s.open) : '') +
        (_ciM(s.won_by_currency) ? ' · ' + _ciM(s.won_by_currency) : ''));
    }).join(''), 'По аккаунту, нашедшему клиента первым.');
  }
  if (a.neglected.length) {
    h += _ciSec('🕸 Без следующего шага', a.neglected.map(function (x) {
      const k = x.deal_key;
      const open = (x.deal_kind === 'contact' && DK_UUID.test(k)) || (x.deal_kind === 'deal' && /^\d+$/.test(k))
        ? 'dkOpenStuck(\'' + x.deal_kind + '\',\'' + k + '\')' : '';
      return _ciRow(esc(x.name || 'Без имени'), esc(x.stage_label) + ' · без касания ' + _dkDays(x.idle_days) +
        (x.reminder_overdue ? ' · просрочено' : '') +
        (x.value ? ' · ' + num(Math.round(x.value)) + ' ' + esc(x.currency) : ''), open);
    }).join(''), 'Нет касания ' + num(a.neglect_days) + '+ дн и напоминания' +
      (a.neglected_total > a.neglected.length ? ' · показаны ' + num(a.neglected.length) + ' из ' + num(a.neglected_total) : '') + '.');
  }
  box.innerHTML = h;
}
