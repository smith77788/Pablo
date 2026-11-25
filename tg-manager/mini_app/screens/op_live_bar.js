// ── Плашка «идёт операция» (screens/op_live_bar.js) ────────────────────────
// Массовая операция — это минуты и часы работы флота, и всё это время о ней
// не было видно НИЧЕГО, кроме экрана очереди. Запустив инвайт и уйдя в
// контакты, владелец узнавал об окончании только из личного сообщения бота;
// опрос в приложении (`pollOpResult`) сдаётся через минуту словами «ещё
// выполняется — см. Очередь операций», то есть отправляет человека искать
// самому.
//
// Теперь поверх всех экранов висит узкая плашка: что идёт, сколько сделано и
// сколько осталось. Нажатие ведёт в саму операцию. Числа приходят из той же
// ручки, что значки вкладок (`/api/miniapp/badges`), поэтому второго опроса
// нет — только чаще, пока работа идёт.
'use strict';

const OP_BAR_FAST = 20e3;     // как часто обновляем, пока операция идёт
let _opBarId = null;
let _opBarTimer = null;

function renderOpLiveBar(op) {
  let bar = document.getElementById('opLiveBar');
  if (!op || !op.id) {
    _opBarId = null;
    if (bar && bar.parentNode) bar.parentNode.removeChild(bar);
    clearTimeout(_opBarTimer);
    _opBarTimer = null;
    return;
  }
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'opLiveBar';
    bar.className = 'op-live';
    bar.setAttribute('role', 'status');
    document.body.appendChild(bar);
  }
  _opBarId = op.id;
  const total = Number(op.total) || 0;
  const done = Number(op.done) || 0;
  const pct = total > 0 ? Math.min(100, Math.round(done * 100 / total)) : 0;
  // Остаток времени считает тот же код, что и экран операции: два разных
  // ответа про одну работу на соседних экранах — это не оценка, а шум.
  let eta = '';
  try {
    if (typeof _opEta === 'function') eta = _opEta(done, total, op.started_at);
  } catch (_) { eta = ''; }
  const right = total > 0 ? pct + '%' : '…';
  bar.innerHTML =
    '<span class="op-live-ico">⚙️</span>' +
    '<span class="op-live-txt">' + esc(op.label || 'Операция') +
    (total > 0 ? ' · ' + num(done) + ' из ' + num(total) : '') +
    (eta ? ' · ' + esc(eta) : '') + '</span>' +
    '<span class="op-live-pct">' + right + '</span>' +
    '<span class="op-live-fill" style="width:' + pct + '%"></span>';
  bar.onclick = () => {
    if (typeof openOpDetail === 'function') openOpDetail(op.id);
  };
}

// Пока операция идёт, числа устаревают за секунды, поэтому спрашиваем чаще
// обычного — но тем же запросом, что значки вкладок.
function opLiveBarTick() {
  clearTimeout(_opBarTimer);
  if (!_opBarId) return;
  _opBarTimer = setTimeout(() => {
    if (typeof refreshTabBadges === 'function') refreshTabBadges(true);
  }, OP_BAR_FAST);
}
