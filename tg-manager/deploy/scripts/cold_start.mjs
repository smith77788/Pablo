/**
 * Замер холодного старта мини-аппа на «слабом телефоне».
 *
 * Документ про разрыв с лидерами ставил цель «главный экран за 2 секунды на
 * среднем телефоне», а время старта не мерилось ни разу. Здесь — честный
 * замер в Chromium с теми же ограничениями, что у DevTools:
 *   * сеть «Slow 4G»: 150 мс задержки, 1,6 Мбит/с вниз, 750 кбит/с вверх;
 *   * процессор замедлен в 4 раза («средний телефон») и в 6 раз («слабый»);
 *   * пустой кэш на каждом прогоне (новый контекст браузера);
 *   * статика отдаётся с gzip, как в бою (services/mini_app_api.py, serve_index).
 * Бэкенд заменён мгновенными пустыми ответами: мерится сам мини-апп, а не
 * сервер и база.
 *
 * «Главный экран готов» — момент, когда loadHome() дорисовал показатели
 * (строка состояния «Показатели обновлены…»): дальше человек может работать.
 *
 * Запуск: node deploy/scripts/cold_start.mjs [--json] [--runs N]
 */
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const http = require('http');
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const { LAUNCH, chromiumOf } = await import('./_chrome.mjs');
const chromium = await chromiumOf();

// MINIAPP_DIR — каталог с уже очищенной копией (так её отдаёт сервер, см.
// services/miniapp_minify.py); без него — исходники как есть.
const ROOT = process.env.MINIAPP_DIR
  || path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..', 'mini_app');
const asJson = process.argv.includes('--json');
const runsArg = process.argv.indexOf('--runs');
const RUNS = runsArg > 0 ? Number(process.argv[runsArg + 1]) || 3 : 3;

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'application/javascript',
  '.css': 'text/css', '.svg': 'image/svg+xml', '.webmanifest': 'application/manifest+json' };
const EMPTY = JSON.stringify({ ok: true, accounts: [], bots: [], channels: [], items: [], rows: [],
  stats: {}, total: 0, count: 0, recent_activity: [], suggestions: [], steps: [] });
const gz = new Map();
let bytes = 0;

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://x');
  if (url.pathname.startsWith('/api/')) {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    return res.end(EMPTY);
  }
  let rel = url.pathname.replace(/^\/miniapp\/?/, '/');
  if (rel === '/' || rel === '') rel = '/index.html';
  const file = path.join(ROOT, path.normalize(rel));
  if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404); return res.end();
  }
  if (!gz.has(file)) gz.set(file, zlib.gzipSync(fs.readFileSync(file), { level: 6 }));
  const body = gz.get(file);
  bytes += body.length;
  res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream',
    'Content-Encoding': 'gzip', 'Cache-Control': 'no-store' });
  res.end(body);
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}/miniapp/`;

const browser = await chromium.launch(LAUNCH);

async function once(cpu) {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await ctx.newPage();
  await page.route('**://telegram.org/**', r => r.abort());
  await page.addInitScript(() => {
    const WA = { ready(){}, expand(){}, initData: 'stub', initDataUnsafe: { user: { id: 1, first_name: 'T' } },
      colorScheme: 'dark', themeParams: {}, platform: 'android', version: '8.0', isVersionAtLeast: () => true,
      MainButton: { show(){}, hide(){}, setText(){}, onClick(){}, setParams(){} },
      BackButton: { show(){}, hide(){}, onClick(){} },
      HapticFeedback: { impactOccurred(){}, notificationOccurred(){}, selectionChanged(){} },
      onEvent(){}, offEvent(){}, openTelegramLink(){}, openLink(){}, showAlert(){}, showConfirm(){},
      close(){}, setHeaderColor(){}, setBackgroundColor(){} };
    const T = {};
    Object.defineProperty(T, 'WebApp', { get: () => WA, set: () => {}, configurable: true });
    Object.defineProperty(window, 'Telegram', { get: () => T, set: () => {}, configurable: true });
    try { localStorage.setItem('infragram_session_token', 'stub'); } catch (_) {}
  });
  const cdp = await ctx.newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: 150,
    downloadThroughput: 1.6 * 1024 * 1024 / 8, uploadThroughput: 750 * 1024 / 8 });
  await cdp.send('Emulation.setCPUThrottlingRate', { rate: cpu });
  bytes = 0;
  const t0 = Date.now();
  await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 120000 });
  let homeMs = null;
  try {
    await page.waitForFunction(() => {
      const s = document.getElementById('homeDataStatus');
      return s && /обновлены/.test(s.textContent || '');
    }, null, { timeout: 60000, polling: 50 });
    homeMs = Date.now() - t0;
  } catch (_) { /* не дошли — так и запишем */ }
  const t = await page.evaluate(() => {
    const n = performance.getEntriesByType('navigation')[0] || {};
    const fcp = performance.getEntriesByName('first-contentful-paint')[0];
    return { dcl: Math.round(n.domContentLoadedEventEnd || 0), load: Math.round(n.loadEventEnd || 0),
      fcp: fcp ? Math.round(fcp.startTime) : null };
  });
  const out = { ...t, home: homeMs, kb: Math.round(bytes / 1024) };
  await ctx.close();
  return out;
}

const median = a => { const s = a.filter(x => x != null).sort((x, y) => x - y); return s.length ? s[Math.floor(s.length / 2)] : null; };
const result = { network: 'Slow 4G (150 мс, 1,6 Мбит/с)', runs: RUNS, profiles: {} };
for (const [name, cpu] of [['mid', 4], ['low', 6]]) {
  const rows = [];
  for (let i = 0; i < RUNS; i++) rows.push(await once(cpu));
  result.profiles[name] = { cpu_slowdown: cpu, fcp_ms: median(rows.map(r => r.fcp)),
    dcl_ms: median(rows.map(r => r.dcl)), home_ms: median(rows.map(r => r.home)),
    transfer_kb: median(rows.map(r => r.kb)), all: rows };
}
await browser.close();
server.close();

if (asJson) console.log(JSON.stringify(result));
else {
  console.log(`Сеть: ${result.network}, прогонов: ${RUNS}, кэш пустой`);
  for (const [n, p] of Object.entries(result.profiles)) {
    console.log(`${n} (CPU ×${p.cpu_slowdown}): первая отрисовка ${p.fcp_ms} мс · DOM ${p.dcl_ms} мс · `
      + `главный экран ${p.home_ms} мс · передано ${p.transfer_kb} КБ`);
  }
}
