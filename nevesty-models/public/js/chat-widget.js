(function () {
  'use strict';

  // Only initialize once
  if (window.__nmChatLoaded) return;
  window.__nmChatLoaded = true;

  var HISTORY_KEY = 'nm_chat_history';
  var MAX_HISTORY = 10;
  var API_URL = '/api/chat/ask';

  // ─── Inline CSS ────────────────────────────────────────────────────────────
  var style = document.createElement('style');
  style.textContent = [
    /* Chat button */
    '#nm-chat-btn{position:fixed;bottom:90px;right:20px;z-index:9998;width:52px;height:52px;border-radius:50%;border:none;cursor:pointer;background:linear-gradient(135deg,#c8a84b,#e8c96a);box-shadow:0 4px 14px rgba(0,0,0,.35);font-size:22px;display:none;align-items:center;justify-content:center;transition:transform .2s,box-shadow .2s;animation:nmChatPulse 2.5s ease-in-out infinite;}',
    '#nm-chat-btn.nm-btn-visible{display:flex;}',
    '#nm-chat-btn:hover{transform:scale(1.1);box-shadow:0 6px 20px rgba(200,168,75,.55);animation:none;}',
    '@keyframes nmChatPulse{0%,100%{box-shadow:0 4px 14px rgba(200,168,75,.35)}50%{box-shadow:0 4px 28px rgba(200,168,75,.65),0 0 0 8px rgba(200,168,75,.12)}}',
    /* Chat window */
    '#nm-chat-win{position:fixed;bottom:155px;right:20px;z-index:9999;width:320px;max-width:calc(100vw - 40px);height:440px;background:#fff;border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,.22);display:flex;flex-direction:column;overflow:hidden;font-family:system-ui,-apple-system,sans-serif;transition:opacity .3s,transform .3s;}',
    '#nm-chat-win.nm-hidden{opacity:0;transform:translateY(18px);pointer-events:none;}',
    /* Header */
    '#nm-chat-header{background:linear-gradient(135deg,#c8a84b,#e8c96a);padding:12px 14px;display:flex;align-items:center;justify-content:space-between;color:#1a1a1a;}',
    '#nm-chat-header span{font-weight:700;font-size:14px;}',
    '#nm-chat-close{background:none;border:none;cursor:pointer;font-size:18px;color:#1a1a1a;line-height:1;padding:0 2px;}',
    /* Messages */
    '#nm-chat-messages{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px;background:#f7f7f8;}',
    '.nm-msg{max-width:82%;padding:8px 12px;border-radius:14px;font-size:13px;line-height:1.45;word-break:break-word;}',
    '.nm-msg.nm-user{align-self:flex-end;background:linear-gradient(135deg,#c8a84b,#e8c96a);color:#1a1a1a;border-bottom-right-radius:4px;}',
    '.nm-msg.nm-bot{align-self:flex-start;background:#fff;color:#222;border:1px solid #e0e0e0;border-bottom-left-radius:4px;}',
    '.nm-msg.nm-typing{color:#888;font-style:italic;}',
    /* Sending animation */
    '.nm-msg.nm-sending{opacity:.6;transition:opacity .3s;}',
    /* Quick replies */
    '#nm-quick-replies{display:flex;flex-wrap:wrap;gap:6px;padding:8px 12px 0;background:#f7f7f8;}',
    '.nm-quick-btn{background:#fff;border:1px solid #c8a84b;border-radius:20px;padding:5px 12px;font-size:12px;cursor:pointer;color:#8a6f20;transition:background .2s,color .2s;white-space:nowrap;}',
    '.nm-quick-btn:hover{background:#c8a84b;color:#fff;}',
    /* Form */
    '#nm-chat-form{display:flex;padding:8px;gap:6px;background:#fff;border-top:1px solid #e8e8e8;}',
    '#nm-chat-input{flex:1;border:1px solid #ddd;border-radius:20px;padding:8px 14px;font-size:13px;outline:none;resize:none;font-family:inherit;}',
    '#nm-chat-input:focus{border-color:#c8a84b;}',
    '#nm-chat-send{background:linear-gradient(135deg,#c8a84b,#e8c96a);border:none;border-radius:50%;width:36px;height:36px;cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:opacity .2s,transform .15s;}',
    '#nm-chat-send:disabled{opacity:.5;cursor:default;}',
    '#nm-chat-send.nm-sent{transform:scale(1.3);}',
    /* Sent confirmation */
    '.nm-confirm{align-self:flex-end;font-size:11px;color:#999;margin-top:-4px;padding-right:4px;}',
    /* Dark mode */
    '@media(prefers-color-scheme:dark){#nm-chat-win{background:#1e1e1e;}#nm-chat-messages{background:#161616;}#nm-quick-replies{background:#161616;}.nm-quick-btn{background:#2a2a2a;color:#c8a84b;border-color:#c8a84b;}.nm-msg.nm-bot{background:#2a2a2a;color:#eee;border-color:#333;}#nm-chat-form{background:#1e1e1e;border-color:#333;}#nm-chat-input{background:#2a2a2a;color:#eee;border-color:#444;}}',
  ].join('');
  document.head.appendChild(style);

  // ─── Chat button (hidden initially, revealed after 3s delay) ──────────────
  var btn = document.createElement('button');
  btn.id = 'nm-chat-btn';
  btn.title = 'Задать вопрос';
  btn.setAttribute('aria-label', 'Открыть чат');
  btn.innerHTML = '&#x1F4AC;'; // 💬
  document.body.appendChild(btn);

  // Delay appearance by 3 seconds
  setTimeout(function () {
    btn.classList.add('nm-btn-visible');
  }, 3000);

  // ─── Chat window ──────────────────────────────────────────────────────────
  var win = document.createElement('div');
  win.id = 'nm-chat-win';
  win.setAttribute('role', 'dialog');
  win.setAttribute('aria-label', 'Чат с ассистентом');
  win.innerHTML = [
    '<div id="nm-chat-header">',
    '  <span>&#x1F916; Ассистент агентства</span>',
    '  <button id="nm-chat-close" aria-label="Закрыть чат">&#x2715;</button>',
    '</div>',
    '<div id="nm-quick-replies">',
    '  <button class="nm-quick-btn" data-text="Хочу узнать цены">💰 Хочу узнать цены</button>',
    '  <button class="nm-quick-btn" data-text="Записаться на съёмку">📸 Записаться на съёмку</button>',
    '</div>',
    '<div id="nm-chat-messages" role="log" aria-live="polite" aria-relevant="additions"></div>',
    '<form id="nm-chat-form" autocomplete="off">',
    '  <input id="nm-chat-input" type="text" placeholder="Задайте вопрос..." maxlength="500" aria-label="Сообщение" />',
    '  <button id="nm-chat-send" type="submit" aria-label="Отправить">&#x27A4;</button>',
    '</form>',
  ].join('');
  win.classList.add('nm-hidden');
  document.body.appendChild(win);

  var messagesEl = document.getElementById('nm-chat-messages');
  var inputEl = document.getElementById('nm-chat-input');
  var sendBtn = document.getElementById('nm-chat-send');
  var closeBtn = document.getElementById('nm-chat-close');
  var formEl = document.getElementById('nm-chat-form');
  var quickRepliesEl = document.getElementById('nm-quick-replies');

  var isOpen = false;

  // ─── Session history ───────────────────────────────────────────────────────
  function loadHistory() {
    try {
      return JSON.parse(sessionStorage.getItem(HISTORY_KEY) || '[]');
    } catch (_) {
      return [];
    }
  }

  function saveHistory(history) {
    try {
      sessionStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(-MAX_HISTORY)));
    } catch (_) {}
  }

  function renderHistory(history) {
    messagesEl.innerHTML = '';
    history.forEach(function (msg) {
      appendBubble(msg.role === 'user' ? 'nm-user' : 'nm-bot', msg.content, false);
    });
    if (!history.length) {
      appendBubble(
        'nm-bot',
        'Здравствуйте! Я ассистент агентства. Чем могу помочь? Спросите о ценах, бронировании, контактах или портфолио.',
        false
      );
    }
    scrollBottom();
  }

  // ─── Bubble helper ────────────────────────────────────────────────────────
  function appendBubble(cls, text, scroll) {
    var div = document.createElement('div');
    div.className = 'nm-msg ' + cls;
    div.textContent = text;
    messagesEl.appendChild(div);
    if (scroll !== false) scrollBottom();
    return div;
  }

  function scrollBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // ─── Toggle open/close ─────────────────────────────────────────────────────
  function openChat() {
    if (isOpen) return;
    isOpen = true;
    win.classList.remove('nm-hidden');
    var history = loadHistory();
    renderHistory(history);
    inputEl.focus();
  }

  function closeChat() {
    if (!isOpen) return;
    isOpen = false;
    win.classList.add('nm-hidden');
  }

  btn.addEventListener('click', function () {
    if (isOpen) closeChat();
    else openChat();
  });
  closeBtn.addEventListener('click', closeChat);

  // Close on Escape key
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && isOpen) closeChat();
  });

  // ─── Quick reply buttons ───────────────────────────────────────────────────
  quickRepliesEl.addEventListener('click', function (e) {
    var quickBtn = e.target.closest('.nm-quick-btn');
    if (!quickBtn) return;
    var text = quickBtn.dataset.text;
    if (!text) return;
    inputEl.value = text;
    formEl.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
  });

  // ─── Send message ──────────────────────────────────────────────────────────
  formEl.addEventListener('submit', function (e) {
    e.preventDefault();
    var text = inputEl.value.trim();
    if (!text || sendBtn.disabled) return;

    var history = loadHistory();

    // Show user bubble with sending animation
    var userBubble = appendBubble('nm-user nm-sending', text);
    history.push({ role: 'user', content: text });
    saveHistory(history);
    inputEl.value = '';
    sendBtn.disabled = true;

    // Animate send button
    sendBtn.classList.add('nm-sent');
    setTimeout(function () {
      sendBtn.classList.remove('nm-sent');
    }, 200);

    // Fade in user bubble (remove sending class after brief delay)
    setTimeout(function () {
      userBubble.classList.remove('nm-sending');
      // Show "sent" tick confirmation
      var confirm = document.createElement('div');
      confirm.className = 'nm-confirm';
      confirm.textContent = '✓ Отправлено';
      messagesEl.appendChild(confirm);
      scrollBottom();
    }, 250);

    // Typing indicator
    var typingBubble = appendBubble('nm-bot nm-typing', '...');

    // Build minimal history payload (last 6 entries)
    var historyPayload = history.slice(-6).map(function (m) {
      return { role: m.role, content: m.content };
    });

    fetch(API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, history: historyPayload }),
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        messagesEl.removeChild(typingBubble);
        var reply = data.reply || data.error || 'Не удалось получить ответ. Попробуйте позже.';
        appendBubble('nm-bot', reply);
        history.push({ role: 'assistant', content: reply });
        saveHistory(history);
      })
      .catch(function () {
        messagesEl.removeChild(typingBubble);
        appendBubble('nm-bot', 'Ошибка соединения. Проверьте интернет и попробуйте ещё раз.');
      })
      .finally(function () {
        sendBtn.disabled = false;
        inputEl.focus();
      });
  });

  // Allow Enter to submit (Shift+Enter for newline — but input is single-line so just submit on Enter)
  inputEl.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      formEl.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
    }
  });
})();
