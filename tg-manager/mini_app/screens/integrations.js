// Интеграции: ключи API, исходящие вебхуки на события, Google Sheets, CRM
// (amoCRM, Bitrix24) и ссылка на документацию API.
// Самодостаточный экран (создаёт свою разметку). Бэкенд —
// services/mini_app_integrations.py. Классический скрипт: делит глобальную
// область с index.html, поэтому имена — с приставкой _ig.

let _igData = null;
let _igWhId = null;
let _igCrm = null;
const _IG_CRM_KINDS = ['amocrm', 'bitrix24'];

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
    // Экран «Интеграций» — вкладка модуля (SCREEN_MODULES.integrations).
    (id === 's-integrations' ? '<div class="filter-chips mod-tabs" data-module="integrations" data-tab="connect" role="tablist" aria-label="Вкладки модуля «Интеграции»"></div>' : '') +
    '<div class="sb"><div id="' + id + '-body" style="padding:6px 0">' +
      '<div class="spin-wrap"><div class="spin"></div></div>' +
    '</div></div>';
  document.body.appendChild(el);
  // Экран рождается после загрузки страницы: общий рендер вкладок его не видел.
  if (typeof _renderModuleTabs === 'function') _renderModuleTabs();
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
    '</div>' +
    '<button class="btn btn-s" style="width:100%;margin-top:8px" onclick="igOpenDocs()">📖 Документация API и вебхуков</button>');

  // ── Вебхуки ──
  h += '<div class="sec">📡 Вебхуки на события</div>';
  h += _igHint('Infragram сам сообщит вашей системе (n8n, Make, Zapier, свой сервер), когда операция ' +
    'закончилась, аккаунт добавлен, потерян или получил ограничение. Запрос подписан ' +
    '(<code>X-Infragram-Signature</code>), неудачные доставки повторяются до 8 раз.');
  const eps = (d.endpoints || []).filter(ep => _IG_CRM_KINDS.indexOf(ep.kind) < 0);
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

  // ── CRM ──
  h += '<div class="sec">🤝 CRM: amoCRM и Bitrix24</div>';
  h += _igHint('Каждый человек из воронки, бота и заказов становится контактом и сделкой в вашей CRM: ' +
    'этап, сумма, ответы и оплата приходят сами. Смена этапа в CRM возвращается в Infragram.');
  const crms = d.crms || [];
  h += crms.map(c => {
    const state = c.is_active ? '🟢 работает' : '🔴 выключено';
    return '<div class="row" onclick="openCrm(' + c.id + ')" style="cursor:pointer">' +
      '<div class="row-body">' +
        '<div class="row-name">' + esc(c.label) + ' · ' + esc(c.account || c.url) + '</div>' +
        '<div class="row-val" style="color:var(--hint)">' + state + ' · воронка «' + esc(c.pipeline_name) + '» · ' +
          'в CRM записей: ' + (c.synced_records || 0) + '</div>' +
      '</div><div class="row-right">›</div></div>';
  }).join('');
  const have = new Set(crms.map(c => c.kind));
  const free = (d.crm_kinds || []).filter(k => !have.has(k.id));
  if (free.length) {
    h += '<div style="display:flex;gap:8px;margin:8px 0 4px">' + free.map(k =>
      '<button class="btn btn-s" style="flex:1" onclick="igNewCrm(\'' + esc(k.id) + '\')">+ ' + esc(k.label) + '</button>').join('') +
    '</div>';
  }

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

// Подпись после включения. Если получателя выключила серия сбоев, сервер при
// включении возвращает в очередь недоставленное за последние дни и отдаёт их
// число в endpoint.requeued. Владелец должен видеть, что пропущенные заказы и
// оплаты придут, а не гадать, потерялись ли они. Ручную паузу сервер не
// догоняет, requeued не приходит, и подпись обычная.
function igEnabledToast(r, on, word) {
  if (!on) return word === 'f' ? '🔴 Выключен' : '🔴 Выключено';
  const base = word === 'f' ? '🟢 Включён' : '🟢 Включено';
  const n = Math.max(0, Math.trunc(Number(r && r.endpoint && r.endpoint.requeued) || 0));
  if (!n) return base;
  return base + ' · догоняем ' + n + ' ' + plural(n, 'событие', 'события', 'событий') + ' за время простоя';
}

async function igToggleWebhook(id, on) {
  try {
    const r = await api('/api/miniapp/webhooks/' + id, { method: 'POST', body: JSON.stringify({ is_active: !!on }) });
    toast(igEnabledToast(r, on, 'f'));
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

// ── Документация ───────────────────────────────────────────────────────────
function igOpenDocs() {
  const url = (_igData && _igData.docs_url) || '/api/docs';
  try {
    if (window.Telegram && Telegram.WebApp && Telegram.WebApp.openLink) {
      Telegram.WebApp.openLink(url, { try_instant_view: false });
      return;
    }
  } catch (e) { /* вне Telegram — обычное окно */ }
  window.open(url, '_blank', 'noopener');
}

// ── CRM: amoCRM, Bitrix24 ──────────────────────────────────────────────────
function igNewCrm(kind) {
  _igScreen('s-ig-crm-new', '🤝 Подключить CRM', 'igNewCrm(window._igCrmKind)');
  window._igCrmKind = kind;
  push('s-ig-crm-new');
  const body = document.getElementById('s-ig-crm-new-body');
  let h = '';
  if (kind === 'amocrm') {
    h += _igBox('<div><b>amoCRM: как получить токен</b></div>' + _igHint(
      '1. В amoCRM: Настройки → Интеграции → «Создать интеграцию» → «Внешняя интеграция».<br>' +
      '2. Отметьте доступ «Все» и сохраните.<br>' +
      '3. На вкладке «Ключи и доступы» нажмите «Сгенерировать токен» (долгосрочный) и скопируйте его.<br>' +
      'Токен хранится зашифрованным и никому не показывается.'));
    h += '<div class="field"><label>Адрес amoCRM</label><input id="ig-crm-domain" class="inp" placeholder="mycompany.amocrm.ru"></div>' +
      '<div class="field"><label>Долгосрочный токен</label><textarea id="ig-crm-token" class="input-bc" rows="3" style="width:100%" placeholder="eyJ0eXAiOiJKV1Qi…"></textarea></div>';
  } else {
    h += _igBox('<div><b>Bitrix24: как получить адрес вебхука</b></div>' + _igHint(
      '1. В Bitrix24: Приложения → Разработчикам → Другое → «Входящий вебхук».<br>' +
      '2. В правах доступа отметьте <b>CRM</b> и сохраните.<br>' +
      '3. Скопируйте адрес вида https://портал.bitrix24.ru/rest/1/код/.<br>' +
      'Адрес содержит секрет: хранится зашифрованным и никому не показывается.'));
    h += '<div class="field"><label>Адрес входящего вебхука</label><input id="ig-crm-url" class="inp" type="url" placeholder="https://portal.bitrix24.ru/rest/1/abc123/"></div>';
  }
  h += '<div class="field-err" id="ig-crm-err"></div>' +
    '<button class="btn btn-p" style="width:100%;margin-top:8px" id="ig-crm-save" onclick="igCrmConnect()">Проверить и подключить</button>';
  body.innerHTML = h;
}

async function igCrmConnect() {
  const kind = window._igCrmKind;
  const btn = document.getElementById('ig-crm-save');
  const err = document.getElementById('ig-crm-err');
  err.textContent = '';
  btn.disabled = true;
  const payload = { kind };
  if (kind === 'amocrm') {
    payload.domain = document.getElementById('ig-crm-domain').value;
    payload.token = document.getElementById('ig-crm-token').value;
  } else {
    payload.webhook_url = document.getElementById('ig-crm-url').value;
  }
  try {
    const r = await api('/api/miniapp/crm_connections', { method: 'POST', body: JSON.stringify(payload) });
    back();
    await openIntegrations();
    toast('✅ CRM подключена');
    openCrm(r.crm.id);
  } catch (e) {
    err.textContent = errRu(e);
  } finally {
    btn.disabled = false;
  }
}

async function openCrm(id) {
  _igScreen('s-ig-crm', '🤝 CRM', 'igReloadCrm()');
  push('s-ig-crm');
  const body = document.getElementById('s-ig-crm-body');
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    _igCrm = await api('/api/miniapp/crm_connections/' + id);
    body.innerHTML = _igRenderCrm(_igCrm);
  } catch (e) {
    body.innerHTML = errHtml(errRu(e), 'openCrm(' + id + ')');
  }
}

function _igStageOptions(stages, selected) {
  return '<option value="">— не менять этап —</option>' + (stages || []).map(s =>
    '<option value="' + esc(s.id) + '"' + (String(s.id) === String(selected || '') ? ' selected' : '') + '>' +
    esc(s.name) + (s.kind === 'won' ? ' ✅' : s.kind === 'lost' ? ' ❌' : '') + '</option>').join('');
}

function _igCrmPipeline(d, pid) {
  return (d.pipelines || []).find(p => String(p.id) === String(pid)) || null;
}

function _igRenderCrm(d) {
  const c = d.crm;
  let h = _igBox('<div><b>' + esc(c.label) + '</b> · ' + esc(c.account || '') + '</div>' +
    '<div style="margin-top:4px;word-break:break-all">' + esc(c.url) + '</div>' +
    '<div style="margin-top:4px">В CRM записей: <b>' + (c.synced_records || 0) + '</b></div>',
    c.is_active ? 'ok' : 'bad');
  h += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">' +
    '<button class="btn btn-p" style="flex:1" onclick="igTestCrm(' + c.id + ')">Проверить</button>' +
    '<button class="btn btn-s" style="flex:1" onclick="openWebhook(' + c.id + ')">Журнал</button>' +
    '<button class="btn btn-s" style="flex:1" onclick="igToggleCrm(' + c.id + ',' + (!c.is_active) + ')">' + (c.is_active ? 'Выключить' : 'Включить') + '</button>' +
    '<button class="btn btn-d" style="flex:1" onclick="igDeleteCrm(' + c.id + ')">Отключить</button>' +
  '</div><div id="ig-crm-res"></div>';

  // Воронка и этапы
  h += '<div class="sec">Воронка и этапы</div>';
  if (d.pipelines_error) {
    h += _igBox('❌ ' + esc(d.pipelines_error) +
      '<button class="btn btn-s" style="width:100%;margin-top:8px" onclick="openCrm(' + c.id + ')">Повторить</button>', 'bad');
  } else {
    h += '<div class="field"><label>Воронка в CRM</label><select id="ig-crm-pipe" class="inp" onchange="igCrmPipeChanged()">' +
      (d.pipelines || []).map(p => '<option value="' + esc(p.id) + '"' + (String(p.id) === String(c.pipeline_id) ? ' selected' : '') + '>' + esc(p.name) + '</option>').join('') +
      '</select></div>';
    h += _igHint('Какому этапу CRM соответствует каждый этап Infragram. «Не менять этап» — сделка остаётся там, где её поставили в CRM.');
    h += '<div id="ig-crm-map">' + _igCrmMapHtml(d, c.pipeline_id, c.stage_map) + '</div>';
    h += '<button class="btn btn-p" style="width:100%;margin:8px 0 10px" id="ig-crm-cfg" onclick="igCrmSaveMap(' + c.id + ')">Сохранить этапы</button>';
  }

  // Обратная связь из CRM
  h += '<div class="sec">Смена этапа в CRM → Infragram</div>';
  if (c.kind === 'amocrm') {
    h += c.inbound_auto
      ? _igBox('✅ Подписка оформлена в amoCRM автоматически. Перенесите сделку в другой этап — этап контакта в Infragram сменится.', 'ok')
      : _igBox('<div>amoCRM не дала подписаться автоматически. Добавьте вебхук вручную: Настройки → Интеграции → Веб-хуки, события «Сделка изменена» и «Смена этапа сделки».</div>' +
          _igHint('Адрес:') + '<div style="font-family:monospace;font-size:12px;word-break:break-all;user-select:all">' + esc(c.inbound_url) + '</div>' +
          '<button class="btn btn-s" style="width:100%;margin-top:8px" onclick="igCopyCrmInbound()">📋 Скопировать адрес</button>');
  } else {
    h += _igBox('<div>В Bitrix24: Приложения → Разработчикам → Другое → «Исходящий вебхук», событие <b>«Обновление сделки» (ONCRMDEALUPDATE)</b>, адрес обработчика:</div>' +
      '<div style="font-family:monospace;font-size:12px;word-break:break-all;user-select:all;margin-top:6px">' + esc(c.inbound_url) + '</div>' +
      '<button class="btn btn-s" style="width:100%;margin-top:8px" onclick="igCopyCrmInbound()">📋 Скопировать адрес</button>' +
      _igHint('Bitrix24 покажет «Токен приложения» — вставьте его сюда, иначе входящие запросы не принимаются.') +
      '<div class="field" style="margin-top:6px"><input id="ig-crm-apptoken" class="inp" placeholder="' + (c.inbound_secret_set ? 'токен сохранён — вставьте новый, чтобы заменить' : 'токен приложения') + '"></div>' +
      '<button class="btn btn-p" style="width:100%" onclick="igCrmSaveToken(' + c.id + ')">Сохранить токен</button>',
      c.inbound_secret_set ? 'ok' : '');
  }
  if (c.last_inbound_at) h += _igHint('Последний вызов из CRM: ' + ago(c.last_inbound_at));

  // Выгрузка
  h += '<div class="sec">Перенести текущую воронку</div>' +
    _igHint('Новые события едут сами. Чтобы в CRM оказались и люди, которые уже в воронке, выгрузите её один раз.') +
    '<button class="btn btn-s" style="width:100%;margin-top:8px" id="ig-crm-bf" onclick="igCrmBackfill(' + c.id + ')">Выгрузить воронку в CRM</button>' +
    '<div id="ig-crm-bf-res"></div>';
  return h;
}

function _igCrmMapHtml(d, pid, map) {
  const pipe = _igCrmPipeline(d, pid);
  const stages = pipe ? pipe.stages : [];
  return (d.stage_labels || []).map(st =>
    '<div class="field"><label>' + esc(st.label) + '</label>' +
    '<select class="inp ig-crm-st" data-stage="' + esc(st.id) + '">' + _igStageOptions(stages, (map || {})[st.id]) + '</select></div>'
  ).join('');
}

function igCrmPipeChanged() {
  if (!_igCrm) return;
  const pid = document.getElementById('ig-crm-pipe').value;
  // Другая воронка — этапы прежней в ней не существуют: подставляем
  // автоматическое соответствие, его посчитает сервер при сохранении.
  const same = String(pid) === String(_igCrm.crm.pipeline_id);
  document.getElementById('ig-crm-map').innerHTML = _igCrmMapHtml(_igCrm, pid, same ? _igCrm.crm.stage_map : {});
}

async function igCrmSaveMap(id) {
  const btn = document.getElementById('ig-crm-cfg');
  btn.disabled = true;
  const pid = document.getElementById('ig-crm-pipe').value;
  const same = _igCrm && String(pid) === String(_igCrm.crm.pipeline_id);
  const payload = { pipeline_id: pid };
  if (same) {
    const m = {};
    document.querySelectorAll('.ig-crm-st').forEach(x => { m[x.dataset.stage] = x.value; });
    payload.stage_map = m;
  }
  try {
    await api('/api/miniapp/crm_connections/' + id, { method: 'POST', body: JSON.stringify(payload) });
    toast(same ? '✅ Этапы сохранены' : '✅ Воронка выбрана, этапы сопоставлены автоматически');
    openCrm(id);
  } catch (e) { toast('⚠️ ' + errRu(e)); }
  finally { btn.disabled = false; }
}

async function igCrmSaveToken(id) {
  const v = (document.getElementById('ig-crm-apptoken') || {}).value || '';
  if (!v.trim()) { toast('Вставьте токен приложения из Bitrix24'); return; }
  try {
    await api('/api/miniapp/crm_connections/' + id, { method: 'POST', body: JSON.stringify({ inbound_secret: v }) });
    toast('✅ Токен сохранён');
    openCrm(id);
  } catch (e) { toast('⚠️ ' + errRu(e)); }
}

function igCopyCrmInbound() {
  const u = _igCrm && _igCrm.crm && _igCrm.crm.inbound_url;
  if (u) copyToClipboard(u);
}

async function igTestCrm(id) {
  const box = document.getElementById('ig-crm-res');
  if (box) box.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const r = await api('/api/miniapp/webhooks/' + id + '/test', { method: 'POST', body: '{}' });
    const html = r.ok
      ? _igBox('✅ CRM на связи' + (r.account ? ': ' + esc(r.account) : '') + '.' + (r.warning ? _igHint('⚠️ ' + esc(r.warning)) : ''), r.warning ? '' : 'ok')
      : _igBox('❌ ' + esc(r.error || 'CRM не ответила') +
          '<button class="btn btn-s" style="width:100%;margin-top:8px" onclick="igTestCrm(' + id + ')">Повторить</button>', 'bad');
    if (box) box.innerHTML = html;
  } catch (e) {
    if (box) box.innerHTML = _igBox('❌ ' + esc(errRu(e)) +
      '<button class="btn btn-s" style="width:100%;margin-top:8px" onclick="igTestCrm(' + id + ')">Повторить</button>', 'bad');
  }
}

async function igToggleCrm(id, on) {
  try {
    const r = await api('/api/miniapp/webhooks/' + id, { method: 'POST', body: JSON.stringify({ is_active: !!on }) });
    toast(igEnabledToast(r, on, 'n'));
    back();
    openCrm(id);
  } catch (e) { toast('⚠️ ' + errRu(e)); }
}

async function igDeleteCrm(id) {
  if (!await askConfirm('Отключить CRM? Новые события перестанут уходить, связи записей забудутся. Данные в самой CRM останутся.')) return;
  try {
    await api('/api/miniapp/webhooks/' + id, { method: 'DELETE' });
    toast('Отключено');
    back();
    openIntegrations();
  } catch (e) { toast('⚠️ ' + errRu(e)); }
}

async function igCrmBackfill(id) {
  if (!await askConfirm('Выгрузить всю текущую воронку и сделки в CRM? Уже выгруженные записи не задвоятся.')) return;
  const btn = document.getElementById('ig-crm-bf');
  const res = document.getElementById('ig-crm-bf-res');
  btn.disabled = true;
  try {
    const r = await api('/api/miniapp/crm_connections/' + id + '/backfill', { method: 'POST', body: '{}' });
    res.innerHTML = _igBox('⏳ В очереди записей: <b>' + r.queued + '</b>. Ход — в журнале.' +
      (r.queued >= r.limit ? _igHint('За раз выгружается до ' + r.limit + ' последних записей каждого вида.') : ''), 'ok');
  } catch (e) {
    res.innerHTML = _igBox('❌ ' + esc(errRu(e)) +
      '<button class="btn btn-s" style="width:100%;margin-top:8px" onclick="igCrmBackfill(' + id + ')">Повторить</button>', 'bad');
  } finally {
    btn.disabled = false;
  }
}

function igReloadCrm() {
  if (_igCrm && _igCrm.crm) openCrm(_igCrm.crm.id);
}
