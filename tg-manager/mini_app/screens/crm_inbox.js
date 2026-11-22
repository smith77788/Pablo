// Единые «Входящие»: ответы людей со всех аккаунтов флота, Хранилища,
// релея ботов и ботов-продавцов — одним списком по человеку.
//
// До этого ответ на рассылку было видно только в самом Telegram, открыв ту
// сессию, которой человек ответил. Сервер сводит источники
// (services/unified_inbox.py); экран показывает, кто ждёт ответа, и даёт три
// действия: ответить (через релей бота — где человек писал боту), открыть в
// Telegram и открыть карточку клиента.

let _ibWaiting = false;
let _ibRows = [];
let _ibPeer = null;
// Куда рисуется переписка: экран s-inboxthread (телефон) или правая колонка
// s-inbox-pane (широкий экран). Поля ответа и ИИ ищутся ВНУТРИ него: в DOM
// могут жить оба места сразу, и getElementById взял бы чужое.
let _ibHost = null;
// Поиск по списку: по имени, нику и последнему сообщению. Длинный список
// разговоров иначе листался глазами.
let _ibQuery = '';
function _ibEl(id) {
  return (_ibHost && _ibHost.querySelector('#' + id)) || document.getElementById(id);
}
// Широкий экран: список слева, переписка справа, как в почте и в amoCRM.
// Решение принимается в момент клика: окно могли сузить между открытиями.
function _ibSplit() {
  return document.body.classList.contains('wide');
}

function _ibScreen(id, title, refreshFn) {
  let el = document.getElementById(id);
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = id;
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="back" onclick="back()" role="button" tabindex="0" aria-label="Назад">←</div>' +
      '<div class="hdr-title" id="' + id + '-title">' + title + '</div>' +
      '<div class="icon-btn" role="button" tabindex="0" aria-label="Обновить" ' +
        'style="margin-left:auto" onclick="' + refreshFn + '">🔄</div>' +
    '</div>' +
    '<div class="sb"><div id="' + id + '-body" style="padding:6px 0">' +
      '<div class="spin-wrap"><div class="spin"></div></div>' +
    '</div></div>';
  document.body.appendChild(el);
  return el;
}

const IB_SRC_ICON = {fleet: '👤', vault: '🗄', bot: '🤖', relay: '🔁'};
// Тип ждущего входящего (services/ai_inbox_assist.label_threads).
const IB_INTENT_ICON = {ready_to_buy: '🔥', price: '💰', complaint: '⚠️', question: '❓'};

function _ibWhen(ts) {
  if (!ts) return '';
  try { return ago(ts); } catch (_) { return ''; }
}

// Счётчик «ждут ответа» на плитке «Входящие»: сколько людей написали и ещё не
// получили ответа. Без него, чтобы узнать, есть ли кому отвечать, надо было
// открыть экран. Плитка с ждущими открывает сразу фильтр ждущих.
var _ibWaitingCount = 0;
let _ibBadgeAt = 0;
async function loadInboxBadge() {
  if (Date.now() - _ibBadgeAt < 60e3) return;   // не чаще раза в минуту
  _ibBadgeAt = Date.now();
  try {
    const d = await api('/api/miniapp/inbox?waiting=1&limit=1');
    _ibWaitingCount = Number(d.waiting || 0);
    txt('m-inbox-cnt', _ibWaitingCount ? '⏳ ' + num(_ibWaitingCount) + ' ждут' : (d.total ? 'все отвечены' : ''));
  } catch (_) { _ibBadgeAt = 0; }
}

// Вход из подсказки копайлота «N горячих ждут ответа» — сразу фильтр ждущих.
function openInboxWaiting() { return openInbox(true); }

async function openInbox(waiting) {
  _ibTpl = null;   // шаблоны могли поменять на своём экране
  if (waiting !== undefined) _ibWaiting = !!waiting;
  _ibScreen('s-inbox', '📥 Входящие', 'openInbox()');
  push('s-inbox');
  const body = document.getElementById('s-inbox-body');
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const d = await api(_ibWaiting ? '/api/miniapp/inbox?waiting=1' : '/api/miniapp/inbox');
    _ibRows = d.threads || [];
    _ibWaitingCount = Number(d.waiting || 0);
    txt('m-inbox-cnt', _ibWaitingCount ? '⏳ ' + num(_ibWaitingCount) + ' ждут' : (d.total ? 'все отвечены' : ''));
    let out = '<div class="seg" style="margin:8px 16px">' +
      '<div class="seg-opt' + (_ibWaiting ? '' : ' on') + '" role="button" tabindex="0" onclick="openInbox(false)">Все · ' + num(d.total || 0) + '</div>' +
      '<div class="seg-opt' + (_ibWaiting ? ' on' : '') + '" role="button" tabindex="0" onclick="openInbox(true)">⏳ Ждут ответа · ' + num(d.waiting || 0) + '</div>' +
      '</div>';
    if (_ibRows.length > 5) {
      out += '<div style="margin:0 16px 8px"><input id="ibSearch" class="inp" type="search" ' +
        'placeholder="🔍 Имя, ник или текст" value="' + esc(_ibQuery) + '" oninput="inboxFilter(this.value)" style="width:100%"></div>';
    }
    if (!_ibRows.length) {
      out += empty('📥', _ibWaiting ? 'Все ответили' : 'Входящих пока нет',
        'Сюда собираются ответы людей: на аккаунты с включённым слушателем, в Хранилище бизнес-аккаунта, в релей бота и ботам-продавцам.',
        {label: '📇 К контактам', fn: 'openContacts()'});
      body.innerHTML = out;
      return;
    }
    out += '<div class="ib-split"><div class="ib-list"><div class="lst" style="margin:0 16px">';
    out += _ibRows.map(function (t, i) {
      const nm = t.name || (t.username ? '@' + t.username : 'ID ' + t.peer_user_id);
      const ch = (t.channels || []).map(function (c) {
        return (IB_SRC_ICON[c.source] || '•') + ' ' + esc(c.label || '');
      }).join(' · ');
      return '<div class="row ib-row" data-i="' + i + '" role="button" tabindex="0" onclick="openInboxThread(' + i + ')">' +
        '<div class="row-body">' +
          '<div class="row-name">' + (t.waiting ? '⏳ ' : '') + esc(nm) +
            (t.intent_label ? ' <span style="font-size:11px;padding:1px 6px;border-radius:8px;background:var(--sec-bg,rgba(127,127,127,.15))">' +
              (IB_INTENT_ICON[t.intent] || '') + ' ' + esc(t.intent_label) + '</span>' : '') +
            (t.unread ? ' <span style="color:var(--accent);font-size:12px">· ' + num(t.unread) + ' нов.</span>' : '') + '</div>' +
          '<div class="row-val" style="font-size:12px;color:var(--hint)">' + esc(_ibWhen(t.last_at)) + '</div>' +
        '</div>' +
        '<div style="font-size:13px;margin-top:2px;white-space:normal">' + esc(String(t.last_text || '').substring(0, 120)) + '</div>' +
        '<div style="font-size:11px;color:var(--hint);margin-top:2px">' + ch + '</div>' +
      '</div>';
    }).join('');
    out += '</div></div>' +
      '<div class="ib-pane" id="s-inbox-pane">' +
        '<div style="padding:40px 16px;text-align:center;color:var(--hint);font-size:13px">Выберите разговор слева</div>' +
      '</div></div>';
    body.innerHTML = out;
    if (_ibQuery) inboxFilter(_ibQuery);
    // На широком экране сразу открыт первый разговор: пустая правая половина
    // ничего не сообщает, а первый в списке — тот, кто ждёт дольше всех.
    if (_ibSplit()) openInboxThread(0);
  } catch (e) {
    body.innerHTML = errHtml(errRu(e), 'openInbox()');
  }
}

async function openInboxThread(i) {
  const t = _ibRows[i];
  if (!t) return;
  _ibPeer = t;
  const nm = t.name || (t.username ? '@' + t.username : 'ID ' + t.peer_user_id);
  const pane = document.getElementById('s-inbox-pane');
  const inList = pane && document.getElementById('s-inbox')?.classList.contains('show');
  let body;
  if (_ibSplit() && inList) {
    // Правая колонка: без перехода на другой экран, список остаётся на месте.
    document.querySelectorAll('#s-inbox .ib-row').forEach(function (r) {
      r.classList.toggle('on', r.dataset.i === String(i));
    });
    pane.innerHTML = '<div class="ib-pane-hdr">💬 ' + esc(nm) + '</div><div id="s-inbox-pane-body"></div>';
    body = document.getElementById('s-inbox-pane-body');
  } else {
    _ibScreen('s-inboxthread', '💬 Переписка', 'openInboxThread(' + i + ')');
    push('s-inboxthread');
    const ttl = document.getElementById('s-inboxthread-title');
    if (ttl) ttl.textContent = '💬 ' + nm;
    body = document.getElementById('s-inboxthread-body');
  }
  _ibHost = body;
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const d = await api('/api/miniapp/inbox/' + t.peer_user_id);
    // Открыли — значит прочитали; ошибка отметки не мешает чтению.
    api('/api/miniapp/inbox/' + t.peer_user_id + '/read', {method: 'POST'}).catch(function () {});
    const msgs = d.messages || [];
    let out = '<div style="padding:8px 16px">';
    if (!msgs.length) out += '<div style="color:var(--hint);font-size:13px">Сообщений нет</div>';
    out += msgs.map(function (m) {
      const mine = m.direction === 'out';
      return '<div style="display:flex;justify-content:' + (mine ? 'flex-end' : 'flex-start') + ';margin-bottom:8px">' +
        '<div style="max-width:82%;background:' + (mine ? 'rgba(59,130,246,.15)' : 'var(--bg2)') +
          ';border-radius:12px;padding:8px 10px">' +
          '<div style="font-size:14px;white-space:pre-wrap;word-break:break-word">' + esc(m.text || '') + '</div>' +
          '<div style="font-size:10px;color:var(--hint);margin-top:4px">' + esc(m.channel || '') +
            (m.at ? ' · ' + esc(new Date(m.at).toLocaleString('ru-RU')) : '') + '</div>' +
        '</div></div>';
    }).join('');
    out += '</div>';
    if (d.truncated) {
      out += '<div style="margin:0 16px 8px;font-size:11px;color:var(--hint)">Показаны последние сообщения — более ранние сюда не вошли.</div>';
    }
    const canRelay = (t.channels || []).some(function (c) { return c.source === 'relay'; });
    // ИИ для оператора: черновик ответа и сводка — по кнопке, а не сами:
    // каждый вызов тратит обращение к ИИ из дневного лимита тарифа.
    out += '<div style="display:flex;gap:8px;padding:0 16px 8px">' +
      '<button class="btn btn-s" style="flex:1;font-size:12px;padding:10px 6px" onclick="inboxAiDraft()">✍️ Черновик ответа</button>' +
      '<button class="btn btn-s" style="flex:1;font-size:12px;padding:10px 6px" onclick="inboxAiSummary()">📝 Сводка разговора</button>' +
      '</div><div id="ibAiOut" style="padding:0 16px"></div>';
    if (canRelay) {
      out += '<div style="padding:0 16px 8px">' +
        '<div class="preset-row" id="ibQuick"></div>' +
        '<textarea id="ibReplyText" class="input-bc" rows="3" maxlength="4000" placeholder="Ответ через бота…" style="width:100%" ' +
          'oninput="_ibKeepDraft()">' + esc(loadDraft(_ibDraftKey(t))) + '</textarea>' +
        '<button class="btn" style="margin-top:6px;width:100%" onclick="sendInboxReply()">Отправить через бота</button>' +
        '</div>';
    }
    out += '<div style="display:flex;gap:8px;padding:4px 16px 20px">';
    const link = t.username ? 'https://t.me/' + encodeURIComponent(t.username) : 'tg://user?id=' + t.peer_user_id;
    out += '<a class="btn btn-s" style="flex:1;text-align:center;text-decoration:none;font-size:12px;padding:10px 6px" href="' + esc(link) + '" target="_blank">✈️ Открыть в Telegram</a>';
    const cid = d.contact_id || t.contact_id;
    if (cid) {
      out += '<button class="btn btn-s" style="flex:1;font-size:12px;padding:10px 6px" onclick="openContactTimeline(\'' + esc(cid) + '\')">📇 Карточка клиента</button>';
    }
    out += '</div>';
    // Разбор очереди «ждут ответа» подряд: из разговора — сразу к следующему
    // ждущему, без возврата в список и поиска глазами.
    const nx = inboxNextWaiting(_ibRows, i);
    if (nx >= 0) {
      out += '<div style="padding:0 16px 16px"><button class="btn btn-s" style="width:100%" onclick="openInboxThread(' + nx + ')">' +
        '⏭ Следующий ждущий ответа</button></div>';
    }
    if (!canRelay) {
      out += '<div style="margin:0 16px 16px;font-size:11px;color:var(--hint);line-height:1.4">' +
        'Ответить отсюда можно, только если человек писал в релей бота. Аккаунт флота в это время держит слушатель, ' +
        'и второе подключение к той же сессии Telegram оборвёт её — поэтому ответ аккаунтом идёт из самого Telegram.</div>';
    }
    body.innerHTML = out;
    if (canRelay) _ibQuickReplies();
  } catch (e) {
    body.innerHTML = errHtml(errRu(e), 'openInboxThread(' + i + ')');
  }
}

function _ibAiIntent(x) {
  if (!x || !x.label) return '';
  return '<div style="display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;background:var(--bg2);margin-bottom:6px">' +
    esc(x.label) + (x.by === 'rules' ? ' · по фразам' : '') + '</div>';
}

// Чем черновик собран: голосом какой персоны и с каким состоянием человека
// из «Модели поведения». Оператор должен видеть, почему ответ звучит так.
function _ibAiBasis(d) {
  const bits = [];
  if (d && d.persona) bits.push('🎭 голосом персоны «' + esc(d.persona) + '»');
  if (d && d.state) bits.push('🧠 клиент: ' + esc(d.state));
  return bits.length ? '<div style="font-size:11px;color:var(--hint);padding:2px 0 4px">' + bits.join(' · ') + '</div>' : '';
}

async function inboxAiDraft() {
  const t = _ibPeer;
  const box = _ibEl('ibAiOut');
  if (!t || !box) return;
  box.innerHTML = '<div style="font-size:12px;color:var(--hint);padding:6px 0">⏳ ИИ пишет черновик…</div>';
  try {
    const d = await api('/api/miniapp/inbox/' + t.peer_user_id + '/ai-draft', {method: 'POST'});
    if (!d.draft) {
      box.innerHTML = _ibAiIntent(d.intent) + '<div style="font-size:12px;color:var(--hint);padding:6px 0">' + esc(d.reason || 'Черновика нет') + '</div>';
      return;
    }
    const ta = _ibEl('ibReplyText');
    if (ta) {
      ta.value = d.draft;
      _ibKeepDraft();
      box.innerHTML = _ibAiIntent(d.intent) + _ibAiBasis(d) + '<div style="font-size:12px;color:var(--hint);padding:6px 0">Черновик в поле ответа — проверьте и отправьте сами.</div>';
    } else {
      box.innerHTML = _ibAiIntent(d.intent) + _ibAiBasis(d) +
        '<div style="font-size:13px;white-space:pre-wrap;background:var(--bg2);border-radius:10px;padding:8px 10px">' + esc(d.draft) + '</div>' +
        '<div style="font-size:11px;color:var(--hint);padding:6px 0">Скопируйте и отправьте из Telegram.</div>';
    }
  } catch (e) {
    box.innerHTML = '<div style="font-size:12px;color:var(--hint);padding:6px 0">⚠️ ' + esc(errRu(e, 'Черновик не получился')) + '</div>';
  }
}

async function inboxAiSummary() {
  const t = _ibPeer;
  const box = _ibEl('ibAiOut');
  if (!t || !box) return;
  box.innerHTML = '<div style="font-size:12px;color:var(--hint);padding:6px 0">⏳ ИИ сводит разговор…</div>';
  try {
    const d = await api('/api/miniapp/inbox/' + t.peer_user_id + '/ai-summary', {method: 'POST'});
    box.innerHTML = _ibAiIntent(d.intent) + (d.summary
      ? '<div style="font-size:13px;white-space:pre-wrap;background:var(--bg2);border-radius:10px;padding:8px 10px">' + esc(d.summary) + '</div>'
      : '<div style="font-size:12px;color:var(--hint);padding:6px 0">' + esc(d.reason || 'Сводки нет') + '</div>');
  } catch (e) {
    box.innerHTML = '<div style="font-size:12px;color:var(--hint);padding:6px 0">⚠️ ' + esc(errRu(e, 'Сводка не получилась')) + '</div>';
  }
}

// Быстрые ответы: те же шаблоны постов, что у рассылок, — одним касанием в
// поле ответа. Подстановки ({name}, {username}) берутся из этого человека,
// варианты {а|б} раскрываются: в личный ответ скобки уйти не должны.
let _ibTpl = null;
async function _ibQuickReplies() {
  const box = _ibEl('ibQuick');
  if (!box) return;
  if (!_ibTpl) {
    try { _ibTpl = (await api('/api/miniapp/templates?limit=100')).templates || []; }
    catch (e) { _ibTpl = []; }
  }
  const head = _ibTpl.slice(0, 6);
  box.innerHTML = head.map(function (t, k) {
    return '<span class="preset-chip" role="button" tabindex="0" onclick="inboxUseTpl(' + k + ')">' + esc(t.name || 'Шаблон') + '</span>';
  }).join('') +
    (_ibTpl.length > head.length ? '<span class="preset-chip" role="button" tabindex="0" onclick="inboxPickTpl()">📝 Все шаблоны · ' + _ibTpl.length + '</span>' : '') +
    '<span class="preset-chip" role="button" tabindex="0" onclick="inboxSaveTpl()">💾 В шаблоны</span>';
}
function _ibFill(text) {
  const t = _ibPeer || {};
  const first = String(t.name || '').split(' ')[0] || '';
  let s = String(text || '')
    .replace(/\{(name|first_name)\}/g, first)
    .replace(/\{username\}/g, t.username ? '@' + t.username : first);
  let guard = 0;
  while (/\{[^{}]*\|[^{}]*\}/.test(s) && guard++ < 50) {
    s = s.replace(/\{([^{}]*\|[^{}]*)\}/g, function (m, g) {
      const p = g.split('|'); return p[Math.floor(Math.random() * p.length)];
    });
  }
  return s;
}
function inboxUseTpl(k) {
  const t = _ibTpl && _ibTpl[k];
  const ta = _ibEl('ibReplyText');
  if (!t || !ta) return;
  ta.value = _ibFill(t.template && t.template.text);
  _ibKeepDraft();
  ta.focus();
}

// Черновик ответа живёт у каждого разговора свой: переход к соседнему
// разговору или выход из экрана больше не стирает недописанный ответ.
function _ibDraftKey(t) { return 'inbox_' + Number((t || {}).peer_user_id || 0); }
function _ibKeepDraft() {
  const ta = _ibEl('ibReplyText');
  if (ta && _ibPeer) saveDraft(_ibDraftKey(_ibPeer), ta.value);
}

function inboxFilter(q) {
  _ibQuery = String(q || '');
  const needle = _ibQuery.trim().toLowerCase();
  let shown = 0;
  document.querySelectorAll('#s-inbox .ib-row').forEach(function (r) {
    const t = _ibRows[Number(r.dataset.i)] || {};
    const hay = [t.name, t.username ? '@' + t.username : '', t.last_text].join(' ').toLowerCase();
    const ok = !needle || hay.indexOf(needle) >= 0;
    r.style.display = ok ? '' : 'none';
    if (ok) shown++;
  });
  let note = document.getElementById('ibSearchNone');
  if (!note) {
    const list = document.querySelector('#s-inbox .ib-list .lst');
    if (!list) return shown;
    note = document.createElement('div');
    note.id = 'ibSearchNone';
    note.style.cssText = 'padding:16px;text-align:center;color:var(--hint);font-size:13px';
    note.textContent = 'Ничего не найдено';
    list.appendChild(note);
  }
  note.style.display = shown ? 'none' : '';
  return shown;
}
async function inboxPickTpl() {
  const id = await askChoice('📝 Быстрый ответ', (_ibTpl || []).map(function (t) {
    return {value: t.id, label: t.name || 'Шаблон', sub: String((t.template && t.template.text) || '').slice(0, 80)};
  }));
  const k = (_ibTpl || []).findIndex(function (t) { return t.id === id; });
  if (k >= 0) inboxUseTpl(k);
}
async function inboxSaveTpl() {
  const ta = _ibEl('ibReplyText');
  const text = (ta && ta.value || '').trim();
  if (!text) { toast('Сначала напишите ответ'); return; }
  const name = ((await askPrompt('Название быстрого ответа', {value: text.slice(0, 40)})) || '').trim();
  if (!name) return;
  try {
    await api('/api/miniapp/template', {method: 'POST', body: JSON.stringify({name: name, text: text})});
    _ibTpl = null;
    _ibQuickReplies();
    toast('💾 Сохранено в шаблоны');
  } catch (e) { toast('⚠️ ' + errRu(e, 'Не удалось сохранить')); }
}

// Индекс следующего ждущего ответа разговора после from (по кругу), -1 — нет.
function inboxNextWaiting(rows, from) {
  const n = (rows || []).length;
  for (let k = 1; k < n; k++) {
    const j = (from + k) % n;
    if (rows[j] && rows[j].waiting) return j;
  }
  return -1;
}

async function sendInboxReply() {
  const t = _ibPeer;
  const el = _ibEl('ibReplyText');
  const text = (el && el.value || '').trim();
  if (!t || !text) { toast('Напишите ответ'); return; }
  const relay = (t.channels || []).find(function (c) { return c.source === 'relay'; }) || {};
  try {
    await api('/api/miniapp/inbox/' + t.peer_user_id + '/reply', {
      method: 'POST', body: JSON.stringify({text: text, bot_id: relay.bot_id || 0})});
    toast('✅ Отправлено');
    clearDraft(_ibDraftKey(t));
    // Ответили — разговор больше не ждёт; без этого «Следующий ждущий»
    // из соседнего разговора вёл бы обратно сюда.
    t.waiting = false;
    const i = _ibRows.indexOf(t);
    if (i >= 0) openInboxThread(i);
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось отправить'));
  }
}
