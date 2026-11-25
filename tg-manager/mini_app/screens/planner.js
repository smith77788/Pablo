// «Запланировано» — всё, что продукт сделает сам по времени, на одном экране.
//
// Раньше отложенные рассылки бота жили в карточке каждого бота отдельно,
// отложенные публикации — в общей очереди операций вперемешку с историей, а
// повторяемые круги автопостинга вообще нигде не были видны как расписание.
// Перенести время было нельзя — только отменить и завести заново.
//
// Здесь: сводка «что впереди» по дням, перенос времени, смена текста и
// повтора, снятие повтора, отмена и итог последних запусков. Сервер —
// services/mini_app_planner.py; запускают по-прежнему scheduler.py и op_worker.

let _PL_DATA = null;
let _PL_FORM = null;

const PL_REPEAT = [
  [0, 'Один раз'], [60, 'Каждый час'], [180, 'Каждые 3 часа'], [360, 'Каждые 6 часов'],
  [720, 'Каждые 12 часов'], [1440, 'Ежедневно'], [10080, 'Еженедельно'],
];
// Рассылку бота создаёт прежняя ручка, она знает только «разово/день/неделя».
const PL_REPEAT_BOT_NEW = [[0, 'Один раз', 'none'], [1440, 'Ежедневно', 'daily'], [10080, 'Еженедельно', 'weekly']];

function plRepeatLabel(min) {
  const m = Number(min) || 0;
  if (m <= 0) return '';
  if (m === 1440) return 'ежедневно';
  if (m === 10080) return 'еженедельно';
  if (m % 1440 === 0) return 'каждые ' + (m / 1440) + ' дн.';
  if (m === 60) return 'каждый час';
  if (m % 60 === 0) return 'каждые ' + (m / 60) + ' ч';
  return 'каждые ' + m + ' мин';
}

function _plScreen() {
  let el = document.getElementById('s-planner');
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = 's-planner';
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="back" onclick="back()" role="button" tabindex="0" aria-label="Назад">←</div>' +
      '<div class="hdr-title">🗓 Запланировано</div>' +
      '<div class="hdr-meta"><button class="btn btn-p" style="padding:7px 14px;font-size:13px" onclick="plNew()">+ Запланировать</button></div>' +
    '</div>' +
    '<div class="sb"><div id="plBody" style="padding:6px 0">' +
      '<div class="spin-wrap"><div class="spin"></div></div>' +
    '</div></div>';
  document.body.appendChild(el);
  return el;
}

async function openPlanner() {
  _plScreen();
  push('s-planner');
  await plLoad();
}

async function plLoad() {
  const body = document.getElementById('plBody');
  if (!body) return;
  if (!_PL_DATA) body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    _PL_DATA = await api('/api/miniapp/planner');
    body.innerHTML = _plRender(_PL_DATA);
  } catch (e) {
    body.innerHTML = errHtml(errRu(e, 'Не удалось загрузить запланированное'), retryCall('plLoad'));
  }
}

function _plDayLabel(d) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(d); day.setHours(0, 0, 0, 0);
  const diff = Math.round((day - today) / 86400000);
  if (diff === 0) return 'Сегодня';
  if (diff === 1) return 'Завтра';
  if (diff < 0) return 'Уже подошло';
  return day.toLocaleDateString('ru-RU', {weekday: 'long', day: 'numeric', month: 'long'});
}

function _plTime(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'});
}

function _plTitle(it) {
  if (it.kind === 'broadcast') return it.title;
  return it.title || opRu(it.op_type);
}

function _plStatus(it) {
  if (it.kind === 'broadcast') {
    const map = {pending: ['b-or', '⏳ Ждёт'], processing: ['b-gr', '▶ Отправляется'],
                 done: ['b-bl', '✓ Отправлено'], cancelled: ['b-gy', '— Отменено'],
                 missed: ['b-rd', '⚠ Пропущено']};
    return map[it.status] || stb(it.status);
  }
  if (it.status === 'pending' || it.status === 'scheduled') return ['b-or', '⏳ Ждёт'];
  return stb(it.status);
}

function _plResult(it) {
  const r = it.result;
  if (!r) return it.status === 'missed' ? 'Время прошло, пока бот был недоступен' : '';
  if (it.kind === 'broadcast') {
    const tail = r.failed ? ' · не доставлено ' + num(r.failed) : '';
    return 'Доставлено ' + num(r.sent) + (r.total ? ' из ' + num(r.total) : '') + tail;
  }
  if (r.error && (it.status === 'failed' || it.status === 'partial')) return r.error;
  if (r.total) return 'Выполнено ' + num(r.done) + ' из ' + num(r.total);
  return '';
}

function _plActions(it) {
  const a = [];
  const k = it.kind === 'broadcast' ? 'b' : 'o';
  if (it.editable) a.push(`<span class="ui-btn" role="button" tabindex="0" onclick="plEdit('${k}',${Number(it.id)})">✏️ Изменить</span>`);
  if (it.kind === 'operation' && it.repeat_min > 0) a.push(`<span class="ui-btn" role="button" tabindex="0" onclick="plStopRepeat(${Number(it.id)})">⏹ Снять повтор</span>`);
  if (it.cancellable) a.push(`<span class="ui-btn" role="button" tabindex="0" style="color:var(--red)" onclick="plCancel('${k}',${Number(it.id)})">✕ Отменить</span>`);
  if (it.kind === 'operation') a.push(`<span class="ui-btn" role="button" tabindex="0" onclick="openOpDetail(${Number(it.id)})">Подробнее ›</span>`);
  return a.length ? `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">${a.join('')}</div>` : '';
}

function _plRow(it, withDate) {
  const [bc, bl] = _plStatus(it);
  const ico = it.kind === 'broadcast' ? '📨' : '⚙️';
  const live = it.status === 'running' || it.status === 'processing';
  const when = live && !withDate ? 'Идёт' : (it.when ? (withDate
    ? new Date(it.when).toLocaleString('ru-RU', {dateStyle: 'short', timeStyle: 'short'})
    : _plTime(it.when)) : 'Без времени');
  const rep = plRepeatLabel(it.repeat_min);
  const preview = (it.text || '').substring(0, 90);
  // Итог «выполнено 0 из 14» у ещё не начатого запуска — шум, не итог.
  const res = (it.status === 'pending' || it.status === 'scheduled') ? '' : _plResult(it);
  return `<div class="li" style="display:block;cursor:default;padding:11px 14px">
    <div style="display:flex;align-items:flex-start;gap:10px">
      <div class="ava ava-orange">${ico}</div>
      <div class="li-body">
        <div class="li-name">${esc(when)} · ${esc(_plTitle(it))}</div>
        ${(rep || preview) ? `<div class="li-sub">${rep ? '🔁 ' + esc(rep) + (preview ? ' · ' : '') : ''}${esc(preview)}${(it.text || '').length > 90 ? '…' : ''}</div>` : ''}
        <div class="li-sub" style="margin-top:4px;display:flex;align-items:center;gap:6px;flex-wrap:wrap">
          <span class="badge ${bc}">${esc(bl)}</span>${res ? `<span>${esc(res)}</span>` : ''}
        </div>
      </div>
    </div>
    ${_plActions(it)}
  </div>`;
}

function _plRender(d) {
  const c = d.counts || {};
  const up = d.upcoming || [];
  const hist = d.history || [];
  let h = UI.stats([
    {ico: '🗓', label: 'Впереди', value: c.upcoming || 0},
    {ico: '⏰', label: 'За 24 часа', value: c.next_24h || 0},
    {ico: '🔁', label: 'Повторяются', value: c.recurring || 0},
    {ico: '⚠️', label: 'Сбои за 2 недели', value: c.failed || 0},
  ]);
  if (!up.length) {
    h += `<div style="padding:18px 16px;text-align:center">
      <div style="font-size:34px">🗓</div>
      <div style="font-weight:600;margin:6px 0 4px">Ничего не запланировано</div>
      <div style="font-size:13px;color:var(--hint);line-height:1.5">Поставьте рассылку бота или публикацию на время, или повторяйте прошедшую операцию по расписанию — всё появится здесь.</div>
      <button class="btn btn-p" style="margin-top:12px" onclick="plNew()">+ Запланировать</button>
    </div>`;
  } else {
    // Группировка по дням: «что будет завтра» — главный вопрос к экрану.
    // Идущие круги (без времени впереди) — отдельной группой сверху.
    const groups = [];
    const byKey = {};
    up.forEach(it => {
      const running = it.status === 'running' || it.status === 'processing' || it.status === 'paused';
      const key = running ? '__now' : (it.when ? new Date(it.when).toDateString() : '__none');
      if (!byKey[key]) {
        byKey[key] = {title: running ? 'Сейчас' : (it.when ? _plDayLabel(it.when) : 'Без времени'), items: []};
        groups.push(key);
      }
      byKey[key].items.push(it);
    });
    groups.sort((a, b) => (a === '__now' ? -1 : b === '__now' ? 1 : 0));
    groups.forEach(k => {
      const g = byKey[k];
      h += `<div class="sec">${esc(g.title)}</div><div class="lst">${g.items.map(it => _plRow(it, false)).join('')}</div>`;
    });
  }
  if (hist.length) {
    h += `<div class="sec" style="margin-top:12px">Последние запуски</div><div class="lst">${hist.map(it => _plRow(it, true)).join('')}</div>`;
  }
  return h;
}

function _plFind(k, id) {
  const all = ((_PL_DATA && _PL_DATA.upcoming) || []);
  return all.find(it => it.id === id && (k === 'b' ? it.kind === 'broadcast' : it.kind === 'operation'));
}

// ── Форма: изменить / создать ────────────────────────────────────────────────

function _plModal() {
  let el = document.getElementById('plMbg');
  if (el) return el;
  el = document.createElement('div');
  el.className = 'mbg';
  el.id = 'plMbg';
  el.onclick = (e) => { if (e.target === el) plClose(); };
  el.innerHTML = '<div class="modal"><div class="modal-x" onclick="plClose()" role="button" tabindex="0" aria-label="Закрыть">✕</div><div id="plModalBody"></div></div>';
  document.body.appendChild(el);
  return el;
}
function plClose() { const el = document.getElementById('plMbg'); if (el) el.classList.remove('show'); _PL_FORM = null; }
function _plShow(html) {
  const el = _plModal();
  document.getElementById('plModalBody').innerHTML = html;
  el.classList.add('show');
  try { tg.HapticFeedback?.impactOccurred('medium'); } catch (_) {}
}

function _plRepeatSelect(opts, cur) {
  return '<select id="plRepeat">' + opts.map(o =>
    `<option value="${Number(o[0])}"${Number(o[0]) === Number(cur) ? ' selected' : ''}>${esc(o[1])}</option>`).join('') + '</select>';
}

function _plForm({title, sub, text, when, repeat, repeatOpts, btn}) {
  return `<div class="modal-title">${esc(title)}</div>
    ${sub ? `<div style="font-size:13px;color:var(--hint);margin:-4px 0 10px">${esc(sub)}</div>` : ''}
    ${text !== undefined ? `<div class="field"><label>Текст сообщения</label><textarea id="plText" rows="5" maxlength="4096">${esc(text)}</textarea></div>` : ''}
    <div class="field"><label>Дата и время (по вашим часам)</label><input type="datetime-local" id="plWhen" value="${esc(when)}"></div>
    <div class="field"><label>Повтор</label>${_plRepeatSelect(repeatOpts, repeat)}
      <div class="field-note">Повторяемое действие само ставит следующий запуск. Снять повтор можно в любой момент.</div></div>
    <div class="field-err" id="plErr"></div>
    <button class="btn btn-p" data-main id="plBtn" onclick="plSave()" style="width:100%">${esc(btn)}</button>`;
}

function _plDefaultWhen() {
  const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(10, 0, 0, 0);
  return toLocalInput(d);
}

function plEdit(k, id) {
  const it = _plFind(k, id);
  if (!it) { toast('⚠️ Пункт уже не в расписании — обновите экран'); return; }
  _PL_FORM = {mode: k === 'b' ? 'edit-b' : 'edit-o', id};
  _plShow(_plForm({
    title: '✏️ Изменить запуск', sub: _plTitle(it),
    text: k === 'b' ? (it.text || '') : undefined,
    when: it.when ? toLocalInput(new Date(it.when)) : _plDefaultWhen(),
    repeat: it.repeat_min, repeatOpts: _plRepeatOpts(it.repeat_min), btn: '💾 Сохранить',
  }));
}

// Нестандартный период (заведённый в другом месте) не теряется при правке.
function _plRepeatOpts(cur) {
  const m = Number(cur) || 0;
  if (PL_REPEAT.some(o => o[0] === m)) return PL_REPEAT;
  return PL_REPEAT.concat([[m, plRepeatLabel(m)]]);
}

function plNew() {
  _PL_FORM = null;
  _plShow(`<div class="modal-title">🗓 Что запланировать?</div>
    <div class="lst">
      <div class="li" role="button" tabindex="0" onclick="plPickBot()"><div class="ava ava-orange">📨</div><div class="li-body"><div class="li-name">Рассылка подписчикам бота</div><div class="li-sub">Текст, время и повтор</div></div><span class="chev">›</span></div>
      <div class="li" role="button" tabindex="0" onclick="plClose();openMassPub()"><div class="ava ava-orange">📡</div><div class="li-body"><div class="li-name">Публикация в каналы</div><div class="li-sub">Время запуска и повтор — внизу формы публикации</div></div><span class="chev">›</span></div>
      <div class="li" role="button" tabindex="0" onclick="plPickOp()"><div class="ava ava-orange">🔁</div><div class="li-body"><div class="li-name">Повторить операцию по расписанию</div><div class="li-sub">Прошедшую публикацию, рассылку или проверку флота</div></div><span class="chev">›</span></div>
    </div>`);
}

async function plPickBot() {
  _plShow('<div class="modal-title">📨 Выберите бота</div><div id="plPick"><div class="spin-wrap"><div class="spin"></div></div></div>');
  try {
    const d = await api('/api/miniapp/bots?limit=500');
    const bots = (d.bots || []).filter(b => b.is_active);
    const box = document.getElementById('plPick');
    if (!box) return;
    box.innerHTML = bots.length ? '<div class="lst">' + bots.map(b =>
      `<div class="li" role="button" tabindex="0" onclick="plNewBroadcast(${Number(b.bot_id)})"><div class="ava ava-orange">🤖</div><div class="li-body"><div class="li-name">@${esc(b.username || b.first_name || b.bot_id)}</div><div class="li-sub">${esc(num(b.subscriber_count || 0))} подп.</div></div><span class="chev">›</span></div>`
    ).join('') + '</div>' : '<div style="font-size:13px;color:var(--hint);padding:6px 0">Нет активных ботов — добавьте бота во вкладке «Боты».</div>';
  } catch (e) {
    const box = document.getElementById('plPick');
    if (box) box.innerHTML = errHtml(errRu(e, 'Не удалось загрузить ботов'), retryCall('plPickBot'));
  }
}

function plNewBroadcast(botId) {
  _PL_FORM = {mode: 'new-b', id: botId};
  _plShow(_plForm({
    title: '📨 Рассылка по расписанию', text: '', when: _plDefaultWhen(),
    repeat: 0, repeatOpts: PL_REPEAT_BOT_NEW, btn: '⏰ Запланировать',
  }));
}

async function plPickOp() {
  _plShow('<div class="modal-title">🔁 Какую операцию повторять?</div><div id="plPick"><div class="spin-wrap"><div class="spin"></div></div></div>');
  try {
    const d = await api('/api/miniapp/planner/candidates');
    const items = d.items || [];
    const box = document.getElementById('plPick');
    if (!box) return;
    box.innerHTML = items.length ? '<div class="lst">' + items.map(o =>
      `<div class="li" role="button" tabindex="0" onclick="plNewOp(${Number(o.id)})"><div class="ava ava-orange">⚙️</div><div class="li-body"><div class="li-name">${esc(o.title || opRu(o.op_type))}</div><div class="li-sub">${esc((o.text || '').substring(0, 70) || opRu(o.op_type))}${o.finished_at ? ' · ' + esc(ago(o.finished_at)) : ''}</div></div><span class="chev">›</span></div>`
    ).join('') + '</div>' : '<div style="font-size:13px;color:var(--hint);padding:6px 0">Пока нечего повторять: здесь появятся завершённые публикации, рассылки и проверки флота.</div>';
  } catch (e) {
    const box = document.getElementById('plPick');
    if (box) box.innerHTML = errHtml(errRu(e, 'Не удалось загрузить операции'), retryCall('plPickOp'));
  }
}

function plNewOp(opId) {
  _PL_FORM = {mode: 'new-o', id: opId};
  _plShow(_plForm({
    title: '🔁 Повторить по расписанию', sub: 'Та же работа, те же цели — в выбранное время',
    when: _plDefaultWhen(), repeat: 1440, repeatOpts: PL_REPEAT, btn: '⏰ Запланировать',
  }));
}

async function plSave() {
  const f = _PL_FORM;
  if (!f) return;
  const errEl = document.getElementById('plErr');
  const fail = (m) => { errEl.textContent = m; errEl.style.display = 'block'; };
  errEl.style.display = 'none';
  const whenVal = document.getElementById('plWhen').value;
  const dt = new Date(whenVal);  // datetime-local — локальное время устройства
  if (!whenVal || isNaN(dt.getTime())) return fail('Выберите дату и время');
  if (dt <= new Date()) return fail('Время должно быть в будущем');
  const repeat = Number(document.getElementById('plRepeat').value) || 0;
  const textEl = document.getElementById('plText');
  const text = textEl ? textEl.value.trim() : undefined;
  if (textEl && !text) return fail('Введите текст');
  const btn = document.getElementById('plBtn');
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '⏳ Сохранение…';
  try {
    let msg = '✅ Сохранено';
    if (f.mode === 'edit-b') {
      await api('/api/miniapp/planner/broadcast/' + f.id, {method: 'POST',
        body: JSON.stringify({run_at: dt.toISOString(), repeat_min: repeat, text})});
    } else if (f.mode === 'edit-o') {
      await api('/api/miniapp/planner/operation/' + f.id, {method: 'POST',
        body: JSON.stringify({run_at: dt.toISOString(), repeat_min: repeat})});
    } else if (f.mode === 'new-b') {
      const kind = (PL_REPEAT_BOT_NEW.find(o => o[0] === repeat) || PL_REPEAT_BOT_NEW[0])[2];
      await api('/api/miniapp/bot/' + f.id + '/schedule', {method: 'POST',
        body: JSON.stringify({text, execute_at: dt.toISOString(), repeat: kind})});
      msg = '✅ Рассылка запланирована';
    } else if (f.mode === 'new-o') {
      await api('/api/miniapp/planner/operation/' + f.id + '/schedule', {method: 'POST',
        body: JSON.stringify({run_at: dt.toISOString(), repeat_min: repeat})});
      msg = '✅ Операция запланирована';
    }
    plClose();
    toast(msg);
    await plLoad();
  } catch (e) {
    fail(errRu(e, 'Не удалось сохранить'));
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

async function plCancel(k, id) {
  const it = _plFind(k, id);
  const chain = it && it.repeat_min > 0 ? ' Это повторяемое действие — отмена снимает и все следующие запуски.' : '';
  if (!await askConfirm('Отменить запланированный запуск?' + chain + ' Вернуть его нельзя — только запланировать заново.')) return;
  try {
    if (k === 'b') await api('/api/miniapp/schedule/' + id + '/cancel', {method: 'POST'});
    else await api('/api/miniapp/operation/' + id + '/cancel', {method: 'POST'});
    toast('Отменено');
    await plLoad();
  } catch (e) { toast('⚠️ ' + errRu(e, 'Не удалось отменить')); }
}

async function plStopRepeat(id) {
  if (!await askConfirm('Снять повтор? Ближайший запуск пройдёт как запланирован, следующие ставиться не будут.')) return;
  try {
    await api('/api/miniapp/planner/operation/' + id + '/stop_repeat', {method: 'POST'});
    toast('Повтор снят');
    await plLoad();
  } catch (e) { toast('⚠️ ' + errRu(e, 'Не удалось снять повтор')); }
}
