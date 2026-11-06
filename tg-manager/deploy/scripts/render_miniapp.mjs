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

const { LAUNCH: _LAUNCH, chromiumOf } = await import('./_chrome.mjs');
const chromium = await chromiumOf();

const arg1 = process.argv[2] || 'home';
const sweepMode = arg1 === 'sweep';
const tab = sweepMode ? 'home' : arg1;
const outDir = sweepMode ? '.' : (process.argv[3] || '.');
const sweepList = sweepMode ? (process.argv[3] || '') : '';

// Путь к Chromium и к Playwright — из общего ./_chrome.mjs: пятая копия
// списка путей жила здесь.
const browser = await chromium.launch(_LAUNCH);
// Ширина окна — главный множитель обрезки, и 390 (iPhone 14) её почти не
// вызывает. Узкие телефоны в ходу: 320 — iPhone SE первого поколения и
// недорогие Android. MINIAPP_WIDTH позволяет прогнать обход по обеим.
const VW = Number(process.env.MINIAPP_WIDTH) || 390;
const ctx = await browser.newContext({ viewport: { width: VW, height: 844 }, deviceScaleFactor: sweepMode ? 1 : 2 });
const page = await ctx.newPage();
// MINIAPP_THEME=light|dark — какой темой «клиента Telegram» открыть приложение.
// Приложение красится по WebApp.colorScheme и themeParams, поэтому заглушка
// отдаёт стандартные цвета Telegram для выбранной схемы. Без переменной —
// пустые themeParams и тёмная схема, как раньше.
await page.addInitScript((t) => { window.__RENDER_THEME = t; }, process.env.MINIAPP_THEME || '');
// Приложение, не найдя SDK Телеграма, один раз подтягивает его с telegram.org и
// на `onload` делает `location.reload()` (mini_app/index.html, ~90). В обходе
// это срывало весь прогон на случайном экране: страница перезагружалась в
// середине `page.evaluate`, и Playwright падал с «Resulting promise was garbage
// collected» — ни отчёта, ни охвата, причём через раз. Метку попытки ставим
// заранее: приложение видит, что ретрай уже был, и скрипт не подключает.
// addInitScript выполняется до скриптов страницы на КАЖДОЙ навигации, поэтому
// метка есть и после любой перезагрузки.
await page.addInitScript(() => {
  try { sessionStorage.setItem('__sdk_cdn_try', '1'); } catch (_) {}
});
// Одной метки мало, и это выяснилось обходом: ветка «ретрай уже был» СНИМАЕТ
// метку (`sessionStorage.removeItem(RK)`), так что после первой загрузки она
// пропадает — и любая следующая навигация снова тянет скрипт с telegram.org,
// снова зовёт `location.reload()` и снова рвёт `page.evaluate` на случайном
// экране (в прогоне 06.11.2026 — `openShieldCheck`). Поэтому запрос к CDN
// отменяем совсем: `onload` не наступает, перезагрузки нет. Обход проверяет
// приложение, а не доступность telegram.org, и локальная копия SDK в
// репозитории есть (mini_app/telegram-web-app.js).
await page.route('**://telegram.org/**', (route) => route.abort());
await page.route('**://*.telegram.org/**', (route) => route.abort());
await page.addInitScript((mode) => {
  const fat = mode === 'fat';
  const TH = window.__RENDER_THEME;
  const TP = TH === 'light'
    ? { bg_color:'#ffffff', secondary_bg_color:'#efeff4', text_color:'#000000', hint_color:'#8e8e93', link_color:'#2481cc', button_color:'#2481cc', button_text_color:'#ffffff', accent_text_color:'#2481cc', destructive_text_color:'#e53935', header_bg_color:'#ffffff', section_bg_color:'#ffffff' }
    : TH === 'dark'
    ? { bg_color:'#17212b', secondary_bg_color:'#232e3c', text_color:'#f5f5f5', hint_color:'#708499', link_color:'#6ab3f3', button_color:'#5288c1', button_text_color:'#ffffff', accent_text_color:'#6ab2f2', destructive_text_color:'#ec3942', header_bg_color:'#17212b', section_bg_color:'#17212b' }
    : {};
  // Заглушку надо ДЕРЖАТЬ, а не просто поставить. Рядом с index.html лежит
  // настоящий telegram-web-app.js, он грузится и делает `window.Telegram = {...}`,
  // затем `window.Telegram.WebApp = ...` — то есть затирает заглушку, и
  // initData становится пустой. Приложение уходит в ветку связывания
  // устройства, поверх всего встаёт оверлей ввода кода, boot() до конца не
  // доходит: любой снимок — это фотография окна входа, а не приложения.
  // Поэтому и `Telegram`, и `Telegram.WebApp` отдаются через getter с
  // глотающим setter: присваивание SDK молча проходит и ничего не меняет
  // (бросать нельзя — упадёт сам SDK, а с ним и загрузка страницы).
  const WA = { ready(){}, expand(){}, initData:'stub-init-data', initDataUnsafe:{ user:{ id:1, first_name:'Test' } }, colorScheme: TH === 'light' ? 'light' : 'dark', themeParams: TP, MainButton:{show(){},hide(){},setText(){},onClick(){}}, BackButton:{show(){},hide(){},onClick(){}}, HapticFeedback:{impactOccurred(){},notificationOccurred(){}}, onEvent(){}, offEvent(){}, openTelegramLink(){}, showAlert(){}, showConfirm(){}, close(){}, setHeaderColor(){}, setBackgroundColor(){} };
  const TGSTUB = {};
  Object.defineProperty(TGSTUB, 'WebApp', { get: () => WA, set: () => {}, configurable: true });
  Object.defineProperty(window, 'Telegram', { get: () => TGSTUB, set: () => {}, configurable: true });
  const ok = (o) => new Response(JSON.stringify(o), { status: 200, headers: { 'Content-Type': 'application/json' } });
  const EMPTY = { ok:true, token:'stub-token', accounts:[], bots:[], channels:[], keywords:[], alerts:[], suggestions:[], bot_suggestions:[], rows:[], items:[], stats:{}, total:0, count:0, presets:[], recent:[], history:[], top_channels:[], recent_activity:[], by_stage:[] };
  if (mode === 'fail' || mode === 'fail-en') {
    // Третий режим: бэкенд отвечает пятисоткой на всё, кроме входа. Вход
    // оставлен рабочим намеренно — иначе поверх всего встанет окно входа, и
    // проверяться будет оно одно, а не поведение экранов при сбое.
    // Пятый режим (fail-en): тело ошибки ПО-АНГЛИЙСКИ — ровно то, что отдаёт
    // Telethon/asyncpg, если текст пройдёт мимо `_owner_ru` на сервере.
    // Владелец по-английски не читает, и единственное место, где видно, дошла
    // ли такая фраза до ЭКРАНА, — обход. Статическая проверка разметки этого
    // не покажет: текст приходит в ответе, а не лежит в вёрстке.
    const errBody = mode === 'fail-en'
      ? { error: 'Chat admin privileges are required to do that' }
      : { error: 'Внутренняя ошибка' };
    // Счётчик отданных пятисоток. Нужен, чтобы отличить экран, который СБОЙ
    // СКРЫЛ, от экрана, который к серверу вовсе не обращался (форма,
    // инструкция, локальный расчёт): требовать разметку ошибки от второго —
    // ложная находка, а ложная находка в такой мерке дороже пропущенной.
    window.__failServed = 0;
    const boom = () => {
      window.__failServed = (window.__failServed || 0) + 1;
      return new Response(JSON.stringify(errBody),
        { status: 500, headers: { 'Content-Type': 'application/json' } });
    };
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
    'conflicts','networks','instances','ecosystems','gifts','stars','strikes',
    'users','posts',
    // Добавлено 06.11.2026: эти экраны обход считал «спокойно пустыми» — то
    // есть не мерил вовсе, а пустота была его собственной. Все четыре ключа
    // на всех своих маршрутах — СПИСКИ записей (services/mini_app_accounts.py:
    // dialogs; mini_app_bots.py и mini_app_presence.py: profiles;
    // mini_app_api.py: campaigns; mini_app_networks.py: pairs).
    'dialogs','campaigns','profiles','pairs',
    // Та же причина, второй заход: расписания (mini_app_bots.py),
    // рассылки (mini_app_broadcast.py), жалобы (mini_app_admin.py и
    // mini_app_api.py), команды бота (mini_app_bots.py), разбивка
    // кампании по аккаунтам и верхние ошибки (mini_app_dm.py) — везде
    // списки записей.
    'schedules','broadcasts','reports','commands','by_account','top_errors'];
  window.fetch = async (u) => {
    const url = String(u || '');
    const o = { ok:true, token:'stub-token', total: 1234567, count: 1234567, stats: {}, page: { limit: 25, offset: 0, has_more: true } };
    for (const k of ARRAY_KEYS) o[k] = ROWS;
    // `warnings` у бэкенда — список СТРОК (services/mini_app_fleet.py), а не
    // записей, и экран печатает их через esc(). Общий список записей давал там
    // «[object Object]» — находку, которой в приложении нет.
    o.warnings = [LONG + '. Проверьте нагрузку на аккаунты.',
                  'Часть прокси не отвечает — операции будут падать.'];
    // Форма берётся по адресу, а не одна на всех. Список операций читает
    // `i.action` как СТРОКУ, а чеклист активации — `s.action.kind` без всякой
    // проверки: одной плоской строкой обе формы не удовлетворить, и заглушка
    // неизбежно ломала одно из двух. Сломанное место уходило в отчёт ошибкой
    // JS, которой в приложении нет (бэкенд отдаёт обе формы правильно, см.
    // services/onboarding.py), а выдуманная находка в следующий раз
    // закрывается списком исключений — и дыра оказывается там, где её не было.
    // Одно и то же имя поля на разных адресах значит разное. `proxies` на
    // `/transport` — ЧИСЛО прокси (services/mini_app_api.py: transport_get),
    // а на экране прокси — список записей; `accounts`, `channels`, `bots` на
    // `/topology` — счётчики (services/mini_app_parser.py:
    // topology_overview), а в списках — записи. Заглушка, раздающая одну
    // форму по имени поля, печатала на этих двух экранах
    // «[object Object],[object Object]» — подсказку о беде, которой в
    // приложении нет. Подсказка, которую нельзя закрыть, со временем
    // перестаёт читаться вся целиком, и настоящая уходит вместе с ней.
    if (url.indexOf('/transport') >= 0) {
      o.proxies = 12; o.accounts_with_proxy = 34;
    }
    if (url.indexOf('/topology') >= 0 && url.indexOf('/topology/') < 0) {
      o.accounts = 25; o.channels = 14; o.bots = 7;
      o.channel_links = 31; o.bot_users_total = 1234567;
    }
    // Экран рефералов рисует копирование кода, ссылку и «Поделиться» ТОЛЬКО
    // при `ref_code` (index.html: openReferral). Без него у экрана не
    // остаётся ни одного действия — и мерка действий объявила бы мёртвым
    // экран, который просто не накормлен. Отчёт, зовущий на работу, которой
    // нет, — та же ложь, что пропущенная беда.
    // Пост открывается по `link`, а подпись к нему берётся из `body` и
    // `channel_title` (index.html: openVaPosts). Общая запись этих полей не
    // знает, и экран оставался без единого действия — «не накормлен», а не
    // мёртв.
    if (url.indexOf('/va/posts') >= 0) {
      o.posts = ROWS.slice(0, 10).map((r, i) => ({
        ...r, body: LONG + ' ' + LONG, channel_title: LONG, pillar: 'польза',
        published_at: '2026-10-30T04:00:00Z',
        link: 'https://t.me/c/1234567890/' + (i + 1), is_private: false,
      }));
    }
    if (url.indexOf('/referral') >= 0) {
      o.ref_code = 'REF123456';
      o.ref_link = 'https://t.me/infragram_bot?start=REF123456';
      o.tier = 'bronze'; o.commission_rate = 20;
      o.referral_count = 3; o.paid_count = 1; o.total_earned = 12.5;
      o.referrals = ROWS.slice(0, 5).map((r, i) => ({ ...r, status: ['paid', 'active', 'pending'][i % 3] }));
    }
    // `/ai/forecast` — число, а не список: `expected_buyers` это сумма
    // вероятностей (services/ai_predict.py, forecast). Заглушка его не давала,
    // и экран прогноза печатал «Ожидаем покупателей за 30 дней: undefined» —
    // подсказку о беде, которой в приложении нет: сервер это поле отдаёт
    // всегда, у `forecast` один возврат. Проверено, что все 45 мест
    // `esc(String(...))` в мини-аппе либо защищены в разметке, либо поле
    // гарантировано сервером; оставить подсказку значило бы приучить себя её
    // не читать, а вместе с ней уйдёт и настоящая.
    // Шаблон виджета — не плоская запись: экран сразу читает `t.fields.map`
    // (screens/widgets.js: _wgRenderFields), без всякой проверки, потому что
    // сервер поля отдаёт всегда (services/bot_widgets.py: templates_public —
    // id, name, desc, fields, votes, packed). Общая запись `fields` не знала,
    // и экран падал с «Cannot read properties of undefined (reading 'map')» —
    // находкой, которой в приложении нет, а заодно печатал «undefined» вместо
    // названий шаблонов.
    if (url.indexOf('/widgets/templates') >= 0) {
      o.templates = [
        { id: 'poll', name: 'Опрос с голосами', desc: LONG, votes: true, packed: 2048,
          fields: [{ key: 'q', label: 'Вопрос', kind: 'text', max: 140, need: true },
                   { key: 'o', label: 'Варианты (каждый с новой строки, 2–6)',
                     kind: 'options', need: true }] },
        { id: 'countdown', name: 'Отсчёт до события', desc: LONG, votes: false, packed: 1536,
          fields: [{ key: 't', label: 'Заголовок', kind: 'text', max: 80, need: true,
                     hint: 'Например: До старта распродажи' },
                   { key: 'at', label: 'Дата и время события', kind: 'when', need: true }] },
      ];
      o.limits = { max_bytes: 10240, per_day: 50, left_today: 37 };
    }
    if (url.indexOf('/ai/forecast') >= 0) {
      o.expected_buyers = 7.5; o.buyers = 12; o.horizon_days = 30;
    }
    // ЭКРАНЫ ДЕТАЛЕЙ отдают сущность ОТДЕЛЬНЫМ ключом: `d.contact`, `d.plan`,
    // `d.eco`, `d.funnel`, `d.user`, `d.campaign`, `d.bot`, `d.crm`,
    // `d.endpoint` — и читают его БЕЗ проверки (`const c = d.contact;` и сразу
    // `c.name`). Плоская запись такого ключа не даёт, экран падал на TypeError
    // и показывал «Что-то пошло не так» — одиннадцать экранов обход не видел
    // вовсе, а в отчёте они лежали среди «почти мёртвых»: приговор по
    // экранам, о которых ничего не известно.
    //
    // Ключи раздаются ПО АДРЕСУ, а не всем сразу: `plan` на `/subscription` —
    // НАЗВАНИЕ тарифа строкой (index.html: updatePlan(d.plan, …)), и общая
    // запись нарисовала бы там «[object Object]» — выдуманную находку. Та же
    // причина, по которой `proxies` и `accounts` разведены по адресам выше.
    const ent = (extra) => ({ ...row(1), ...(extra || {}) });
    if (url.indexOf('/uch/contacts/') >= 0) o.contact = ent({ tags: ['клиент'] });
    // `virtual_admin` в плане — ОБЪЕКТ настроек (services/geo_va_link.py:
    // config_from_plan), и экран читает `va.enabled`, `va.topic`,
    // `va.publish_mode`. С `true` вместо объекта строка администратора и
    // кнопка повторного подключения не рисовались вовсе: обход числил экран
    // «не накормленным», хотя действия в коде есть.
    if (url.indexOf('/global_presence/') >= 0) {
      o.plan = ent({ status: 'pending', asset_type: 'channel',
        geo_selection: JSON.stringify({ countries: ['RU', 'KZ'],
          virtual_admin: { enabled: true, topic: 'городские новости',
                           publish_mode: 'auto', lead_contact: '@менеджер' } }) });
      o.targets_total = 25;
    }
    if (url.indexOf('/ecosystem/') >= 0) o.eco = ent({ health_score: 0.82 });
    if (url.indexOf('/auto_funnel/') >= 0) {
      o.funnel = ent({ is_active: true });
      o.steps = ROWS.slice(0, 6).map((r, i) => ({ ...r, step_num: i + 1, delay_hours: 3 * (i + 1) }));
      o.runs = { by_status: { running: 7, done: 12, failed: 1 },
                 waiting_on_step: { 1: 3, 2: 4 } };
    }
    if (url.indexOf('/admin/user/') >= 0) {
      // `plan_ru` — НАЗВАНИЕ тарифа по-русски (services/mini_app_admin.py):
      // без него образец текста показывал «💳 Подписка: pro», и читатель отчёта
      // думал, что экран всё ещё печатает код тарифа.
      o.user = ent({ current_plan: 'pro', plan_ru: 'Про', is_banned: false });
      // `bots` тут ЧИСЛО (index.html: «🤖 Ботов: ${d.bots}»), а не список —
      // та же разводка по адресам, что у `proxies` и `accounts`.
      o.bots = 7; o.accounts = 25; o.channels = 14;
    }
    if (url.indexOf('/narrative/') >= 0) o.campaign = ent({ kind: 'trend', campaign_type: 'trend' });
    if (url.indexOf('/miniapp/bot/') >= 0) {
      o.bot = ent({ health: { silent: true, reason: 'webhook_conflict', since: '2026-10-30T04:00:00Z' } });
    }
    if (url.indexOf('/crm_connections/') >= 0) {
      o.crm = ent({ provider: 'bitrix24', account: 'портал.bitrix24.ru',
                    url: 'https://портал.bitrix24.ru/rest/1/код/', synced_records: 1234 });
      o.stages = ROWS.slice(0, 4).map((r, i) => ({ ...r, kind: ['new', 'won', 'lost', 'work'][i] }));
    }
    if (url.indexOf('/deliveries') >= 0) {
      o.endpoint = ent({ kind: 'sheets', target: 'https://docs.google.com/spreadsheets/d/abc' });
      o.deliveries = ROWS.slice(0, 10).map((r, i) => ({
        ...r, status: ['delivered', 'pending', 'failed', 'skipped'][i % 4],
        attempts: 1 + (i % 3), status_code: 200 }));
    }
    // Пара дублей — это ДВЕ вложенные карточки
    // (services/contacts_hub/trust_engine.py), и экран читает
    // `dp.contact_a.name` без проверки.
    if (url.indexOf('/uch/duplicates') >= 0) {
      o.duplicates = ROWS.slice(0, 12).map((r, i) => ({
        contact_a: { id: r.id, name: LONG },
        contact_b: { id: r.id + 100, name: LONG + ' (второй)' },
        confidence: 0.95 - i * 0.05,
        reasons: ['same_phone', 'same_username'],
        reason_labels: ['один номер телефона', 'одинаковый ник'],
      }));
      o.skipped_groups = 3;
    }
    // Запись ИИ-памяти обязана иметь `body`: экран режет его
    // (`m.body.slice(0, 40)`) ещё до всякой проверки. Своего ключа у заглушки
    // не было, а `listOf` подставлял общий список записей — и экран падал.
    if (url.indexOf('/ai_memory') >= 0) {
      o.memories = ROWS.slice(0, 12).map((r, i) => ({
        ...r, body: LONG + ' ' + LONG, kind: ['note', 'fact', 'person'][i % 3],
        pinned: i === 0, tags: ['клиенты', 'продажи'],
      }));
    }
    // Тринадцать экранов обход видел, но без данных: действия в их коде есть, а
    // на экране ни одного — рисовать нечего. Приговор по ним невозможен, и
    // прежний вывод «ожидаемой пользы мало» был догадкой: ровно такую догадку
    // уже опровергли одиннадцать упавших экранов, среди которых нашёлся
    // s-aimemory, невидимый по числам в принципе.
    if (url.indexOf('/channel/') >= 0) {
      o.channel = ent({ members: 123456, linked_account_id: 1, invite_link: 'https://t.me/+кодприглашения' });
      o.linked_account = ent({});
      o.member_growth = ROWS.slice(0, 7).map((r, i) => ({ ...r, day: `2026-10-2${i}`, members: 1000 + i * 37 }));
      o.posts_published = 42;
      o.recent_ops = ROWS.slice(0, 5);
    }
    // Детали операции отдают саму операцию ВЕРХНИМ уровнем, и экран
    // проверяет `!o.id` — у общего ответа этого поля не было вовсе, экран
    // писал «не найдена». Формы `params_view` и `quick_actions` взяты у
    // services/op_params_view.py и services/op_quick_actions.py.
    if (url.indexOf('/operation/') >= 0) {
      Object.assign(o, ent({ op_type: 'invite_users', status: 'running',
        total_items: 1000, done_items: 250, duration_s: 3600,
        summary: LONG,
        accounts_impact: ROWS.slice(0, 4).map((r, i) => ({
          ...r, floods: i, max_wait: 60 * (i + 1) })),
        params_view: [{ label: 'Канал', value: LONG }, { label: 'Пауза', value: '45 с' }],
        quick_actions: [{ id: 'repeat', label: 'Повторить на остатке',
                          fn: 'opRepeat(1)', reason: 'часть целей не дошла' }] }));
    }
    if (url.indexOf('/audience') >= 0) {
      o.growth = ROWS.slice(0, 7).map((r, i) => ({ ...r, day: `2026-10-2${i}`, count: 10 + i }));
      o.new_today = 12; o.new_7d = 84; o.new_30d = 310; o.active = 1200;
      o.tags = ['клиенты', 'вебинар', 'оплатившие'];
    }
    if (url.indexOf('/engagement') >= 0) {
      o.hot = 142; o.warm = 310; o.cold = 540; o.lost = 1204; o.cohort_ready = true;
    }
    if (url.indexOf('/threads') >= 0) {
      o.threads = ROWS.slice(0, 8).map((r, i) => ({
        ...r, entity_type: ['channel', 'bot', 'account'][i % 3], entity_id: r.id,
        tg_thread_id: 100 + i, topic_name: LONG, status: i % 2 ? 'open' : 'closed' }));
    }
    if (url.indexOf('/ghost/') >= 0) {
      // Имена полей взяты у экрана (index.html: openGhostDetail), иначе он
      // печатает «Окно активности undefined:00–undefined:00» — подсказку о
      // беде, которой в приложении нет.
      o.profile = ent({ is_running: true, mode: 'reader', active_hours_start: 9,
                        active_hours_end: 21, cooldown_minutes: 45 });
      o.log = ROWS.slice(0, 10).map((r, i) => ({ ...r, action: 'прочитал диалог', at: '2026-10-30T04:0' + i + ':00Z' }));
      o.log_total = 120; o.today_actions = 7;
    }
    if (url.indexOf('/webhook') >= 0) {
      o.url = 'https://пример.рф/telegram/вебхук';
      o.allowed_updates = ['message', 'callback_query', 'my_chat_member'];
      o.pending_update_count = 3; o.max_connections = 40;
      o.last_error_message = 'Connection timed out';
    }
    if (url.indexOf('/warmup/') >= 0) {
      o.plan = ent({ phase: 'warming', day: 4 });
      o.week = { '2026-10-26': 5, '2026-10-27': 7 };
      o.circle = { joined: 3, read: 12, reacted: 4 };
      o.actions = ROWS.slice(0, 10).map((r, i) => ({ ...r, kind: 'join_channel', at: '2026-10-30T04:0' + i + ':00Z' }));
    }
    if (url.indexOf('/content_mesh/') >= 0) {
      o.mesh = ent({ delay_minutes: 15, source_channel: LONG, is_active: true,
                     append_text: LONG });
      o.targets = ROWS.slice(0, 6).map((r) => ({ ...r, source_channel: LONG, delay_minutes: 15 }));
      // Срез очереди репостов: экран печатает «Пост №${r.source_msg_id}».
      o.recent = ROWS.slice(0, 8).map((r, i) => ({
        ...r, source_msg_id: 1000 + i, error_msg: i === 2 ? LONG : null,
        status: ['pending', 'sent', 'error'][i % 3],
        scheduled_at: '2026-10-30T04:00:00Z', sent_at: '2026-10-30T04:05:00Z' }));
      o.recent_total = 400;
      o.queue = { pending: 12, sent: 340, error: 48 };
    }
    if (url.indexOf('/admin/actions') >= 0) {
      o.actions = ROWS.slice(0, 20).map((r, i) => ({
        ...r, action: 'ban_user', action_ru: 'Заблокировал пользователя',
        admin_id: 1000 + i, admin_username: 'админ_' + i,
        target_user_id: 2000 + i, target_username: 'клиент_' + i }));
    }
    // Шаг трассы читается по именам from_name/to_name и по id ботов владельца
    // (services/mini_app_parser.py: mesh_task_detail), а не по `bot_name`:
    // с чужими ключами экран рисовал «undefined → undefined» и ни одной
    // двери, и обход числил его «не накормленным».
    if (url.indexOf('/mesh/task/') >= 0) {
      Object.assign(o, ent({ status: 'running', depth: 2, origin_name: LONG,
        origin_id: 7012345678, drop_reason: null,
        hops: ROWS.slice(0, 5).map((r, i) => ({
          step: i, from_name: LONG, to_name: '@исполнитель_' + i,
          from_id: 7012345678, to_id: 7012345679,
          capability: 'написать пост', outcome: ['accepted', 'handled', 'dropped', 'failed'][i % 4],
          reason: i % 4 === 2 ? 'no_capability' : null,
          created_at: '2026-10-30T04:00:00Z' })) }));
    }
    if (url.indexOf('/invite/account/') >= 0) {
      Object.assign(o, ent({ label: LONG, age_days: 120, recommended_limit: 35,
        recommended_pause_s: 45, remaining_today: 12, limit_basis: 'возраст и прогрев',
        limit_factors: { notes: ['возраст 120 дней', 'прогрев пройден',
                                 'флудов за неделю нет'] },
        week: { ok: 120, floods: 0 } }));
    }
    // Сводка оформления и охват администратора — это ИМЕНА ПОЛЕЙ запроса
    // (services/mini_app_presence.py): `applicable` включает блок пакетных
    // операций, а `eligible > installed` — кнопку повторного подключения.
    // Ключи `total`/`covered` не читает никто.
    if (url.indexOf('/global_presence/') >= 0) {
      o.dressing = { done: 3, applicable: 3, with_avatar: 2, with_username: 3 };
      o.virtual_admin_coverage = { eligible: 6, installed: 2, enabled: 4 };
    }
    // Ответ выбирает ВЕТКУ экрана. У облака первая же строка — калитка
    // `if (!st.access)`: без доступа экран рисует платный барьер (текст и
    // «Посмотреть подписку»), и обход мерил ровно барьер, а сам экран —
    // квота, загрузка файла, список — не мерился ни разу. Числа «1 действие,
    // 246 знаков» были правдой про барьер и ничего не говорили про экран.
    // Поля — у services/mini_app_cloud.py: cloud_status.
    if (url.indexOf('/cloud/status') >= 0) {
      o.access = true; o.admin = false; o.used_bytes = 1234567890;
      o.limit_bytes = 5368709120; o.max_upload_bytes = 2147483648;
    }
    // Сводка операций платформы отдаёт `stats` СПИСКОМ по типам
    // (services/mini_app_admin.py: admin_ops_stats), а общий ответ держит в
    // этом ключе объект — экран падал на `rows.map`.
    if (url.indexOf('/admin/ops/stats') >= 0) {
      o.stats = ['invite_users', 'mass_dm', 'warmup_accounts', 'parse_audience'].map((t, i) => ({
        op_type: t, type_ru: ['Инвайт участников', 'Массовая рассылка в личные',
                              'Прогрев аккаунтов', 'Парсинг аудитории'][i],
        total: 1234 - i * 100, done: 1000 - i * 100, failed: i * 60,
        fail_pct: i * 12.5, avg_duration_s: 60 * (i + 1) + 0.5,
      }));
    }
    if (url.indexOf('/onboarding') >= 0) {
      o.activated = false; o.next_step = 1; o.done = 3; o.total = ROWS.length;
      o.steps = ROWS.map((r, i) => ({ ...r, done: i < 3, optional: i % 4 === 0,
        hint: LONG, cta: 'Подключить аккаунты', action: { kind: 'import_accounts' } }));
    }
    // Последние пять экранов из «показывать нечего»: у них ключи по-своему
    // устроены, и общая запись их не даёт. Формы — у ручек, которые отдают.
    if (url.indexOf('/miniapp/broadcasts') >= 0) {
      // services/mini_app_broadcast.py: counts — по статусам, totals — за всю
      // историю; экран считал доставляемость именно по ним.
      // Статусы — те, что ставит database/db.py: update_broadcast
      // (pending/sending/done/failed/partial/cancelled). Экран считает
      // запланированными `pending` и всё со scheduled_at, выполненными —
      // `done`: выдуманный 'scheduled' не попадал ни в одну из групп, и
      // экран оставался пустым по вине заглушки.
      o.counts = { pending: 3, sending: 1, done: 28, failed: 2 };
      o.totals = { sent: 12400, failed: 180 };
      o.broadcasts = ROWS.slice(0, 12).map((r, i) => ({
        ...r, bot_id: 7012345678, bot_username: 'бот_' + i,
        status: ['pending', 'sending', 'done', 'failed'][i % 4],
        text: LONG, sent_count: 400 - i * 10, failed_count: i,
        total_users: 420, scheduled_at: '2026-11-07T09:00:00Z',
        created_at: '2026-10-30T04:00:00Z' }));
    }
    if (url.indexOf('/dm_campaign/') >= 0) {
      // services/mini_app_dm.py: отчёт кампании.
      Object.assign(o, { campaign: ent({ status: 'running', sent_count: 1200,
          fail_count: 80, total_targets: 1500, target_type: 'contacts' }),
        by_status: [{ status: 'sent', n: 1200 }, { status: 'failed', n: 80 },
                    { status: 'skipped', n: 20 }],
        by_account: ROWS.slice(0, 6).map((r, i) => ({ account_id: 1000 + i,
          label: 'Аккаунт ' + (1000 + i), sent: 300 - i * 20, failed: i * 3 })),
        top_errors: [{ reason: 'PEER_FLOOD', n: 42 },
                     { reason: 'USER_IS_BLOCKED', n: 18 },
                     { reason: 'без описания', n: 5 }] });
    }
    if (url.indexOf('/audience_dna/') >= 0) {
      // services/mini_app_api.py: _dna_to_dict + recommendations.
      Object.assign(o, { computed: true, bot_id: 7012345678,
        peak_hours: [9, 13, 20], peak_days: [1, 3, 5],
        best_content_types: ['видео', 'опрос'], avg_engagement_rate: 7.4,
        churn_risk_pct: 12.5, top_topics: ['переезд', 'упаковка', 'склад'],
        total_users_analyzed: 4210, computed_at: '2026-10-30T04:00:00Z',
        recommendations: [LONG, 'Публикуйте опросы по средам в 13:00.'] });
    }
    if (url.indexOf('/uch/graph/stats') >= 0) {
      // services/contacts_hub/relationship_engine.py: get_graph_stats.
      Object.assign(o, { total_relationships: 318,
        by_type: [{ relationship_type: 'колледж', n: 120 },
                  { relationship_type: 'работа', n: 98 },
                  { relationship_type: 'семья', n: 100 }],
        strongest: ROWS.slice(0, 8).map((r, i) => ({
          contact_a_id: 'c0ffee00-0000-0000-0000-00000000000' + i,
          contact_b_id: 'c0ffee00-0000-0000-0000-00000000010',
          a_name: LONG, b_name: LONG + ' (второй)',
          relationship_type: 'работа', strength: 0.95 - i * 0.05 })) });
    }
    if (url.indexOf('/ab/results') >= 0) {
      // services/mini_app_api.py: ab_results — победитель и лидер это ПОДПИСИ
      // вариантов, а не индексы.
      Object.assign(o, { batch: 7, winner: 'Вариант Б', leader: 'Вариант Б',
        confident: true,
        variants: [{ label: 'Вариант А', sent: 500, delivered: 40, rate: 8.0, ops: [11] },
                   { label: 'Вариант Б', sent: 500, delivered: 65, rate: 13.0, ops: [12, 13] }] });
    }
    // Четыре подсказки «undefined» и «[object Object]» из обхода 06.11.2026 —
    // все четыре мои: общая запись по имени ключа не знает полей, которые
    // читает экран, а `dialogs` на аналитике хранилища — вообще ЧИСЛО.
    if (url.indexOf('/vault/analytics') >= 0) {
      // services/mini_app_api.py: vault_analytics — диалогов тут СТОЛЬКО, а
      // не список. Общая запись печатала «[object Object],[object Object]».
      o.dialogs = 128; o.reply_rate_pct = 76; o.median_response_min = 42;
      o.busiest_hour = 13; o.unanswered_dialogs = 3;
      o.top_peers = ROWS.slice(0, 3).map((r, i) => ({ chat_id: -100123456789 - i,
        name: LONG, count: 120 - i * 10 }));
    }
    if (url.indexOf('/miniapp/ghost') >= 0) {
      // Строка призрака печатает лимит и окно активности числами, без
      // подстановки: «undefined действий в день · undefined:00».
      o.profiles = ROWS.slice(0, 8).map((r, i) => ({ ...r, daily_cap: 10 + i,
        active_hours_start: 9, active_hours_end: 22, cooldown_minutes: 45,
        personality: 'friendly', enabled: i % 2 === 0, is_active: true }));
      o.today_actions = 4;
    }
    if (url.indexOf('/multigeo') >= 0) {
      // services/mini_app_bots.py: multigeo_get — профиль на ЯЗЫК.
      o.profiles = ['', 'ru', 'en', 'de'].map((lang) => ({
        lang, name: LONG, description: LONG + ' ' + LONG, short_description: LONG }));
    }
    if (url.indexOf('/chatwarmup/session/') >= 0) {
      // Разбивка по аккаунтам тут про ходы: «написал N · пропусков M».
      o.by_account = ROWS.slice(0, 5).map((r, i) => ({ account_id: 1000 + i,
        label: LONG, sent: 12 - i, missed: i,
        last_at: '2026-10-30T04:00:00Z' }));
    }
    // ОТВЕТ-МАССИВ. Пять ручек отдают не объект, а голый список записей
    // (`_json_resp([dict(r) for r in rows])`), и заглушка, всегда отвечавшая
    // объектом, оставляла эти экраны пустыми — пустоту создавала она сама, а
    // в отчёте они лежали в «показывать нечего — действий мало законно».
    // Поля названы по тем, которые читает экран; ручка отдаёт колонки запроса
    // ровно под этими именами.
    const ARR = [
      ['/miniapp/swarm', (r, i) => ({ ...r, bot_id: 7012345678 + i, bot_role: 'хаб',
        score: 90 - i * 3, ctr: 12.5 - i, conversion_rate: 4.2, retention_d1: 55,
        retention_d7: 21, swarm_enabled: i % 2 === 0, cluster: 'кластер ' + (i % 3 + 1) })],
      ['/miniapp/ecosystems', (r, i) => ({ ...r, ecosystem_type: 'сеть каналов',
        status: 'active', health_score: 0.82, risk_level: ['low', 'mid', 'high'][i % 3],
        region: 'RU', member_count: 12 - i })],
      ['/miniapp/physics', (r, i) => ({ ...r, account_id: 1000 + i, risk_score: 90 - i * 4,
        ban_probability: 0.42, ops_24h: 18, flood_rate_1h: 0.3,
        last_flood_at: '2026-10-30T04:00:00Z', computed_at: '2026-10-30T04:00:00Z' })],
      ['/miniapp/graph/overlaps', (r, i) => ({ ...r, title_a: LONG, title_b: LONG + ' (2)',
        username_a: 'канал_' + i, username_b: 'канал_' + (i + 1),
        overlap_pct: 45 - i * 2, shared_users: 1200 - i * 50,
        computed_at: '2026-10-30T04:00:00Z' })],
      ['/miniapp/content_meshes', (r, i) => ({ ...r, enabled: i % 2 === 0,
        source_channel: 'источник_' + i, delay_minutes: 30 * (i + 1),
        targets_count: 4, pending_posts: i })],
    ];
    for (const [route, make] of ARR) {
      if (url.indexOf(route) >= 0) return ok(ROWS.slice(0, 12).map(make));
    }
    return ok(o);
  };
}, !sweepMode ? 'empty'
   : process.env.MINIAPP_FAIL_EN ? 'fail-en'
   : process.env.MINIAPP_FAIL ? 'fail'
   : process.env.MINIAPP_EMPTY ? 'empty' : 'fat');

// MINIAPP_EMPTY — обход на ПУСТЫХ ответах. Толстые данные проверяют вёрстку, а
// пустые — другое свойство: сказал ли экран хоть слово, когда показывать
// нечего. Экран, нарисовавший один заголовок и ничего больше, читается как
// поломка приложения; в этом и состоит находка.
// Мерка немоты нужна обоим режимам без данных: и когда показывать нечего, и
// когда бэкенд отвечает ошибкой. Экран, промолчавший в ответ на сбой, —
// отдельная беда: человек видит пустоту и не знает, сломалось или просто пусто.
const emptyMode = !!(process.env.MINIAPP_EMPTY || process.env.MINIAPP_FAIL
                     || process.env.MINIAPP_FAIL_EN);
// Режим СБОЯ — не то же, что режим пустоты. На пустых данных спокойное
// «Нет диалогов» правильно; на пятисотке оно врёт: человек видит пустоту и
// идёт искать несуществующую причину вместо того, чтобы повторить. Признак
// ошибки для этого уже есть (ACTIONS.erred), но до сих пор ни один приговор
// его в этом режиме не читал — то есть обход гонялся, а вопрос, ради которого
// режим заведён, не задавался.
const failMode = !!process.env.MINIAPP_FAIL;
// Фраза, которой заглушка отвечает на всё в режиме сбоя. `errRu` отдаёт
// сообщение сервера как есть, если в нём есть кириллица, — значит экран,
// честно сказавший о сбое своими словами (а не разметкой `errHtml`), эту
// фразу покажет. Поэтому приговор читает ДВА признака, и хватает любого:
// разметку ошибки или фразу сервера на экране.
//
// Проверено на себе: первая версия признака знала только разметку, и первой
// же находкой стал `openBotNote` — экран, который о сбое говорит, просто
// своим `#noteErr` и со своей ссылкой «↺ Повторить». Мерка, знающая одну
// форму записи, объявляет бедой чужую форму — ровно то, за чем здесь следят
// самопроверки.
//
// В режиме fail-en фраза приходит по-английски, `errRu` подставляет
// СОБСТВЕННЫЙ текст экрана (у каждого свой), и признак по фразе там не
// работает — поэтому приговор только для MINIAPP_FAIL. Английский в том
// режиме ловит своя мерка.
const FAIL_PHRASE = 'Внутренняя ошибка';
// Текст всего экрана целиком — для поиска фразы. `ACTIONS.sample` обрезан до
// 220 знаков, и фраза, сказанная ниже, в него не попадает.
const SCREEN_TEXT = () => {
  const scr = document.querySelector('.screen.show');
  if (!scr) return { text: '', id: null };
  let t = '';
  for (const el of scr.children) {
    if (el.classList && (el.classList.contains('hdr') || el.classList.contains('nav'))) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    t += ' ' + (el.innerText || el.textContent || '');
  }
  return { text: t.replace(/\s+/g, ' ').trim(), id: scr.id };
};
// Потолок ожидания одной открывашки. Открывашки честно ждут сеть, поэтому
// потолок щедрый: он ловит «не вернётся никогда», а не «отвечает медленно».
const OPENER_MS = Number(process.env.MINIAPP_OPENER_MS) || 15000;
const failEnMode = !!process.env.MINIAPP_FAIL_EN;
let enToastSeen = 0;

let live = [];
page.on('pageerror', e => live.push('PAGEERROR: ' + String(e.message).slice(0, 160)));
// Свидетель навигаций. Сорванный вызов («Resulting promise was garbage
// collected») я дважды объяснял перезагрузкой страницы — и дважды лечил не то:
// ни метка в sessionStorage, ни отмена запросов к telegram.org срыв не убрали.
// Навигация это или висящая открывашка — видно только отсюда: каждая смена
// адреса главного кадра ПОСЛЕ первой загрузки записывается и попадает в отчёт.
// Пустой список при сорванном вызове означает, что страница никуда не уходила,
// и искать надо в самой открывашке.
const navs = [];
let navWatch = false;
page.on('framenavigated', (fr) => {
  if (!navWatch || fr !== page.mainFrame()) return;
  navs.push(String(fr.url()).slice(-80));
});
const root = path.resolve(process.cwd(), 'mini_app/index.html');
await page.goto('file://' + root, { waitUntil: 'load', timeout: 20000 });
await page.waitForTimeout(1200);
navWatch = true;   // первая загрузка — не навигация обхода, её не считаем

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

// Текст, который закрыт собой же приложением и до которого нельзя доскроллить.
//
// Обрезку мерили только по горизонтали. По вертикали у приложения есть полосы,
// прибитые к низу окна (`#tabbar`, `.sel-bar` над ним), и список под ними
// прокручивается ДО СВОЕГО конца, а не до низа окна: если контейнеру не дали
// запаса снизу, последние строки остаются под полосой навсегда. Прокрутка их
// не достанет — она уже в упоре.
//
// Признак берём не из геометрии полос, а из попадания точки: что лежит в
// середине элемента по `elementFromPoint`. Это ровно тот ответ, который
// получает палец владельца, и он сам разбирается и с z-index, и с
// `pointer-events:none` (тост закрывает вид, но не перехватывает касание, и
// находкой быть не должен).
//
// Мерить имеет смысл только в упоре: до прокрутки под полосой лежит половина
// списка, и это нормально. Поэтому контейнер сначала доводят до конца.
const COVERED = () => {
  const scr = document.querySelector('.screen.show');
  if (!scr) return { n: 0, worst: [], ran: false };
  const W = window.innerWidth, H = window.innerHeight;
  // Накрывающая полоса — только прибитая к НИЗУ. Первая редакция мерки этого
  // не требовала и выдала находки на 106 экранах: при прокрутке в упор строки
  // списка уезжают под липкую шапку, и это не беда, а то, ради чего шапку
  // делали липкой, — прокрутил вверх, и строка на месте. Беда ровно одна: то,
  // что не достаётся прокруткой НИ В КАКОМ положении, то есть закрытое снизу
  // в упоре. Поэтому накрывающий элемент обязан быть fixed или sticky и
  // лежать нижней половиной окна.
  //
  // Чего мерка не мерит и не обещает: наложения без прикрепления (z-index
  // внутри самого экрана) — они прокруткой меняются, и разбор им нужен свой.
  const pinnedBottom = (node) => {
    for (let a = node; a && a !== document.body; a = a.parentElement) {
      const st = getComputedStyle(a);
      if (st.position === 'fixed' || st.position === 'sticky') {
        return a.getBoundingClientRect().bottom > H / 2;
      }
    }
    return false;
  };
  const out = [];
  for (const el of scr.querySelectorAll('*')) {
    if (el.children.length) continue;                 // только листья
    const txt = (el.textContent || '').trim();
    if (!txt) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 6) continue;
    // Вне окна — не находка: это просто непрокрученная часть списка.
    if (r.bottom <= 0 || r.top >= H || r.right <= 0 || r.left >= W) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    const x = Math.min(Math.max(r.left + r.width / 2, 1), W - 2);
    const y = Math.min(Math.max(r.top + r.height / 2, 1), H - 2);
    const top = document.elementFromPoint(x, y);
    if (!top || top === el || el.contains(top) || top.contains(el)) continue;
    if (!pinnedBottom(top)) continue;
    // Тост накрывает по замыслу и уходит сам через несколько секунд — это не
    // «не достаётся прокруткой». В режиме английской ошибки он висит почти
    // постоянно (каждый запрос — пятисотка), и без этого исключения находка
    // приходит на каждом экране с тостом, то есть сообщает о режиме обхода, а
    // не о вёрстке. Текст самого тоста читает мерка английского.
    if (top.closest && top.closest('#toast')) continue;
    out.push(el.tagName.toLowerCase() + '.' + String(el.className || '').split(' ')[0]
             + ' под ' + top.tagName.toLowerCase() + '.'
             + String(top.className || '').split(' ')[0]
             + ' «' + txt.slice(0, 28) + '»');
  }
  return { n: out.length, worst: out.slice(0, 3), ran: true };
};

// Довести прокручиваемые ящики экрана до упора: мерка закрытого текста имеет
// смысл только там.
//
// Ящик ищем по факту (`scrollHeight > clientHeight`), а не по классу. Первая
// редакция брала `.sb` внутри экрана — на вкладках прокручивается не он, и
// присваивание `scrollTop` чужому элементу молча ничего не делает. Мерка тогда
// снималась НЕ в упоре, и обычные строки в начале списка, честно уходящие под
// нижнюю панель, выглядели находкой: два экрана «с находками» были ложью, и
// снимок состояния показал это сразу — список стоял в самом верху.
//
// Поэтому ящик не только крутится, но и отчитывается, дошёл ли до конца.
const SCROLL_END = () => {
  const scr = document.querySelector('.screen.show');
  if (!scr) return { ran: false, atEnd: false, boxes: 0 };
  // Ящик — тот, который МОЖНО прокрутить: переполнение само по себе ещё не
  // прокрутка. На экране массовой публикации `div.field` переполнен на семь
  // пикселей при `overflow:visible`, прокрутить его нельзя, и отчёт честно, но
  // бесполезно сообщал «прокрутка не дошла до конца» на ровном месте.
  const scrolls = (el) => {
    const oy = getComputedStyle(el).overflowY;
    return oy === 'auto' || oy === 'scroll' || oy === 'overlay';
  };
  const cands = [scr, document.scrollingElement, ...scr.querySelectorAll('*')]
    .filter(el => el && el.scrollHeight - el.clientHeight > 4
                  && (el === document.scrollingElement || scrolls(el)));
  for (const el of cands) el.scrollTop = el.scrollHeight;
  const stuck = cands.filter(
    el => el.scrollTop + el.clientHeight < el.scrollHeight - 4);
  // Какой именно ящик не доехал — часть ответа, а не подробность: без имени
  // строка «прокрутка не дошла» не говорит, чинить мерку или приложение.
  return {
    ran: true, atEnd: stuck.length === 0, boxes: cands.length,
    stuck: stuck.slice(0, 2).map(el => el.tagName.toLowerCase() + '.'
      + String(el.className || '').split(' ')[0]
      + ' ' + Math.round(el.scrollTop) + '/' + Math.round(el.scrollHeight - el.clientHeight)),
  };
};

// Сколько осмысленного текста показал экран, не считая его шапки и нижней
// навигации. Ноль означает ровно то, что видит владелец: пустоту без единого
// слова — ни «пусто», ни «ошибка», ни кнопки.
// Сколько на экране ДЕЙСТВИЙ: кнопок, полей, ссылок, строк с onclick.
//
// Жалоба владельца (28.09.2026): «слишком много экранов, которые только
// отображают какие-то числа или количество или вообще ничего толком не
// отображает, и в них всё не кликабельно, ничего не настраивается, не
// выбирается». Мерка отвечает на неё числом.
//
// Считаем САМЫЙ ВНЕШНИЙ кликабельный элемент цепочки: вложенный span внутри
// кнопки — не отдельное действие. Возврат «назад» и бургер в счёт не идут —
// они есть у каждого экрана; остальная шапка идёт, там живут «+ Создать» и
// «⟳».
const ACTIONS = () => {
  const scr = document.querySelector('.screen.show');
  if (!scr) return { acts: 0, chars: 0, id: null, none: true };
  const isAct = (n) => {
    if (n.closest('.hdr')) {
      const cls = String(n.className || '');
      if (/(^|\s)(back|hdr-burger)(\s|$)/.test(cls)) return false;
      if (n.closest('.back, .hdr-burger')) return false;
      if (/^(‹|←|◀️?)$/.test((n.textContent || '').trim())) return false;
    }
    const t = n.tagName;
    if (t === 'BUTTON' || t === 'INPUT' || t === 'SELECT' || t === 'TEXTAREA') return true;
    if (t === 'A' && n.getAttribute('href')) return true;
    if (n.hasAttribute('onclick')) return true;
    return /(^|\s)(btn|chip|seg-opt|tap|toggle|switch)(\s|$)/.test(String(n.className || ''));
  };
  let acts = 0;
  for (const n of scr.querySelectorAll('*')) {
    const cs = getComputedStyle(n);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    if (!isAct(n)) continue;
    let outer = true;
    for (let p = n.parentElement; p && p !== scr; p = p.parentElement) if (isAct(p)) { outer = false; break; }
    if (outer) acts++;
  }
  let chars = 0;
  let sample = '';
  for (const el of scr.children) {
    if (el.classList && (el.classList.contains('hdr') || el.classList.contains('nav'))) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const t = (el.innerText || el.textContent || '').trim();
    chars += t.length;
    if (sample.length < 220) sample += (sample ? ' ' : '') + t;
  }
  // Экран в состоянии ОШИБКИ обход не видел: что на нём, неизвестно, и ни
  // «мёртвый», ни «тонкий» про него сказать нельзя. Признак берём у разметки,
  // а не у текста: `errHtml()` рисует `.err-msg`, а `empty('⚠️', …)` ставит
  // этот значок в `.empty-ico` — текст сообщения при этом любой, в том числе
  // пришедший с сервера.
  const erred = !!scr.querySelector('.err-msg')
    || [...scr.querySelectorAll('.empty-ico')]
         .some((e) => (e.textContent || '').indexOf('⚠️') >= 0);
  // СПОКОЙНОЕ пустое состояние — тоже не приговор экрану: `empty()` без значка
  // ошибки значит «показывать нечего», и действий на таком экране мало
  // законно. Из тридцати четырёх «почти мёртвых» такими оказались почти все:
  // заглушка не дала списка, экран честно сказал «Нет диалогов», а отчёт
  // позвал разбираться с его вёрсткой.
  // Начало текста экрана. Без него «1 действие, 54 знака» одинаково выглядит у
  // экрана, которому правда нечего показать, и у экрана, который заглушка
  // оставила пустым: приговор по одним числам вынести нельзя.
  const blank = !erred && !!scr.querySelector('.empty');
  // Строк на экране. «Пустое состояние» у экрана с НЕСКОЛЬКИМИ списками
  // ничего не значило: расписание рассылок рисует три списка, пустым был
  // только один (аналитика), и весь экран уходил в «показывать нечего» —
  // то есть не мерился, хотя расписание и A/B-блок на нём были. Пустым
  // экран считается, когда ни одной строки на нём нет.
  const rows = scr.querySelectorAll('.li, .row, tr, .s2col, .kpi-card').length;
  return { acts, chars, id: scr.id, erred, blank, rows,
           sample: sample.replace(/\s+/g, ' ').slice(0, 220) };
};

// Призыв к действию из `empty()` считается действием экрана.
const cta_ = (r) => !!(r && r.cta);

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
// Английская ФРАЗА на экране. Признак — служебные слова, а не перечень
// «плохих»: ни одно новое имя канала, поля или хоста не заставит сюда
// дописывать исключения. Одного слова мало — им бывает название канала или
// имя поля; фраза это два служебных слова либо одно плюс латиница вокруг.
// Тот же признак, что в tests/test_the_visible_text_is_not_in_english.py, но
// там он читает РАЗМЕТКУ, а здесь — то, что реально нарисовано в ответ на
// ошибку сервера. Разметку статическая проверка уже держит; этот путь — нет.
const ENGLISH = () => {
  // Корень — окно, если оно открыто, и экран иначе: ровно как у мерки
  // обрезки, которая этому уже научена. Внутри `.modal`, а не в подложке
  // `.mbg`: подложка — flex-строка во весь экран.
  const _mb = document.querySelector('.mbg.show');
  const scr = _mb ? (_mb.querySelector('.modal') || _mb)
                  : document.querySelector('.screen.show');
  if (!scr) return { hits: [] };
  // Тост — ГЛАВНОЕ место, где владелец видит текст ошибки, и он лежит ВНЕ
  // `.screen` (`<div id="toast">` рядом с ним). Первая редакция мерки читала
  // только экран, отчитывалась «английского нет» на 207 экранах и была при
  // этом слепа именно к тосту: самопроверка кладёт строку в экран, поэтому
  // зелёная самопроверка ничего про тост не говорила.
  // Видимость тоста берём по его СОБСТВЕННОМУ признаку — классу `show`,
  // которым его показывает `toast()`. По computed opacity нельзя: на #toast
  // висит `transition:opacity .25s`, и сразу после добавления класса
  // вычисленная прозрачность ещё 0. Именно на это мерка и попалась: первая
  // редакция фильтровала по opacity и отчитывалась «английского нет», будучи
  // слепой к тосту; самопроверка это и показала словом «в тосте=false».
  const extra = [document.getElementById('toast')]
    .filter(el => el && el.classList.contains('show'));
  const MARK = /(?<![A-Za-z])(the|and|with|without|for|you|your|are|not|this|that|have|has|was|were|from|must|cannot|could|should|would|will|please|required|require|invalid|allowed|failed|unable|already|does|too|than|while|when|which|there|their|been|being|only|every|wait|seconds|minutes|retry|again|expired|unknown|missing|exists|denied|refused|closed|timeout|connect|connection|privileges|peer)(?![A-Za-z])/gi;
  const t = [scr, ...extra].map(el => el.innerText || '').join('  ')
    .replace(/\s+/g, ' ');
  const hits = [];
  for (const chunk of t.split(/(?<=[.!?·|])\s+|\s{2,}/)) {
    const marks = new Set((chunk.match(MARK) || []).map(w => w.toLowerCase()));
    const words = (chunk.match(/[A-Za-z]{2,}/g) || []).length;
    if (marks.size >= 2 || (marks.size && words >= 3)) hits.push(chunk.trim().slice(0, 90));
  }
  // `toast` в ответе — это ЗНАМЕНАТЕЛЬ ветки тоста. Без него «английского нет»
  // не отличить от «во время замера тоста ни разу не было»: тост живёт 5
  // секунд на ошибке, обход меряет через 120–220 мс после открытия, и это
  // надо показывать числом, а не выводить из таймингов.
  return { hits, id: scr.id, toast: extra.some(el => el.id === 'toast') };
};

const GIBBERISH = () => {
  // Корень — окно, если открыто. Прежняя редакция читала только экран, и все
  // семьдесят одно окно оставались без мерки машинного текста: проход по
  // окнам звал одну мерку обрезки. `undefined` в окне владелец видит так же
  // хорошо, как на экране, а в окнах он вводит данные.
  const _mb = document.querySelector('.mbg.show');
  const scr = _mb ? (_mb.querySelector('.modal') || _mb)
                  : document.querySelector('.screen.show');
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

// Самопроверка мерки машинного текста. Подсказок стало ноль — и ровно с этой
// минуты «подсказок нет» перестало что-либо означать: мерка могла ослепнуть
// тем же способом, каким ослепла мерка в окнах (корень не тот, текст не
// читается). Кладём на экран заведомо машинное слово и требуем его найти.
async function gibberishSelfCheck() {
  return page.evaluate((src) => {
    const gib = new Function('return (' + src + ')()');
    const scr = document.querySelector('.screen.show');
    if (!scr) return { ran: false };
    const base = gib().hits.length;
    const bad = document.createElement('div');
    bad.textContent = 'заведомо undefined в тексте';
    scr.appendChild(bad);
    const caught = gib().hits.length > base;
    bad.remove();
    const restored = gib().hits.length === base;
    return { ran: true, caught, restored, base };
  }, GIBBERISH.toString());
}

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

// Самопроверка мерки закрытого текста. Кладём на экран строку и прибиваем
// поверх неё полосу, как это делает `.sel-bar`, — мерка обязана её найти.
// Иначе «закрытого текста нет» ниже не значит ничего.
async function coveredSelfCheck() {
  // Сначала в упор, как и всякий замер этой меркой. Иначе фон самопроверки
  // считается на непрокрученном экране и показывает находки, которых нет:
  // в режиме пустых данных он печатал «своих находок на экране: 3», тогда как
  // весь обход на тех же данных не нашёл ни одной. Число, которое ничего не
  // значит, стоит рядом с числами, которые значат, — и выглядит так же.
  await page.evaluate((src) => new Function('return (' + src + ')()')(), SCROLL_END.toString());
  await page.waitForTimeout(80);
  return page.evaluate((src) => {
    const covered = new Function('return (' + src + ')()');
    const scr = document.querySelector('.screen.show');
    if (!scr) return { ran: false };
    // Три замера, а не два. Сравнивать «с полосой» с нулём нельзя: на экране
    // бывают и настоящие находки. Сравнивать с фоном БЕЗ подопытной строки
    // тоже мало — на 320 подопытное место само лежит под настоящей полосой, и
    // строка там закрыта независимо от нашей. Поэтому: фон, фон со строкой,
    // и только потом строка под полосой.
    const base = covered().n;
    const line = document.createElement('div');
    line.style.cssText = 'position:fixed;left:20px;bottom:200px;width:180px;height:24px';
    line.textContent = 'заведомо закрытая строка';
    scr.appendChild(line);
    const withLine = covered().n;
    const bar = document.createElement('div');
    bar.style.cssText = 'position:fixed;left:0;right:0;bottom:180px;height:60px;'
      + 'background:#123;z-index:999';
    document.body.appendChild(bar);
    const caught = covered().n > withLine;
    bar.remove();
    line.remove();
    // Всё снято — находок должно стать ровно столько же, сколько было.
    const restored = covered().n === base;
    return { ran: true, caught, restored, base };
  }, COVERED.toString());
}

let selScreens = 0;                // на скольких экранах удалось выделить строки
const scrollStuck = [];            // где прокрутка не дошла до упора

// Полоса действий над выделением (`.sel-bar`) лежит поверх списка и
// показывается, только когда что-то выделено. Пока обход ничего не выделял,
// самый вероятный случай закрытого текста мерке был не виден. Нажимаем ту же
// ссылку «Все», что и владелец: показать полосу руками через `display` значило
// бы померить состояние, в котором приложение не бывает.
//
// Зовётся и из прохода по открывашкам, и из прохода по вкладкам. Первый раз
// проход стоял только в открывашках, и знаменатель это показал: полос
// выделения в разметке три, а сработала одна — две другие живут на вкладках
// «Аккаунты» и «Боты», куда проход по открывашкам не заходит.
async function coveredWithSelection() {
  // Нажатия «Все» мало: полоса показывается не от выделения, а от РЕЖИМА
  // выбора, который включает отдельная кнопка. Пока проход жал только «Все»,
  // знаменатель говорил «выделили на двух экранах», а полоса не появлялась ни
  // разу — то есть мерить было нечего, а выглядело как мерили.
  const shown = await page.evaluate(() => {
    const scr = document.querySelector('.screen.show');
    if (!scr) return false;
    const vis = (el) => !!el && el.getBoundingClientRect().height > 4;
    if (!scr.querySelector('.sel-bar')) return false;
    if (!vis(scr.querySelector('.sel-bar'))) {
      // По ИМЕНИ обработчика, а не по тексту кнопки. Текст искали как
      // «…Выбрать» в конце — и знаменатель показал «2» при трёх полосах:
      // у каналов кнопка называется «☑️ Выбрать каналы». Подбирать условие
      // под надписи значит ждать следующей надписи; режим выбора включает
      // функция, и имя у неё одно на все три экрана.
      const on = [...scr.querySelectorAll('[onclick]')].find(
        e => /toggle\w*SelMode\(/.test(e.getAttribute('onclick') || ''));
      if (on) on.click();
    }
    const all = [...scr.querySelectorAll('.sel-bar-link')]
      .find(e => (e.textContent || '').trim() === 'Все');
    if (all) all.click();
    return vis(scr.querySelector('.sel-bar'));
  });
  // Считаем экраны, где полоса ДЕЙСТВИТЕЛЬНО показалась, а не где нашлась
  // ссылка: знаменатель, считающий нажатия, не знает, что из них вышло.
  if (!shown) return null;
  selScreens++;
  await page.waitForTimeout(140);
  const se = await page.evaluate((src) => new Function('return (' + src + ')()')(), SCROLL_END.toString());
  await page.waitForTimeout(80);
  const sv = se.atEnd
    ? await page.evaluate((src) => new Function('return (' + src + ')()')(), COVERED.toString())
    : { n: 0, worst: [] };
  if (!se.atEnd) scrollStuck.push('выделение');
  if (process.env.MINIAPP_SHOT_DIR) {
    // Снимок именно этого состояния: находку «закрыто при выделении» иначе
    // нечем посмотреть — обычные снимки обхода сняты до нажатия «Все».
    const id = await page.evaluate(() => {
      const el = document.querySelector('.screen.show');
      return el ? el.id : 'none';
    });
    await page.screenshot({ path: path.join(process.env.MINIAPP_SHOT_DIR, `sel_${id}.png`) });
  }
  return sv.n ? `ЗАКРЫТО ПРИ ВЫДЕЛЕНИИ ${sv.n}: ` + sv.worst.join(' ; ') : null;
}

// Четыре экрана обход не показывал — и не потому, что они сломаны. Их
// открывашка требует состояния, которое в живом приложении оставляет
// ПРЕДЫДУЩИЙ экран: выбранный бот, отмеченные подарки, загруженный список
// сегментов. Вызов «с нуля» упирается в `if (!CUR_BOT) return;` — экран
// честно не открывается, и обход честно его не считает.
//
// Затравка ставит ровно то состояние, которое ставит приложение, и ничего
// больше: подделывать здесь ДАННЫЕ экрана нельзя — мерка мерила бы подделку.
// Переменные объявлены через `let` на верхнем уровне, то есть в window их нет;
// присваивание без объявления попадает в то самое связывание.
//
// Если затравка перестанет работать (переменную переименовали), экран снова
// пропадёт из охвата — поэтому ниже на каждую затравку есть самопроверка: не
// открылся засеянный экран — отчёт кричит, а не возвращается молча к прежним
// «не показано: 4».
const SEED = {
  openArScreen: () => { CUR_BOT = { id: 1, name: 'Бот' }; },
  giftsTransferStart: () => {
    GIFT_ROWS = [{ id: 1, stars_cost: 50, title: 'Подарок' }];
    GIFT_SEL = new Set([0]);
  },
  openAudSegment: () => {
    _AUD_SEGMENTS = [{ key: 'a', name: 'Сегмент А' }, { key: 'b', name: 'Сегмент Б' }];
  },
};

if (sweepMode) {
  // Строка списка — либо `имя`, либо `имя|аргумент`. Второй вид — ПРОБНЫЙ
  // вызов: функция требует id сущности, заглушка отдаёт одни и те же данные на
  // любой URL, поэтому экран детали рисуется при любом id. Падение такого
  // вызова — не находка, а непокрытый экран: он виден в строке охвата.
  const fns = fs.readFileSync(sweepList, 'utf8').split('\n').map(s => s.trim()).filter(Boolean)
    .map(line => {
      // `имя[|1…][ s-экран]`. Хвост после пробела — экран, который открывашка
      // показывает своим `push`; без него обход засчитывал ЛЮБОЙ появившийся
      // экран, в том числе чужой.
      const [call, want] = line.split(/\s+/);
      const [name, ...args] = call.split('|');
      return { name, args, want: want || null, probe: args.length > 0 };
    });
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
  // Мерка действий тоже обязана показать, что видит: гасим у показанного
  // экрана все действия и требуем ноль, потом возвращаем и требуем больше
  // нуля. Без этого «мёртвых экранов нет» не значит ничего.
  const as_ = await page.evaluate((src) => {
    const act = new Function('return (' + src + ')()');
    const scr = document.querySelector('.screen.show');
    if (!scr) return { ran: false };
    const before = act().acts;
    const hid = [];
    for (const n of scr.querySelectorAll('button, input, select, textarea, a[href], [onclick], .btn, .chip, .seg-opt, .tap, .toggle, .switch')) {
      hid.push([n, n.style.display]); n.style.display = 'none';
    }
    const silent = act().acts === 0;
    hid.forEach(([n, d]) => { n.style.display = d; });
    return { ran: true, silent, before, back: act().acts };
  }, ACTIONS.toString());
  console.log(`самопроверка мерки действий: экран без действий найден=${as_.silent}`
    + ` | действия вернулись=${as_.back > 0} (было ${as_.before})`);
  if (!as_.ran || !as_.silent || !(as_.back > 0)) {
    console.log('МЕРКА ДЕЙСТВИЙ СЛЕПА — «мёртвых экранов нет» ничего не означает');
    process.exitCode = 2;
  }
  // Признак состояния ошибки тоже обязан показать, что видит: кладём на экран
  // разметку ошибки и требуем признак, снимаем — и требуем его отсутствия.
  //
  // Проверяем НЕ на живом экране, а на своей пустой площадке. В режиме сбоя
  // (MINIAPP_FAIL) живой экран сам в состоянии ошибки — всё отвечает
  // пятисоткой, — и третий корень («спокойная пустота отличена от ошибки»)
  // проваливался всегда: `erred` находит `.err-msg` в любом месте экрана, и
  // чужая разметка ошибки, уже лежавшая там, перебивала подложенную
  // спокойную пустоту. Отчёт писал «ПРИЗНАК ОШИБКИ СЛЕП», то есть объявлял
  // негодной мерку — а негодной была площадка. Ровно в том режиме, ради
  // которого признак и нужен (кто промолчал в ответ на сбой), самопроверка
  // не значила ничего.
  const es_ = await page.evaluate((src) => {
    const act = new Function('return (' + src + ')()');
    const real = document.querySelector('.screen.show');
    if (!real) return { ran: false };
    // ACTIONS читает `.screen.show`, поэтому площадку надо не просто создать,
    // а сделать единственной показанной — и вернуть всё как было.
    real.classList.remove('show');
    const scr = document.createElement('div');
    scr.className = 'screen show';
    scr.id = 's-selfcheck-erred';
    document.body.appendChild(scr);
    const finish = (r) => {
      scr.remove();
      real.classList.add('show');
      return r;
    };
    const base = act().erred;
    const baseBlank = act().blank;
    if (base || baseBlank) {
      // Пустая площадка ни ошибкой, ни пустотой быть не может. Если может —
      // мерка читает что-то вне экрана, и дальше проверять нечего.
      return finish({ ran: true, base, dirty: true });
    }
    const box = document.createElement('div');
    box.className = 'err-msg';
    box.innerHTML = '<div class="err-msg-ico">⚠️</div><div class="err-msg-txt">проверка</div>';
    scr.appendChild(box);
    const caught = act().erred;
    box.remove();
    // Второй корень — спокойное пустое состояние со значком ошибки: именно так
    // показывают ошибку детальные экраны.
    const emp = document.createElement('div');
    emp.className = 'empty';
    emp.innerHTML = '<div class="empty-ico">⚠️</div><div class="empty-txt">проверка</div>';
    scr.appendChild(emp);
    const caughtEmpty = act().erred;
    emp.remove();
    // Третий корень — СПОКОЙНОЕ пустое состояние: та же разметка, но значок не
    // ошибки. Признак обязан назвать её пустотой и НЕ назвать ошибкой, иначе
    // «пустых экранов нет» и «ошибок нет» оба ничего не значат.
    const calm = document.createElement('div');
    calm.className = 'empty';
    calm.innerHTML = '<div class="empty-ico">💬</div><div class="empty-txt">проверка</div>';
    scr.appendChild(calm);
    const r3 = act();
    const caughtBlank = r3.blank && !r3.erred;
    calm.remove();
    return finish({ ran: true, base, caught, caughtEmpty, caughtBlank,
                    restored: act().erred === base && act().blank === baseBlank });
  }, ACTIONS.toString());
  // Признак «сказал о сбое своими словами» тоже обязан показать, что видит:
  // фраза сервера на чистой площадке должна находиться, а без неё — нет.
  // Без этого «сбой нигде не скрыт» значило бы ровно столько же, сколько
  // прежнее «ошибок нет» в режиме, где мерка была слепа.
  if (failMode) {
    const ps_ = await page.evaluate(({ tsrc, phrase }) => {
      const txt = new Function('return (' + tsrc + ')()');
      const real = document.querySelector('.screen.show');
      if (!real) return { ran: false };
      real.classList.remove('show');
      const scr = document.createElement('div');
      scr.className = 'screen show';
      document.body.appendChild(scr);
      const fin = (r) => { scr.remove(); real.classList.add('show'); return r; };
      const base = txt().text.indexOf(phrase) >= 0;
      const d = document.createElement('div');
      d.textContent = 'что-то ' + phrase + ' и дальше';
      scr.appendChild(d);
      const found = txt().text.indexOf(phrase) >= 0;
      d.remove();
      return fin({ ran: true, base, found, gone: txt().text.indexOf(phrase) < 0 });
    }, { tsrc: SCREEN_TEXT.toString(), phrase: FAIL_PHRASE });
    console.log(`самопроверка слов о сбое: фраза сервера найдена=${ps_.found}`
      + ` | после снятия её нет=${ps_.gone} (своё состояние ${ps_.base})`);
    if (!ps_.ran || !ps_.found || !ps_.gone || ps_.base) {
      console.log('ПРИЗНАК СЛОВ О СБОЕ СЛЕП — «сбой нигде не скрыт» ничего не означает');
      process.exitCode = 2;
    }
  }
  console.log(`самопроверка признака ошибки: разметка ошибки найдена=${es_.caught}`
    + ` | пустое состояние со значком найдено=${es_.caughtEmpty}`
    + ` | спокойная пустота отличена от ошибки=${es_.caughtBlank}`
    + ` | после снятия столько же=${es_.restored} (своё состояние ${es_.base})`);
  if (es_.dirty) {
    console.log('ПЛОЩАДКА САМОПРОВЕРКИ НЕ ЧИСТА — признак читает что-то вне экрана');
  }
  if (!es_.ran || es_.dirty || !es_.caught || !es_.caughtEmpty || !es_.caughtBlank
      || !es_.restored) {
    console.log('ПРИЗНАК ОШИБКИ СЛЕП — «экранов с ошибкой нет» ничего не означает');
    process.exitCode = 2;
  }
  const gs = await gibberishSelfCheck();
  console.log(`самопроверка мерки машинного текста: заведомо машинное слово`
    + ` найдено=${gs.caught} | после снятия подсказок столько же=${gs.restored}`);
  if (!gs.caught || !gs.restored) {
    console.log('МЕРКА МАШИННОГО ТЕКСТА СЛЕПА — «подсказок нет» ничего не означает');
    process.exitCode = 2;
  }
  if (failEnMode) {
    // Мерка английского нужна только в режиме английской ошибки, и ровно там
    // она обязана доказать, что видит: кладём на экран английскую фразу.
    const es = await page.evaluate((src) => {
      const en = new Function('return (' + src + ')()');
      const scr = document.querySelector('.screen.show');
      const toast = document.getElementById('toast');
      if (!scr || !toast) return { ran: false };
      const base = en().hits.length;
      const PHRASE = 'the account is not allowed to do that';
      // Корень первый — экран.
      const bad = document.createElement('div');
      bad.textContent = PHRASE;
      scr.appendChild(bad);
      const onScreen = en().hits.length > base;
      bad.remove();
      // Корень второй — ТОСТ, ровно так, как его показывает `toast()`:
      // своим текстом и классом `show`. Из-за него мерка и была слепа.
      const hadShow = toast.classList.contains('show');
      const hadHtml = toast.innerHTML;
      toast.innerHTML = '<span class="toast-msg"></span>';
      toast.firstChild.textContent = PHRASE;
      toast.classList.add('show');
      const inToast = en().hits.length > base;
      toast.innerHTML = hadHtml;
      if (!hadShow) toast.classList.remove('show');
      return { ran: true, caught: onScreen, inToast,
               restored: en().hits.length === base, base };
    }, ENGLISH.toString());
    console.log(`самопроверка мерки английского: заведомо английская фраза`
      + ` найдена на экране=${es.caught} | в тосте=${es.inToast}`
      + ` | после снятия столько же=${es.restored}`
      + ` | своих находок: ${es.base}`);
    if (!es.caught || !es.inToast || !es.restored) {
      console.log('МЕРКА АНГЛИЙСКОГО СЛЕПА — «английского нет» ничего не означает');
      process.exitCode = 2;
    }
  }
  const cs = await coveredSelfCheck();
  console.log(`самопроверка мерки закрытого текста: заведомо закрытая строка`
    + ` найдена=${cs.caught} | после снятия находок столько же=${cs.restored}`
    + ` | своих находок на экране: ${cs.base}`);
  if (!cs.caught || !cs.restored) {
    console.log('МЕРКА ЗАКРЫТОГО ТЕКСТА СЛЕПА — «закрытого нет» ничего не означает');
    process.exitCode = 2;
  }
  const report = [];
  const acts = new Map();      // экран → сколько на нём действий
  const seen = new Set();          // какие экраны обход ДЕЙСТВИТЕЛЬНО показал
  const mute = [];                 // открывашки, не открывшие ничего
  const hints = [];                // машинный текст: подсказка, не находка
  const seedDead = [];             // затравка есть, а экран всё равно не открылся
  const torn = [];                 // вызовы, на которых обход сорвался (перезагрузка страницы)
  let probesShown = 0, probesTotal = 0;
  for (const { name: fn, args, want, probe } of fns) {
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
    // Пробный вызов пробует подставленный id, а БЕЗ аргументов — только если
    // с ним экран не открылся. Порядок именно такой, и он важен в обе стороны.
    // Единственный параметр открывашки — не всегда id: у целого семейства
    // (openNarrative, openNewUsers, openPresencePacks и ещё с десяток) он
    // зовётся `keep` и значит «догрузить страницу, экран не открывать» —
    // подставленная туда единица экран не откроет, и спасает вызов без
    // аргументов. Но обратный порядок ломает семейство, где id ОБЯЗАТЕЛЕН:
    // `openOpDetail()` без него тоже «открывает» экран — с заголовком
    // «Операция #undefined» и телом «не найдена». Экран засчитан, а показали
    // мы пустышку. Поэтому сначала id, и только потом — без него.
    if (failMode) await page.evaluate(() => { window.__failServed = 0; });
    if (SEED[fn]) await page.evaluate(SEED[fn]);
    const attempts = probe ? [args, []] : [[]];
    let r = {};
    for (const a of attempts) {
      // Вызов открывашки — в try: если страница перезагрузится в середине
      // (так делал ретрай SDK Телеграма), Playwright рушит ВЕСЬ прогон, и
      // отчёта нет вообще. Сорванный вызов — это один непокрытый экран, он
      // виден в строке охвата; терять из-за него остальные двести нельзя.
      try {
        // Потолок ждём ВНУТРИ страницы, а не снаружи: открывашка, которая не
        // возвращается (висящий `await api(...)`), оставляет обещание
        // неразрешённым, и Playwright рано или поздно сообщает «Resulting
        // promise was garbage collected» — то есть рушит ВЕСЬ прогон из-за
        // одного экрана, и выглядит это как перезагрузка страницы, хотя
        // никакой навигации не было. Такой экран — находка сама по себе:
        // пользователь остался бы на спиннере навсегда. Поэтому ждём
        // ограниченно и возвращаем `hung`, а не висим.
        r = await page.evaluate(async ({ name, a, ms }) => {
          if (typeof window[name] !== 'function') return { missing: true };
          let timer = null;
          const capped = new Promise((res) => { timer = setTimeout(() => res({ hung: ms }), ms); });
          const call = (async () => {
            try { await window[name](...a); return {}; }
            catch (e) { return { threw: String(e && e.message || e).slice(0, 160) }; }
          })();
          try { return await Promise.race([call, capped]); }
          finally { clearTimeout(timer); }
        }, { name: fn, a, ms: OPENER_MS });
      } catch (e) {
        r = { threw: 'обход сорвался: ' + String(e && e.message || e).slice(0, 120) };
        torn.push(fn);
        break;
      }
      await page.waitForTimeout(120);
      const now = await page.evaluate(() => {
        const el = document.querySelector('.screen.show');
        return el ? el.id : null;
      });
      // Открылся ИМЕННО свой экран — хватит. Чужой не годится: с
      // подставленным id `openSalesManager(1)` уходит сразу в редактор
      // менеджера, и на этом обход прежде останавливался, засчитав попытку и
      // оставив `s-sales` непокрытым. Теперь такая попытка не считается
      // удачной, и следом пробуется вызов без аргументов.
      if (now && now !== beforeId && (!want || now === want)) break;
      await page.evaluate(() => { try { while (typeof STACK !== 'undefined' && STACK.length) back(); } catch (_) {} });
    }
    await page.waitForTimeout(220);
    // MINIAPP_SHOT_DIR — снимок каждого экрана обхода. Отчёт говорит, ЧТО
    // обрезано, а посмотреть, как это выглядит, иначе нечем: экраны обхода не
    // вкладки, и одиночный режим их не открывает.
    if (process.env.MINIAPP_SHOT_DIR) {
      await page.screenshot({ path: path.join(process.env.MINIAPP_SHOT_DIR, `miniapp_${fn}.png`) });
    }
    // MINIAPP_CONTRAST=1 — нечитаемый текст: контраст цвета текста с его
    // настоящим фоном (первый непрозрачный фон вверх по дереву) ниже 3:1.
    // Нужен, чтобы проверять ОБЕ темы: цвет, прописанный под тёмный фон, на
    // светлой теме почти исчезает, а снимок уменьшенным этого не покажет.
    if (process.env.MINIAPP_CONTRAST) {
      const low = await page.evaluate(() => {
        const P = (c) => { const m = String(c).match(/rgba?\(([^)]+)\)/); if (!m) return null; const v = m[1].split(',').map(Number); return { r: v[0], g: v[1], b: v[2], a: v.length > 3 ? v[3] : 1 }; };
        const L = (c) => { const f = (x) => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); }; return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
        const over = (top, bot) => ({ r: top.r * top.a + bot.r * (1 - top.a), g: top.g * top.a + bot.g * (1 - top.a), b: top.b * top.a + bot.b * (1 - top.a), a: 1 });
        const bgOf = (el) => { const st = []; for (let e = el; e; e = e.parentElement) { const cs = getComputedStyle(e); if (cs.backgroundImage && cs.backgroundImage !== 'none' && !cs.backgroundImage.startsWith('radial')) return null; const c = P(cs.backgroundColor); if (c && c.a > 0) { st.push(c); if (c.a >= 0.99) break; } } let base = P(getComputedStyle(document.body).backgroundColor) || { r: 255, g: 255, b: 255, a: 1 }; for (let i = st.length - 1; i >= 0; i--) base = over(st[i], base); return base; };
        const scr = document.querySelector('.screen.show'); if (!scr) return [];
        const out = [];
        for (const el of scr.querySelectorAll('*')) {
          if (!el.childNodes.length || ![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 1)) continue;
          const r = el.getBoundingClientRect(); if (!r.width || !r.height || r.bottom < 0 || r.top > innerHeight) continue;
          const cs = getComputedStyle(el); if (cs.visibility === 'hidden' || +cs.opacity < 0.3) continue;
          const fg = P(cs.color), bg = bgOf(el); if (!fg || !bg) continue;
          const f = over(fg, bg); const a = L(f), b = L(bg); const k = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
          if (k < 3) out.push(k.toFixed(2) + ' ' + el.tagName.toLowerCase() + (el.className && typeof el.className === 'string' ? '.' + el.className.split(' ')[0] : '') + ' «' + el.textContent.trim().slice(0, 30) + '» ' + cs.color + ' на ' + `rgb(${bg.r|0},${bg.g|0},${bg.b|0})`);
        }
        return out.slice(0, 6);
      });
      if (low.length) console.log('  КОНТРАСТ ' + fn + ' | ' + low.join(' ; '));
    }
    const m = await page.evaluate((src) => new Function('return (' + src + ')()')(), CLIPPED.toString());
    // В упоре прокрутки — и только там — имеет смысл спрашивать, не закрыт ли
    // хвост списка полосой, прибитой к низу окна.
    const se = await page.evaluate((src) => new Function('return (' + src + ')()')(), SCROLL_END.toString());
    await page.waitForTimeout(80);
    // Не дошли до упора — мерка закрытого текста не имеет смысла, и молчать об
    // этом нельзя: «находок нет» и «мерить было негде» выглядят одинаково.
    const cv = se.atEnd
      ? await page.evaluate((src) => new Function('return (' + src + ')()')(), COVERED.toString())
      : { n: 0, worst: [], stuck: true };
    if (cv.stuck) scrollStuck.push(fn + ' [' + (se.stuck || []).join(' ; ') + ']');
    const shownId = await page.evaluate(() => {
      const el = document.querySelector('.screen.show');
      return el ? el.id : null;
    });
    const opened = shownId && shownId !== beforeId && (!want || shownId === want);
    if (opened) { seen.add(shownId); if (probe) probesShown++; }
    // Что именно осталось на виду — часть находки, а не подробность: «экран
    // не сменился» одинаково звучит и когда открывашка ничего не сделала, и
    // когда она открыла экран, который уже был показан.
    else if (!probe) mute.push(`${fn} (${beforeId}→${shownId})`);
    // Отдельным `if`, а НЕ веткой `else if` выше: вставленный между `if` и
    // `else` оператор перевешивает `else` на себя, и список немых открывашек
    // начинает собирать как раз те, что отработали. Проверено на себе.
    if (SEED[fn] && !opened) seedDead.push(fn);
    // Действия считаем только у своего открывшегося экрана и только в
    // обычном режиме: в пустом и в режиме ошибки экран пуст законно.
    // Пробные вызовы тоже мерим: 85 экранов детали из 207 оставались без
    // замера вовсе, и «мёртвый один» считалось по 123 экранам из 207.
    // Условие здесь — `opened`, то есть на виду оказался ИМЕННО свой экран.
    if (opened && !emptyMode) {
      const ac = await page.evaluate((src) => new Function('return (' + src + ')()')(), ACTIONS.toString());
      const prev = acts.get(shownId);
      if (!prev || prev.acts < ac.acts) {
        // Отличаем «делать нечего» от «заглушка не накормила»: если в коде
        // открывашки действий не видно вовсе — это свойство экрана, а если
        // видно, а на экране их нет — это непокрытый данными экран, и звать
        // на работу по нему нельзя.
        // Исходник открывашки И вызванных ею функций приложения — на один
        // уровень вглубь. Без этого признак слеп там, где действие рисует
        // помощник: `openAdminActions` зовёт `moreBtn()`, кнопка приходит
        // оттуда, и экран попадал в «мёртвые» по ошибке. Отчёт, зовущий на
        // работу, которой нет, — та же ложь, что пропущенная беда.
        const src = await page.evaluate((n) => {
          const own = String(window[n] || '');
          let all = own;
          // Кнопка ОБЩЕГО помощника — не действие экрана. `empty()` и
          // `errHtml()` сами рисуют «← Назад»/«↺ Повторить», поэтому при
          // чтении на уровень вглубь признак срабатывал у любого экрана,
          // который их зовёт, — то есть почти у всех. Экраны-витрины
          // (`s-audience`, `s-nodethreads`, `s-invacc`: числа, график и
          // список с `cursor:default`, ни одного своего действия) попадали не
          // в «мёртвые», а в «не накормлены заглушкой», и жалоба владельца от
          // 28.09 на них не показывала. Диалоги туда же: `toast`,
          // `askConfirm`, `confirmDelete` — чужая разметка.
          const CHROME = new Set(['empty', 'errHtml', 'toast', 'askConfirm',
                                  'confirmDelete', 'modernizeIcons']);
          const seen = new Set([n]);
          for (const m of own.matchAll(/\b([A-Za-z_$][\w$]*)\s*\(/g)) {
            const k = m[1];
            if (seen.has(k) || CHROME.has(k)) continue;
            seen.add(k);
            if (typeof window[k] === 'function') all += '\n' + String(window[k]);
          }
          // Призыв к действию, переданный в `empty()` четвёртым аргументом
          // (`{label, fn}`), — действие ИМЕННО экрана, и он виден в своём же
          // исходнике.
          const cta = /empty\([^;]*\bfn\s*:/.test(own);
          return { src: all, cta };
        }, fn);
        const canAct = cta_(src)
          || /onclick|<button|class="btn|class="row tap|\bchip\b|<input|<select|<textarea/.test(src.src);
        acts.set(shownId, { fn, acts: ac.acts, chars: ac.chars, canAct,
                            erred: ac.erred, blank: ac.blank, rows: ac.rows,
                            sample: ac.sample });
      }
    }
    const problems = [];
    // Скрытый сбой считается раньше остальных находок (ниже, перед нажатиями),
    // а в отчёт идёт вместе с ними.
    const hidFail = [];
    // У пробного вызова падение значит «подставленный id не подошёл», а не
    // поломку: в находки оно не идёт, иначе отчёт утонет в шуме и перестанет
    // что-либо значить. Обрезка считается у обоих одинаково — она мерится по
    // тому, что ДЕЙСТВИТЕЛЬНО нарисовано.
    if (!probe) {
      if (r.missing) problems.push('НЕТ ФУНКЦИИ');
      if (r.threw) problems.push('THREW: ' + r.threw);
      // Открывашка не вернулась за потолок: экран показан (push прошёл), а
      // данные не пришли и ошибки нет — для пользователя это вечный спиннер.
      if (r.hung) problems.push(`НЕ ВЕРНУЛАСЬ за ${Math.round(r.hung / 1000)} с `
        + '(экран остался бы на спиннере)');
      if (live.length) problems.push(...live.slice(0, 2));
    }
    if (m.n) problems.push(`ОБРЕЗАНО ${m.n}: ` + m.worst.join(' ; '));
    if (cv.n) problems.push(`ЗАКРЫТО ${cv.n}: ` + cv.worst.join(' ; '));
    // Приговор о скрытом сбое — ДО нажатий. Ниже проход сам включает режим
    // выбора и жмёт «Все» (coveredWithSelection), а `chSelAll()` перерисовывает
    // список и затирает блок ошибки: находкой стал `openChannels`, который
    // ошибку показывает правильно — проверено в браузере руками. Мерка,
    // судящая экран после собственных нажатий, судит свою работу.
    if (failMode && opened) {
      // Ждём, пока экран ДОСКАЖЕТ. Открывашка асинхронна: отказ `fetch`
      // доходит до `catch` через пару микрозадач, и первая версия приговора
      // мерила экран со спиннером — находкой стал `openChannels`, который
      // ошибку показывает правильно (проверено в браузере руками). Приговор,
      // срабатывающий на собственной спешке, хуже отсутствующего: он уводит
      // разбираться с исправным экраном. Поэтому ждём до 1.2 с и выходим
      // раньше, едва экран сказал о сбое.
      // Счётчик пятисоток — ОБЩИЙ на страницу, и этого мало: поздний ответ
      // предыдущего экрана или фоновый опрос увеличивают его, а виноватым
      // выходит текущий экран. Так в находки попали `openUchAI` и
      // `openGlobalSearch` — у них в открывашке запроса нет вовсе: ассистент
      // ждёт вопроса, поиск — нажатия 🔍. Поэтому спрашиваем ещё и код: судим
      // только экран, который САМ грузит данные при открытии (`api(` в
      // открывашке или в её помощнике на один уровень вглубь — так же, как
      // это уже делает приговор о мёртвых экранах).
      const loadsOnOpen = await page.evaluate((n) => {
        const CHROME = new Set(['empty', 'errHtml', 'toast', 'askConfirm',
                                'confirmDelete', 'modernizeIcons', 'push',
                                'txt', 'esc', 'escJs', 'retryCall']);
        let all = String(window[n] || '');
        const seen = new Set([n]);
        for (const m of all.matchAll(/\b([A-Za-z_$][\w$]*)\s*\(/g)) {
          const k = m[1];
          if (seen.has(k) || CHROME.has(k)) continue;
          seen.add(k);
          if (typeof window[k] === 'function') all += '\n' + String(window[k]);
        }
        return /\bapi\s*\(/.test(all);
      }, fn);
      let fs = null;
      for (let i = 0; i < 12; i++) {
        fs = await page.evaluate(({ src, tsrc }) => ({
          served: window.__failServed || 0,
          ...new Function('return (' + src + ')()')(),
          text: new Function('return (' + tsrc + ')()')().text,
        }), { src: ACTIONS.toString(), tsrc: SCREEN_TEXT.toString() });
        if (fs.erred || String(fs.text || '').indexOf(FAIL_PHRASE) >= 0) break;
        await page.waitForTimeout(100);
      }
      const said = fs.erred || String(fs.text || '').indexOf(FAIL_PHRASE) >= 0;
      if (process.env.MINIAPP_DUMP && loadsOnOpen && fs.served > 0 && !said) {
        const dump = await page.evaluate(() => {
          const scr = document.querySelector('.screen.show');
          if (!scr) return 'нет экрана';
          const parts = [];
          for (const el of scr.children) {
            if (el.classList && (el.classList.contains('hdr') || el.classList.contains('nav'))) continue;
            parts.push('[' + (el.id || el.className || el.tagName) + '] '
              + el.innerHTML.replace(/<!--[\s\S]*?-->/g, '').slice(0, 300));
          }
          return parts.join(' ||| ').slice(0, 2000);
        });
        console.log('  DUMP ' + fn + ' >>> ' + dump.replace(/\s+/g, ' '));
      }
      if (loadsOnOpen && fs.served > 0 && !said) {
        hidFail.push(`СБОЙ СКРЫТ ${fs.id}: пятисоток ${fs.served}, ни разметки ошибки,`
          + ` ни слова о сбое`
          + (fs.blank ? ' (нарисовано спокойное «показывать нечего»)' : '')
          + ` «${String(fs.sample || '').slice(0, 90)}»`);
      }
    }
    const sel = await coveredWithSelection();
    if (sel) problems.push(sel);
    const gb = await page.evaluate((src) => new Function('return (' + src + ')()')(), GIBBERISH.toString());
    // В находки это НЕ идёт — см. заголовок раздела при печати. Заглушка
    // отдаёт одну форму на 1019 маршрутов, и поле, которое на одном экране
    // число, на другом список; отличить тут беду приложения от беды заглушки
    // нельзя. Это подсказка «посмотри сюда», а не приговор.
    if (gb.hits && gb.hits.length) hints.push(fn + ' | ' + gb.hits.slice(0, 2).join(' | '));
    if (failEnMode) {
      // Здесь это именно НАХОДКА, а не подсказка: разметка по-русски (её
      // держит отдельная проверка), значит английская фраза на экране
      // пришла из тела ответа и её увидел бы владелец.
      const en = await page.evaluate((src) => new Function('return (' + src + ')()')(), ENGLISH.toString());
      if (en.toast) enToastSeen++;
      if (en.hits && en.hits.length) {
        problems.push(`АНГЛИЙСКИЙ ${en.hits.length}: ` + en.hits.slice(0, 2).join(' ; '));
      }
    }
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
    if (hidFail.length) problems.unshift(...hidFail);
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
    // Набор мерок здесь был ТРИ из шести, а вкладки — пять самых частых
    // экранов владельца: главный, аккаунты, боты, рассылки, ещё. Закрытый
    // текст, упор прокрутки, машинный текст, английский и немота на них не
    // мерились вовсе. Разъехалось оттого, что код прохода скопирован, а не
    // общий; равенство наборов теперь держит проверка
    // tests/test_the_layout_check_can_actually_fail.py.
    const m = await page.evaluate((src) => new Function('return (' + src + ')()')(), CLIPPED.toString());
    const se = await page.evaluate((src) => new Function('return (' + src + ')()')(), SCROLL_END.toString());
    await page.waitForTimeout(80);
    const cv = se.atEnd
      ? await page.evaluate((src) => new Function('return (' + src + ')()')(), COVERED.toString())
      : { n: 0, worst: [], stuck: true };
    if (cv.stuck) scrollStuck.push('вкладка ' + t + ' [' + (se.stuck || []).join(' ; ') + ']');
    const problems = [];
    if (live.length) problems.push(...live.slice(0, 2));
    if (m.n) problems.push(`ОБРЕЗАНО ${m.n}: ` + m.worst.join(' ; '));
    if (cv.n) problems.push(`ЗАКРЫТО ${cv.n}: ` + cv.worst.join(' ; '));
    const sel = await coveredWithSelection();
    if (sel) problems.push(sel);
    const gb = await page.evaluate((src) => new Function('return (' + src + ')()')(), GIBBERISH.toString());
    if (gb.hits && gb.hits.length) hints.push('вкладка ' + t + ' | ' + gb.hits.slice(0, 2).join(' | '));
    if (failEnMode) {
      const en = await page.evaluate((src) => new Function('return (' + src + ')()')(), ENGLISH.toString());
      if (en.toast) enToastSeen++;
      if (en.hits && en.hits.length) {
        problems.push(`АНГЛИЙСКИЙ ${en.hits.length}: ` + en.hits.slice(0, 2).join(' ; '));
      }
    }
    if (emptyMode) {
      const mu = await page.evaluate((src) => new Function('return (' + src + ')()')(), MUTE.toString());
      if (mu.id && mu.len === 0) problems.push(`НЕМОЙ ЭКРАН ${mu.id}: ни одного знака`);
    }
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
    // Мерки текста в окнах — тоже со своей самопроверкой, и по той же причине,
    // по которой мерка английского оказалась слепа к тосту: корень не тот.
    const tcm = await page.evaluate((srcs) => {
      const gib = new Function('return (' + srcs[0] + ')()');
      const en = new Function('return (' + srcs[1] + ')()');
      const mb = document.querySelector('.mbg.show');
      const root = mb && (mb.querySelector('.modal') || mb);
      if (!root) return { ran: false };
      const g0 = gib().hits.length, e0 = en().hits.length;
      const bad = document.createElement('div');
      bad.textContent = 'заведомо undefined в окне, the field is not allowed';
      root.appendChild(bad);
      const gib1 = gib().hits.length > g0, en1 = en().hits.length > e0;
      bad.remove();
      return { ran: true, gib: gib1, en: en1,
               back: gib().hits.length === g0 && en().hits.length === e0 };
    }, [GIBBERISH.toString(), ENGLISH.toString()]);
    await page.evaluate((i) => document.getElementById(i).classList.remove('show'), modalIds[0]);
    console.log(`самопроверка мерки в окне (${modalIds[0]}): `
      + `заведомо обрезанный элемент найден=${scm.caught}`
      + ` | машинный текст найден=${tcm.gib} | английский найден=${tcm.en}`
      + ` | после снятия столько же=${tcm.back}`);
    if (!scm.caught) {
      console.log('МЕРКА СЛЕПА В ОКНАХ — «обрезки нет» по окнам ничего не означает');
      process.exitCode = 2;
    }
    if (!tcm.gib || !tcm.en || !tcm.back) {
      console.log('МЕРКИ ТЕКСТА СЛЕПЫ В ОКНАХ — по окнам они ничего не означают');
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
    const mg = await page.evaluate((src) => new Function('return (' + src + ')()')(), GIBBERISH.toString());
    if (mg.hits && mg.hits.length) hints.push('окно ' + id + ' | ' + mg.hits.slice(0, 2).join(' | '));
    if (failEnMode) {
      const me = await page.evaluate((src) => new Function('return (' + src + ')()')(), ENGLISH.toString());
      if (me.hits && me.hits.length) {
        report.push('окно ' + id + ' | ' + `АНГЛИЙСКИЙ ${me.hits.length}: ` + me.hits.slice(0, 2).join(' ; '));
      }
    }
    // `live` здесь обнулялся и больше НИКОГДА не читался: ошибка страницы,
    // поднятая при открытии окна, молча терялась. В проходе по экранам она
    // в находки идёт, в проходе по окнам — не шла.
    if (live.length) report.push('окно ' + id + ' | ' + live.slice(0, 2).join(' | '));
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
  // Мёртвые экраны — ответ на жалобу владельца от 28.09.2026. Две строки, а
  // не одна: «делать нечего» и «данных не досталось» — разные беды, и вторая
  // моя, а не приложения.
  if (acts.size) {
    const all = [...acts.entries()].map(([id, v]) => ({ id, ...v }));
    // Экран, ответивший ошибкой, в приговоры НЕ ИДЁТ: обход его не видел.
    // Прежде такие одиннадцать лежали в «почти мёртвых» — отчёт звал на
    // работу по экранам, о которых ничего не знал, и прятал, что охват
    // неполный.
    const erred = all.filter(r => r.erred);
    const rows = all.filter(r => !r.erred);
    const dead = rows.filter(r => r.acts === 0 && !r.canAct).sort((a, b) => a.chars - b.chars);
    const unfed = rows.filter(r => r.acts === 0 && r.canAct);
    // «Пусто» и «тонко» делит ОДНО правило, иначе экран со строками и одним
    // пустым подсписком не попадал ни в одно ведро и исчезал из отчёта
    // совсем — хуже, чем попасть не в то.
    const isBlank = (r) => r.blank && !r.rows;
    const blank = rows.filter(r => isBlank(r) && r.acts <= 2);
    const thin = rows.filter(r => r.acts > 0 && r.acts <= 2 && r.chars < 400
                                  && !isBlank(r));
    console.log(`экранов с замером действий: ${rows.length} из ${total.length}`);
    console.log(`ОТВЕТИЛИ ОШИБКОЙ (замер по ним ничего не значит): ${erred.length}`
      + (erred.length ? '\n  ' + erred.map(r => `${r.id} (${r.fn}) «${r.sample}»`).join('\n  ') : ''));
    console.log(`МЁРТВЫЕ (ни одного действия, и в коде открывашки их не видно): ${dead.length}`
      + (dead.length ? ' — ' + dead.map(r => `${r.id} (${r.fn}, знаков ${r.chars})`).join(' ; ') : ''));
    console.log(`НЕ НАКОРМЛЕНЫ ЗАГЛУШКОЙ (действия в коде есть, на экране нет): ${unfed.length}`
      + (unfed.length ? ' — ' + unfed.map(r => `${r.id} (${r.fn})`).join(' ; ') : ''));
    // Образец текста нужен и здесь: «пусто» без образца — такой же приговор
    // без дела, как «1 действие, 54 знака». Ровно так я искал, почему
    // расписание рассылок осталось пустым при накормленной ручке.
    console.log(`ПУСТОЕ СОСТОЯНИЕ (показывать нечего — действий мало законно): ${blank.length}`
      + (blank.length ? '\n  ' + blank.map(r => `${r.id} (${r.fn}, действий ${r.acts}) «${r.sample}»`).join('\n  ') : ''));
    console.log(`ПОЧТИ МЁРТВЫЕ (1-2 действия, меньше 400 знаков, и не пусто): ${thin.length}`
      + (thin.length ? '\n  ' + thin.map(r => `${r.id} (${r.fn}, действий ${r.acts}, знаков ${r.chars}) «${r.sample}»`).join('\n  ') : ''));
  }
  // Сорванные вызовы — не находка приложения, а дыра в охвате, и молчать о
  // ней нельзя: отчёт без этой строки выглядит полным.
  if (torn.length) {
    console.log(navs.length
      ? `СТРАНИЦА УХОДИЛА С АДРЕСА ${navs.length} раз(а): ${navs.join(' ')}`
      : 'СТРАНИЦА НИКУДА НЕ УХОДИЛА — срыв не в навигации, ищите в открывашке');
    console.log(`ОБХОД СОРВАЛСЯ НА ВЫЗОВАХ: ${torn.length} — ${torn.join(' ')}`
      + ' (страница перезагрузилась; эти экраны не мерились)');
    process.exitCode = 2;
  }
  if (missed.length) {
    // Список печатается ЦЕЛИКОМ: именно он и говорит, куда смотреть дальше.
    // Обрезанный на двенадцати, он превращался в украшение отчёта.
    console.log(`не показано экранов: ${missed.length} — ${missed.join(' ')}`);
  }
  if (seedDead.length) {
    // Затравка сломалась — её переменную переименовали или открывашку
    // переписали. Экран при этом просто выпадает из охвата, а это ровно то
    // молчание, против которого затравка и заведена.
    console.log(`ЗАТРАВКА НЕ СРАБОТАЛА: ${seedDead.join(' ')}`
      + ' — экран не открылся, состояние ставится не тем, чем нужно');
    process.exitCode = 2;
  }
  // Знаменатель прохода с выделением. Ноль значит, что полоса действий не
  // показалась ни разу, и «закрытого при выделении нет» не значит ничего.
  console.log(`экранов, где удалось выделить строки: ${selScreens}`);
  if (scrollStuck.length) {
    console.log(`прокрутка не дошла до конца: ${scrollStuck.length}`
      + ` — ${scrollStuck.slice(0, 10).join(' ')}`
      + ' (закрытый текст там НЕ мерился)');
  }
  if (failEnMode) {
    console.log(`экранов, где во время замера висел тост: ${enToastSeen}`);
    if (!enToastSeen) {
      console.log('ВЕТКА ТОСТА НЕ ПРОВЕРЕНА — «английского нет» сказано только про экран');
      process.exitCode = 2;
    }
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
