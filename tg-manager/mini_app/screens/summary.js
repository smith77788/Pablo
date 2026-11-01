// ── «Итоги»: результат за неделю на главном экране (screens/summary.js) ────
// Главный экран показывал устройство системы — аккаунты, давление, операции.
// Владелец приходит с другим вопросом: «что мне это принесло». Здесь четыре
// числа результата за 7 дней (деньги, лиды, ответы, охват) с изменением к
// прошлой неделе и не больше трёх карточек «требует внимания». Всё — из
// /api/miniapp/summary (services/results_summary.py); тот же расчёт видит
// бот в кнопке «📊 Итоги», чтобы два интерфейса не расходились.
//
// Ниже — шесть разделов по задачам. Их состав не дублируется здесь: плитки
// собираются из разметки экрана «Разделы» (data-section), а она — из одной
// карты services/task_sections.py.
//
// Первый экран на компонентах дизайн-системы (screens/ui_kit.js, ui_kit.css).
'use strict';

let _SUM_ATT = [];

const _SUM_CUR = { USD: '$', EUR: '€', RUB: '₽', XTR: '⭐' };

function _sumMoney(m) {
  if (!m) return null;
  const parts = Object.entries(m.by_currency || {})
    .filter(([, c]) => c)
    .sort((a, b) => b[1] - a[1])
    .map(([cur, cents]) => {
      const v = UI.num(cents / 100);
      const s = _SUM_CUR[cur];
      return s ? s + v : v + ' ' + cur;
    });
  if (m.deals_won) parts.push('сделки ' + UI.num(m.deals_value));
  return parts.length ? parts.slice(0, 2).join(' · ') : '0';
}

function _sumMoneyDelta(m) {
  if (!m) return null;
  let cur = 0, prev = 0;
  Object.values(m.by_currency || {}).forEach(v => { cur += v || 0; });
  Object.values(m.prev_by_currency || {}).forEach(v => { prev += v || 0; });
  return prev ? Math.round((cur - prev) * 100 / prev) : null;
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
    toast('⚠️ ' + ((e && e.message) || 'Не получилось'));
  }
}

function _sumSections() {
  const more = document.getElementById('s-more');
  if (!more) return [];
  const out = [];
  more.querySelectorAll('.sec[data-section]').forEach(sec => {
    const key = sec.dataset.section;
    // На главной — шесть рабочих разделов. Настройки и Лаборатория живут в
    // «Разделах», чтобы не встречать новичка служебным и экспериментальным.
    if (key === 'settings' || key === 'lab' || key === 'overview') return;
    const t = (sec.textContent || '').trim();
    const sp = t.indexOf(' ');
    const desc = more.querySelector('.sec-desc[data-section="' + key + '"]');
    out.push({ key, ico: sp > 0 ? t.slice(0, sp) : '•', title: sp > 0 ? t.slice(sp + 1) : t,
               desc: desc ? (desc.textContent || '').trim() : '' });
  });
  return out;
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
      sub: d.money ? ('заказов: ' + UI.num(d.money.orders)) : '', fn: 'sumOpen', args: ['money'] },
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
  } else {
    att = UI.ok('Ничего не требует внимания: операции идут, прокси и боты живы.');
  }
  const more = (d.attention_total || 0) > _SUM_ATT.length
    ? { label: 'Все ' + d.attention_total + ' →', fn: 'sumOpen', args: ['attention'] } : null;
  const secs = _sumSections();
  wrap.innerHTML =
    UI.block('📊 Итоги за ' + days + ' дней', statsHtml, { label: 'Подробнее →', fn: 'sumOpen', args: ['attention'] }) +
    UI.block('Требует внимания', att, more) +
    (secs.length ? UI.block('Разделы', UI.sections(secs), { label: 'Все разделы →', fn: 'goTab', args: ['more'] }) : '');
}

async function loadSummary() {
  const wrap = document.getElementById('summaryWrap');
  if (!wrap) return;
  const cached = (typeof getCached === 'function') ? getCached('infragram_cache_summary') : null;
  if (cached) renderSummary(cached);
  else wrap.innerHTML = UI.block('📊 Итоги за 7 дней', UI.skeleton(4));
  try {
    const d = await api('/api/miniapp/summary?days=7');
    if (typeof setCache === 'function') setCache('infragram_cache_summary', d);
    renderSummary(d);
  } catch (e) {
    if (!cached) {
      wrap.innerHTML = UI.block('📊 Итоги за 7 дней',
        (typeof errHtml === 'function') ? errHtml(errRu(e), 'loadSummary()') : UI.empty('Не удалось загрузить итоги'));
    }
  }
}
