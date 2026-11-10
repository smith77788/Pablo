// 🕵️ Поиск по нику: аккаунты человека по нику или почте на ~600 сайтах.
//
// Сам поиск — модуль blackbird (корневой каталог blackbird/, сервис
// services/osint_search.py). Экран ставит операцию osint_search и сразу
// отпускает человека: поиск идёт до пары минут, итог приходит сообщением в
// боте и появляется здесь в истории. Найденные профили — строки журнала
// операции (target = ссылка), поэтому список читается общей ручкой
// /api/miniapp/operation/{id}/log, а не своей.

function _osScreen() {
  let el = document.getElementById('s-osint');
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = 's-osint';
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="back" onclick="back()" role="button" tabindex="0" aria-label="Назад">←</div>' +
      '<div class="hdr-title">🕵️ Поиск по нику</div>' +
      '<div class="icon-btn" role="button" tabindex="0" aria-label="Обновить" ' +
        'style="margin-left:auto" onclick="openOsint()">🔄</div>' +
    '</div>' +
    '<div class="sb">' +
      '<div style="margin:8px 16px 0;font-size:12px;color:var(--hint);line-height:1.45">' +
        'Ищет аккаунты по нику или почте примерно на 600 сайтах: соцсети, форумы, ' +
        'сервисы для разработчиков, игры. Проверяются только открытые страницы, ' +
        'аккаунты Telegram не используются.</div>' +
      '<div style="padding:8px 16px">' +
        '<div class="field"><label>Ник или почта</label>' +
          '<input id="osintInput" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" ' +
          'placeholder="johndoe или john@example.com" ' +
          'onkeydown="if(event.key===\'Enter\')submitOsint()"></div>' +
        '<div class="field-err" id="osintErr"></div>' +
        '<button class="btn btn-p" data-main id="osintBtn" onclick="submitOsint()" style="width:100%">🔍 Найти</button>' +
      '</div>' +
      '<div class="sec" style="margin-top:8px">История поиска</div>' +
      '<div class="lst" id="osintHistory" style="margin:0 16px"></div>' +
    '</div>';
  document.body.appendChild(el);
  return el;
}

async function openOsint() {
  _osScreen();
  push('s-osint');
  const box = document.getElementById('osintHistory');
  box.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const d = await api('/api/miniapp/operations?op_type=osint_search&limit=20&offset=0');
    const ops = d.operations || [];
    if (!ops.length) {
      box.innerHTML = empty('🕵️', 'Поисков ещё не было', 'Здесь появятся ваши поиски и найденные профили');
      return;
    }
    box.innerHTML = ops.map(function (o) {
      const st = stb(o.status);
      return '<div class="row tap" onclick="openOsintResult(' + Number(o.id) + ')" role="button" tabindex="0">' +
        '<div class="row-ico" style="background:var(--bg-blue-16)">🕵️</div>' +
        '<div class="row-body">' +
          '<div class="row-name">' + esc(o.label || 'Поиск по нику') + '</div>' +
          '<div class="row-val"><span class="badge ' + st[0] + '">' + st[1] + '</span>' +
            (o.created_at ? ' · ' + esc(new Date(o.created_at).toLocaleString('ru-RU')) : '') + '</div>' +
        '</div><span class="chev">›</span></div>';
    }).join('');
  } catch (e) {
    box.innerHTML = errHtml(errRu(e), 'openOsint()');
  }
}

async function submitOsint() {
  const inp = document.getElementById('osintInput');
  const errEl = document.getElementById('osintErr');
  const q = (inp.value || '').trim();
  if (!q) { errEl.textContent = 'Введите ник или почту'; return; }
  const btn = document.getElementById('osintBtn');
  btn.disabled = true; btn.textContent = 'Запускаю…';
  try {
    const r = await api('/api/miniapp/osint_search', {method: 'POST', body: JSON.stringify({query: q})});
    errEl.textContent = '';
    inp.value = '';
    toast('🕵️ Ищу: ' + r.query + ' (#' + r.op_id + '). Итог придёт сообщением');
    openOsint();
  } catch (e) {
    errEl.textContent = errRu(e, 'Не удалось запустить поиск');
  } finally {
    btn.disabled = false; btn.textContent = '🔍 Найти';
  }
}

// Ссылки последнего открытого результата: в onclick уходит индекс, а не
// сама ссылка (апостроф в адресе порвал бы разметку атрибута).
let _osLinks = [];

function _osOpenLink(i) {
  const url = String(_osLinks[Number(i)] || '');
  if (!/^https?:\/\//i.test(url)) return;
  try { tg.openLink(url, {try_instant_view: false}); }
  catch (_) { window.open(url, '_blank'); }
}

function _osResultScreen() {
  let el = document.getElementById('s-osint-res');
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = 's-osint-res';
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="back" onclick="back()" role="button" tabindex="0" aria-label="Назад">←</div>' +
      '<div class="hdr-title" id="osintResTitle">🕵️ Результат</div>' +
    '</div>' +
    '<div class="sb"><div id="osintResBody" style="padding:6px 0"></div></div>';
  document.body.appendChild(el);
  return el;
}

async function openOsintResult(opId) {
  _osResultScreen();
  push('s-osint-res');
  const body = document.getElementById('osintResBody');
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const o = await api('/api/miniapp/operation/' + Number(opId));
    document.getElementById('osintResTitle').textContent = '🕵️ ' + (o.label || 'Результат');
    const st = stb(o.status);
    let out = '<div style="margin:8px 16px"><span class="badge ' + st[0] + '">' + st[1] + '</span></div>';
    if (o.status === 'pending' || o.status === 'running') {
      out += '<div style="margin:0 16px;font-size:13px;color:var(--hint);line-height:1.45">' +
        'Поиск ещё идёт. Обычно это до пары минут, итог придёт сообщением в боте.</div>' +
        '<div style="padding:12px 16px"><button class="btn btn-s" style="width:100%" ' +
        'onclick="back();openOsintResult(' + Number(opId) + ')">🔄 Проверить снова</button></div>';
      body.innerHTML = out;
      return;
    }
    if (o.error_msg) {
      out += '<div style="margin:0 16px;color:var(--red);font-size:13px">' + esc(o.error_msg) + '</div>';
    }
    const lg = await api('/api/miniapp/operation/' + Number(opId) + '/log?status=ok&limit=1000');
    const rows = (lg.logs || []).filter(function (r) { return r.target; });
    if (!rows.length) {
      out += o.error_msg ? '' : empty('🕵️', 'Аккаунтов не найдено', 'Попробуйте другой ник или почту');
    } else {
      out += '<div class="sec">Найдено профилей: ' + num(rows.length) + '</div><div class="lst" style="margin:0 16px">';
      _osLinks = rows.map(function (r) { return String(r.target); });
      out += rows.map(function (r, i) {
        const url = _osLinks[i];
        return '<div class="row tap" role="button" tabindex="0" onclick="_osOpenLink(' + i + ')">' +
          '<div class="row-body"><div class="row-name">' + esc(r.message || 'Сайт') + '</div>' +
          '<div class="row-val" style="word-break:break-all">' + esc(url) + '</div></div>' +
          '<span class="chev">↗</span></div>';
      }).join('');
      out += '</div>';
    }
    out += '<div style="display:flex;gap:8px;padding:12px 16px 20px">' +
      '<button class="btn btn-s" style="flex:1" onclick="openOpDetail(' + Number(opId) + ')">📋 Детали операции</button>' +
      '</div>';
    body.innerHTML = out;
  } catch (e) {
    body.innerHTML = errHtml(errRu(e), 'openOsintResult(' + Number(opId) + ')');
  }
}
