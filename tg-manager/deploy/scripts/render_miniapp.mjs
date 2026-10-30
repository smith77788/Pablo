/**
 * Render the Telegram mini-app (mini_app/index.html) headlessly for visual/layout
 * verification — the project previously had NO way to render it (CLAUDE.md:
 * «в песочнице нет рендера Telegram-мини-аппа»), so UI bugs (clipping, dead
 * screens) shipped unverified. This stubs window.Telegram.WebApp + fetch and
 * screenshots any screen at a mobile viewport, and reports clipped content.
 *
 * Usage:
 *   node deploy/scripts/render_miniapp.mjs [screenTab] [outDir]
 *       screenTab: home|accounts|bots|broadcasts|more  (default: home)
 *   node deploy/scripts/render_miniapp.mjs sweep <файл-со-списком-функций>
 *       Открывает ПО ОЧЕРЕДИ каждый экран из списка (по одной open-функции на
 *       строку) и отчитывается об ошибках JS и обрезанном содержимом. Список
 *       строится из исходника; экранов, открываемых без аргументов, — 114, а
 *       вкладок, которые умел проверять прежний режим, — 5.
 *
 * ПРО МЕРКУ ОБРЕЗКИ — это главное здесь. Прежняя проверка сравнивала
 * `document.documentElement.scrollWidth` с `window.innerWidth` и была
 * СТРУКТУРНО ПУСТОЙ: в этом приложении `html,body{overflow:hidden}` и
 * `.screen{overflow-x:hidden}`, поэтому scrollWidth не может превысить
 * innerWidth НИКОГДА. Строка «h-overflow: OK» печаталась всегда, на любом
 * экране и при любой вёрстке.
 *
 * И беда тут другой формы: слишком широкое содержимое не прокручивается, а
 * ОБРЕЗАЕТСЯ — владелец просто не видит правую часть кнопки или числа.
 * Поэтому мерится правый край самих элементов внутри показанного экрана.
 * Элементы внутри предка с `overflow-x:auto|scroll` не считаются: полоса
 * чипов прокручивается сама, её содержимое за краем доступно, а не потеряно
 * (без этого исключения мерка давала шесть ложных находок на полосах чипов).
 *
 * Каждый запуск печатает САМОПРОВЕРКУ: в показанный экран подставляется
 * заведомо широкий элемент, и мерка обязана его назвать. Без этой строки
 * «обрезки нет» не значит ничего — ровно так прежняя проверка и молчала.
 *
 * Requires Playwright + a Chromium build. Resolves both from common locations;
 * override with PLAYWRIGHT_JS and CHROME_BIN env vars.
 */
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const path = require('path');
const fs = require('fs');

const PW = process.env.PLAYWRIGHT_JS
  || ['playwright', '/opt/node22/lib/node_modules/playwright/index.mjs'].find(p => { try { require.resolve(p); return true; } catch { return p.startsWith('/'); } });
const _pw = await import(PW);
const chromium = _pw.chromium || (_pw.default && _pw.default.chromium);

const arg1 = process.argv[2] || 'home';
const sweepMode = arg1 === 'sweep';
const tab = sweepMode ? 'home' : arg1;
const outDir = sweepMode ? '.' : (process.argv[3] || '.');
const sweepList = sweepMode ? (process.argv[3] || '') : '';

const CHROME = process.env.CHROME_BIN || [
  '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
  '/opt/pw-browsers/chromium/chrome-linux/chrome',
].find(p => { try { fs.accessSync(p); return true; } catch { return false; } });

const browser = await chromium.launch(CHROME ? { executablePath: CHROME } : {});
// Ширина окна — главный множитель обрезки, и 390 (iPhone 14) её почти не
// вызывает. Узкие телефоны в ходу: 320 — iPhone SE первого поколения и
// недорогие Android. MINIAPP_WIDTH позволяет прогнать обход по обеим.
const VW = Number(process.env.MINIAPP_WIDTH) || 390;
const ctx = await browser.newContext({ viewport: { width: VW, height: 844 }, deviceScaleFactor: sweepMode ? 1 : 2 });
const page = await ctx.newPage();
await page.addInitScript((fat) => {
  // Заглушку надо ДЕРЖАТЬ, а не просто поставить. Рядом с index.html лежит
  // настоящий telegram-web-app.js, он грузится и делает `window.Telegram = {...}`,
  // затем `window.Telegram.WebApp = ...` — то есть затирает заглушку, и
  // initData становится пустой. Приложение уходит в ветку связывания
  // устройства, поверх всего встаёт оверлей ввода кода, boot() до конца не
  // доходит: любой снимок — это фотография окна входа, а не приложения.
  // Поэтому и `Telegram`, и `Telegram.WebApp` отдаются через getter с
  // глотающим setter: присваивание SDK молча проходит и ничего не меняет
  // (бросать нельзя — упадёт сам SDK, а с ним и загрузка страницы).
  const WA = { ready(){}, expand(){}, initData:'stub-init-data', initDataUnsafe:{ user:{ id:1, first_name:'Test' } }, colorScheme:'dark', themeParams:{}, MainButton:{show(){},hide(){},setText(){},onClick(){}}, BackButton:{show(){},hide(){},onClick(){}}, HapticFeedback:{impactOccurred(){},notificationOccurred(){}}, onEvent(){}, offEvent(){}, openTelegramLink(){}, showAlert(){}, showConfirm(){}, close(){}, setHeaderColor(){}, setBackgroundColor(){} };
  const TGSTUB = {};
  Object.defineProperty(TGSTUB, 'WebApp', { get: () => WA, set: () => {}, configurable: true });
  Object.defineProperty(window, 'Telegram', { get: () => TGSTUB, set: () => {}, configurable: true });
  const ok = (o) => new Response(JSON.stringify(o), { status: 200, headers: { 'Content-Type': 'application/json' } });
  const EMPTY = { ok:true, token:'stub-token', accounts:[], bots:[], channels:[], keywords:[], alerts:[], suggestions:[], bot_suggestions:[], rows:[], items:[], stats:{}, total:0, count:0, presets:[], recent:[], history:[], top_channels:[], recent_activity:[], by_stage:[] };
  if (!fat) { window.fetch = async () => ok(EMPTY); return; }
  // «Толстые» данные: длинные строки — главная причина обрезки по ширине.
  // Пустой ответ проверяет только пустые состояния экранов.
  const LONG = 'Канал про автоматизацию и продвижение в Телеграме без границ';
  const row = (i) => ({
    id: i, acc_id: i, bot_id: i, channel_id: -1000000000000 - i, user_id: 1000 + i,
    contact_id: i, keyword_id: i, op_id: i, rental_id: i, offering_id: i,
    name: LONG, title: LONG, label: LONG, first_name: LONG, last_name: LONG,
    username: 'очень_длинный_псевдоним_пользователя_' + i, phone: '+7999000' + i,
    text: LONG + ' ' + LONG, message_text: LONG, note: LONG, reason: LONG,
    status: 'active', phase: 'warming', state: 'running', kind: 'note',
    created_at: '2026-10-30T04:00:00Z', updated_at: '2026-10-30T04:00:00Z',
    finished_at: '2026-10-30T04:00:00Z', checked_at: '2026-10-30T04:00:00Z',
    started_at: '2026-10-30T04:00:00Z', next_send_at: '2026-10-30T04:00:00Z',
    count: 1234567, total: 1234567, value: 1234567, price: 1234567,
    position: 7, prev_position: 12, pos: 7, prev_pos: 12, trust_score: 88,
    is_active: true, enabled: true, has_session: true, ok: true,
  });
  const ROWS = Array.from({ length: 25 }, (_, i) => row(i + 1));
  const ARRAY_KEYS = ['accounts','bots','channels','keywords','alerts','suggestions',
    'bot_suggestions','rows','items','presets','recent','history','top_channels',
    'recent_activity','by_stage','orders','files','contacts','groups','links',
    'proxies','personas','experiments','workflows','nodes','edges','deals',
    'templates','segments','warnings','people','operations','ops','sessions',
    'members','devices','plans','rules','tags','meshes','targets','variants',
    'steps','funnels','subscribers','logs','events','reminders','duplicates',
    'conflicts','networks','instances','ecosystems','gifts','stars','strikes'];
  window.fetch = async () => {
    const o = { ok:true, token:'stub-token', total: 1234567, count: 1234567, stats: {}, page: { limit: 25, offset: 0, has_more: true } };
    for (const k of ARRAY_KEYS) o[k] = ROWS;
    return ok(o);
  };
}, sweepMode);

let live = [];
page.on('pageerror', e => live.push('PAGEERROR: ' + String(e.message).slice(0, 160)));
const root = path.resolve(process.cwd(), 'mini_app/index.html');
await page.goto('file://' + root, { waitUntil: 'load', timeout: 20000 });
await page.waitForTimeout(1200);

// Мерка обрезки — одним текстом и для отчёта, и для самопроверки.
const CLIPPED = () => {
  const scr = document.querySelector('.screen.show');
  const W = window.innerWidth;
  const out = [];
  if (!scr) return { n: 0, worst: [] };
  for (const el of scr.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    let scrollable = false;
    for (let a = el.parentElement; a && a !== scr; a = a.parentElement) {
      const ox = getComputedStyle(a).overflowX;
      if (ox === 'auto' || ox === 'scroll') { scrollable = true; break; }
    }
    if (scrollable) continue;
    if (r.right > W + 1) {
      out.push(el.tagName.toLowerCase() + '.' + String(el.className || '').split(' ')[0]
               + ' right=' + Math.round(r.right) + ' «' + (el.textContent || '').trim().slice(0, 28) + '»');
    }
  }
  return { n: out.length, worst: out.slice(0, 3) };
};

async function selfCheck() {
  return page.evaluate((src) => {
    const clipped = new Function('return (' + src + ')()');
    const scr = document.querySelector('.screen.show');
    if (!scr) return { ran: false };
    const bad = document.createElement('div');
    bad.style.cssText = 'width:900px;height:20px';
    bad.textContent = 'заведомо широкий элемент';
    scr.appendChild(bad);
    const caught = clipped().n > 0;
    bad.remove();
    const clean = clipped().n;
    const old = document.documentElement.scrollWidth > window.innerWidth;
    return { ran: true, caught, clean, old };
  }, CLIPPED.toString());
}

if (sweepMode) {
  const fns = fs.readFileSync(sweepList, 'utf8').split('\n').map(s => s.trim()).filter(Boolean);
  const sc = await selfCheck();
  console.log(`самопроверка мерки: заведомо обрезанный элемент найден=${sc.caught}`
    + ` | прежняя мерка (scrollWidth) его видит=${sc.old}`);
  if (!sc.caught) {
    console.log('МЕРКА СЛЕПА — «обрезки нет» ниже ничего не означает');
    process.exitCode = 2;
  }
  const report = [];
  for (const fn of fns) {
    live = [];
    const r = await page.evaluate(async (name) => {
      if (typeof window[name] !== 'function') return { missing: true };
      try { await window[name](); } catch (e) { return { threw: String(e && e.message || e).slice(0, 160) }; }
      return {};
    }, fn);
    await page.waitForTimeout(220);
    // MINIAPP_SHOT_DIR — снимок каждого экрана обхода. Отчёт говорит, ЧТО
    // обрезано, а посмотреть, как это выглядит, иначе нечем: экраны обхода не
    // вкладки, и одиночный режим их не открывает.
    if (process.env.MINIAPP_SHOT_DIR) {
      await page.screenshot({ path: path.join(process.env.MINIAPP_SHOT_DIR, `miniapp_${fn}.png`) });
    }
    const m = await page.evaluate((src) => new Function('return (' + src + ')()')(), CLIPPED.toString());
    const problems = [];
    if (r.missing) problems.push('НЕТ ФУНКЦИИ');
    if (r.threw) problems.push('THREW: ' + r.threw);
    if (live.length) problems.push(...live.slice(0, 2));
    if (m.n) problems.push(`ОБРЕЗАНО ${m.n}: ` + m.worst.join(' ; '));
    if (problems.length) report.push(fn + ' | ' + problems.join(' | '));
    await page.evaluate(() => { try { while (typeof STACK !== 'undefined' && STACK.length) back(); } catch(_) {} });
    await page.waitForTimeout(60);
  }
  console.log(`экранов проверено: ${fns.length} (ширина окна ${VW})`);
  console.log(`с находками: ${report.length}`);
  report.forEach(r => console.log('  ' + r));
  if (report.length) process.exitCode = 1;
} else {
  if (tab !== 'home') {
    await page.evaluate((t) => { if (typeof navGo === 'function') navGo(t, null); else if (typeof goTab === 'function') goTab(t); }, tab);
    await page.waitForTimeout(1000);
  }
  const shot = path.join(outDir, `miniapp_${tab}.png`);
  await page.screenshot({ path: shot });
  const sc = await selfCheck();
  const m = await page.evaluate((src) => new Function('return (' + src + ')()')(), CLIPPED.toString());
  console.log(`screen=${tab} -> ${shot}`);
  console.log(`самопроверка мерки: заведомо обрезанный элемент найден=${sc.caught}`
    + ` | прежняя мерка (scrollWidth) его видит=${sc.old}`);
  console.log(`clipped: ${m.n === 0 ? 'OK' : 'ОБРЕЗАНО ' + m.n + ' — ' + m.worst.join(' ; ')}`);
  console.log(`js-errors: ${live.length}`);
  live.slice(0, 15).forEach(e => console.log('  ' + e));
}
await browser.close();
