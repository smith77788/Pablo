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
await page.addInitScript((mode) => {
  const fat = mode === 'fat';
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
  if (mode === 'fail') {
    // Третий режим: бэкенд отвечает пятисоткой на всё, кроме входа. Вход
    // оставлен рабочим намеренно — иначе поверх всего встанет окно входа, и
    // проверяться будет оно одно, а не поведение экранов при сбое.
    const boom = () => new Response(JSON.stringify({ error: 'Внутренняя ошибка' }),
      { status: 500, headers: { 'Content-Type': 'application/json' } });
    window.fetch = async (u) => {
      const url = String(u || '');
      if (url.indexOf('/auth') >= 0 || url.indexOf('/config') >= 0) return ok(EMPTY);
      return boom();
    };
    return;
  }
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
    action: 'Проверка аккаунтов и очистка мёртвых разом',
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
  window.fetch = async (u) => {
    const url = String(u || '');
    const o = { ok:true, token:'stub-token', total: 1234567, count: 1234567, stats: {}, page: { limit: 25, offset: 0, has_more: true } };
    for (const k of ARRAY_KEYS) o[k] = ROWS;
    // Форма берётся по адресу, а не одна на всех. Список операций читает
    // `i.action` как СТРОКУ, а чеклист активации — `s.action.kind` без всякой
    // проверки: одной плоской строкой обе формы не удовлетворить, и заглушка
    // неизбежно ломала одно из двух. Сломанное место уходило в отчёт ошибкой
    // JS, которой в приложении нет (бэкенд отдаёт обе формы правильно, см.
    // services/onboarding.py), а выдуманная находка в следующий раз
    // закрывается списком исключений — и дыра оказывается там, где её не было.
    if (url.indexOf('/onboarding') >= 0) {
      o.activated = false; o.next_step = 1; o.done = 3; o.total = ROWS.length;
      o.steps = ROWS.map((r, i) => ({ ...r, done: i < 3, optional: i % 4 === 0,
        hint: LONG, cta: 'Подключить аккаунты', action: { kind: 'import_accounts' } }));
    }
    return ok(o);
  };
}, !sweepMode ? 'empty'
   : process.env.MINIAPP_FAIL ? 'fail'
   : process.env.MINIAPP_EMPTY ? 'empty' : 'fat');

// MINIAPP_EMPTY — обход на ПУСТЫХ ответах. Толстые данные проверяют вёрстку, а
// пустые — другое свойство: сказал ли экран хоть слово, когда показывать
// нечего. Экран, нарисовавший один заголовок и ничего больше, читается как
// поломка приложения; в этом и состоит находка.
// Мерка немоты нужна обоим режимам без данных: и когда показывать нечего, и
// когда бэкенд отвечает ошибкой. Экран, промолчавший в ответ на сбой, —
// отдельная беда: человек видит пустоту и не знает, сломалось или просто пусто.
const emptyMode = !!(process.env.MINIAPP_EMPTY || process.env.MINIAPP_FAIL);

let live = [];
page.on('pageerror', e => live.push('PAGEERROR: ' + String(e.message).slice(0, 160)));
const root = path.resolve(process.cwd(), 'mini_app/index.html');
await page.goto('file://' + root, { waitUntil: 'load', timeout: 20000 });
await page.waitForTimeout(1200);

// Мерка обрезки — одним текстом и для отчёта, и для самопроверки.
const CLIPPED = () => {
  // Окно лежит ПОВЕРХ экрана и мерится вместо него: пока корнем был только
  // `.screen.show`, семьдесят одно модальное окно не мерилось вовсе — ни
  // одной находки оттуда прийти не могло.
  //
  // Корень — именно `.modal`, а не подложка `.mbg`. Подложка это flex-строка
  // во весь экран, и всё, что в неё положить, она ужимает по своей ширине:
  // самопроверка показала это прямо — заведомо широкий элемент, подставленный
  // в подложку, мерка не находила. Внутри `.modal` он находится.
  const _mb = document.querySelector('.mbg.show');
  const scr = _mb ? (_mb.querySelector('.modal') || _mb)
                  : document.querySelector('.screen.show');
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

// Сколько осмысленного текста показал экран, не считая его шапки и нижней
// навигации. Ноль означает ровно то, что видит владелец: пустоту без единого
// слова — ни «пусто», ни «ошибка», ни кнопки.
const MUTE = () => {
  const scr = document.querySelector('.screen.show');
  if (!scr) return { len: 0, id: null, none: true };
  let n = 0;
  for (const el of scr.children) {
    if (el.classList && (el.classList.contains('hdr') || el.classList.contains('nav'))) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    n += (el.innerText || el.textContent || '').trim().length;
  }
  return { len: n, id: scr.id };
};

// Следы машинного происхождения, вылезшие в текст для человека. Список —
// литералы, а не догадка о «латинице»: каждое попадание наверняка беда, а
// ложных находок такой список не даёт. Пропустить он может, и пусть: ложная
// находка здесь дороже пропущенной — её закроют списком исключений.
const GIBBERISH = () => {
  const scr = document.querySelector('.screen.show');
  if (!scr) return { hits: [] };
  const bad = ['undefined', 'NaN', '[object Object]', 'TypeError', 'ReferenceError',
               'Failed to fetch', 'Internal Server Error'];
  const t = (scr.innerText || '').replace(/\s+/g, ' ');
  const hits = [];
  for (const b of bad) {
    const i = t.indexOf(b);
    if (i >= 0) hits.push(b + ' « ' + t.slice(Math.max(0, i - 24), i + b.length + 16).trim() + ' »');
  }
  return { hits, id: scr.id };
};

async function selfCheck() {
  return page.evaluate((src) => {
    const clipped = new Function('return (' + src + ')()');
    const _mb = document.querySelector('.mbg.show');
    const scr = _mb ? (_mb.querySelector('.modal') || _mb)
                    : document.querySelector('.screen.show');
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
  // Строка списка — либо `имя`, либо `имя|аргумент`. Второй вид — ПРОБНЫЙ
  // вызов: функция требует id сущности, заглушка отдаёт одни и те же данные на
  // любой URL, поэтому экран детали рисуется при любом id. Падение такого
  // вызова — не находка, а непокрытый экран: он виден в строке охвата.
  const fns = fs.readFileSync(sweepList, 'utf8').split('\n').map(s => s.trim()).filter(Boolean)
    .map(line => { const [name, ...args] = line.split('|'); return { name, args, probe: args.length > 0 }; });
  const sc = await selfCheck();
  console.log(`самопроверка мерки: заведомо обрезанный элемент найден=${sc.caught}`
    + ` | прежняя мерка (scrollWidth) его видит=${sc.old}`);
  if (!sc.caught) {
    console.log('МЕРКА СЛЕПА — «обрезки нет» ниже ничего не означает');
    process.exitCode = 2;
  }
  if (emptyMode) {
    // Мерка немоты тоже обязана показать, что видит. Прячем у показанного
    // экрана всё, кроме шапки, и она должна насчитать ноль знаков.
    const ms = await page.evaluate((src) => {
      const mute = new Function('return (' + src + ')()');
      const scr = document.querySelector('.screen.show');
      if (!scr) return { ran: false };
      const hid = [];
      for (const el of scr.children) {
        if (el.classList && el.classList.contains('hdr')) continue;
        hid.push([el, el.style.display]); el.style.display = 'none';
      }
      const silent = mute().len === 0;
      hid.forEach(([el, d]) => { el.style.display = d; });
      return { ran: true, silent, back: mute().len };
    }, MUTE.toString());
    console.log(`самопроверка мерки немоты: заведомо немой экран найден=${ms.silent}`
      + ` | текст вернулся=${ms.back > 0}`);
    if (!ms.silent || !(ms.back > 0)) {
      console.log('МЕРКА НЕМОТЫ СЛЕПА — «немых экранов нет» ничего не означает');
      process.exitCode = 2;
    }
  }
  const report = [];
  const seen = new Set();          // какие экраны обход ДЕЙСТВИТЕЛЬНО показал
  const mute = [];                 // открывашки, не открывшие ничего
  const hints = [];                // машинный текст: подсказка, не находка
  let probesShown = 0, probesTotal = 0;
  for (const { name: fn, args, probe } of fns) {
    live = [];
    if (probe) probesTotal++;
    // Какой экран показан ДО вызова. Без этого «показалось 57 из 57» было
    // неправдой: упавшая открывашка оставляет на виду прежний экран, и в
    // зачёт шёл он. Числитель, который считает чужую работу своей, не лучше
    // отсутствующего знаменателя.
    const beforeId = await page.evaluate(() => {
      const el = document.querySelector('.screen.show');
      return el ? el.id : null;
    });
    const r = await page.evaluate(async ({ name, a }) => {
      if (typeof window[name] !== 'function') return { missing: true };
      try { await window[name](...a); }
      catch (e) { return { threw: String(e && e.message || e).slice(0, 160) }; }
      return {};
    }, { name: fn, a: args });
    await page.waitForTimeout(220);
    // MINIAPP_SHOT_DIR — снимок каждого экрана обхода. Отчёт говорит, ЧТО
    // обрезано, а посмотреть, как это выглядит, иначе нечем: экраны обхода не
    // вкладки, и одиночный режим их не открывает.
    if (process.env.MINIAPP_SHOT_DIR) {
      await page.screenshot({ path: path.join(process.env.MINIAPP_SHOT_DIR, `miniapp_${fn}.png`) });
    }
    const m = await page.evaluate((src) => new Function('return (' + src + ')()')(), CLIPPED.toString());
    const shownId = await page.evaluate(() => {
      const el = document.querySelector('.screen.show');
      return el ? el.id : null;
    });
    const opened = shownId && shownId !== beforeId;
    if (opened) { seen.add(shownId); if (probe) probesShown++; }
    else if (!probe) mute.push(fn);   // позвали без аргументов, экран не сменился
    const problems = [];
    // У пробного вызова падение значит «подставленный id не подошёл», а не
    // поломку: в находки оно не идёт, иначе отчёт утонет в шуме и перестанет
    // что-либо значить. Обрезка считается у обоих одинаково — она мерится по
    // тому, что ДЕЙСТВИТЕЛЬНО нарисовано.
    if (!probe) {
      if (r.missing) problems.push('НЕТ ФУНКЦИИ');
      if (r.threw) problems.push('THREW: ' + r.threw);
      if (live.length) problems.push(...live.slice(0, 2));
    }
    if (m.n) problems.push(`ОБРЕЗАНО ${m.n}: ` + m.worst.join(' ; '));
    const gb = await page.evaluate((src) => new Function('return (' + src + ')()')(), GIBBERISH.toString());
    // В находки это НЕ идёт — см. заголовок раздела при печати. Заглушка
    // отдаёт одну форму на 1019 маршрутов, и поле, которое на одном экране
    // число, на другом список; отличить тут беду приложения от беды заглушки
    // нельзя. Это подсказка «посмотри сюда», а не приговор.
    if (gb.hits && gb.hits.length) hints.push(fn + ' | ' + gb.hits.slice(0, 2).join(' | '));
    if (emptyMode) {
      const mu = await page.evaluate((src) => new Function('return (' + src + ')()')(), MUTE.toString());
      // Порог — НОЛЬ, и это намеренно. Пробовали сорок знаков: под них попали
      // `empty('✅','Дубликатов нет','Все контакты уникальны')` (39 знаков),
      // `empty('⚙️','Операция не найдена')` (22) и экран-форма, у которой текст
      // живёт в placeholder'ах (12). Все три работают правильно — то есть три
      // находки из пяти были ложными. Ложная находка в такой мерке дороже
      // пропущенной: в следующий раз её закроют списком исключений, и дыра
      // окажется там, где её не было. Ни одного знака — признак без толкований.
      if (mu.id && mu.len === 0) problems.push(`НЕМОЙ ЭКРАН ${mu.id}: ни одного знака`);
    }
    if (problems.length) report.push(fn + ' | ' + problems.join(' | '));
    await page.evaluate(() => { try { while (typeof STACK !== 'undefined' && STACK.length) back(); } catch(_) {} });
    await page.waitForTimeout(60);
  }
  // ── Вкладки ────────────────────────────────────────────────────────────
  // Пять вкладок открываются не функцией-открывашкой, а навигацией, поэтому в
  // список они не попадают и до этого не мерились вовсе.
  for (const t of ['home', 'accounts', 'bots', 'broadcasts', 'more']) {
    live = [];
    await page.evaluate((x) => {
      try { while (typeof STACK !== 'undefined' && STACK.length) back(); } catch (_) {}
      if (typeof navGo === 'function') navGo(x, null);
      else if (typeof goTab === 'function') goTab(x);
    }, t);
    await page.waitForTimeout(400);
    const shownId = await page.evaluate(() => {
      const el = document.querySelector('.screen.show');
      return el ? el.id : null;
    });
    if (shownId) seen.add(shownId);
    if (process.env.MINIAPP_SHOT_DIR) {
      await page.screenshot({ path: path.join(process.env.MINIAPP_SHOT_DIR, `tab_${t}.png`) });
    }
    const m = await page.evaluate((src) => new Function('return (' + src + ')()')(), CLIPPED.toString());
    const problems = [];
    if (live.length) problems.push(...live.slice(0, 2));
    if (m.n) problems.push(`ОБРЕЗАНО ${m.n}: ` + m.worst.join(' ; '));
    if (problems.length) report.push('вкладка ' + t + ' | ' + problems.join(' | '));
  }

  // ── Окна ───────────────────────────────────────────────────────────────
  // Окно открывается добавлением класса `show` его подложке — JS для этого не
  // нужен, и содержимое окон в разметке статично. Поэтому мерится каждое, а
  // не те, до которых удалось бы доковырять открывающей функцией.
  const modalIds = await page.evaluate(() =>
    [...document.querySelectorAll('.mbg')].map(e => e.id).filter(Boolean));
  // Самопроверка отдельно для окон: у них свой корень, и «в окнах обрезки
  // нет» без неё значило бы ровно столько же, сколько прежнее «h-overflow:
  // OK» — то есть ничего.
  if (modalIds.length) {
    await page.evaluate((i) => document.getElementById(i).classList.add('show'), modalIds[0]);
    await page.waitForTimeout(120);
    const scm = await selfCheck();
    await page.evaluate((i) => document.getElementById(i).classList.remove('show'), modalIds[0]);
    console.log(`самопроверка мерки в окне (${modalIds[0]}): `
      + `заведомо обрезанный элемент найден=${scm.caught}`);
    if (!scm.caught) {
      console.log('МЕРКА СЛЕПА В ОКНАХ — «обрезки нет» по окнам ничего не означает');
      process.exitCode = 2;
    }
  }
  for (const id of modalIds) {
    live = [];
    await page.evaluate((i) => document.getElementById(i).classList.add('show'), id);
    await page.waitForTimeout(120);
    if (process.env.MINIAPP_SHOT_DIR) {
      await page.screenshot({ path: path.join(process.env.MINIAPP_SHOT_DIR, `modal_${id}.png`) });
    }
    const m = await page.evaluate((src) => new Function('return (' + src + ')()')(), CLIPPED.toString());
    if (m.n) report.push('окно ' + id + ' | ' + `ОБРЕЗАНО ${m.n}: ` + m.worst.join(' ; '));
    await page.evaluate((i) => document.getElementById(i).classList.remove('show'), id);
  }

  // ── Отчёт ──────────────────────────────────────────────────────────────
  // Знаменатель называется вслух. «Находок нет» без него не значит ничего:
  // обход открывает только экраны с функцией БЕЗ аргументов, а экранов в
  // разметке больше. Пока это не печаталось, треть приложения молчала.
  const total = await page.evaluate(() =>
    [...document.querySelectorAll('.screen')].map(e => e.id).filter(Boolean));
  const missed = total.filter(i => !seen.has(i));
  console.log(`экранов проверено: ${seen.size} из ${total.length} в разметке `
    + `(ширина окна ${VW}); окон проверено: ${modalIds.length}`);
  console.log(`из них пробными вызовами с подставленным id: ${probesShown} `
    + `из ${probesTotal} попыток`);
  if (mute.length) {
    console.log(`открывашек без аргументов, НЕ открывших свой экран: ${mute.length}`
      + ` — ${mute.slice(0, 12).join(' ')}` + (mute.length > 12 ? ' …' : ''));
  }
  if (missed.length) {
    console.log(`не показано экранов: ${missed.length} — ${missed.slice(0, 12).join(' ')}`
      + (missed.length > 12 ? ' …' : ''));
  }
  console.log(`с находками: ${report.length}`);
  report.forEach(r => console.log('  ' + r));
  if (hints.length) {
    console.log(`\nмашинный текст на экране (ПОДСКАЗКА, не находка — заглушка`
      + ` отдаёт одну форму ответа на все маршруты и сама порождает часть`
      + ` этих мест; проверять каждое вручную): ${hints.length}`);
    hints.forEach(h => console.log('  ' + h));
  }
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
