/**
 * Эргономика мини-аппа на узком телефоне (360 CSS-пикселей).
 *
 * Статичная разметка почти ничего не показывает: списки пустые, длинных
 * названий нет. Поэтому сервер здесь отвечает ЩЕДРО — по 25 строк с длинными
 * названиями без пробелов, — после чего скрипт обходит все экраны, которые
 * открываются функцией без аргументов, и замеряет два свойства:
 *
 *   • горизонтальное переполнение — элемент вылез за 360px и не лежит внутри
 *     горизонтального скроллера;
 *   • зону нажатия — САМЫЙ ВНЕШНИЙ кликабельный элемент ниже 30 пикселей
 *     (вложенные span внутри кнопки отдельной зоной не считаются).
 *
 * Запуск:  node deploy/scripts/check_tap_targets.mjs
 * Ждём:    «переполнений: 0  мелких зон: 0».
 *
 * Постоянную защиту от возврата мелких значков держит
 * tests/test_miniapp_tap_targets.py — он читает таблицу стилей и работает без
 * браузера, то есть в CI. Этот скрипт — измеритель для глаз и для случаев,
 * когда размер получается из вёрстки, а не из объявленного стиля.
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
  const LONG = 'Канал про длинные названия которые никто не проверял на узком телефоне';
  const row = (i) => ({
    id: i, bot_id: i, account_id: i, channel_id: i, op_id: i, user_id: i, tg_id: 100000000 + i,
    title: LONG, name: LONG, first_name: LONG, username: 'ochen_dlinnyy_username_bez_probelov_' + i,
    phone: '+7999000' + i, status: 'active', state: 'active', kind: 'broadcast', type: 'mass_invite',
    text: LONG + ' ' + LONG, message: LONG, created_at: '2026-09-20T10:00:00Z', ts: '2026-09-20T10:00:00Z',
    count: 123456, total: 123456, members: 123456, subscribers: 123456, progress: 50, done: 50,
    url: 'https://example.com/ochen/dlinnyy/adres/bez/probelov/' + i, link: 'https://t.me/+AAAAAAAAAAAAAAAAAAAAAA',
    ok: true, enabled: true, percent: 50,
  });
  const arr = (n) => Array.from({ length: n }, (_, i) => row(i + 1));
  const BIG = arr(25);
  window.fetch = async () => {
    const o = { ok: true, total: 999, count: 999, stats: {}, plan: 'pro' };
    for (const k of ['accounts','bots','channels','groups','items','rows','list','data','messages','operations','ops','users','contacts','keywords','alerts','suggestions','bot_suggestions','presets','recent','history','campaigns','broadcasts','links','folders','templates','segments','rules','members','sessions','logs','events','targets','nodes','competitors','results','entries','warnings','tasks']) o[k] = BIG;
    return new Response(JSON.stringify(o), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
});
const errs = [];
page.on('pageerror', e => errs.push(e.message));
await page.goto('file://' + path.resolve(process.cwd(), 'mini_app/index.html'), { waitUntil:'load', timeout:30000 });
await page.waitForTimeout(1500);

const res = await page.evaluate(async () => {
  const VW = 360, TAP = 30, EPS = 2;
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const findings = [];
  const measure = (label) => {
    const over = [], taps = [];
    for (const el of document.querySelectorAll('.screen.show')) {
      for (const n of el.querySelectorAll('*')) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
        const r = n.getBoundingClientRect();
        if (!r.width && !r.height) continue;
        if (!/auto|scroll/.test(cs.overflowX) && r.right > VW + EPS) {
          let p = n.parentElement, inSc = false;
          while (p && p !== el) { if (/auto|scroll/.test(getComputedStyle(p).overflowX)) { inSc = true; break; } p = p.parentElement; }
          if (!inSc) over.push({ tag:n.tagName.toLowerCase(), cls:String(n.className||'').slice(0,50), right:Math.round(r.right), txt:(n.textContent||'').trim().slice(0,40) });
        }
        const isClick = (x) => x && (x.tagName==='BUTTON'||x.tagName==='A'||x.hasAttribute('onclick')||/(^|\s)(btn|chip|tap)(\s|$)/.test(String(x.className||'')));
        // считаем только САМЫЙ ВНЕШНИЙ кликабельный элемент цепочки: вложенные
        // <span> внутри кнопки — это не отдельная зона нажатия.
        let outer = true;
        for (let p = n.parentElement; p && p !== el; p = p.parentElement) if (isClick(p)) { outer = false; break; }
        if (isClick(n) && outer && r.height < TAP && r.width > 0)
          taps.push({ cls:String(n.className||'').slice(0,40), w:Math.round(r.width), h:Math.round(r.height), txt:(n.textContent||'').trim().slice(0,30) });
      }
    }
    if (over.length || taps.length) findings.push({ label, over: over.slice(0,6), taps: taps.slice(0,40), nOver: over.length, nTap: taps.length });
  };
  for (const t of ['home','accounts','bots','broadcasts','more']) {
    try { goTab(t); } catch(e) {}
    await sleep(700); measure('tab:' + t);
  }
  const opens = Object.keys(window).filter(k => /^(open|load|show)[A-Z]/.test(k) && typeof window[k] === 'function' && window[k].length === 0);
  let ran = 0;
  for (const fn of opens) {
    try { const r = window[fn](); if (r && r.then) await r; ran++; } catch(e) { continue; }
    await sleep(120);
    measure('fn:' + fn);
    try { while (typeof STACK !== 'undefined' && STACK.length) back(); } catch(e) {}
  }
  return { findings, opens: opens.length, ran };
});
console.log('open/load функций без аргументов:', res.opens, ' выполнено:', res.ran);
console.log('экранов с находками:', res.findings.length);
let o=0,t=0; res.findings.forEach(f=>{o+=f.nOver;t+=f.nTap;});
console.log('переполнений:', o, ' мелких зон:', t);
const agg = {};
for (const f of res.findings) for (const t of f.taps) { const k = (t.cls||'(без класса)') + ' | h=' + t.h + ' | w=' + t.w + ' | «' + (t.txt||'') + '» | ' + f.label; agg[k] = (agg[k]||0)+1; }
console.log(Object.entries(agg).sort((a,b)=>b[1]-a[1]).slice(0,30).map(([k,v])=>`  ${v}x  ${k}`).join('\n'));
console.log('js-ошибок:', errs.length); errs.slice(0,6).forEach(e=>console.log('  '+e));
await browser.close();
