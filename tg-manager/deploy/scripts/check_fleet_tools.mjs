/**
 * Живая проверка массового управления ресурсами в настоящем мини-аппе.
 *
 * Открывает mini_app/index.html в Chromium с подставным API и проверяет то,
 * что обещают требования документа: таблицу аккаунтов на широком экране и
 * карточки на телефоне, сортировку кликом по заголовку, сохранённый вид,
 * чипы тегов, выбор диапазона через Shift и массовое действие по прокси.
 * Печатает JSON с результатом; tests/test_fleet_tools_browser.py его читает.
 *
 * Запуск: node deploy/scripts/check_fleet_tools.mjs
 */
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const http = require('http');
const fs = require('fs');
const path = require('path');

const { LAUNCH, chromiumOf } = await import('./_chrome.mjs');
const chromium = await chromiumOf();
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..', 'mini_app');

const ACCS = [1, 2, 3].map(i => ({ id: i, phone: '+7900000000' + i, first_name: 'Акк' + i,
  is_active: true, acc_status: i === 2 ? 'banned' : 'ok', trust_score: 30 * i, stage: null,
  pool: i === 1 ? 'ru' : 'eu', has_session: true, has_proxy: i !== 3, proxy_alive: true,
  proxy_label: 'DE', tags: i < 3 ? ['прогрев'] : [], last_used: null }));
const PROXIES = [1, 2, 3].map(i => ({ id: 10 + i, label: 'p' + i, proxy_url: 'h' + i + ':1',
  proxy_type: 'socks5', is_alive: i !== 2, is_backup: false, acc_count: 0, latency_ms: 100 }));
const calls = [];
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'application/javascript',
  '.css': 'text/css', '.svg': 'image/svg+xml', '.webmanifest': 'application/manifest+json' };

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://x');
  if (url.pathname.startsWith('/api/')) {
    let body = '';
    req.on('data', c => { body += c; });
    req.on('end', () => {
      calls.push({ method: req.method, path: url.pathname, qs: url.search, body });
      let out = { ok: true };
      if (url.pathname === '/api/miniapp/accounts') out = { accounts: ACCS, stats: { total: 3 },
        page: { filtered_total: 3, has_more: false } };
      else if (url.pathname === '/api/miniapp/accounts/tags') out = { tags: [{ tag: 'прогрев', count: 2 }] };
      else if (url.pathname === '/api/miniapp/views') out = req.method === 'GET'
        ? { views: [{ id: 7, name: 'Мёртвые', params: { filter: 'dead', sort: 'trust', dir: 'asc' } }] }
        : { ok: true, id: 8 };
      else if (url.pathname === '/api/miniapp/proxies') out = { proxies: PROXIES, total: 3,
        counts: { alive: 2, dead: 1, avg_latency: 100 } };
      else if (url.pathname === '/api/miniapp/proxies/bulk') out = { ok: true, count: 3 };
      else out = { ok: true, accounts: [], bots: [], channels: [], items: [], rows: [], stats: {},
        total: 0, recent_activity: [], suggestions: [], steps: [] };
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(out));
    });
    return;
  }
  let rel = url.pathname.replace(/^\/miniapp\/?/, '/');
  if (rel === '/' || rel === '') rel = '/index.html';
  const file = path.join(ROOT, path.normalize(rel));
  if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404); return res.end();
  }
  res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' });
  res.end(fs.readFileSync(file));
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}/miniapp/`;

const browser = await chromium.launch(LAUNCH);
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await ctx.newPage();
const errors = [];
page.on('pageerror', e => errors.push(String(e.message || e)));
await page.route('**://telegram.org/**', r => r.abort());
await page.addInitScript(() => {
  const WA = { ready(){}, expand(){}, initData: 'stub', initDataUnsafe: { user: { id: 1, first_name: 'T' } },
    colorScheme: 'dark', themeParams: {}, platform: 'tdesktop', version: '8.0', isVersionAtLeast: () => true,
    MainButton: { show(){}, hide(){}, setText(){}, onClick(){}, setParams(){} },
    BackButton: { show(){}, hide(){}, onClick(){} },
    HapticFeedback: { impactOccurred(){}, notificationOccurred(){}, selectionChanged(){} },
    onEvent(){}, offEvent(){}, openTelegramLink(){}, openLink(){}, showAlert(){}, showConfirm(){},
    close(){}, setHeaderColor(){}, setBackgroundColor(){} };
  const T = {};
  Object.defineProperty(T, 'WebApp', { get: () => WA, set: () => {}, configurable: true });
  Object.defineProperty(window, 'Telegram', { get: () => T, set: () => {}, configurable: true });
  try { localStorage.setItem('infragram_session_token', 'stub'); localStorage.removeItem('fx_sort_accounts'); } catch (_) {}
});
await page.goto(BASE, { waitUntil: 'load', timeout: 60000 });
const settle = () => page.waitForTimeout(400);
const out = {};

await page.evaluate(() => goTab('accounts'));
await page.waitForSelector('#accList table.fx-table', { timeout: 15000 });
out.wide_rows = await page.$$eval('#accList table.fx-table tbody tr', r => r.length);
out.tag_chip = await page.$eval('#accTagChips', e => e.textContent.trim());
out.view_chip = await page.$eval('#fx-accounts', e => e.textContent.includes('Мёртвые'));
out.sort_select = await page.$$eval('#fx-accounts select option', o => o.length);

calls.length = 0;
await page.click('#accList th:has-text("Траст")');
await settle();
out.header_sort = calls.filter(c => c.path === '/api/miniapp/accounts').map(c => c.qs).pop() || '';

calls.length = 0;
await page.click('#fx-accounts .fx-view');
await settle();
out.view_query = calls.filter(c => c.path === '/api/miniapp/accounts').map(c => c.qs).pop() || '';

calls.length = 0;
await page.click('#accTagChips .lf-chip');
await settle();
out.tag_query = calls.filter(c => c.path === '/api/miniapp/accounts').map(c => c.qs).pop() || '';

await page.evaluate(() => toggleAccSelMode());
await page.click('#accList tbody tr:nth-child(1)');
await page.click('#accList tbody tr:nth-child(3)', { modifiers: ['Shift'] });
out.range_selected = await page.evaluate(() => ACC_SEL.size);
await page.evaluate(() => toggleAccSelMode());

await page.setViewportSize({ width: 390, height: 844 });
await settle();
out.phone_cards = await page.$$eval('#accList .acc-row', r => r.length);
out.phone_table = await page.$$eval('#accList table', r => r.length);
await page.screenshot({ path: process.env.FX_SHOT_PHONE || '/dev/null' }).catch(() => {});
await page.setViewportSize({ width: 1280, height: 900 });
await settle();
await page.evaluate(() => { ACC_TAG_FILTER = ''; ACC_FILTER = 'all'; reloadAccounts(); });
await settle();
await page.screenshot({ path: process.env.FX_SHOT_WIDE || '/dev/null' }).catch(() => {});

await page.evaluate(() => { openProxyPool(); });
await page.waitForSelector('#proxyPoolList .li', { timeout: 15000 });
await page.evaluate(() => togglePpSelMode());
await page.click('#proxyPoolList .li:nth-child(1)');
await page.click('#proxyPoolList .li:nth-child(3)', { modifiers: ['Shift'] });
out.pp_selected = await page.evaluate(() => PP_SEL.size);
calls.length = 0;
await page.evaluate(() => runPpMass('backup_on'));
await settle();
const bulk = calls.find(c => c.path === '/api/miniapp/proxies/bulk');
out.pp_bulk = bulk ? JSON.parse(bulk.body) : null;
out.errors = errors;

console.log(JSON.stringify(out));
await browser.close();
server.close();
