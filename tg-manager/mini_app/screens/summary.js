// ── «Итоги»: результат за неделю на главном экране (screens/summary.js) ────
// Главный экран показывал устройство системы — аккаунты, давление, операции.
// Владелец приходит с другим вопросом: «что мне это принесло». Здесь четыре
// числа результата за 7 дней (деньги, лиды, ответы, охват) с изменением к
// прошлой неделе и не больше трёх карточек «требует внимания». Всё — из
// /api/miniapp/summary (services/results_summary.py); тот же расчёт видит
// бот в кнопке «📊 Итоги», чтобы два интерфейса не расходились.
//
// Первый экран на компонентах дизайн-системы (screens/ui_kit.js, ui_kit.css).
'use strict';

let _SUM_ATT = [];

const _SUM_CUR = { USD: '$', EUR: '€', RUB: '₽', XTR: '⭐' };

// Период итогов. Был только «за 7 дней»: месяц к прошлому месяцу владелец
// считал руками. Выбор запоминается, чтобы главный экран не сбрасывал его.
const _SUM_PERIODS = [7, 30];
function _sumDays() {
  const p = (typeof loadFormPrefs === 'function') ? (loadFormPrefs('summary') || {}) : {};
  const d = Number(p.days);
  return _SUM_PERIODS.includes(d) ? d : 7;
}
function sumSetDays(days) {
  const d = Number(days);
  if (!_SUM_PERIODS.includes(d) || d === _sumDays()) return;
  if (typeof saveFormPrefs === 'function') saveFormPrefs('summary', { days: d });
  loadSummary.lastData = null;
  loadSummary();
}
// Переключатель периода стоит ВНУТРИ блока итогов, прямо под заголовком:
// отдельной группой под блоком он выпадал из отступов экрана (прилипал к
// левому краю) и читался как чужой раздел, а не как настройка этих чисел.
function _sumPeriodRow(days) {
  return UI.segmented(_SUM_PERIODS.map(d => ({ value: d, label: d + ' дней' })), days, 'sumSetDays');
}

// Деньги в разных валютах не складываются: крупным числом показываем основную
// валюту (больше всего заказов на сумму), остальные — подписью. Изменение к
// прошлой неделе — тоже по основной валюте: сумма рублей с долларами дала бы
// бессмысленный процент.
function _sumMainCur(m) {
  const e = Object.entries((m && m.by_currency) || {}).filter(([, c]) => c).sort((a, b) => b[1] - a[1]);
  return e.length ? e[0][0] : null;
}
function _sumFmtCur(cur, cents) {
  const v = UI.num(cents / 100);
  const s = _SUM_CUR[cur];
  return s ? s + v : v + ' ' + cur;
}
function _sumMoney(m) {
  if (!m) return null;
  const cur = _sumMainCur(m);
  if (cur) return _sumFmtCur(cur, m.by_currency[cur]);
  if (m.deals_won) return UI.num(m.deals_value);
  return '0';
}
function _sumMoneySub(m) {
  if (!m) return '';
  const cur = _sumMainCur(m);
  const rest = Object.entries(m.by_currency || {}).filter(([c, v]) => v && c !== cur)
    .map(([c, v]) => '+ ' + _sumFmtCur(c, v));
  const parts = ['заказов: ' + UI.num(m.orders)];
  if (m.deals_won) parts.push(cur ? 'сделок: ' + UI.num(m.deals_won) : 'выиграно сделок: ' + UI.num(m.deals_won));
  return rest.concat(parts).join(' · ');
}
function _sumMoneyDelta(m) {
  const cur = _sumMainCur(m);
  if (!cur) return null;
  const prev = (m.prev_by_currency || {})[cur] || 0;
  return prev ? Math.round((m.by_currency[cur] - prev) * 100 / prev) : null;
}

// Куда ведёт нажатие на показатель. Явный список, а не имя функции из ответа
// сервера: переименование экрана не должно молча делать кнопку мёртвой.
function sumOpen(kind) {
  try {
    if (kind === 'money') return openSection('sales');
    if (kind === 'leads') return (typeof openNewUsers === 'function') ? openNewUsers() : openSection('audience');
    if (kind === 'replies') return (typeof openInbox === 'function') ? openInbox() : goTab('bots');
    if (kind === 'reach') return goTab('broadcasts');
    if (kind === 'attention') return openUnifiedDashboard();
  } catch (e) { toast('⚠️ Не удалось открыть раздел'); }
}

async function sumAct(idx) {
  const it = _SUM_ATT[idx];
  const a = it && it.action;
  if (!a) return;
  if (a.screen) {
    const go = (typeof _UD_SCREENS === 'object') && _UD_SCREENS[a.screen];
    if (go) go(); else toast('⚠️ Раздел недоступен');
    return;
  }
  if (!a.endpoint) return;
  try {
    const opts = { method: a.method || 'POST' };
    if (a.body) opts.body = JSON.stringify(a.body);
    const r = await api(a.endpoint, opts);
    toast('✅ ' + (r && r.summary ? String(r.summary).slice(0, 80)
                                  : (r && r.op_id ? 'Операция #' + r.op_id : 'Готово')));
    if (a.reload) loadSummary();
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не получилось'));
  }
}

function renderSummary(d) {
  const wrap = document.getElementById('summaryWrap');
  if (!wrap) return;
  d = d || {};
  _SUM_ATT = d.attention || [];
  const days = d.days || 7;
  const leads = d.leads || {}, replies = d.replies || {}, reach = d.reach || {};
  const statsHtml = UI.stats([
    { ico: '💰', label: 'Деньги', value: _sumMoney(d.money), delta: _sumMoneyDelta(d.money),
      sub: _sumMoneySub(d.money), fn: 'sumOpen', args: ['money'] },
    { ico: '🧲', label: 'Лиды', value: leads.value == null ? null : UI.num(leads.value),
      delta: leads.delta_pct, sub: 'новые подписчики ботов', fn: 'sumOpen', args: ['leads'] },
    { ico: '💬', label: 'Ответы', value: replies.value == null ? null : UI.num(replies.value),
      delta: replies.delta_pct, sub: 'входящие сообщения', fn: 'sumOpen', args: ['replies'] },
    { ico: '📣', label: 'Охват', value: reach.value == null ? null : UI.num(reach.value),
      delta: reach.delta_pct, sub: 'доставлено рассылками', fn: 'sumOpen', args: ['reach'] },
  ]);
  let att;
  if (_SUM_ATT.length) {
    att = _SUM_ATT.map((it, i) => UI.alert({
      severity: it.severity, title: it.title, detail: it.detail,
      buttonHtml: it.action ? UI.button(it.action.label || 'Открыть', 'sumAct', i) : '',
    })).join('');
  } else if (d.has_resources === false) {
    // Пустой проект — не «всё в порядке»: следить пока не за чем.
    att = UI.alert({ severity: 'info', title: 'Пока нечего проверять',
      detail: 'Подключите бота или аккаунт — здесь появятся сбои и то, что требует вашего решения.',
      buttonHtml: UI.button('🔑 Подключить бота', 'openBotAddModal') });
  } else {
    att = UI.ok('Ничего не требует внимания: операции идут, прокси и боты живы.');
  }
  const more = (d.attention_total || 0) > _SUM_ATT.length
    ? { label: 'Все ' + d.attention_total + ' →', fn: 'sumOpen', args: ['attention'] } : null;
  const attention = UI.group('Требует внимания', att, more);
  wrap.innerHTML =
    UI.block('📊 Итоги за ' + days + ' дней', _sumPeriodRow(days) + statsHtml + attention,
      { label: 'Подробнее →', fn: 'sumOpen', args: ['attention'] });
}

async function loadSummary() {
  const wrap = document.getElementById('summaryWrap');
  if (!wrap || loadSummary.pending) return;
  loadSummary.pending = true;
  const days = _sumDays();
  // У каждого периода свой кэш: иначе после переключения на месяц экран
  // сначала показал бы недельные числа под заголовком «за 30 дней».
  const cacheKey = days === 7 ? 'infragram_cache_summary' : 'infragram_cache_summary_' + days;
  const last = loadSummary.lastData && Number(loadSummary.lastData.days || 7) === days ? loadSummary.lastData : null;
  const cached = last || ((typeof getCached === 'function') ? getCached(cacheKey) : null);
  if (cached && !last) renderSummary(cached);
  else if (!cached) wrap.innerHTML = UI.block('📊 Итоги за ' + days + ' дней', UI.skeleton(4));
  function summaryState(message, retry) {
    let status = document.getElementById('summaryDataStatus');
    if (!status) {
      status = document.createElement('div');
      status.id = 'summaryDataStatus';
      status.className = 'home-data-status';
      status.setAttribute('role', 'status');
      status.setAttribute('aria-live', 'polite');
      wrap.appendChild(status);
    }
    homeLoadState('summaryDataStatus', message, retry);
  }
  summaryState(cached ? 'Обновляем итоги. Пока показаны предыдущие данные.' : 'Загружаем итоги…');
  try {
    const d = await api('/api/miniapp/summary?days=' + days);
    if (typeof setCache === 'function') setCache(cacheKey, d);
    loadSummary.lastData = d;
    renderSummary(d);
    summaryState('Итоги обновлены в ' + new Date().toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'}));
  } catch (e) {
    if (!cached) {
      wrap.innerHTML = UI.block('📊 Итоги за ' + days + ' дней',
        (typeof errHtml === 'function') ? errHtml(errRu(e), 'loadSummary()') : UI.empty('Не удалось загрузить итоги'));
    }
    summaryState(cached ? 'Не удалось обновить итоги. Показаны предыдущие данные, они могут быть устаревшими.' : 'Итоги недоступны. Это не нулевые значения.', true);
  } finally { loadSummary.pending = false; }
}
