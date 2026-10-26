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
  const body = document.getElementById('s-shieldcheck-body');
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
  body.innerHTML = _shRender(t) + _shGeoRender(g);
  loadShieldOps();
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
           'сейчас различимо: <b>' + geos + '</b>. Подключите аккаунт с прокси ещё одной страны.';
    btn = '<button class="btn btn-s" disabled style="width:100%">🌍 Нужны аккаунты в 2+ странах</button>';
  } else {
    note = 'Проверит каждого публичного бота с аккаунтов <b>' + geos + '</b> разных стран и покажет, ' +
           'где он ограничен, а где доступен. Ничего не меняет.';
    btn = '<button class="btn btn-p" id="shGeoBtn" onclick="runBotGeoban()" style="width:100%">' +
          '🌍 Проверить гео-бан ' + bots + ' ' + _shBotPlural(bots) + '</button>';
  }
  return '' +
    '<div class="sec">🌍 Гео-бан ботов</div>' +
    '<div class="lst" style="padding:14px">' +
      '<div style="font-size:13px;line-height:1.5;color:var(--hint);margin-bottom:12px">' + note + '</div>' +
      btn +
    '</div>';
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
