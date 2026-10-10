/**
 * Экраны, на которых нечего делать.
 *
 * Жалоба владельца (28.09.2026): «слишком много экранов, которые только
 * отображают какие-то числа или количество или вообще ничего толком не
 * отображает, и в них всё не кликабельно, ничего не настраивается, не
 * выбирается».
 *
 * Скрипт отвечает на сервер ЩЕДРО (по 25 строк), обходит все экраны,
 * открывающиеся функцией без аргументов, и считает в ТЕЛЕ экрана (шапка не в
 * счёт: кнопка «назад» есть у всех) элементы, с которыми можно что-то
 * сделать: кнопки, ссылки, поля ввода, переключатели, чипы, строки с onclick.
 *
 *   0 действий        — мёртвый экран: только читать;
 *   тело пустое       — экран вообще ничего не показал.
 *
 * Запуск:  node deploy/scripts/check_dead_screens.mjs
 *
 * ВАЖНО: измеритель врёт, если заглушка ответа не подошла экрану. Поэтому он
 * печатает контрольные экраны (заведомо живой и заведомо пустой) — если они
 * встали не на свои места, список ниже читать нельзя.
 */
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = await import(process.env.PLAYWRIGHT_JS || '/opt/node22/lib/node_modules/playwright/index.mjs');
const fs = require('fs'), path = require('path');
const CHROME = ['/opt/pw-browsers/chromium-1194/chrome-linux/chrome','/opt/pw-browsers/chromium/chrome-linux/chrome']
  .find(p => { try { fs.accessSync(p); return true; } catch { return false; } });
const browser = await chromium.launch(CHROME ? { executablePath: CHROME } : {});
const ctx = await browser.newContext({ viewport: { width: 360, height: 780 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();
await page.addInitScript(() => {
  window.Telegram = { WebApp: { ready(){}, expand(){}, initData:'', initDataUnsafe:{ user:{ id:1, first_name:'Test' } }, colorScheme:'dark', themeParams:{}, MainButton:{show(){},hide(){},setText(){},onClick(){}}, BackButton:{show(){},hide(){},onClick(){}}, HapticFeedback:{impactOccurred(){},notificationOccurred(){}}, onEvent(){}, offEvent(){}, openTelegramLink(){}, showAlert(){}, showConfirm(){}, close(){}, setHeaderColor(){}, setBackgroundColor(){} } };
  const LONG = 'Название подлиннее чтобы строка была похожа на настоящую';
  const row = (i) => ({
    id: i, bot_id: i, account_id: i, channel_id: i, op_id: i, user_id: i, keyword_id: i,
    tg_id: 100000000 + i, title: LONG, name: LONG, label: LONG, first_name: LONG,
    keyword: 'ключевое слово ' + i, tag: 'метка' + i, username: 'user_' + i,
    phone: '+7999000' + i, status: 'active', state: 'active', result: 'success',
    kind: 'broadcast', type: 'mass_invite', text: LONG, message: LONG,
    created_at: '2026-09-20T10:00:00Z', ts: '2026-09-20T10:00:00Z',
    date: '2026-09-20', occurred_at: '2026-09-20T10:00:00Z', checked_at: '2026-09-20T10:00:00Z',
    count: 1234, cnt: 1234, total: 1234, members: 1234, subscribers: 1234,
    progress: 50, done: 50, done_items: 50, total_items: 100, position: 5, value: 42,
    url: 'https://example.com/' + i, link: 'https://t.me/+AAAAAAAAAAAAAAAAAAAAAA',
    ok: true, enabled: true, is_active: true, percent: 50,
  });
  const arr = (n) => Array.from({ length: n }, (_, i) => row(i + 1));
  const BIG = arr(25);
  window.fetch = async () => {
    const o = { ok: true, total: 999, count: 999, stats: {}, plan: 'pro' };
    for (const k of ['accounts','bots','channels','groups','items','rows','list','data','messages','operations','ops','users','contacts','keywords','alerts','suggestions','bot_suggestions','presets','recent','history','campaigns','broadcasts','links','folders','templates','segments','rules','members','sessions','logs','events','targets','nodes','competitors','results','entries','warnings','tasks','devices','tags','hops','files','reports','proxies','payments','invoices','jobs','runs','snapshots','packs','flows','funnels'])
      o[k] = BIG;
    return new Response(JSON.stringify(o), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
});
const errs = [];
page.on('pageerror', e => errs.push(e.message));
await page.goto('file://' + path.resolve(process.cwd(), 'mini_app/index.html'), { waitUntil:'load', timeout:30000 });
await page.waitForTimeout(1500);

const res = await page.evaluate(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const out = [];
  const isAction = (n) => {
    // Шапку целиком выбрасывать нельзя: там живут «+ Создать» и «⟳». Не в
    // счёт только возврат назад — он есть у каждого экрана.
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
  const measure = (label) => {
    const el = document.querySelector('.screen.show');
    if (!el) return;
    const body = el;
    const text = ((el.querySelector('.sub-sb, .sb, .scroll, .body') || el).innerText || '').trim();
    let acts = 0;
    for (const n of body.querySelectorAll('*')) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      if (!isAction(n)) continue;
      let outer = true;
      for (let p = n.parentElement; p && p !== body; p = p.parentElement) if (isAction(p)) { outer = false; break; }
      if (outer) acts++;
    }
    out.push({ label, id: el.id, acts, chars: text.length, head: text.slice(0, 70).replace(/\s+/g, ' ') });
  };
  const opens = Object.keys(window).filter(k => /^open[A-Z]/.test(k) && typeof window[k] === 'function' && window[k].length === 0);
  for (const fn of opens) {
    try { const r = window[fn](); if (r && r.then) await r; } catch (e) { continue; }
    await sleep(140);
    measure(fn);
    try { while (typeof STACK !== 'undefined' && STACK.length) back(); } catch (e) {}
  }
  return out;
});
await browser.close();

const byId = new Map();
for (const r of res) if (!byId.has(r.id) || byId.get(r.id).acts < r.acts) byId.set(r.id, r);
const rows = [...byId.values()];
const dead = rows.filter(r => r.acts === 0);
const thin = rows.filter(r => r.acts > 0 && r.acts <= 2 && r.chars < 400);
console.log('экранов обойдено:', rows.length);
console.log('\nКОНТРОЛЬ (измеритель врёт, если тут не то, что ожидается):');
for (const id of ['s-contactgroups', 's-botmesh', 's-uchsmarttags']) {
  const r = rows.find(x => x.id === id);
  console.log('  ' + id + ': ' + (r ? 'действий ' + r.acts + ', знаков ' + r.chars : 'не открылся'));
}
console.log('\nМЁРТВЫЕ (ни одного действия в теле):', dead.length);
dead.sort((a, b) => a.chars - b.chars).forEach(r => console.log(`  ${r.id}  знаков=${r.chars}  ${r.label}  «${r.head}»`));
console.log('\nПОЧТИ МЁРТВЫЕ (1-2 действия, мало текста):', thin.length);
thin.forEach(r => console.log(`  ${r.id}  действий=${r.acts}  знаков=${r.chars}  ${r.label}`));
console.log('\njs-ошибок:', errs.length); errs.slice(0, 5).forEach(e => console.log('  ' + e));
