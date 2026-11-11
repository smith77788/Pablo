# Mini-App Bridge — Dynamic PoC Harness

Defensive reproduction tooling for the Telegram mini-app bridge findings
(Finding 4 primarily; also 1, 5, 6). It runs locally in headless Chromium and
**attacks no one** — it exercises a **stub that models the client's bridge
behaviour as read from the open-source code**, so it validates the attack
*logic* and emits the exact bridge payloads to reproduce against a real test
bot.

## What it shows

`node run.js` runs two scenarios in real Chromium:

- **`sameOrigin == false` (legacy bot) — Finding 4:** a page on the *attacker*
  origin (loaded after the legit mini-app redirected to it) calls the bridge and
  **receives** the user's clipboard and the legit bot's biometric Keychain token
  — i.e. exfiltration succeeds. SSRF/IP-leak fetches (`share_to_story`,
  `file_download` size probe) also fire with no consent.
- **`sameOrigin == true` (modern bot) — secure path:** every event is rejected,
  nothing is exfiltrated.

### Observed output (local run)

```
SCENARIO: sameOrigin == FALSE (legacy bot) — Finding 4
  [ATTACKER GOT]  clipboard_text_received  {"data":"SECRET-2FA-CODE 889922"}
  [KEYCHAIN_READ] bot_777  LEGIT_BOT_SESSION_TOKEN_abc  (no accessGranted gate)
  [ATTACKER GOT]  biometry_auth_requested  {"status":"authorized","token":"LEGIT_BOT_SESSION_TOKEN_abc"}
  [OUTBOUND] share_to_story  http://169.254.169.254/latest/meta-data/   (no consent)
  [OUTBOUND] file_download.getFileSize HEAD  http://192.168.1.1/probe    (proxy bypass)
  >>> EXFILTRATED TO ATTACKER PAGE: clipboard + biometric token

SCENARIO: sameOrigin == TRUE (modern bot) — secure path
  [REJECTED] web_app_read_text_from_clipboard / biometry_request_auth / share_to_story / file_download
  >>> EXFILTRATED TO ATTACKER PAGE: (nothing)
```

## Run it

```
cd docs/poc-harness
npm i -D playwright   # or use a preinstalled Chromium
node run.js
```

## Honest scope / limitation

- `native-stub.js` reproduces the **logic** of the real handlers
  (`isTrustedMainFrameMessage`, `sendEvent`, the `web_app_*` handlers) as read
  from `WebpageModalController.swift` / `WebAppController.swift` /
  `attach_bot_webview.cpp` / `BotWebViewContainer.java`. It is **not** the real
  client binary. It proves the *control/data flow* and the payloads, not the
  native implementation.
- To confirm on a real build, reproduce the **same payloads** against an actual
  Telegram **test** client (see below). Do this only in the test environment,
  with your own bot, never touching other users.

## Reproduce on a real test client (per bug-bounty rules)

1. In the Telegram **test DC**, create a bot via **@BotFather**; enable a Web App
   / attach-menu for it. Use a dedicated test account.
2. Host a mini-app page that, on load, does
   `window.location.href = "https://<your-second-origin>/hijack"` (legacy launch,
   `sameOrigin` flag NOT set).
3. On `<your-second-origin>/hijack`, use the exact payloads from
   `pages/attacker.html` (`TelegramWebviewProxy.postEvent(...)`) and capture
   `TelegramGameProxy.receiveEvent`.
4. Observe clipboard / biometric-token delivery to the second origin, and the
   pre-consent outbound requests to your logging host.

Keep this repository private until fixes ship. Report to security@telegram.org
(drafts in `../telegram-bugbounty-drafts.md`).
