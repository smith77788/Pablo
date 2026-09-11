// Защита от накрутки/ботов — экран на карточке бота.
// Включается ОТДЕЛЬНО на каждого бота (по умолчанию выкл). Реагирует на резкий
// всплеск новых подписчиков; помечает/чистит накрученных. Самодостаточный экран.

let _fgBotId = null;
let _fgData = null;

const FG_MODES = [
  ['off', '⚪️ Выключена', 'Ничего не меняем — накрутка работает как есть.'],
  ['detect', '👁 Наблюдение', 'Только фиксируем всплеск и предупреждаем вас. Аудиторию не трогаем.'],
  ['protect', '🛡 Защита', 'Помечаем подписчиков всплеска и убираем их из аудитории рассылок и статистики.'],
  ['block', '⛔️ Блок', 'То же + подозрительным не даём ничего: ни автоответа, ни воронки, ни ИИ-менеджера.'],
];

function _fgScreen(id, title) {
  let el = document.getElementById(id);
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = id;
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="hdr-burger" onclick="back()">‹</div>' +
      '<div class="hdr-title">' + esc(title) + '</div>' +
      '<div class="hdr-burger" onclick="openFloodGuard(' + '_fgBotId' + ')" title="Обновить">⟳</div>' +
    '</div>' +
    '<div class="sb"><div id="' + id + '-body" style="padding:6px 0">' +
      '<div style="text-align:center;color:var(--hint);padding:40px">Загрузка…</div>' +
    '</div></div>';
  document.body.appendChild(el);
  return el;
}

async function openFloodGuard(botId) {
  _fgBotId = botId;
  _fgScreen('s-floodguard', '🛡 Защита от накрутки');
  push('s-floodguard');
  const body = document.getElementById('s-floodguard-body');
  body.innerHTML = '<div style="text-align:center;color:var(--hint);padding:40px">Загрузка…</div>';
  try {
    _fgData = await api('/api/miniapp/flood/' + botId);
    body.innerHTML = _fgRender(_fgData);
  } catch (e) {
    body.innerHTML = '<div style="color:var(--red);padding:20px">' + esc(e.message) + '</div>';
  }
}

function _fgRender(d) {
  const cfg = (d && d.config) || { mode: 'off', threshold_per_min: 30 };
  const ep = d && d.episode;
  const suspects = (d && d.suspect_count) || 0;
  let h = '';

  h += '<div style="font-size:13px;color:var(--hint);padding:2px 2px 12px">' +
    'Защита включается отдельно на каждого бота и реагирует на <b>резкий всплеск</b> ' +
    'новых подписчиков. По умолчанию выключена — если вы намеренно используете ' +
    'накрутку для продвижения, просто оставьте «Выключена».</div>';

  // Статус
  if (ep) {
    h += '<div style="padding:10px;background:rgba(248,113,113,.10);border-radius:10px;margin-bottom:12px">' +
      '<div style="color:var(--red);font-weight:600">⚠️ Идёт всплеск активности</div>' +
      '<div style="font-size:12px;color:var(--hint);margin-top:4px">Пик: ' +
      esc(String(ep.peak_per_min || 0)) + '/мин · помечено: ' + esc(String(ep.suspected_count || 0)) + '</div></div>';
  }
  h += '<div style="padding:10px;background:var(--bg2);border-radius:10px;margin-bottom:14px">' +
    '<div style="font-size:18px;font-weight:600">' + suspects + '</div>' +
    '<div style="font-size:12px;color:var(--hint)">помечено как накрученные (вне аудитории рассылок)</div></div>';

  // Режим
  h += '<div class="sec">Режим защиты</div>';
  h += FG_MODES.map(m =>
    '<label style="display:flex;gap:10px;align-items:flex-start;padding:8px;border-radius:8px;' +
    'background:var(--bg2);margin-bottom:6px;cursor:pointer">' +
    '<input type="radio" name="fg_mode" value="' + m[0] + '"' + (cfg.mode === m[0] ? ' checked' : '') +
    ' style="margin-top:3px">' +
    '<div><div style="font-weight:500">' + esc(m[1]) + '</div>' +
    '<div style="font-size:12px;color:var(--hint)">' + esc(m[2]) + '</div></div></label>').join('');

  // Порог
  h += '<div class="field"><label>Порог всплеска (новых подписчиков в минуту)</label>' +
    '<input type="number" id="fg_threshold" min="1" value="' + (parseInt(cfg.threshold_per_min, 10) || 30) + '">' +
    '<div class="field-note">Выше порога защита считает поток накруткой. Обычному боту хватает 20–40.</div></div>';

  h += '<button class="btn btn-p" style="width:100%;margin:6px 0 16px" onclick="fgSave()">💾 Сохранить</button>';

  // Быстрые действия
  h += '<div class="sec">Быстрая очистка</div>';
  h += '<div style="display:flex;gap:8px;margin-bottom:6px">' +
    '<input type="number" id="fg_minutes" min="1" value="10" class="inp" style="flex:1" placeholder="минут">' +
    '<button class="btn btn-s" style="flex:2" onclick="fgFlagRecent()">🚫 Заблокировать волну</button></div>' +
    '<div style="font-size:12px;color:var(--hint);padding:0 2px 10px">Пометит всех, кто присоединился за указанное число минут.</div>';
  h += '<button class="btn btn-s" style="width:100%;margin-bottom:6px" onclick="fgPurge(false)">🧹 Очистить накрученных (мягко — убрать из аудитории)</button>';
  h += '<button class="btn btn-s" style="width:100%;color:var(--red);margin-bottom:6px" onclick="fgPurge(true)">🗑 Удалить накрученных (безвозвратно)</button>';
  h += '<button class="btn btn-s" style="width:100%;margin-bottom:20px" onclick="fgUnflag()">↩️ Снять все флаги (если ложное срабатывание)</button>';
  return h;
}

async function fgSave() {
  const modeEl = document.querySelector('input[name="fg_mode"]:checked');
  const mode = modeEl ? modeEl.value : 'off';
  const threshold = parseInt((document.getElementById('fg_threshold') || {}).value || '30', 10) || 30;
  try {
    await api('/api/miniapp/flood/' + _fgBotId,
      { method: 'PATCH', body: JSON.stringify({ mode: mode, threshold_per_min: threshold }) });
    toast('Сохранено ✅');
    openFloodGuard(_fgBotId);
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function fgFlagRecent() {
  const minutes = parseInt((document.getElementById('fg_minutes') || {}).value || '10', 10) || 10;
  if (!confirm('Пометить всех, кто присоединился за последние ' + minutes + ' мин, как накрученных?')) return;
  try {
    const r = await api('/api/miniapp/flood/' + _fgBotId + '/flag-recent',
      { method: 'POST', body: JSON.stringify({ minutes: minutes }) });
    toast('Помечено: ' + (r.flagged || 0));
    openFloodGuard(_fgBotId);
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function fgPurge(hard) {
  const msg = hard
    ? 'Удалить накрученных подписчиков БЕЗВОЗВРАТНО?'
    : 'Убрать накрученных из аудитории (мягко, можно вернуть)?';
  if (!confirm(msg)) return;
  try {
    const r = await api('/api/miniapp/flood/' + _fgBotId + '/purge',
      { method: 'POST', body: JSON.stringify({ hard: !!hard }) });
    toast('Обработано: ' + (r.purged || 0));
    openFloodGuard(_fgBotId);
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function fgUnflag() {
  if (!confirm('Снять флаги «накрутка» со всех подписчиков этого бота?')) return;
  try {
    const r = await api('/api/miniapp/flood/' + _fgBotId + '/unflag', { method: 'POST', body: '{}' });
    toast('Снято флагов: ' + (r.unflagged || 0));
    openFloodGuard(_fgBotId);
  } catch (e) { toast(e.message || 'Ошибка'); }
}
