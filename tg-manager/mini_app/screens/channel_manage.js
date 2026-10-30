// Управление каналами до конца: удалить канал в самом Telegram, очистить посты,
// выйти из чужого канала, отвязать от Infragram и быстро завести автопостинг —
// для одного канала (карточка) и для выбранных (режим выбора в списке).
//
// Раньше «🗑 Убрать» только стирало запись в Infragram: канал оставался в
// Telegram со всеми постами, и удалить его совсем было нечем.
//
// Сервер: POST /api/miniapp/channel/{id}/manage, POST /api/miniapp/channels/mass,
// POST /api/miniapp/mass_publish (с повтором), GET /api/miniapp/channels/autoposts.
// Убрать из системы без действий в Telegram — прежние removeChannel/bulkDeleteChannels.
// Удаление и очистку сервер принимает только с набранным словом подтверждения.

const CM_CONFIRM_WORD = 'УДАЛИТЬ';
let _cmIds = [];
let _cmOp = null;

function _cmModal(id, inner) {
  let el = document.getElementById(id);
  if (!el) {
    el = document.createElement('div');
    el.className = 'mbg';
    el.id = id;
    el.addEventListener('click', ev => { if (ev.target === el) closeModal(id); });
    document.body.appendChild(el);
  }
  el.innerHTML = '<div class="modal">' +
    `<div class="modal-x" onclick="closeModal('${id}')" role="button" tabindex="0" aria-label="Закрыть">✕</div>` +
    inner + '</div>';
  return el;
}

function _cmIdsNorm(ids) {
  return [...new Set((ids || []).map(Number).filter(n => Number.isFinite(n) && n !== 0))];
}

function _cmName(id) {
  const c = (typeof CUR_CHANNELS !== 'undefined' ? CUR_CHANNELS : []).find(x => String(x.channel_id) === String(id));
  if (!c) return 'Канал #' + id;
  return c.title || (c.username ? '@' + c.username : 'Канал #' + id);
}

// «канал «Новости»» для одного, «5 каналов» для многих — в заголовках и тостах.
function _cmWhat(ids) {
  if (ids.length === 1) return '«' + _cmName(ids[0]) + '»';
  return num(ids.length) + ' ' + plural(ids.length, 'канал', 'канала', 'каналов');
}

// Список названий, чтобы владелец видел, ЧТО именно уйдёт, а не только число.
function _cmNamesHtml(ids) {
  const shown = ids.slice(0, 6).map(id => '• ' + esc(_cmName(id))).join('<br>');
  const more = ids.length > 6 ? `<br>…и ещё ${num(ids.length - 6)}` : '';
  return `<div style="font-size:13px;line-height:1.5;background:var(--bg3);border-radius:var(--radius);padding:8px 10px;margin-bottom:10px;max-height:140px;overflow:auto">${shown}${more}</div>`;
}

function _cmAfterBulk() {
  if (typeof CH_SEL_MODE !== 'undefined' && CH_SEL_MODE) toggleChSelMode();
}

// После завершения операции на сервере обновляем список и, если действие шло
// из карточки удалённого канала, уходим из неё.
function _cmRefreshAfter(ids, leaveCard) {
  return () => {
    if (leaveCard && typeof STACK !== 'undefined' && STACK[STACK.length - 1] === 's-chdetail') back();
    if (typeof STACK !== 'undefined' && STACK[STACK.length - 1] === 's-channels') openChannels();
  };
}

async function _cmSubmit(op, ids, extra) {
  const body = Object.assign({op}, extra || {});
  if (ids.length === 1 && typeof STACK !== 'undefined' && STACK[STACK.length - 1] === 's-chdetail') {
    return api('/api/miniapp/channel/' + ids[0] + '/manage', {method: 'POST', body: JSON.stringify(body)});
  }
  body.channel_ids = ids;
  return api('/api/miniapp/channels/mass', {method: 'POST', body: JSON.stringify(body)});
}

// ── Удаление в Telegram и очистка постов (необратимо) ─────────────────────
function openChDestructive(op, ids) {
  ids = _cmIdsNorm(ids);
  if (!ids.length) { toast('Выберите хотя бы один канал'); return; }
  _cmIds = ids; _cmOp = op;
  const isDel = op === 'delete';
  const title = isDel ? '💥 Удалить в Telegram' : '🧹 Очистить посты';
  const warn = isDel
    ? `${ids.length > 1 ? esc(_cmWhat(ids)) + ' будут <b>удалены' : 'Канал ' + esc(_cmWhat(ids)) + ' будет <b>удалён'} в самом Telegram</b> вместе со всеми постами и подписчиками — для всех, навсегда. Восстановить нельзя. Удалить может только аккаунт-создатель канала. Из Infragram канал тоже исчезнет вместе с автопостингом и администратором.`
    : `Посты в ${esc(_cmWhat(ids))} будут <b>удалены для всех подписчиков</b>. Восстановить нельзя. Сам канал и подписчики останутся. Аккаунту нужны права администратора на удаление сообщений.`;
  const clearOpts = isDel ? '' : `
    <div class="field">
      <label style="font-size:13px;color:var(--hint)">Что удалить</label>
      <select id="cmClearMode" class="sel" onchange="_cmClearModeChanged()">
        <option value="0">Все посты</option>
        <option value="10">Последние 10</option>
        <option value="50">Последние 50</option>
        <option value="100">Последние 100</option>
        <option value="500">Последние 500</option>
        <option value="custom">Своё число…</option>
      </select>
      <input id="cmClearN" type="number" min="1" max="100000" placeholder="Сколько последних постов" style="display:none;margin-top:6px;width:100%;box-sizing:border-box;padding:10px;border-radius:var(--radius);background:var(--bg3);color:var(--text);border:1px solid var(--border-input);font-size:14px">
    </div>`;
  _cmModal('mo-ch-destructive', `
    <div class="modal-title">${title}</div>
    <div style="font-size:13px;line-height:1.5;color:var(--red);margin-bottom:10px">⚠️ ${warn}</div>
    ${_cmNamesHtml(ids)}
    ${clearOpts}
    <div class="field">
      <label style="font-size:13px;color:var(--hint)">Для подтверждения введите слово <b>${CM_CONFIRM_WORD}</b></label>
      <input id="cmConfirm" autocomplete="off" oninput="_cmWordInput()" placeholder="Слово подтверждения" style="width:100%;box-sizing:border-box;padding:10px;border-radius:var(--radius);background:var(--bg3);color:var(--text);border:1px solid var(--border-input);font-size:14px">
    </div>
    <div id="cmErr" style="color:var(--red);font-size:13px;margin-top:6px;min-height:16px"></div>
    <button class="btn btn-d" id="cmBtn" onclick="submitChDestructive()" style="width:100%;margin-top:8px" disabled>${isDel ? '💥 Удалить навсегда' : '🧹 Удалить посты'}</button>
    <button class="btn btn-s" onclick="closeModal('mo-ch-destructive')" style="width:100%;margin-top:8px">Отмена</button>`);
  showModal('mo-ch-destructive');
}

function _cmClearModeChanged() {
  const sel = document.getElementById('cmClearMode');
  const n = document.getElementById('cmClearN');
  if (sel && n) n.style.display = sel.value === 'custom' ? '' : 'none';
}

function _cmWordInput() {
  const v = (document.getElementById('cmConfirm')?.value || '').trim().toUpperCase();
  const btn = document.getElementById('cmBtn');
  if (btn) btn.disabled = v !== CM_CONFIRM_WORD;
}

async function submitChDestructive() {
  const errEl = document.getElementById('cmErr'); if (errEl) errEl.textContent = '';
  const confirmWord = (document.getElementById('cmConfirm')?.value || '').trim();
  if (confirmWord.toUpperCase() !== CM_CONFIRM_WORD) {
    if (errEl) errEl.textContent = 'Введите слово ' + CM_CONFIRM_WORD; return;
  }
  const extra = {confirm: confirmWord};
  if (_cmOp === 'clear') {
    const mode = document.getElementById('cmClearMode')?.value || '0';
    let n = mode === 'custom' ? parseInt(document.getElementById('cmClearN')?.value || '', 10) : parseInt(mode, 10);
    if (mode === 'custom' && !(n > 0)) { if (errEl) errEl.textContent = 'Укажите, сколько последних постов удалить'; return; }
    extra.clear_limit = n || 0;
  }
  const ids = _cmIds.slice(), op = _cmOp;
  const btn = document.getElementById('cmBtn'); if (btn) { btn.disabled = true; btn.textContent = 'Ставлю в очередь…'; }
  try {
    const r = await _cmSubmit(op, ids, extra);
    closeModal('mo-ch-destructive');
    const label = op === 'delete' ? 'Удаление в Telegram' : 'Очистка постов';
    toast(`⏳ ${label}: ${_cmWhat(ids)} — в очереди`);
    _cmAfterBulk();
    if (r && r.op_id) pollOpResult(r.op_id, label, _cmRefreshAfter(ids, op === 'delete'));
  } catch (e) {
    if (errEl) errEl.textContent = errRu(e, 'Ошибка');
    if (btn) { btn.disabled = false; btn.textContent = op === 'delete' ? '💥 Удалить навсегда' : '🧹 Удалить посты'; }
  }
}

// ── Выход аккаунта из канала и отвязка ────────────────────────────────────
async function runChLeave(ids) {
  ids = _cmIdsNorm(ids);
  if (!ids.length) { toast('Выберите хотя бы один канал'); return; }
  if (!await askConfirm(`Выйти аккаунтом из ${_cmWhat(ids)}?\n\nСам канал в Telegram продолжит жить, но управлять им отсюда будет нельзя — из Infragram он тоже уйдёт. Если аккаунт — создатель, канал останется без владельца в списке; чтобы удалить его совсем, используйте «💥 Удалить в Telegram».`)) return;
  try {
    const r = await _cmSubmit('leave', ids, {});
    toast(`⏳ Выход из ${_cmWhat(ids)} — в очереди`);
    _cmAfterBulk();
    if (r && r.op_id) pollOpResult(r.op_id, 'Выход из каналов', _cmRefreshAfter(ids, true));
  } catch (e) { toast('⚠️ ' + (errRu(e, 'Ошибка'))); }
}

// ── Быстрый автопостинг ───────────────────────────────────────────────────
function openChAutopost(ids) {
  ids = _cmIdsNorm(ids);
  if (!ids.length) { toast('Выберите хотя бы один канал'); return; }
  _cmIds = ids;
  _cmModal('mo-ch-autopost', `
    <div class="modal-title">🔁 Автопостинг</div>
    <div style="font-size:12px;color:var(--hint);margin-bottom:10px">Пост будет публиковаться в ${esc(_cmWhat(ids))} по расписанию. Spintax {A|B} даёт каждому каналу и каждому кругу свой вариант текста.</div>
    <textarea id="capText" rows="5" placeholder="Текст поста… Spintax: {Привет|Здравствуйте}!" style="width:100%;box-sizing:border-box;padding:10px;border-radius:var(--radius);background:var(--bg3);color:var(--text);border:1px solid var(--border-input);font-size:14px;resize:vertical"></textarea>
    <div class="field">
      <label style="font-size:13px;color:var(--hint)">Как часто</label>
      <select id="capEvery" class="sel">
        <option value="60">Каждый час</option>
        <option value="180">Каждые 3 часа</option>
        <option value="360">Каждые 6 часов</option>
        <option value="720">Каждые 12 часов</option>
        <option value="1440" selected>Каждый день</option>
        <option value="4320">Раз в 3 дня</option>
        <option value="10080">Каждую неделю</option>
      </select>
    </div>
    <div class="field">
      <label style="font-size:13px;color:var(--hint)">Первая публикация</label>
      <select id="capStart" class="sel">
        <option value="0">Сразу</option>
        <option value="60">Через 1 час</option>
        <option value="180">Через 3 часа</option>
        <option value="720">Через 12 часов</option>
        <option value="1440">Через сутки</option>
      </select>
    </div>
    <div class="field">
      <label style="font-size:13px;color:var(--hint)">Сколько раз</label>
      <select id="capCount" class="sel">
        <option value="">Пока не остановлю</option>
        <option value="3">3 публикации</option>
        <option value="7">7 публикаций</option>
        <option value="14">14 публикаций</option>
        <option value="30">30 публикаций</option>
      </select>
    </div>
    <div id="capErr" style="color:var(--red);font-size:13px;margin-top:6px;min-height:16px"></div>
    <button class="btn btn-p" id="capBtn" onclick="submitChAutopost()" style="width:100%;margin-top:8px">🔁 Запустить автопостинг</button>
    <div id="capActive" style="margin-top:12px"></div>`);
  showModal('mo-ch-autopost');
  loadChAutoposts(ids.length === 1 ? ids[0] : null, 'capActive');
}

function _cmSelLabel(id) {
  const e = document.getElementById(id);
  return e && e.selectedIndex >= 0 ? (e.options[e.selectedIndex].textContent || '').trim() : '';
}

async function submitChAutopost() {
  const errEl = document.getElementById('capErr'); if (errEl) errEl.textContent = '';
  const text = (document.getElementById('capText')?.value || '').trim();
  if (!text) { if (errEl) errEl.textContent = 'Введите текст поста'; return; }
  if (text.length > 4096) { if (errEl) errEl.textContent = 'Текст длиннее 4096 символов'; return; }
  const every = parseInt(document.getElementById('capEvery')?.value || '0', 10) || 0;
  const start = parseInt(document.getElementById('capStart')?.value || '0', 10) || 0;
  const count = parseInt(document.getElementById('capCount')?.value || '', 10) || 0;
  const ids = _cmIds.slice();
  // Публикацию подписчики видят сразу — масштаб и ритм называем до нажатия.
  if (!await askConfirm(`Запустить автопостинг в ${_cmWhat(ids)}?\n${_cmSelLabel('capEvery')}, первая публикация: ${_cmSelLabel('capStart').toLowerCase()}, ${count ? count + ' раз' : 'пока не остановите'}.`)) return;
  const payload = {text, channel_ids: ids, repeat_interval_min: every, target: 'all'};
  if (start > 0) payload.scheduled_for = new Date(Date.now() + start * 60000).toISOString();
  if (count > 0) payload.repeat_count = count;
  const btn = document.getElementById('capBtn'); if (btn) { btn.disabled = true; btn.textContent = 'Запускаю…'; }
  try {
    await api('/api/miniapp/mass_publish', {method: 'POST', body: JSON.stringify(payload)});
    toast('🔁 Автопостинг запущен');
    closeModal('mo-ch-autopost');
    _cmAfterBulk();
    if (ids.length === 1 && document.getElementById('chdAutoposts')) loadChAutoposts(ids[0], 'chdAutoposts');
  } catch (e) {
    if (errEl) errEl.textContent = errRu(e, 'Ошибка');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🔁 Запустить автопостинг'; }
  }
}

function _cmEvery(min) {
  const m = Number(min) || 0;
  if (m % 10080 === 0) return m === 10080 ? 'каждую неделю' : `раз в ${m / 10080} нед.`;
  if (m % 1440 === 0) return m === 1440 ? 'каждый день' : `раз в ${m / 1440} дн.`;
  if (m % 60 === 0) return m === 60 ? 'каждый час' : `каждые ${m / 60} ч`;
  return `каждые ${m} мин`;
}

function _cmWhen(iso) {
  if (!iso) return 'скоро';
  try {
    return new Date(iso).toLocaleString('ru-RU', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
  } catch (_) { return 'скоро'; }
}

// Действующие цепочки автопостинга: видно, что публикуется и когда следующий
// круг, и любую можно остановить. chId=null — все цепочки владельца.
async function loadChAutoposts(chId, boxId) {
  const box = document.getElementById(boxId);
  if (!box) return;
  let d;
  try {
    d = await api('/api/miniapp/channels/autoposts' + (chId ? '?channel_id=' + encodeURIComponent(chId) : ''));
  } catch (_) { box.innerHTML = ''; return; }
  const rows = d.autoposts || [];
  const head = `<div class="sec" style="margin-top:8px">Автопостинг${chId ? ' канала' : ''}</div>`;
  if (!rows.length) {
    box.innerHTML = head + `<div class="lst" style="padding:12px 14px;font-size:13px;color:var(--hint)">Не настроен. ${boxId === 'chdAutoposts' ? `<a href="#" onclick="openChAutopost([${Number(chId)}]);return false" style="color:var(--accent)">Настроить за минуту →</a>` : ''}</div>`;
    return;
  }
  box.innerHTML = head + '<div class="lst">' + rows.map(r => {
    const left = r.repeat_count != null && r.repeat_count !== '' ? ` · осталось ${esc(String(r.repeat_count))}` : '';
    const chs = Number(r.channels || 1);
    const st = r.status === 'paused' ? ' · ⏸ на паузе' : r.status === 'running' ? ' · публикуется' : '';
    return `<div class="li" style="cursor:default">
      <div class="ava ava-purple">🔁</div>
      <div class="li-body">
        <div class="li-name">${esc((r.text || r.label || 'Пост').slice(0, 60))}</div>
        <div class="li-sub">${_cmEvery(r.repeat_interval_min)} · след.: ${esc(_cmWhen(r.scheduled_for))}${chs > 1 ? ' · каналов: ' + num(chs) : ''}${left}${st}</div>
      </div>
      <button class="btn btn-s" style="flex:0 0 auto;padding:6px 10px;font-size:12px" onclick="stopChAutopost(${Number(r.id)}, ${chId ? Number(chId) : 'null'}, '${boxId}')">⏹ Стоп</button>
    </div>`;
  }).join('') + '</div>';
}

async function stopChAutopost(opId, chId, boxId) {
  if (!await askConfirm('Остановить этот автопостинг? Уже опубликованные посты останутся.')) return;
  try {
    await api('/api/miniapp/operation/' + opId + '/cancel', {method: 'POST', body: '{}'});
    toast('⏹ Автопостинг остановлен');
  } catch (e) { toast('⚠️ ' + (errRu(e, 'Ошибка'))); }
  loadChAutoposts(chId, boxId);
}
