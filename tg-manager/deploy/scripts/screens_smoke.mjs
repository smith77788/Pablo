/**
 * Смоук экранов мини-аппа: открыть каждый экран ДВАЖДЫ и поймать живые ошибки JS.
 *
 * Зачем именно дважды. Самый частый способ сломать экран здесь — обратиться к
 * элементу, который лежит внутри контейнера, перерисовываемого этой же
 * функцией. Первое открытие проходит, второе падает на null:
 *
 *     Cannot set properties of null (setting 'textContent')
 *
 * Ровно так лёг экран инвайтинга 2026-09-05. Статически этот класс не ловится:
 * область видимости переменных JS регулярками не разрешается (две попытки дали
 * ложные находки — одноимённые переменные в разных функциях указывают на разные
 * элементы). Поэтому проверка — не разбор текста, а настоящий запуск.
 *
 * Запуск:  node deploy/scripts/screens_smoke.mjs [--json] [--only=openMassInvite]
 * Выход:   0 — ошибок нет; 1 — есть падающие экраны.
 *
 * Требует Playwright + Chromium (пути ищутся как в render_miniapp.mjs;
 * переопределяются PLAYWRIGHT_JS и CHROME_BIN).
 */
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const PW = process.env.PLAYWRIGHT_JS
  || ['playwright', '/opt/node22/lib/node_modules/playwright/index.mjs'].find(p => {
       try { require.resolve(p); return true; } catch { return p.startsWith('/'); } });
const { chromium } = await import(PW);
const path = require('path');
const fs = require('fs');

const args = process.argv.slice(2);
const asJson = args.includes('--json');
const only = (args.find(a => a.startsWith('--only=')) || '').slice(7);

const CHROME = process.env.CHROME_BIN || [
  '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
  '/opt/pw-browsers/chromium/chrome-linux/chrome',
].find(p => { try { fs.accessSync(p); return true; } catch { return false; } });

const browser = await chromium.launch(CHROME ? { executablePath: CHROME } : {});
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
const page = await ctx.newPage();

// Тот же стенд, что у render_miniapp.mjs: без него мини-апп не стартует вне
// Telegram, а любой запрос улетал бы в сеть.
await page.addInitScript(() => {
  window.Telegram = { WebApp: {
    ready(){}, expand(){}, initData:'', initDataUnsafe:{ user:{ id:1, first_name:'Test' } },
    colorScheme:'dark', themeParams:{},
    MainButton:{show(){},hide(){},setText(){},onClick(){}},
    BackButton:{show(){},hide(){},onClick(){}},
    HapticFeedback:{impactOccurred(){},notificationOccurred(){}},
    onEvent(){}, offEvent(){}, openTelegramLink(){}, showAlert(){}, showConfirm(){},
    close(){}, setHeaderColor(){}, setBackgroundColor(){} } };
  const ok = (o) => new Response(JSON.stringify(o), {
    status: 200, headers: { 'Content-Type': 'application/json' } });
  // Пустые коллекции по всем ходовым ключам: экран должен переживать «данных нет».
  window.fetch = async () => ok({
    ok:true, accounts:[], bots:[], channels:[], keywords:[], alerts:[], suggestions:[],
    bot_suggestions:[], rows:[], items:[], stats:{}, total:0, count:0, presets:[],
    recent:[], history:[], top_channels:[], recent_activity:[], by_stage:[],
    operations:[], segments:[], contacts:[], folders:[], proxies:[], messages:[],
    chats:[], targets:[], results:[], plans:[], nodes:[], templates:[], instances:[],
  });
});

const errors = [];
// Настоящий сигнал — необработанное исключение. Ошибки консоли берём тоже, но
// без сетевого шума самого стенда: страница открыта как file://, поэтому любой
// запрос и EventSource честно ругаются на CORS — к качеству экрана это
// отношения не имеет, а падающим экраном выглядит.
const NOISE = /CORS|Failed to load resource|net::ERR_|ERR_FAILED|Access to resource/i;
page.on('pageerror', e => errors.push(String(e && e.message || e)));
page.on('console', m => {
  if (m.type() !== 'error') return;
  const t = m.text();
  if (!NOISE.test(t)) errors.push('console: ' + t);
});

const root = path.resolve(process.cwd(), 'mini_app/index.html');
await page.goto('file://' + root, { waitUntil: 'load', timeout: 30000 });
await page.waitForTimeout(1200);

// Экраны — функции без обязательных аргументов: openFoo(id) без id упал бы по
// причине, не имеющей отношения к делу.
const screens = await page.evaluate(() => Object.getOwnPropertyNames(window)
  .filter(k => /^open[A-Z]/.test(k) && typeof window[k] === 'function' && window[k].length === 0)
  .sort());

const targets = only ? screens.filter(s => s === only) : screens;
const failures = [];

for (const fn of targets) {
  for (const pass of [1, 2]) {
    errors.length = 0;
    const thrown = await page.evaluate(async (name) => {
      try { await window[name](); return null; }
      catch (e) { return String((e && e.message) || e); }
    }, fn).catch(e => String(e && e.message || e));
    await page.waitForTimeout(120);

    const found = [...new Set(errors)];
    if (thrown) found.unshift(thrown);
    if (found.length) {
      failures.push({ screen: fn, pass, errors: found.slice(0, 3) });
      break;                       // второй проход после падения ничего не добавит
    }
    // возвращаемся назад, чтобы стек экранов не рос до бесконечности
    await page.evaluate(() => { try { if (typeof back === 'function') back(); } catch {} });
    await page.waitForTimeout(60);
  }
}

await browser.close();

if (asJson) {
  console.log(JSON.stringify({ checked: targets.length, failures }, null, 2));
} else {
  console.log(`экранов проверено: ${targets.length} (каждый открывается дважды)`);
  console.log(`падает: ${failures.length}`);
  for (const f of failures) {
    console.log(`  ${f.screen} — открытие №${f.pass}`);
    f.errors.forEach(e => console.log(`      ${e}`));
  }
}
process.exit(failures.length ? 1 : 0);
