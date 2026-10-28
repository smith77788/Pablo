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
      '<div class="hdr-title">🛡 Проверка ограничений</div>' +
    '</div>' +
    '<div class="sub-sb"><div id="s-shieldcheck-body">' +
      '<div class="spin-wrap"><div class="spin"></div></div>' +
    '</div></div>';
  document.body.appendChild(el);
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
    body.innerHTML = errHtml('Не удалось посчитать сущности: ' + ((e && e.message) || ''), 'openShieldCheck()');
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
          '<option value="720" selected>каждые 12 часов</option>' +
          '<option value="1440">каждые сутки</option>' +
          '<option value="4320">каждые 3 дня</option>' +
          '<option value="10080">каждую неделю</option>' +
        '</select>' +
      '</div>' +
      rows +
    '</div>';
}

async function shMonToggle(kind, enabled) {
  let interval = 720;
  const sel = document.getElementById('shMonInterval');
  if (sel && enabled) { const v = parseInt(sel.value, 10); if (Number.isFinite(v)) interval = v; }
  try {
    await api('/api/miniapp/monitor/toggle', { method: 'POST', body: JSON.stringify({ kind: kind, enabled: enabled, interval_min: interval }) });
    tg.HapticFeedback?.notificationOccurred('success');
    toast(enabled ? '🔁 Авто-мониторинг включён' : '⏹ Авто-мониторинг выключен');
    _shLoadBody();
  } catch (e) {
    tg.HapticFeedback?.notificationOccurred('error');
    toast((e && e.message) || 'Не удалось изменить мониторинг');
  }
}

// Блок проверки гео-бана ботов: ограничен ли бот в ОДНИХ странах и доступен в
// ДРУГИХ. Виден только при аккаунтах минимум в 2 странах — честно про это пишем.
function _shGeoRender(g) {
  if (!g) return '';
  const bots = Number(g.bots) || 0;
  const geos = Number(g.geos) || 0;
  if (bots === 0) return '';
  let note, btn;
  if (geos < 2) {
    note = 'Для проверки гео-бана нужны аккаунты с прокси минимум в <b>2 разных странах</b> — ' +
           'сейчас различимо: <b>' + geos + '</b>. Добавьте прокси ещё одной страны прямо здесь — ' +
           'страна определится автоматически.';
    btn = '<button class="btn btn-s" disabled style="width:100%;margin-bottom:8px">🌍 Нужны аккаунты в 2+ странах</button>';
  } else {
    note = 'Проверит каждого публичного бота с аккаунтов <b>' + geos + '</b> разных стран и покажет, ' +
           'где он ограничен, а где доступен. Ничего не меняет.';
    btn = '<button class="btn btn-p" id="shGeoBtn" onclick="runBotGeoban()" style="width:100%;margin-bottom:8px">' +
          '🌍 Проверить гео-бан ' + bots + ' ' + _shBotPlural(bots) + '</button>';
  }
  // Добавить прокси прямо отсюда (страна определится авто) + обновить счётчик.
  const tools =
    '<div style="display:flex;gap:8px">' +
      '<button class="btn btn-s" onclick="shAddProxy()" style="flex:1 1 auto;font-size:13px;padding:10px 6px">➕ Добавить прокси</button>' +
      '<button class="btn btn-s" onclick="_shLoadBody()" style="flex:0 0 auto;font-size:13px;padding:10px 12px" title="Обновить счётчик стран">🔄</button>' +
    '</div>';
  return '' +
    '<div class="sec">🌍 Гео-бан ботов</div>' +
    '<div class="lst" style="padding:14px">' +
      '<div style="font-size:13px;line-height:1.5;color:var(--hint);margin-bottom:12px">' + note + '</div>' +
      btn +
      tools +
    '</div>';
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
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Ставлю в очередь…'; }
  try {
    const d = await api('/api/miniapp/check_bot_geoban', { method: 'POST', body: JSON.stringify({}) });
    tg.HapticFeedback?.notificationOccurred('success');
    if (d && d.op_id && typeof openOpDetail === 'function') {
      openOpDetail(d.op_id);
    } else {
      toast('🌍 Проверка гео-бана запущена');
      loadShieldOps();
    }
  } catch (e) {
    tg.HapticFeedback?.notificationOccurred('error');
    toast((e && e.message) || 'Не удалось запустить проверку гео-бана');
    if (btn) { btn.disabled = false; btn.textContent = '🌍 Проверить гео-бан'; }
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
    toast((e && e.message) || 'Не удалось запустить проверку');
    if (btn) { btn.disabled = false; btn.textContent = '🛡 Проверить'; }
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
    box.innerHTML = errHtml('Не удалось загрузить проверки: ' + ((e && e.message) || ''), 'loadShieldOps()');
  }
}
