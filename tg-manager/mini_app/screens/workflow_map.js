// 🗺 Схема сценария: шаги и все переходы между ними одной картинкой.
//
// Экран сценария показывал шаги столбцом, а переходы — строкой текста у шага
// («иначе → шаг 7», «🔘 Хочу купить → шаг 2»). Куда на самом деле ведёт
// ветка, приходилось собирать в голове, прыгая глазами по номерам. У лидеров
// (ManyChat, Salebot) сценарий — это схема со стрелками. Здесь — та же схема,
// только для чтения: шаги прямоугольниками сверху вниз, следующий шаг —
// короткой стрелкой, прыжки и ветки — дугами справа с подписью, конец
// сценария — отдельным узлом. У каждого шага — сколько людей до него дошло.
//
// Правила переходов повторяют исполнитель (services/workflow_runtime.py):
// номера шагов с единицы, переход за конец — завершение, у «условия» без
// «иначе» ветка «нет» заканчивает сценарий, у ожиданий без timeout_goto
// «нет ответа» — тоже.
const _WFM_ICON = { message: '💬', delay: '⏳', condition: '🔀', action: '⚡', webhook: '🌐', ask: '❓', invoice: '💳', split: '🎲', wait: '⏸', ai: '🤖' };
const _WFM_NAME = { message: 'Сообщение', delay: 'Пауза', condition: 'Условие', action: 'Действие', webhook: 'Вебхук', ask: 'Вопрос', invoice: 'Счёт', split: 'A/B-деление', wait: 'Ждать события', ai: 'Ответ ИИ' };

// Переходы шага i (с нуля) в виде {from, to, label}; to — индекс шага или
// 'end'. Чистая функция: её проверяет тест в Node.
function wfEdges(steps) {
  const n = steps.length;
  const tgt = g => { const k = parseInt(g, 10); return (k >= 1 && k <= n) ? k - 1 : 'end'; };
  const next = i => (i + 1 < n ? i + 1 : 'end');
  const out = [];
  steps.forEach((s, i) => {
    const t = (s && typeof s.type === 'string') ? s.type : '';
    const add = (to, label) => out.push({ from: i, to, label: label || '' });
    if (t === 'split') {
      const br = s.branches || [];
      if (!br.length) add(next(i));
      br.forEach((b, k) => add(b.goto ? tgt(b.goto) : next(i), (b.label || String.fromCharCode(65 + k)) + ' · ' + (b.weight || 0) + '%'));
    } else if (t === 'condition') {
      add(next(i), 'да');
      add(s.else_goto ? tgt(s.else_goto) : 'end', 'нет');
    } else if (t === 'message') {
      const br = (s.buttons || []).filter(b => !b.url);
      if (!br.length) { add(next(i)); return; }
      br.forEach(b => add(b.goto ? tgt(b.goto) : next(i), '«' + String(b.text || '').slice(0, 18) + '»'));
      add(s.timeout_goto ? tgt(s.timeout_goto) : 'end', 'нет ответа');
    } else if (t === 'ask') {
      add(next(i), 'ответил');
      add(s.timeout_goto ? tgt(s.timeout_goto) : 'end', 'нет ответа');
    } else if (t === 'invoice') {
      add(next(i), 'оплатил');
      add(s.timeout_goto ? tgt(s.timeout_goto) : 'end', 'не оплатил');
    } else if (t === 'wait') {
      add(next(i), 'дождался');
      add(s.timeout_goto ? tgt(s.timeout_goto) : 'end', 'не дождался');
    } else if (t === 'webhook' && s.else_goto) {
      add(next(i), 'ок');
      add(tgt(s.else_goto), 'сбой');
    } else if (t === 'action' && s.action === 'goto') {
      add(s.goto ? tgt(s.goto) : 'end', 'переход');
    } else if (t === 'action' && s.action === 'stop') {
      add('end', 'стоп');
    } else {
      add(next(i));
    }
  });
  return out;
}

function _wfmSvg(steps, stats) {
  const n = steps.length;
  const W = 230, BX = 4, BH = 50, GAP = 28, LANE = 14;
  const edges = wfEdges(steps);
  const endIdx = n;                      // узел «конец» — после последнего шага
  const yOf = i => 8 + i * (BH + GAP);
  const jumps = edges.filter(e => !(e.to === e.from + 1 || (e.to === 'end' && e.from === n - 1 && !e.label)));
  const lanes = Math.max(1, jumps.length);
  const width = BX + W + 18 + lanes * LANE + 8;
  const height = yOf(endIdx) + 36;
  const reached = i => (((stats || {}).steps || {})[String(i)] || {}).reached;
  const dropped = i => (((stats || {}).steps || {})[String(i)] || {}).dropped;
  let svg = `<svg viewBox="0 0 ${width} ${height}" width="100%" style="max-width:${width}px;display:block" role="img" aria-label="Схема сценария">
    <defs><marker id="wfmArr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--hint)"/></marker></defs>`;
  // Последовательные стрелки (без подписи) — короткие вертикальные.
  edges.forEach(e => {
    const to = e.to === 'end' ? endIdx : e.to;
    if (to === e.from + 1 && !jumps.includes(e)) {
      const x = BX + W / 2, y1 = yOf(e.from) + BH, y2 = yOf(to);
      svg += `<line x1="${x}" y1="${y1}" x2="${x}" y2="${y2 - 2}" stroke="var(--hint)" stroke-width="1.5" marker-end="url(#wfmArr)"/>`;
      if (e.label) svg += `<text x="${x + 6}" y="${(y1 + y2) / 2 + 4}" font-size="10" fill="var(--hint)">${esc(e.label)}</text>`;
    }
  });
  // Прыжки и ветки — дуги справа, каждой своя полоса.
  // Подписи дуг — поверх, с подложкой цвета фона, чтобы чужие дуги их не
  // перечёркивали; несколько выходов одного шага разнесены по высоте.
  const perNode = {};
  let labels = '';
  jumps.forEach((e, k) => {
    const to = e.to === 'end' ? endIdx : e.to;
    const slot = perNode[e.from] = (perNode[e.from] || 0) + 1;
    const x0 = BX + W, xl = x0 + 14 + k * LANE;
    const y1 = yOf(e.from) + 8 + (slot - 1) * 13, y2 = yOf(to) + (to === endIdx ? 12 : BH / 2);
    svg += `<path d="M${x0},${y1} H${xl} V${y2} H${x0 + 2}" fill="none" stroke="var(--accent)" stroke-opacity=".7" stroke-width="1.4" marker-end="url(#wfmArr)"/>`;
    if (e.label) labels += `<text x="${x0 + 4}" y="${y1 - 2}" font-size="9.5" fill="var(--accent)" stroke="var(--bg)" stroke-width="3" paint-order="stroke">${esc(e.label)}</text>`;
  });
  steps.forEach((s, i) => {
    const t = (s && typeof s.type === 'string') ? s.type : '';
    const y = yOf(i), r = reached(i), dr = dropped(i);
    const body = String(s.text || s.condition || s.action || s.url || (t === 'delay' ? (s.delay_minutes ? s.delay_minutes + ' мин' : (s.until_time ? 'до ' + s.until_time : '')) : '') || '').replace(/\s+/g, ' ').slice(0, dr ? 20 : 32);   // справа от текста — «↘ N ушли»
    svg += `<rect x="${BX}" y="${y}" width="${W}" height="${BH}" rx="10" fill="var(--bg2)" stroke="var(--border-strong)"/>
      <text x="${BX + 10}" y="${y + 19}" font-size="12" font-weight="700" fill="var(--text)">${i + 1}. ${_WFM_ICON[t] || '•'} ${esc(_WFM_NAME[t] || t || 'Шаг')}</text>
      <text x="${BX + 10}" y="${y + 37}" font-size="11" fill="var(--hint)">${esc(body)}</text>
      ${r != null ? `<text x="${BX + W - 10}" y="${y + 19}" font-size="11" text-anchor="end" fill="var(--hint)">👥 ${num(r)}</text>` : ''}
      ${dr ? `<text x="${BX + W - 10}" y="${y + 37}" font-size="11" text-anchor="end" fill="var(--red)">↘ ${num(dr)} ушли</text>` : ''}`;
  });
  svg += labels;
  const ye = yOf(endIdx);
  svg += `<rect x="${BX + W / 2 - 50}" y="${ye}" width="100" height="24" rx="12" fill="var(--bg3)" stroke="var(--border)"/>
    <text x="${BX + W / 2}" y="${ye + 16}" font-size="11" text-anchor="middle" fill="var(--hint)">🏁 Конец</text></svg>`;
  return svg;
}

function _wfmModal() {
  let mo = document.getElementById('mo-wf-map');
  if (mo) return mo;
  mo = document.createElement('div');
  mo.className = 'mbg';
  mo.id = 'mo-wf-map';
  mo.setAttribute('onclick', "if(event.target===this)closeModal('mo-wf-map')");
  mo.innerHTML =
    '<div class="modal">' +
      '<div class="modal-x" onclick="closeModal(\'mo-wf-map\')" role="button" tabindex="0" aria-label="Закрыть">✕</div>' +
      '<div class="modal-title" id="wfMapTitle">🗺 Схема сценария</div>' +
      '<div id="wfMapBody"></div>' +
    '</div>';
  document.body.appendChild(mo);
  return mo;
}

// Название берётся из ответа API, а не из onclick: строка в атрибуте-обработчике
// рвёт разметку на апострофе.
async function openWorkflowMap(wfId) {
  const mo = _wfmModal();
  txt('wfMapTitle', '🗺 Схема сценария');
  txt('wfMapBody', '<div class="spin-wrap"><div class="spin"></div></div>');
  mo.classList.add('show');
  let d;
  try { d = await api('/api/miniapp/workflows/' + Number(wfId)); }
  catch (e) { txt('wfMapBody', errHtml(errRu(e), 'openWorkflowMap(' + Number(wfId) + ')')); return; }
  if (d.name) txt('wfMapTitle', '🗺 ' + esc(d.name));
  const steps = d.steps || [];
  if (!steps.length) { txt('wfMapBody', empty('🗺', 'Шагов пока нет', 'Добавьте шаги — схема появится здесь.')); return; }
  txt('wfMapBody', '<div style="overflow-x:auto;-webkit-overflow-scrolling:touch">' + _wfmSvg(steps, d.stats) + '</div>' +
    '<div class="hint" style="margin-top:8px">Короткая стрелка — следующий шаг. Дуга справа — переход или ветка, подпись — условие перехода. 👥 — сколько людей дошли до шага, ↘ — сколько ушли после него, не дойдя до конца (ошибка, истёк срок, остановлен).</div>');
}
