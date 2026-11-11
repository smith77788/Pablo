// Native bridge STUB — models Telegram client WebView bridge behaviour.
// Mirrors the real code paths from WebpageModalController.swift (macOS) /
// WebAppController.swift (iOS) / attach_bot_webview.cpp (Desktop) /
// BotWebViewContainer.java (Android). Used ONLY to demonstrate the logic
// of confirmed findings against a local model — it attacks no one.
//
// Injected before any page script, like the real .atDocumentStart user script.
//
// Config via window.__TG_STUB_CONFIG__ set by the runner:
//   { sameOrigin: bool, trustedOrigin: string, botInAttachMenu: bool,
//     clipboard: string, keychain: {<botKey>: token}, botId: string }
(function () {
  const cfg = window.__TG_STUB_CONFIG__ || {};
  const log = (type, detail) => {
    // Surface to the runner via console (captured by Playwright).
    console.log('TG_BRIDGE ' + JSON.stringify({ type, origin: location.origin, detail }));
  };

  // ---- isTrustedMainFrameMessage (macOS :1691 / iOS :239) ----
  // guard sameOrigin == true else { return true }  => sameOrigin==false trusts ANY origin
  function isTrusted() {
    if (cfg.sameOrigin !== true) return true;              // VULN path (Finding 4)
    return location.origin === cfg.trustedOrigin;          // secure path
  }

  // ---- sendEvent (macOS :2892) ----
  // For sameOrigin==false, delivers to WHATEVER page is loaded (no origin check).
  function sendEvent(name, data) {
    if (cfg.sameOrigin === true && location.origin !== cfg.trustedOrigin) {
      log('sendEvent_blocked', { name });
      return;                                              // secure path blocks
    }
    log('sendEvent_delivered', { name, data });
    try {
      if (window.TelegramGameProxy && window.TelegramGameProxy.receiveEvent) {
        window.TelegramGameProxy.receiveEvent(name, data);
      }
    } catch (e) {}
  }

  // ---- performAction handler (handleScriptMessage) ----
  function handleAction(eventName, eventDataRaw) {
    const trusted = isTrusted();
    log('performAction_recv', { eventName, trusted, eventData: eventDataRaw });
    if (!trusted) { log('performAction_rejected', { eventName }); return; }

    let json = {};
    try { json = typeof eventDataRaw === 'string' ? JSON.parse(eventDataRaw) : (eventDataRaw || {}); } catch (e) {}

    switch (eventName) {
      case 'web_app_share_to_story': {              // Finding 1 (macOS)
        // fetchHttpResource(url: media_url) fires BEFORE any consent (no scheme check)
        log('OUTBOUND_FETCH', { api: 'share_to_story', url: json.media_url, note: 'real client would GET this from the user IP, no consent yet' });
        break;
      }
      case 'web_app_request_file_download': {       // Finding 6 (macOS)
        log('OUTBOUND_FETCH', { api: 'file_download.getFileSize', method: 'HEAD', url: json.url, note: 'URLSession.shared direct HEAD before confirm, bypasses in-app proxy' });
        break;
      }
      case 'web_app_read_text_from_clipboard': {    // gated by attach-menu + touch
        const allowed = (cfg.botInAttachMenu === true);
        const data = allowed ? JSON.stringify({ req_id: json.req_id, data: cfg.clipboard || '' })
                             : JSON.stringify({ req_id: json.req_id });
        sendEvent('clipboard_text_received', data);
        break;
      }
      case 'web_app_biometry_update_token': {       // Finding 5 (macOS: no accessGranted check)
        const key = 'bot_' + (cfg.botId || 'X');
        cfg.keychain = cfg.keychain || {};
        cfg.keychain[key] = json.token;
        log('KEYCHAIN_WRITE', { key, token: json.token, note: 'macOS writes without accessGranted gate' });
        sendEvent('biometry_token_updated', '{status: "updated"}');
        break;
      }
      case 'web_app_biometry_request_auth': {       // Finding 5 (macOS: no accessGranted check)
        const key = 'bot_' + (cfg.botId || 'X');
        const token = (cfg.keychain || {})[key];
        log('KEYCHAIN_READ', { key, token, note: 'macOS reads without accessGranted gate; TouchID prompt shown' });
        sendEvent('biometry_auth_requested', JSON.stringify({ status: token ? 'authorized' : 'failed', token: token || '' }));
        break;
      }
      case 'web_app_request_phone': {
        // result is only a STATUS (phone goes to the legit bot), per our verified analysis
        sendEvent('phone_requested', '{status: "sent"}');
        break;
      }
      default:
        log('unhandled', { eventName });
    }
  }

  // ---- inject the proxies exactly as the client does ----
  // Legacy (sameOrigin==false) proxy: NO origin check (macOS :1271, iOS eventProxySource)
  function Proto() {}
  Proto.prototype.postEvent = function (eventName, eventData) {
    // Models window.webkit.messageHandlers.performAction.postMessage(...)
    handleAction(eventName, eventData);
  };
  window.TelegramWebviewProxy = new Proto();

  // GameProxy receiver placeholder (pages override receiveEvent to capture)
  window.TelegramGameProxy = window.TelegramGameProxy || { receiveEvent: function () {} };

  // Convenience WebApp shim (what mini-apps actually call)
  window.Telegram = window.Telegram || {};
  window.Telegram.WebApp = {
    shareToStory: (url) => window.TelegramWebviewProxy.postEvent('web_app_share_to_story', JSON.stringify({ media_url: url })),
    downloadFile: (o) => window.TelegramWebviewProxy.postEvent('web_app_request_file_download', JSON.stringify(o)),
  };

  log('stub_ready', { sameOrigin: cfg.sameOrigin === true, trustedOrigin: cfg.trustedOrigin || null });
})();
