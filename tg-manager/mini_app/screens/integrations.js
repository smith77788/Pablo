// Интеграции: ключи API, исходящие вебхуки на события, Google Sheets.
// Самодостаточный экран (создаёт свою разметку). Бэкенд —
// services/mini_app_integrations.py. Классический скрипт: делит глобальную
// область с index.html, поэтому имена — с приставкой _ig.

let _igData = null;
let _igWhId = null;

function _igScreen(id, title, reloadFn) {
  let el = document.getElementById(id);
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = id;
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="hdr-burger" onclick="back()">‹</div>' +
      '<div class="hdr-title">' + esc(title) + '</div>' +
      '<div class="hdr-burger" onclick="' + reloadFn + '" title="Обновить">⟳</div>' +
    '</div>' +
    '<div class="sb"><div id="' + id + '-body" style="padding:6px 0">' +
      '<div class="spin-wrap"><div class="spin"></div></div>' +
    '</div></div>';
  document.body.appendChild(el);
  return el;
}

function _igBox(inner, tone) {
  const bg = tone === 'ok' ? 'rgba(52,211,153,.10)' : tone === 'bad' ? 'rgba(248,113,113,.10)' : 'var(--bg2)';
  return '<div style="padding:10px 12px;border-radius:10px;margin-bottom:10px;background:' + bg + '">' + inner + '</div>';
}

function _igHint(t) {
  return '<div style="font-size:12px;color:var(--hint);margin-top:4px;line-height:1.45">' + t + '</div>';
}

async function openIntegrations() {
  _igScreen('s-integrations', '🔌 Интеграции', 'openIntegrations()');
  push('s-integrations');
  const body = document.getElementById('s-integrations-body');
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    _igData = await api('/api/miniapp/integrations');
    body.innerHTML = _igRender(_igData);
  } catch (e) {
    body.innerHTML = errHtml(errRu(e), 'openIntegrations()');
  }
}

function _igRender(d) {
  let h = '';
  // ── API ──
  const keys = (d.keys || []).filter(k => k.is_active);
  h += '<div class="sec">🔑 Публичный API</div>';
  h += _igBox(
    '<div><b>Адрес API:</b> <code style="word-break:break-all">' + esc(d.api_base || '') + '</code></div>' +
    _igHint('Ключ передаётся заголовком <code>X-Api-Key</code>. Владелец берётся из ключа — ' +
      'передавать owner_id не нужно. Доступно: <code>GET /me</code>, <code>/accounts</code>, ' +
      '<code>/operations</code>, <code>/operations/{id}</code>, <code>/get_messages</code>, ' +
      '<code>POST /send_message</code>, <code>/click_button</code>.') +
    '<div style="margin-top:8px">Активных ключей: <b>' + keys.length + '</b> из ' + (d.max_keys || 5) + '</div>' +
    '<div style="display:flex;gap:8px;margin-top:8px">' +
      '<button class="btn btn-p" style="flex:1" onclick="igCreateKey(false)">+ Ключ</button>' +
      '<button class="btn btn-s" style="flex:1" onclick="igCreateKey(true)">+ Только чтение</button>' +
      '<button class="btn btn-s" style="flex:1" onclick="openApiKeys()">Все ключи</button>' +
    '</div>');

  // ── Вебхуки ──
  h += '<div class="sec">📡 Вебхуки на события</div>';
  h += _igHint('Infragram сам сообщит вашей системе (n8n, Make, Zapier, свой сервер), когда операция ' +
    'закончилась, аккаунт добавлен, потерян или получил ограничение. Запрос подписан ' +
    '(<code>X-Infragram-Signature</code>), неудачные доставки повторяются до 8 раз.');
  const eps = d.endpoints || [];
  if (!eps.length) {
    h += _igBox('<div style="color:var(--hint)">Получателей пока нет.</div>');
  }
  h += eps.map(ep => {
    const where = ep.kind === 'sheets'
      ? '📊 Таблица · лист «' + esc(ep.sheet_name) + '»'
      : '🌐 ' + esc(ep.url);
    const evs = (ep.events && ep.events.length) ? ep.events.length + ' соб.' : 'все события';
    const state = ep.is_active
      ? (ep.consecutive_failures ? '🟡 сбоев подряд: ' + ep.consecutive_failures : '🟢 работает')
      : '🔴 выключен';
    return '<div class="row" onclick="openWebhook(' + ep.id + ')" style="cursor:pointer">' +
      '<div class="row-body">' +
        '<div class="row-name">' + esc(ep.name || ('Получатель #' + ep.id)) + '</div>' +
        '<div class="row-val" style="word-break:break-all">' + where + '</div>' +
        '<div class="row-val" style="color:var(--hint)">' + state + ' · ' + evs +
          (ep.last_success_at ? ' · доставлено ' + ago(ep.last_success_at) : '') + '</div>' +
        (ep.disabled_reason ? '<div class="row-val" style="color:var(--red)">' + esc(ep.disabled_reason) + '</div>' : '') +
      '</div><div class="row-right">›</div></div>';
  }).join('');
  h += '<div style="display:flex;gap:8px;margin:8px 0 4px">' +
    '<button class="btn btn-p" style="flex:1" onclick="igNewWebhook(\'http\')">+ Вебхук (URL)</button>' +
    '<button class="btn btn-s" style="flex:1" onclick="igNewWebhook(\'sheets\')">+ Лента в таблицу</button>' +
  '</div>';

  // ── Google Sheets ──
  h += '<div class="sec">📊 Google Sheets</div>';
  if (d.sheets) {
    h += _igBox('<div><b>Подключено:</b> <code style="word-break:break-all">' + esc(d.sheets.client_email) + '</code></div>' +
      _igHint('Откройте свою таблицу на эту почту с правом <b>редактора</b> — иначе Google не даст в неё писать.') +
      '<div style="display:flex;gap:8px;margin-top:8px">' +
        '<button class="btn btn-s" style="flex:1" onclick="igCopySheetsEmail()">📋 Почта</button>' +
        '<button class="btn btn-d" style="flex:1" onclick="igSheetsDisconnect()">Отключить</button>' +
      '</div>', 'ok');
    h += '<div class="field"><label>Ссылка на таблицу</label>' +
      '<input id="ig-sheet" type="url" class="inp" placeholder="https://docs.google.com/spreadsheets/d/…"></div>';
    h += '<div class="field"><label>Что выгрузить</label><select id="ig-ds" class="inp">' +
      (d.datasets || []).map(x => '<option value="' + esc(x.id) + '">' + esc(x.label) + '</option>').join('') +
      '</select></div>';
    h += '<div style="display:flex;gap:8px;margin-bottom:8px">' +
      '<button class="btn btn-s" style="flex:1" onclick="igSheetsCheck()">Проверить доступ</button>' +
      '<button class="btn btn-p" style="flex:2" id="ig-export-btn" onclick="igSheetsExport()">Выгрузить на новый лист</button>' +
    '</div><div id="ig-sheet-res"></div>';
  } else {
    h += _igBox(
      '<div><b>Как подключить</b></div>' + _igHint(
      '1. В Google Cloud создайте сервисный аккаунт и включите Google Sheets API.<br>' +
      '2. Создайте для него ключ JSON и вставьте содержимое файла ниже.<br>' +
      '3. Откройте свою таблицу на почту сервисного аккаунта с правом редактора.<br>' +
      'Ключ хранится зашифрованным и никому не показывается.') +
      '<textarea id="ig-sa" class="input-bc" rows="4" style="width:100%;margin-top:8px" placeholder="{&quot;type&quot;: &quot;service_account&quot;, …}"></textarea>' +
      '<button class="btn btn-p" style="width:100%;margin-top:8px" onclick="igSheetsConnect()">Подключить</button>');
  }
  return h;
}

function igCopySheetsEmail() {
  const em = _igData && _igData.sheets && _igData.sheets.client_email;
  if (em) copyToClipboard(em);
}

// ── Ключи ──────────────────────────────────────────────────────────────────
async function igCreateKey(readOnly) {
  const name = await askPrompt(readOnly ? 'Название ключа (только чтение)' : 'Название ключа',
    { value: readOnly ? 'Отчёты' : 'n8n' });
  if (name === null || name === undefined) return;
  try {
    const r = await api('/api/miniapp/api_key', { method: 'POST',
      body: JSON.stringify({ name: name || '', scopes: readOnly ? ['read'] : ['read', 'write'] }) });
    // Сначала перерисовка (push закрывает все окна), потом окно с ключом.
    await openIntegrations();
    _igShowSecret('🔑 Ключ создан', r.key, 'Ключ показывается один раз — сохраните его сейчас.');
  } catch (e) { toast('⚠️ ' + errRu(e)); }
}

function _igShowSecret(title, value, note) {
  const bg = document.createElement('div');
  bg.className = 'mbg show';
  bg.style.zIndex = '9999';
  bg.innerHTML = '<div class="modal"><div class="modal-title">' + esc(title) + '</div>' +
    '<div style="font-size:13px;color:var(--hint);margin-bottom:6px">⚠️ ' + esc(note) + '</div>' +
    '<div style="font-family:monospace;font-size:13px;word-break:break-all;color:var(--green);user-select:all">' + esc(value) + '</div>' +
    '<div style="display:flex;gap:8px;margin-top:12px">' +
      '<button class="btn btn-s" style="flex:1" id="_igcp">📋 Скопировать</button>' +
      '<button class="btn btn-p" style="flex:1" id="_igok">Готово</button>' +
    '</div></div>';
  document.body.appendChild(bg);
  bg.querySelector('#_igcp').onclick = () => copyToClipboard(value);
  bg.querySelector('#_igok').onclick = () => bg.remove();
}

// ── Вебхуки ────────────────────────────────────────────────────────────────
function _igEventChecks(selected) {
  const sel = new Set(selected || []);
  return (_igData && _igData.events || []).map(ev =>
    '<label style="display:flex;gap:8px;align-items:center;padding:6px 8px;border-radius:8px;background:var(--bg2);margin-bottom:4px">' +
    '<input type="checkbox" class="ig-ev" value="' + esc(ev.id) + '"' + (sel.has(ev.id) ? ' checked' : '') + '>' +
    '<span>' + esc(ev.label) + ' <span style="color:var(--hint);font-size:11px">' + esc(ev.id) + '</span></span></label>'
  ).join('');
}

function _igCheckedEvents() {
  return Array.from(document.querySelectorAll('.ig-ev:checked')).map(x => x.value);
}

function igNewWebhook(kind) {
  if (kind === 'sheets' && !(_igData && _igData.sheets)) {
    toast('Сначала подключите Google Sheets — раздел ниже');
    return;
  }
  _igScreen('s-ig-new', '📡 Новый получатель', 'igNewWebhook(_igNewKind)');
  window._igNewKind = kind;
  push('s-ig-new');
  const body = document.getElementById('s-ig-new-body');
  let h = '<div class="field"><label>Название</label><input id="ig-wh-name" class="inp" maxlength="64" placeholder="Например: CRM"></div>';
  if (kind === 'sheets') {
    h += '<div class="field"><label>Ссылка на таблицу</label><input id="ig-wh-sheet" class="inp" type="url" placeholder="https://docs.google.com/spreadsheets/d/…"></div>' +
      '<div class="field"><label>Лист</label><input id="ig-wh-tab" class="inp" maxlength="100" value="События Infragram"></div>' +
      _igHint('Каждое событие станет строкой. Лист создаётся сам при первой записи.');
  } else {
    h += '<div class="field"><label>Адрес (https)</label><input id="ig-wh-url" class="inp" type="url" placeholder="https://example.com/hooks/infragram"></div>' +
      _igHint('Ответ 2xx — доставлено. Иначе повторим через 1, 5, 15 минут, час и дальше. Повтор несёт тот же X-Infragram-Event-Id.');
  }
  h += '<div class="sec">События</div>' + _igHint('Ничего не отмечено — придут все события.') +
    '<div style="margin-top:6px">' + _igEventChecks([]) + '</div>' +
    '<div class="field-err" id="ig-wh-err"></div>' +
    '<button class="btn btn-p" style="width:100%;margin-top:8px" id="ig-wh-save" onclick="igSaveWebhook()">Сохранить</button>';
  body.innerHTML = h;
}

async function igSaveWebhook() {
  const kind = window._igNewKind || 'http';
  const btn = document.getElementById('ig-wh-save');
  const err = document.getElementById('ig-wh-err');
  err.textContent = '';
  btn.disabled = true;
  const payload = { kind, name: document.getElementById('ig-wh-name').value, events: _igCheckedEvents() };
  if (kind === 'sheets') {
    payload.spreadsheet = document.getElementById('ig-wh-sheet').value;
    payload.sheet_name = document.getElementById('ig-wh-tab').value;
  } else {
    payload.url = document.getElementById('ig-wh-url').value;
  }
  try {
    const r = await api('/api/miniapp/webhooks', { method: 'POST', body: JSON.stringify(payload) });
    back();
    await openIntegrations();
    if (r.signing_secret) {
      _igShowSecret('📡 Получатель добавлен', r.signing_secret,
        'Секрет подписи показывается один раз. Им проверяется заголовок X-Infragram-Signature.');
    } else {
      toast('✅ Получатель добавлен');
    }
  } catch (e) {
    err.textContent = errRu(e);
  } finally {
    btn.disabled = false;
  }
}

async function openWebhook(id) {
  _igWhId = id;
  _igScreen('s-ig-wh', '📡 Получатель', 'openWebhook(_igWhId)');
  push('s-ig-wh');
  const body = document.getElementById('s-ig-wh-body');
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const d = await api('/api/miniapp/webhooks/' + id + '/deliveries');
    body.innerHTML = _igRenderWebhook(d.endpoint, d.deliveries || [], d.total || 0);
  } catch (e) {
    body.innerHTML = errHtml(errRu(e), 'openWebhook(' + id + ')');
  }
}

const _IG_STATUS = { delivered: '✅ доставлено', pending: '⏳ в очереди', failed: '❌ не доставлено', skipped: '⏭ пропущено' };

function _igRenderWebhook(ep, items, total) {
  let h = '';
  const where = ep.kind === 'sheets'
    ? '📊 Лист «' + esc(ep.sheet_name) + '»'
    : '🌐 <span style="word-break:break-all">' + esc(ep.url) + '</span>';
  h += _igBox('<div><b>' + esc(ep.name || ('Получатель #' + ep.id)) + '</b></div><div style="margin-top:4px">' + where + '</div>' +
    (ep.disabled_reason ? '<div style="color:var(--red);margin-top:4px">' + esc(ep.disabled_reason) + '</div>' : '') +
    (ep.last_error && ep.consecutive_failures ? _igHint('Последняя ошибка: ' + esc(ep.last_error)) : ''),
    ep.is_active ? (ep.consecutive_failures ? '' : 'ok') : 'bad');
  h += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">' +
    '<button class="btn btn-p" style="flex:1" onclick="igTestWebhook(' + ep.id + ')">Проверить</button>' +
    '<button class="btn btn-s" style="flex:1" onclick="igToggleWebhook(' + ep.id + ',' + (!ep.is_active) + ')">' + (ep.is_active ? 'Выключить' : 'Включить') + '</button>' +
    (ep.kind === 'http' ? '<button class="btn btn-s" style="flex:1" onclick="igRotateWebhook(' + ep.id + ')">Новый секрет</button>' : '') +
    '<button class="btn btn-d" style="flex:1" onclick="igDeleteWebhook(' + ep.id + ')">Удалить</button>' +
  '</div><div id="ig-test-res"></div>';
  h += '<div class="sec">События</div>' + _igHint('Ничего не отмечено — придут все события.') +
    '<div style="margin-top:6px">' + _igEventChecks(ep.events) + '</div>' +
    '<button class="btn btn-s" style="width:100%;margin:6px 0 10px" onclick="igSaveEvents(' + ep.id + ')">Сохранить события</button>';
  h += '<div class="sec">Последние доставки' + (total > items.length ? ' (' + items.length + ' из ' + total + ')' : '') + '</div>';
  if (!items.length) {
    h += empty('📭', 'Доставок пока не было', 'Как только случится событие, оно появится здесь');
  }
  h += items.map(it =>
    '<div class="row"><div class="row-body">' +
      '<div class="row-name">' + esc(it.summary || it.event_label) + '</div>' +
      '<div class="row-val" style="color:var(--hint)">' + (_IG_STATUS[it.status] || esc(it.status)) +
        ' · попыток ' + it.attempts + (it.status_code ? ' · ответ ' + it.status_code : '') + ' · ' + ago(it.created_at) + '</div>' +
      (it.error && it.status !== 'delivered' ? '<div class="row-val" style="color:var(--red)">' + esc(it.error) + '</div>' : '') +
    '</div>' +
    (it.status === 'failed' || it.status === 'skipped' || it.status === 'delivered'
      ? '<div class="row-right"><button class="btn btn-s" style="padding:5px 10px;min-width:0;font-size:12px" onclick="igRedeliver(' + it.id + ')">Повторить</button></div>' : '') +
    '</div>').join('');
  return h;
}

async function igTestWebhook(id) {
  const box = document.getElementById('ig-test-res');
  if (box) box.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const r = await api('/api/miniapp/webhooks/' + id + '/test', { method: 'POST', body: '{}' });
    const html = r.ok
      ? _igBox('✅ Получатель ответил' + (r.status_code ? ' ' + r.status_code : '') + '. Связь работает.', 'ok')
      : _igBox('❌ ' + esc(r.error || 'Не доставлено') + (r.status_code ? ' (код ' + r.status_code + ')' : ''), 'bad');
    if (box) box.innerHTML = html;
  } catch (e) {
    if (box) box.innerHTML = _igBox('❌ ' + esc(errRu(e)) +
      '<button class="btn btn-s" style="width:100%;margin-top:8px" onclick="igTestWebhook(' + id + ')">Повторить</button>', 'bad');
  }
}

async function igToggleWebhook(id, on) {
  try {
    await api('/api/miniapp/webhooks/' + id, { method: 'POST', body: JSON.stringify({ is_active: !!on }) });
    toast(on ? '🟢 Включён' : '🔴 Выключен');
    openWebhook(id);
  } catch (e) { toast('⚠️ ' + errRu(e)); }
}

async function igSaveEvents(id) {
  try {
    await api('/api/miniapp/webhooks/' + id, { method: 'POST', body: JSON.stringify({ events: _igCheckedEvents() }) });
    toast('✅ События сохранены');
  } catch (e) { toast('⚠️ ' + errRu(e)); }
}

async function igRotateWebhook(id) {
  if (!await askConfirm('Выпустить новый секрет? Старый перестанет подходить сразу.')) return;
  try {
    const r = await api('/api/miniapp/webhooks/' + id + '/rotate', { method: 'POST', body: '{}' });
    _igShowSecret('🔐 Новый секрет', r.signing_secret, 'Показывается один раз — обновите его у получателя.');
  } catch (e) { toast('⚠️ ' + errRu(e)); }
}

async function igDeleteWebhook(id) {
  if (!await askConfirm('Удалить получателя вместе с журналом доставок?')) return;
  try {
    await api('/api/miniapp/webhooks/' + id, { method: 'DELETE' });
    toast('🗑 Удалён');
    back();
    openIntegrations();
  } catch (e) { toast('⚠️ ' + errRu(e)); }
}

async function igRedeliver(deliveryId) {
  try {
    await api('/api/miniapp/webhook_delivery/' + deliveryId + '/retry', { method: 'POST', body: '{}' });
    toast('⏳ Поставлено в очередь');
    if (_igWhId) openWebhook(_igWhId);
  } catch (e) { toast('⚠️ ' + errRu(e)); }
}

// ── Google Sheets ──────────────────────────────────────────────────────────
async function igSheetsConnect() {
  const raw = (document.getElementById('ig-sa') || {}).value || '';
  if (!raw.trim()) { toast('Вставьте JSON-ключ'); return; }
  try {
    await api('/api/miniapp/sheets/connect', { method: 'POST', body: JSON.stringify({ credentials: raw }) });
    toast('✅ Google Sheets подключён');
    openIntegrations();
  } catch (e) { toast('⚠️ ' + errRu(e)); }
}

async function igSheetsDisconnect() {
  if (!await askConfirm('Отключить Google Sheets? Ленты событий в таблицы перестанут доставляться.')) return;
  try {
    await api('/api/miniapp/sheets/disconnect', { method: 'POST', body: '{}' });
    toast('Отключено');
    openIntegrations();
  } catch (e) { toast('⚠️ ' + errRu(e)); }
}

async function igSheetsCheck() {
  const res = document.getElementById('ig-sheet-res');
  res.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const r = await api('/api/miniapp/sheets/check', { method: 'POST',
      body: JSON.stringify({ spreadsheet: document.getElementById('ig-sheet').value }) });
    res.innerHTML = _igBox('✅ Доступ есть. Листы: ' + esc((r.sheets || []).join(', ') || '—'), 'ok');
  } catch (e) {
    res.innerHTML = _igBox('❌ ' + esc(errRu(e)) +
      '<button class="btn btn-s" style="width:100%;margin-top:8px" onclick="igSheetsCheck()">Повторить</button>', 'bad');
  }
}

async function igSheetsExport() {
  const res = document.getElementById('ig-sheet-res');
  const btn = document.getElementById('ig-export-btn');
  btn.disabled = true;
  res.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const r = await api('/api/miniapp/sheets/export', { method: 'POST', body: JSON.stringify({
      spreadsheet: document.getElementById('ig-sheet').value,
      dataset: document.getElementById('ig-ds').value }) });
    res.innerHTML = _igBox('✅ Выгружено строк: <b>' + r.rows + '</b> на лист «' + esc(r.sheet) + '»' +
      (r.truncated ? _igHint('Выгрузка ограничена первыми 50 000 строк.') : '') +
      '<div style="margin-top:6px"><a href="' + esc(r.url) + '" target="_blank" rel="noopener">Открыть таблицу</a></div>', 'ok');
  } catch (e) {
    res.innerHTML = _igBox('❌ ' + esc(errRu(e)), 'bad');
  } finally {
    btn.disabled = false;
  }
}
