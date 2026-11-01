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

function _ibWhen(ts) {
  if (!ts) return '';
  try { return ago(ts); } catch (_) { return ''; }
}

async function openInbox(waiting) {
  if (waiting !== undefined) _ibWaiting = !!waiting;
  _ibScreen('s-inbox', '📥 Входящие', 'openInbox()');
  push('s-inbox');
  const body = document.getElementById('s-inbox-body');
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const d = await api(_ibWaiting ? '/api/miniapp/inbox?waiting=1' : '/api/miniapp/inbox');
    _ibRows = d.threads || [];
    let out = '<div class="seg" style="margin:8px 16px">' +
      '<div class="seg-opt' + (_ibWaiting ? '' : ' on') + '" role="button" tabindex="0" onclick="openInbox(false)">Все · ' + num(d.total || 0) + '</div>' +
      '<div class="seg-opt' + (_ibWaiting ? ' on' : '') + '" role="button" tabindex="0" onclick="openInbox(true)">⏳ Ждут ответа · ' + num(d.waiting || 0) + '</div>' +
      '</div>';
    if (!_ibRows.length) {
      out += empty('📥', _ibWaiting ? 'Все ответили' : 'Входящих пока нет',
        'Сюда собираются ответы людей: на аккаунты с включённым слушателем, в Хранилище бизнес-аккаунта, в релей бота и ботам-продавцам.',
        {label: '📇 К контактам', fn: 'openContacts()'});
      body.innerHTML = out;
      return;
    }
    out += '<div class="lst" style="margin:0 16px">';
    out += _ibRows.map(function (t, i) {
      const nm = t.name || (t.username ? '@' + t.username : 'ID ' + t.peer_user_id);
      const ch = (t.channels || []).map(function (c) {
        return (IB_SRC_ICON[c.source] || '•') + ' ' + esc(c.label || '');
      }).join(' · ');
      return '<div class="row" role="button" tabindex="0" onclick="openInboxThread(' + i + ')">' +
        '<div class="row-body">' +
          '<div class="row-name">' + (t.waiting ? '⏳ ' : '') + esc(nm) +
            (t.unread ? ' <span style="color:var(--accent);font-size:12px">· ' + num(t.unread) + ' нов.</span>' : '') + '</div>' +
          '<div class="row-val" style="font-size:12px;color:var(--hint)">' + esc(_ibWhen(t.last_at)) + '</div>' +
        '</div>' +
        '<div style="font-size:13px;margin-top:2px;white-space:normal">' + esc(String(t.last_text || '').substring(0, 120)) + '</div>' +
        '<div style="font-size:11px;color:var(--hint);margin-top:2px">' + ch + '</div>' +
      '</div>';
    }).join('');
    out += '</div>';
    body.innerHTML = out;
  } catch (e) {
    body.innerHTML = errHtml(errRu(e), 'openInbox()');
  }
}

async function openInboxThread(i) {
  const t = _ibRows[i];
  if (!t) return;
  _ibPeer = t;
  _ibScreen('s-inboxthread', '💬 Переписка', 'openInboxThread(' + i + ')');
  push('s-inboxthread');
  const nm = t.name || (t.username ? '@' + t.username : 'ID ' + t.peer_user_id);
  const ttl = document.getElementById('s-inboxthread-title');
  if (ttl) ttl.textContent = '💬 ' + nm;
  const body = document.getElementById('s-inboxthread-body');
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
    if (canRelay) {
      out += '<div style="padding:0 16px 8px">' +
        '<textarea id="ibReplyText" class="input-bc" rows="3" maxlength="4000" placeholder="Ответ через бота…" style="width:100%"></textarea>' +
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
    if (!canRelay) {
      out += '<div style="margin:0 16px 16px;font-size:11px;color:var(--hint);line-height:1.4">' +
        'Ответить отсюда можно, только если человек писал в релей бота. Аккаунт флота в это время держит слушатель, ' +
        'и второе подключение к той же сессии Telegram оборвёт её — поэтому ответ аккаунтом идёт из самого Telegram.</div>';
    }
    body.innerHTML = out;
  } catch (e) {
    body.innerHTML = errHtml(errRu(e), 'openInboxThread(' + i + ')');
  }
}

async function sendInboxReply() {
  const t = _ibPeer;
  const el = document.getElementById('ibReplyText');
  const text = (el && el.value || '').trim();
  if (!t || !text) { toast('Напишите ответ'); return; }
  const relay = (t.channels || []).find(function (c) { return c.source === 'relay'; }) || {};
  try {
    await api('/api/miniapp/inbox/' + t.peer_user_id + '/reply', {
      method: 'POST', body: JSON.stringify({text: text, bot_id: relay.bot_id || 0})});
    toast('✅ Отправлено');
    const i = _ibRows.indexOf(t);
    if (i >= 0) openInboxThread(i);
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось отправить'));
  }
}
