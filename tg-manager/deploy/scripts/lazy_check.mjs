/**
 * Ленивая загрузка мини-аппа в настоящем Chromium (services/miniapp_lazy.py).
 *
 * Три проверки, каждая против ПОЛНОЙ страницы, собранной из тех же исходников:
 *
 *   parity  — после догрузки всех кусков на window ровно те же функции с тем
 *             же текстом, что у полной страницы. Ни одна не потерялась и не
 *             подменилась.
 *   smoke   — каждый экран open*() без аргументов открывается дважды, БЕЗ
 *             фоновой догрузки: код приходит только через заглушки, то есть
 *             проверяется именно путь «первое открытие раздела». Падения
 *             сверяются с полной страницей — новых быть не должно.
 *   boot    — какие функции вызвал старт до готовой главной (синхронные
 *             догрузки на старте). С --write-boot список дописывается в
 *             mini_app/lazy_boot.txt и сборка повторяется, пока старт не
 *             перестанет догружать что-либо.
 *
 * Запуск: node deploy/scripts/lazy_check.mjs [parity|smoke|boot]... [--write-boot] [--json]
 * Выход:  0 — расхождений нет.
 */
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const http = require('http');
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const os = require('os');
const { execFileSync } = require('child_process');

const { LAUNCH, chromiumOf } = await import('./_chrome.mjs');
const chromium = await chromiumOf();

const HERE = path.dirname(new URL(import.meta.url).pathname);
const APP = path.resolve(HERE, '..', '..');
const args = process.argv.slice(2);
const asJson = args.includes('--json');
const modes = args.filter(a => !a.startsWith('--'));
const want = m => !modes.length || modes.includes(m);
const PY = process.env.PYTHON || 'python3';

function build(dir, ...flags) {
  fs.rmSync(dir, { recursive: true, force: true });
  const out = execFileSync(PY, ['-m', 'services.miniapp_lazy', dir, ...flags], { cwd: APP }).toString();
  return out.trim() ? JSON.parse(out.trim().split('\n').pop()) : null;
}

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'application/javascript',
  '.css': 'text/css', '.svg': 'image/svg+xml', '.webmanifest': 'application/manifest+json' };
const EMPTY = JSON.stringify({ ok: true, accounts: [], bots: [], channels: [], items: [], rows: [],
  stats: {}, total: 0, count: 0, recent_activity: [], suggestions: [], steps: [], keywords: [],
  alerts: [], bot_suggestions: [], presets: [], recent: [], history: [], top_channels: [],
  by_stage: [], operations: [], segments: [], contacts: [], folders: [], proxies: [], messages: [],
  chats: [], targets: [], results: [], plans: [], nodes: [], templates: [], instances: [] });

function serve(root) {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://x');
    if (url.pathname.startsWith('/api/')) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end(EMPTY);
    }
    let rel = url.pathname.replace(/^\/miniapp\/?/, '/');
    if (rel === '/' || rel === '') rel = '/index.html';
    const file = path.join(root, path.normalize(rel));
    if (!file.startsWith(root) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404); return res.end();
    }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream',
      'Cache-Control': 'no-store' });
    res.end(fs.readFileSync(file));
  });
  return new Promise(r => server.listen(0, '127.0.0.1', () =>
    r({ server, base: `http://127.0.0.1:${server.address().port}/miniapp/` })));
}

const browser = await chromium.launch(LAUNCH);
const NOISE = /Failed to load resource|net::ERR_|EventSource|Synchronous XMLHttpRequest/i;

async function open(base, { noIdle = true } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await ctx.newPage();
  await page.route('**://telegram.org/**', r => r.abort());
  await page.addInitScript((noIdle) => {
    if (noIdle) window.__lzNoIdle = true;
    const WA = { ready(){}, expand(){}, initData: 'stub', initDataUnsafe: { user: { id: 1, first_name: 'T' } },
      colorScheme: 'dark', themeParams: {}, platform: 'android', version: '8.0', isVersionAtLeast: () => true,
      MainButton: { show(){}, hide(){}, setText(){}, onClick(){}, offClick(){}, setParams(){} },
      SecondaryButton: { show(){}, hide(){}, setText(){}, onClick(){}, offClick(){}, setParams(){} },
      BackButton: { show(){}, hide(){}, onClick(){}, offClick(){} },
      HapticFeedback: { impactOccurred(){}, notificationOccurred(){}, selectionChanged(){} },
      onEvent(){}, offEvent(){}, openTelegramLink(){}, openLink(){}, showAlert(){}, showConfirm(){},
      close(){}, setHeaderColor(){}, setBackgroundColor(){}, setBottomBarColor(){} };
    const T = {};
    Object.defineProperty(T, 'WebApp', { get: () => WA, set: () => {}, configurable: true });
    Object.defineProperty(window, 'Telegram', { get: () => T, set: () => {}, configurable: true });
    try { localStorage.setItem('infragram_session_token', 'stub'); } catch (_) {}
  }, noIdle);
  const errors = [];
  page.on('pageerror', e => errors.push(String(e && e.message || e)));
  page.on('console', m => { if (m.type() === 'error' && !NOISE.test(m.text())) errors.push('console: ' + m.text()); });
  await page.goto(base, { waitUntil: 'load', timeout: 60000 });
  await page.waitForFunction(() => window.loadHome && window.loadHome.lastData, null,
    { timeout: 30000 }).catch(() => {});
  await page.waitForTimeout(800);
  return { ctx, page, errors };
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'lazy-'));
const FULL = path.join(tmp, 'full'), LAZY = path.join(tmp, 'lazy');
const report = {};
let bad = false;

build(FULL, '--full');
const full = await serve(FULL);

if (want('boot') || args.includes('--write-boot')) {
  const bootFile = path.join(APP, 'mini_app', 'lazy_boot.txt');
  const rounds = [];
  for (let round = 0; round < 6; round++) {
    build(LAZY);
    const lz = await serve(LAZY);
    const { ctx, page } = await open(lz.base);
    const trace = await page.evaluate(() => (window.__lzTrace || []).slice());
    await ctx.close(); lz.server.close();
    rounds.push(trace);
    if (!trace.length || !args.includes('--write-boot')) break;
    const have = fs.existsSync(bootFile) ? fs.readFileSync(bootFile, 'utf8') : '';
    const names = new Set(have.split('\n').map(s => s.trim()).filter(s => s && !s.startsWith('#')));
    trace.forEach(n => names.add(n));
    const head = have.split('\n').filter(s => s.startsWith('#')).join('\n');
    fs.writeFileSync(bootFile, (head ? head + '\n' : '') + [...names].sort().join('\n') + '\n');
  }
  report.boot = { rounds: rounds.map(r => r.length), last: rounds[rounds.length - 1] };
  if (!args.includes('--write-boot') && rounds[0].length) report.boot.note = 'старт догружает куски синхронно';
}

build(LAZY);
const lz = await serve(LAZY);

if (want('parity')) {
  const collect = async (base, loadAll) => {
    const { ctx, page, errors } = await open(base);
    const fns = await page.evaluate((loadAll) => {
      if (loadAll) window.__lazyLoadAll();
      const out = {};
      for (const k of Object.getOwnPropertyNames(window)) {
        let v; try { v = window[k]; } catch (_) { continue; }
        if (typeof v === 'function' && !/\{\s*\[native code\]\s*\}$/.test(Function.prototype.toString.call(v)))
          out[k] = Function.prototype.toString.call(v);
      }
      return out;
    }, loadAll);
    await ctx.close();
    return { fns, errors };
  };
  const a = await collect(full.base, false), b = await collect(lz.base, true);
  const missing = Object.keys(a.fns).filter(k => !(k in b.fns));
  const extra = Object.keys(b.fns).filter(k => !(k in a.fns) && !k.startsWith('__lazy'));
  const changed = Object.keys(a.fns).filter(k => k in b.fns && a.fns[k] !== b.fns[k]);
  report.parity = { functions: Object.keys(a.fns).length, missing, extra, changed: changed.slice(0, 20),
    boot_errors_full: a.errors, boot_errors_lazy: b.errors };
  if (missing.length || extra.length || changed.length || b.errors.length > a.errors.length) bad = true;
}

if (want('smoke')) {
  const smoke = async (base) => {
    const { ctx, page, errors } = await open(base);
    const screens = await page.evaluate(() => Object.getOwnPropertyNames(window)
      .filter(k => /^open[A-Z]/.test(k) && typeof window[k] === 'function' && window[k].length === 0).sort());
    const fails = {};
    for (const fn of screens) {
      for (const pass of [1, 2]) {
        errors.length = 0;
        const thrown = await page.evaluate(async (name) => {
          try { await window[name](); return null; } catch (e) { return String((e && e.message) || e); }
        }, fn).catch(e => String(e && e.message || e));
        await page.waitForTimeout(100);
        const found = [...new Set(errors)];
        if (thrown) found.unshift(thrown);
        if (found.length) { fails[fn] = found.slice(0, 2); break; }
        await page.evaluate(() => { try { if (typeof back === 'function') back(); } catch {} });
        await page.waitForTimeout(40);
      }
    }
    const trace = await page.evaluate(() => (window.__lzTrace || []).length);
    await ctx.close();
    return { screens: screens.length, fails, syncLoads: trace };
  };
  const a = await smoke(full.base), b = await smoke(lz.base);
  const fresh = Object.keys(b.fails).filter(k => !(k in a.fails));
  report.smoke = { screens_full: a.screens, screens_lazy: b.screens, failing_full: Object.keys(a.fails).length,
    failing_lazy: Object.keys(b.fails).length, new_failures: Object.fromEntries(fresh.map(k => [k, b.fails[k]])),
    sync_loads: b.syncLoads };
  if (fresh.length || a.screens !== b.screens) bad = true;
}

await browser.close();
full.server.close(); lz.server.close();
fs.rmSync(tmp, { recursive: true, force: true });
console.log(asJson ? JSON.stringify(report) : JSON.stringify(report, null, 2));
process.exit(bad ? 1 : 0);
