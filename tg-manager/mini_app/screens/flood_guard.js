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
    // errHtml всегда даёт кнопку выхода; своя вёрстка ошибки оставляла
    // экран без единой кнопки, а таббар на подэкране скрыт.
    body.innerHTML = errHtml((e && e.message) || 'Ошибка', 'openFloodGuard(' + JSON.stringify(botId) + ')');
  }
}

function _fgRender(d) {
  const cfg = (d && d.config) || { mode: 'off', threshold_per_min: 30 };
  const ep = d && d.episode;
  const suspects = (d && d.suspect_count) || 0;
  let h = '';

  const on = cfg.mode && cfg.mode !== 'off';

  // Короткий статус-баннер: включена/выключена
  h += '<div style="padding:10px 12px;border-radius:10px;margin-bottom:10px;background:' +
    (on ? 'rgba(52,211,153,.10)' : 'var(--bg2)') + '">' +
    '<b>' + (on ? '🛡 Защита включена' : '⚪️ Защита выключена') + '</b>' +
    '<div style="font-size:12px;color:var(--hint);margin-top:3px">' +
    (on ? 'Бот под наблюдением. Обычные подписчики не затрагиваются — реагируем только на резкий всплеск.'
        : 'Ничего не меняется. Если вы намеренно используете накрутку для продвижения — так и оставьте.') +
    '</div></div>';

  // «Как это работает» — раскрывающийся блок, чтобы не пугать простыней текста
  h += '<details style="margin-bottom:12px;background:var(--bg2);border-radius:10px;padding:8px 12px">' +
    '<summary style="cursor:pointer;font-weight:500">❓ Как это работает и что выбрать</summary>' +
    '<div style="font-size:13px;color:var(--hint);margin-top:8px;line-height:1.5">' +
    '<p>Защита ловит <b>накрутку по скорости</b>: если на бота вдруг подписывается ' +
    'намного больше людей в минуту, чем обычно, — это почти всегда боты. Такой всплеск ' +
    'нельзя «замаскировать под живого», поэтому обойти защиту, в отличие от проверки по ' +
    'аватаркам и именам, нельзя.</p>' +
    '<p><b>Настраивается на каждого бота отдельно</b> — можно защитить рабочего бота с ' +
    'клиентами и оставить открытым бота, которого вы продвигаете накруткой.</p>' +
    '<p><b>Что выбрать:</b><br>' +
    '• Продвигаете бота накруткой ради выдачи → <b>Выключена</b>.<br>' +
    '• Хотите просто видеть атаки → <b>Наблюдение</b>.<br>' +
    '• Бот с живой аудиторией и рассылками → <b>Защита</b> (рекомендуем).<br>' +
    '• Идёт жёсткая атака, нужно отсечь всё → <b>Блок</b>.</p>' +
    '<p><b>Что значит «накрученный» подписчик:</b> он остаётся в базе, но исключается из ' +
    'аудитории рассылок и статистики — реклама и цепочки на него не тратятся. В режиме ' +
    '«Блок» ему вдобавок не отвечает ни автоответчик, ни воронка, ни ИИ-менеджер.</p>' +
    '<p>Пометки полностью обратимы кнопкой «Снять флаги» — если вдруг под раздачу попали ' +
    'живые люди.</p>' +
    '</div></details>';

  // Статус атаки / счётчик
  if (ep) {
    h += '<div style="padding:10px 12px;background:rgba(248,113,113,.10);border-radius:10px;margin-bottom:10px">' +
      '<div style="color:var(--red);font-weight:600">⚠️ Прямо сейчас идёт всплеск</div>' +
      '<div style="font-size:12px;color:var(--hint);margin-top:4px">Пик: ' +
      esc(String(ep.peak_per_min || 0)) + ' новых/мин · помечено в этой атаке: ' +
      esc(String(ep.suspected_count || 0)) + '. Можно «Заблокировать волну» и «Очистить» ниже.</div></div>';
  }
  h += '<div style="display:flex;gap:8px;margin-bottom:14px">' +
    '<div style="flex:1;padding:10px;background:var(--bg2);border-radius:10px">' +
    '<div style="font-size:20px;font-weight:600">' + suspects + '</div>' +
    '<div style="font-size:11px;color:var(--hint)">помечено накрученными (вне рассылок)</div></div></div>';

  // Режим
  h += '<div class="sec">1. Режим защиты</div>';
  h += FG_MODES.map(m => {
    const rec = (m[0] === 'protect') ? ' <span style="color:var(--accent);font-size:11px">рекомендуем</span>' : '';
    return '<label style="display:flex;gap:10px;align-items:flex-start;padding:9px;border-radius:8px;' +
      'background:var(--bg2);margin-bottom:6px;cursor:pointer">' +
      '<input type="radio" name="fg_mode" value="' + m[0] + '"' + (cfg.mode === m[0] ? ' checked' : '') +
      ' style="margin-top:3px">' +
      '<div><div style="font-weight:500">' + esc(m[1]) + rec + '</div>' +
      '<div style="font-size:12px;color:var(--hint)">' + esc(m[2]) + '</div></div></label>';
  }).join('');

  // Порог
  h += '<div class="sec">2. Порог срабатывания</div>';
  h += '<div class="field"><label>Сколько новых подписчиков в минуту считать всплеском</label>' +
    '<input type="number" id="fg_threshold" min="1" value="' + (parseInt(cfg.threshold_per_min, 10) || 30) + '">' +
    '<div class="field-note">Ставьте чуть выше вашего обычного притока.<br>' +
    '• Небольшой бот (до сотен подписчиков в день): <b>20–40</b>.<br>' +
    '• Активный/крупный бот: <b>80–150</b>.<br>' +
    'Слишком низкий порог → под подозрение попадёт живой рост; слишком высокий → ' +
    'часть накрутки проскочит. Начните с 30 и подстройте по факту.</div></div>';

  h += '<button class="btn btn-p" style="width:100%;margin:6px 0 18px" onclick="fgSave()">💾 Сохранить настройки</button>';

  // Быстрые действия
  h += '<div class="sec">3. Быстрая очистка и блок</div>';
  h += '<div style="font-size:12px;color:var(--hint);padding:0 2px 8px">' +
    'Если атака уже прошла или защита была выключена — пометьте и уберите накрученных вручную.</div>';
  h += '<div style="display:flex;gap:8px;margin-bottom:4px">' +
    '<input type="number" id="fg_minutes" min="1" value="10" class="inp" style="flex:1" placeholder="минут">' +
    '<button class="btn btn-s" style="flex:2" onclick="fgFlagRecent()">🚫 Заблокировать волну</button></div>' +
    '<div style="font-size:12px;color:var(--hint);padding:0 2px 12px">Пометит всех, кто подписался за указанное число минут ' +
    '(например, за время атаки). Их можно потом «Очистить».</div>';
  h += '<button class="btn btn-s" style="width:100%;margin-bottom:4px" onclick="fgPurge(false)">🧹 Очистить накрученных — мягко</button>';
  h += '<div style="font-size:12px;color:var(--hint);padding:0 2px 10px">Убирает помеченных из аудитории рассылок. Строки остаются — можно вернуть кнопкой ниже.</div>';
  h += '<button class="btn btn-s" style="width:100%;color:var(--red);margin-bottom:4px" onclick="fgPurge(true)">🗑 Удалить накрученных — безвозвратно</button>';
  h += '<div style="font-size:12px;color:var(--hint);padding:0 2px 10px">Полностью удаляет помеченных из базы бота. Отменить нельзя.</div>';
  h += '<button class="btn btn-s" style="width:100%;margin-bottom:20px" onclick="fgUnflag()">↩️ Снять все флаги (вернуть в аудиторию)</button>';
  h += '<div style="font-size:12px;color:var(--hint);padding:0 2px 24px">Используйте, если защита пометила живых людей — снимает флаг «накрутка» со всех и возвращает их в аудиторию.</div>';
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
  // window.confirm во встроенном браузере Telegram часто блокируется: диалог не
  // показывался, вызов возвращал false — и кнопка просто не срабатывала.
  if (!await askConfirm('Пометить всех, кто присоединился за последние ' + minutes + ' мин, как накрученных?')) return;
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
  if (!await askConfirm(msg)) return;
  try {
    const r = await api('/api/miniapp/flood/' + _fgBotId + '/purge',
      { method: 'POST', body: JSON.stringify({ hard: !!hard }) });
    toast('Обработано: ' + (r.purged || 0));
    openFloodGuard(_fgBotId);
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function fgUnflag() {
  if (!await askConfirm('Снять флаги «накрутка» со всех подписчиков этого бота?')) return;
  try {
    const r = await api('/api/miniapp/flood/' + _fgBotId + '/unflag', { method: 'POST', body: '{}' });
    toast('Снято флагов: ' + (r.unflagged || 0));
    openFloodGuard(_fgBotId);
  } catch (e) { toast(e.message || 'Ошибка'); }
}
