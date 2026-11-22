// Проверка наших каналов/чатов/ботов на ограничения и теневой бан Telegram.
// Read-only массовая операция: аккаунт-наблюдатель резолвит каждую публичную
// сущность, читает флаг ограничения, проверяет видимость в глобальном поиске и
// (для ботов) ответ на /start. Что реально отдаёт Telegram — то и показываем;
// «процент охвата» и точную причину теневого бана не выдумываем.

function _shScreen() {
  let el = document.getElementById('s-shieldcheck');
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = 's-shieldcheck';
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="back" onclick="back()">←</div>' +
      '<div class="hdr-title">🛡 Защита аккаунтов</div>' +
    '</div>' +
    // Полоска вкладок модуля «Защита аккаунтов» (SCREEN_MODULES.shield в index.html).
    '<div class="filter-chips mod-tabs" data-module="shield" data-tab="resources" role="tablist" aria-label="Вкладки модуля «Защита аккаунтов»"></div>' +
    '<div class="sub-sb"><div id="s-shieldcheck-body">' +
      '<div class="spin-wrap"><div class="spin"></div></div>' +
    '</div></div>';
  document.body.appendChild(el);
  // Экран рождается после загрузки страницы: общий рендер вкладок его не видел.
  if (typeof _renderModuleTabs === 'function') _renderModuleTabs();
  return el;
}

async function openShieldCheck() {
  _shScreen();
  push('s-shieldcheck');
  await _shLoadBody();
}

// Перезагрузка тела БЕЗ push — чтобы кнопка «🔄 Обновить» (после добавления
// прокси) обновляла счётчик различимых стран, не плодя экраны в истории.
async function _shLoadBody() {
  const body = document.getElementById('s-shieldcheck-body');
  if (!body) return;
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  let t = { channels: 0, bots: 0, total: 0 };
  try {
    t = await api('/api/miniapp/check_owned_restrictions/targets');
  } catch (e) {
    body.innerHTML = errHtml(errRu(e, 'Не удалось посчитать, что проверять'), 'openShieldCheck()');
    return;
  }
  // Гео-бан — отдельная проверка (нужны аккаунты в разных странах). Считаем её
  // цели мягко: не смогли — просто не показываем блок, основной экран важнее.
  let g = null;
  try { g = await api('/api/miniapp/check_bot_geoban/targets'); } catch (e) { g = null; }
  // Авто-мониторинг: периодические фоновые проверки. Тоже мягко.
  let mon = null;
  try { mon = await api('/api/miniapp/monitor/status'); } catch (e) { mon = null; }
  body.innerHTML = _shRender(t) + _shGeoRender(g) + _shMonRender(mon);
  loadShieldOps();
  loadDeadEntities();
}

// Переход из «Защиты» в карточку конкретной проверки с её сохранённым результатом.
async function loadDeadEntities() {
  const box = document.getElementById('shDeadBox');
  if (!box) return;
  try {
    const result = await api('/api/miniapp/restrictions/dead');
    box.innerHTML = '';
    if (!result.op_id) return;
    const unavailable = Array.isArray(result.dead) ? result.dead : [];
    const rows = result.cleanup_available ? '' : unavailable.map(item =>
      '<div class="row"><div class="row-body"><div class="row-name">'
      + esc(item.title || item.id) + '</div><div class="row-val">'
      + esc(item.reason || 'Недоступен снаружи') + '</div></div></div>').join('');
    box.innerHTML = '<div class="sec">Результат последней проверки</div>'
      + '<div class="lst" style="padding:14px">' + rows
      + '<button type="button" class="btn btn-s" style="width:100%">Открыть карточку проверки</button>'
      + (result.cleanup_available
        ? '<button type="button" class="btn btn-p" style="width:100%;margin-top:8px">Управлять найденными</button>'
        : '') + '</div>';
    const [details, manage] = box.querySelectorAll('button');
    details.onclick = () => openOpDetail(String(result.op_id));
    if (manage) manage.onclick = () => openOperationCleanup(String(result.op_id));
  } catch (_) {
    box.innerHTML = '';
  }
}

// Авто-мониторинг флота: периодические фоновые проверки (здоровье, ограничения,
// гео-бан). Включил — и флот проверяется сам на заданном интервале, находки
// идут в пульс. Переиспользует рекуррентные операции op_worker.
function _shMonFmtInterval(min) {
  const m = Number(min) || 0;
  if (m % (60 * 24) === 0 && m >= 60 * 24) { const d = m / (60 * 24); return 'каждые ' + d + ' ' + _shDayPlural(d); }
  if (m % 60 === 0) { const h = m / 60; return 'каждые ' + h + ' ' + _shHourPlural(h); }
  return 'каждые ' + m + ' мин';
}
function _shHourPlural(n) { const a = n % 10, b = n % 100; if (a === 1 && b !== 11) return 'час'; if (a >= 2 && a <= 4 && (b < 10 || b >= 20)) return 'часа'; return 'часов'; }
function _shDayPlural(n) { const a = n % 10, b = n % 100; if (a === 1 && b !== 11) return 'день'; if (a >= 2 && a <= 4 && (b < 10 || b >= 20)) return 'дня'; return 'дней'; }

// Интервал в списке был всегда «12 часов»: включённый на сутки мониторинг
// показывал рядом «каждые 12 часов», и следующий «Включить» молча ставил
// не тот темп. Теперь список стоит на интервале уже включённой проверки,
// иначе — на последнем выбранном владельцем, и только потом на 12 часах.
const _SH_MON_INTERVALS = [
  [720, 'каждые 12 часов'], [1440, 'каждые сутки'], [4320, 'каждые 3 дня'], [10080, 'каждую неделю'],
];
function _shMonSavedInterval() {
  const p = (typeof loadFormPrefs === 'function') ? (loadFormPrefs('shield_monitor') || {}) : {};
  const v = Number(p.interval_min);
  return Number.isFinite(v) && v > 0 ? v : null;
}
function _shMonInitialInterval(monitors, saved) {
  const on = ['health', 'restrictions', 'geoban']
    .map(k => monitors && monitors[k])
    .find(it => it && it.enabled && Number(it.interval_min) > 0);
  if (on) return Number(on.interval_min);
  if (Number(saved) > 0) return Number(saved);
  return 720;
}
function _shMonIntervalOptions(selected) {
  const opts = _SH_MON_INTERVALS.slice();
  // Интервал, заданный не из этого списка (через бота), не теряем: иначе
  // список показал бы первый пункт и выдал его за текущий.
  if (!opts.some(o => o[0] === selected)) opts.push([selected, _shMonFmtInterval(selected)]);
  return opts.map(o => '<option value="' + o[0] + '"' + (o[0] === selected ? ' selected' : '') + '>' + esc(o[1]) + '</option>').join('');
}

function _shMonRender(mon) {
  if (!mon || !mon.monitors) return '';
  const m = mon.monitors;
  const kinds = ['health', 'restrictions', 'geoban'];
  let rows = '';
  kinds.forEach(function (k) {
    const it = m[k]; if (!it) return;
    const on = !!it.enabled;
    const sub = on ? ('✅ Вкл · ' + _shMonFmtInterval(it.interval_min)) : 'Выкл';
    const btn = on
      ? '<button class="btn btn-s" onclick="shMonToggle(\'' + k + '\',false)" style="flex:0 0 auto;font-size:12px;padding:8px 12px">Выключить</button>'
      : '<button class="btn btn-p" onclick="shMonToggle(\'' + k + '\',true)" style="flex:0 0 auto;font-size:12px;padding:8px 12px">Включить</button>';
    rows +=
      '<div style="display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid var(--sep)">' +
        '<div style="flex:1 1 auto;min-width:0">' +
          '<div style="font-size:14px;font-weight:600">' + esc(it.label || k) + '</div>' +
          '<div style="font-size:12px;color:' + (on ? 'var(--green)' : 'var(--hint)') + '">' + sub + '</div>' +
        '</div>' + btn +
      '</div>';
  });
  return '' +
    '<div class="sec">🔁 Авто-мониторинг флота</div>' +
    '<div class="lst" style="padding:14px">' +
      '<div style="font-size:13px;line-height:1.5;color:var(--hint);margin-bottom:6px">' +
        'Проверяйте флот автоматически на заданном интервале — находки идут в пульс. Ничего не меняет.' +
      '</div>' +
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">' +
        '<span style="font-size:13px;color:var(--hint)">Интервал:</span>' +
        '<select id="shMonInterval" class="sel" style="flex:1 1 auto;padding:8px;font-size:13px">' +
          _shMonIntervalOptions(_shMonInitialInterval(m, _shMonSavedInterval())) +
        '</select>' +
      '</div>' +
      rows +
    '</div>';
}

async function shMonToggle(kind, enabled) {
  let interval = 720;
  const sel = document.getElementById('shMonInterval');
  if (sel && enabled) { const v = parseInt(sel.value, 10); if (Number.isFinite(v)) interval = v; }
  if (enabled && typeof saveFormPrefs === 'function') saveFormPrefs('shield_monitor', { interval_min: interval });
  try {
    await api('/api/miniapp/monitor/toggle', { method: 'POST', body: JSON.stringify({ kind: kind, enabled: enabled, interval_min: interval }) });
    tg.HapticFeedback?.notificationOccurred('success');
    toast(enabled ? '🔁 Авто-мониторинг включён' : '⏹ Авто-мониторинг выключен');
    _shLoadBody();
  } catch (e) {
    tg.HapticFeedback?.notificationOccurred('error');
    toast('⚠️ ' + errRu(e, 'Не удалось изменить мониторинг'));
  }
}

// Блок гео-доступности: открываются ли наши каналы, чаты и боты в ОДНИХ
// странах и закрыты ли в ДРУГИХ. Виден только при аккаунтах минимум в 2
// странах — честно про это пишем.
function _shGeoRender(g) {
  if (!g) return '';
  const bots = Number(g.bots) || 0;
  const channels = Number(g.channels) || 0;
  const total = Number(g.total) || (bots + channels);
  const geos = Number(g.geos) || 0;
  if (total === 0) return '';
  let note, btn;
  if (geos < 2) {
    note = 'Для проверки гео-доступности нужны аккаунты с прокси минимум в <b>2 разных странах</b> — ' +
           'сейчас различимо: <b>' + geos + '</b>. Добавьте прокси ещё одной страны прямо здесь — ' +
           'страна определится автоматически.';
    btn = '<button class="btn btn-s" disabled style="width:100%;margin-bottom:8px">🌍 Нужны аккаунты в 2+ странах</button>';
  } else {
    note = 'Откроет каждый ваш публичный канал, чат и бота (' + channels + ' ' + _shChanPlural(channels) +
           ', ' + bots + ' ' + _shBotPlural(bots) + ') с аккаунтов <b>' + geos + '</b> разных стран ' +
           'и покажет отчёт: где ресурс доступен, а где заблокирован. Ничего не меняет.';
    btn = '<button class="btn btn-p" id="shGeoBtn" onclick="runBotGeoban()" style="width:100%;margin-bottom:8px">' +
          '🌍 Проверить ' + total + ' ' + _shResPlural(total) + ' по странам</button>';
  }
  // Добавить прокси прямо отсюда (страна определится авто) + обновить счётчик.
  const tools =
    '<div style="display:flex;gap:8px">' +
      '<button class="btn btn-s" onclick="shAddProxy()" style="flex:1 1 auto;font-size:13px;padding:10px 6px">➕ Добавить прокси</button>' +
      '<button class="btn btn-s" onclick="_shLoadBody()" style="flex:0 0 auto;font-size:13px;padding:10px 12px" title="Обновить счётчик стран">🔄</button>' +
    '</div>';
  return '' +
    '<div class="sec">🌍 Гео-доступность ресурсов</div>' +
    '<div class="lst" style="padding:14px">' +
      '<div style="font-size:13px;line-height:1.5;color:var(--hint);margin-bottom:12px">' + note + '</div>' +
      btn +
      tools +
    '</div>';
}

function _shResPlural(n) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return 'ресурс';
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return 'ресурса';
  return 'ресурсов';
}

function _shChanPlural(n) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return 'канал или чат';
  return 'каналов и чатов';
}

// Добавить прокси, не уходя с экрана: открываем общую модалку. Страну новый
// прокси получает авто (services.proxy_geo при POST /proxy). После закрытия
// модалки владелец жмёт «🔄», чтобы увидеть обновлённый счётчик стран.
function shAddProxy() {
  if (typeof openProxyModal === 'function') {
    openProxyModal();
    toast('После добавления нажмите 🔄, чтобы обновить счётчик стран');
  } else {
    toast('Откройте раздел «Прокси», чтобы добавить прокси');
  }
}

async function runBotGeoban() {
  const btn = document.getElementById('shGeoBtn');
  // После сбоя кнопка возвращает СВОЮ подпись со счётчиком, а не урезанную
  // «Проверить по странам»: иначе владелец теряет, сколько ресурсов уйдёт.
  const label = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Ставлю в очередь…'; }
  try {
    const d = await api('/api/miniapp/check_bot_geoban', { method: 'POST', body: JSON.stringify({}) });
    tg.HapticFeedback?.notificationOccurred('success');
    if (d && d.op_id && typeof openOpDetail === 'function') {
      openOpDetail(d.op_id);
    } else {
      toast('🌍 Проверка гео-доступности запущена');
      loadShieldOps();
    }
  } catch (e) {
    tg.HapticFeedback?.notificationOccurred('error');
    toast('⚠️ ' + errRu(e, 'Не удалось запустить проверку гео-доступности'));
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

function _shRender(t) {
  const total = Number(t.total) || 0;
  const disabled = total === 0 ? ' disabled' : '';
  return '' +
    '<div class="sec">Что проверяем</div>' +
    '<div class="lst" style="padding:12px 14px;font-size:13px;line-height:1.5;color:var(--hint)">' +
      'Наблюдающий аккаунт по очереди проверяет каждый ваш канал, чат и бота:' +
      '<br>• <b>официальное ограничение</b> — Telegram прямо помечает сущность (блок по стране, копирайт и т.п.);' +
      '<br>• <b>скрыт из поиска</b> — публичная ссылка открывается, но в глобальном поиске сущность не находится (теневой бан);' +
      '<br>• <b>недоступен снаружи</b> — ссылка не открывается с чужого аккаунта;' +
      '<br>• для бота — <b>отвечает ли он на /start</b>.' +
      '<br><br>Проверка ничего не меняет и не публикует. Идёт бережным темпом, ' +
      'поэтому на большом наборе занимает время.' +
    '</div>' +
    '<div class="sec">К проверке</div>' +
    '<div class="lst" style="padding:14px">' +
      '<div style="display:flex;gap:16px;margin-bottom:12px">' +
        '<div><div style="font-size:22px;font-weight:600">' + (Number(t.channels) || 0) + '</div>' +
          '<div style="font-size:12px;color:var(--hint)">каналов и чатов</div></div>' +
        '<div><div style="font-size:22px;font-weight:600">' + (Number(t.bots) || 0) + '</div>' +
          '<div style="font-size:12px;color:var(--hint)">ботов</div></div>' +
      '</div>' +
      (total === 0
        ? '<div style="font-size:13px;color:var(--hint)">Сначала подключите каналы/чаты или ботов — проверять пока нечего.</div>'
        : '<button class="btn btn-p" id="shRunBtn" onclick="runShieldCheck()" style="width:100%"' + disabled + '>' +
            '🛡 Проверить ' + total + ' ' + _shPlural(total) + '</button>') +
    '</div>' +
    // Быстрая уборка мёртвых карточек после проверки (заполняется loadDeadEntities).
    '<div id="shDeadBox"></div>' +
    '<div class="sec">Последние проверки</div>' +
    '<div class="lst" id="shOpsList"><div class="spin-wrap"><div class="spin"></div></div></div>';
}

function _shPlural(n) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return 'сущность';
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return 'сущности';
  return 'сущностей';
}

function _shBotPlural(n) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return 'бота';
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return 'бота';
  return 'ботов';
}

async function runShieldCheck() {
  const btn = document.getElementById('shRunBtn');
  const label = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Ставлю в очередь…'; }
  try {
    const d = await api('/api/miniapp/check_owned_restrictions', { method: 'POST', body: JSON.stringify({}) });
    tg.HapticFeedback?.notificationOccurred('success');
    if (d && d.op_id && typeof openOpDetail === 'function') {
      // Переходим в готовый экран деталей операции — там прогресс и по каждой
      // сущности вердикт (наш operation_log), не дублируем это здесь.
      openOpDetail(d.op_id);
    } else {
      toast('🛡 Проверка запущена');
      loadShieldOps();
    }
  } catch (e) {
    tg.HapticFeedback?.notificationOccurred('error');
    toast('⚠️ ' + errRu(e, 'Не удалось запустить проверку'));
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

async function loadShieldOps() {
  const box = document.getElementById('shOpsList');
  if (!box) return;
  try {
    const d = await api('/api/miniapp/operations?op_type=check_owned_restrictions&limit=5&offset=0');
    const ops = d.operations || [];
    const opsTotal = (d.total != null) ? d.total : ops.length;
    if (!ops.length) {
      box.innerHTML = empty('🛡', 'Проверок ещё не было', 'Здесь появятся ваши проверки ограничений');
      return;
    }
    box.innerHTML = ops.map(function (o) {
      const pct = o.total_items > 0 ? Math.round((o.done_items || 0) / o.total_items * 100) : 0;
      const bl = (typeof stb === 'function') ? stb(o.status) : ['b-gr', o.status];
      return '<div class="li tap" onclick="openOpDetail(' + o.id + ')" style="display:block;padding:11px 14px">' +
        '<div style="display:flex;align-items:center;gap:10px">' +
          '<div class="ava ava-purple">🛡</div>' +
          '<div class="li-body">' +
            '<div class="li-name">Проверка ограничений</div>' +
            '<div class="li-sub">' + progFrac(o) + ' · ' + ago(o.created_at) + '</div>' +
          '</div>' +
          '<span class="badge ' + bl[0] + '">' + bl[1] + '</span>' +
        '</div>' +
        (o.status === 'running' ? '<div class="prog" style="margin-top:7px"><div class="prog-fill" style="width:' + pct + '%"></div></div>' : '') +
      '</div>';
    }).join('') + (opsTotal > ops.length
      ? '<div class="li tap" onclick="openOps()"><div class="li-body"><div class="li-name">Вся история проверок — ' + opsTotal + '</div></div><span class="chev">›</span></div>'
      : '');
  } catch (e) {
    box.innerHTML = errHtml(errRu(e, 'Не удалось загрузить проверки'), 'loadShieldOps()');
  }
}

function _restrScreen() {
  let el = document.getElementById('s-restrcleanup');
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = 's-restrcleanup';
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="back" onclick="back()" role="button" tabindex="0" aria-label="Назад">←</div>' +
      '<div class="hdr-title">🧹 Убрать проблемные</div>' +
    '</div>' +
    '<div class="sub-sb" id="s-restrcleanup-body"><div class="spin-wrap"><div class="spin"></div></div></div>';
  document.body.appendChild(el);
  return el;
}

async function openRestrictionCleanup(opId) {
  if (opId && String(opId) !== '0') return openOpDetail(String(opId));
  _restrScreen();
  push('s-restrcleanup');
  const body = document.getElementById('s-restrcleanup-body');
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const report = await api('/api/miniapp/restrictions/dead');
    if (report.cleanup_available && report.op_id) {
      body.innerHTML = '<div class="actions"><button type="button" class="btn btn-p">Управлять найденными</button>'
        + '<button type="button" class="btn btn-s">Открыть карточку проверки</button></div>';
      const [manage, details] = body.querySelectorAll('button');
      manage.onclick = () => openOperationCleanup(String(report.op_id));
      details.onclick = () => openOpDetail(String(report.op_id));
      return;
    }
    const d = await api('/api/miniapp/restrictions/problems');
    body.innerHTML = _restrCleanupRender((d && d.problems) || []);
  } catch (e) {
    body.innerHTML = errHtml(errRu(e, 'Не удалось получить список'), retryCall('openRestrictionCleanup', 0));
  }
}

function _restrCleanupRender(list) {
  if (!list.length) {
    return '<div style="padding:8px 0">' + empty('✅', 'Проблемных ресурсов нет', 'Последняя проверка не нашла заблокированных, скрытых или недоступных — убирать нечего') + '</div>';
  }
  const kindIco = { channel: '📡', group: '💬', bot: '🤖' };
  const rows = list.map(function (x) {
    const ico = kindIco[x.kind] || '🔗';
    const stColor = x.status === 'restricted' ? 'var(--red)' : x.status === 'unreachable' ? 'var(--red)' : 'var(--orange)';
    // Выбрать можно ЛЮБОЙ проблемный ресурс с id. Недоступные (в Telegram уже
    // мертвы) отмечены сразу; заблокированные и скрытые из поиска живы, поэтому
    // не отмечены по умолчанию — владелец решает по каждому сам.
    return '<label class="chk-row" style="display:flex;align-items:flex-start;gap:8px;padding:9px 0;border-top:1px solid var(--sep);font-size:14px">' +
      '<input type="checkbox" class="restr-cb" ' + (x.id_text ? (x.dead ? 'checked' : '') : 'disabled') + ' value="' + esc(x.id_text || '') + '" data-kind="' + esc(x.kind || '') + '" style="margin-top:3px">' +
      '<div style="flex:1 1 auto;min-width:0">' +
        '<div style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + ico + ' ' + esc(x.title || x.id) + '</div>' +
        '<div style="font-size:12px;margin-top:2px"><span style="color:' + stColor + '">' + esc(x.status_label || '') + '</span>' + (x.reason ? ('<span style="color:var(--hint)"> · ' + esc(x.reason) + '</span>') : '') + '</div>' +
      '</div></label>';
  }).join('');
  return '<div style="font-size:13px;line-height:1.5;color:var(--hint);margin:12px 14px 6px">' +
      'Проверка нашла <b>' + list.length + '</b> ' + _shPlural(list.length) + ' с проблемами. ' +
      'Отметьте, что убрать из вашего списка. <b>Недоступные</b> в Telegram уже мертвы — отмечены сразу. ' +
      '<b>Заблокированные</b> и <b>скрытые из поиска</b> ещё живы: удаление убирает их только из вашего списка в Infragram, в Telegram они остаются.' +
    '</div>' +
    '<div style="display:flex;gap:8px;margin:0 14px 8px">' +
      '<button class="btn btn-s" onclick="restrCleanupSelectAll(true)" style="flex:1 1 auto;font-size:13px;padding:8px 6px">✅ Все</button>' +
      '<button class="btn btn-s" onclick="restrCleanupSelectAll(false)" style="flex:1 1 auto;font-size:13px;padding:8px 6px">⬜ Снять</button>' +
    '</div>' +
    '<div class="lst" style="margin:0 14px">' + rows + '</div>' +
    '<div class="actions" style="padding:10px 14px 4px">' +
      '<button class="btn btn-d" onclick="restrCleanupRemove()" style="width:100%">🗑 Убрать выбранные из списка</button>' +
    '</div>' +
    '<div id="restrCleanupErr" style="color:var(--red);font-size:13px;margin:6px 14px"></div>';
}

function restrCleanupSelectAll(on) {
  document.querySelectorAll('.restr-cb').forEach(function (cb) { cb.checked = !!on && !cb.disabled; });
}

async function restrCleanupRemove() {
  const errEl = document.getElementById('restrCleanupErr');
  if (errEl) errEl.textContent = '';
  const checked = [].slice.call(document.querySelectorAll('.restr-cb:checked'));
  const channel_ids = [], bot_ids = [];
  checked.forEach(function (cb) {
    const id = cb.value;
    if (!/^-?[1-9]\d*$/.test(id)) return;
    if (cb.getAttribute('data-kind') === 'bot') bot_ids.push(id); else channel_ids.push(id);
  });
  const n = channel_ids.length + bot_ids.length;
  if (!n) { if (errEl) errEl.textContent = 'Отметьте хотя бы один ресурс'; return; }
  if (!await askConfirm('Убрать ' + n + ' ' + _shPlural(n) + ' из вашего списка?\n\nЭто снятие с учёта в Infragram. Telegram не трогается: недоступные там уже мертвы, а заблокированные и скрытые останутся в Telegram, просто уйдут из вашего каталога.')) return;
  const payload = {};
  if (channel_ids.length) payload.channel_ids = channel_ids;
  if (bot_ids.length) payload.bot_ids = bot_ids;
  try {
    const r = await api('/api/miniapp/restrictions/remove_dead', { method: 'POST', body: JSON.stringify(payload) });
    tg.HapticFeedback?.notificationOccurred('success');
    toast('🗑 Убрано из списка: ' + (r.removed || 0));
    openRestrictionCleanup(0);
  } catch (e) {
    tg.HapticFeedback?.notificationOccurred('error');
    if (errEl) errEl.textContent = errRu(e, 'Не удалось убрать');
  }
}
