/**
 * Экраны мини-аппа на скудных ответах сервера: ищем в видимом тексте
 * «undefined», «NaN», «null» — то, что человек читает как поломку.
 */
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const PW = process.env.PLAYWRIGHT_JS
  || ['playwright', '/opt/node22/lib/node_modules/playwright/index.mjs'].find(p => { try { require.resolve(p); return true; } catch { return p.startsWith('/'); } });
const { chromium } = await import(PW);
const path = require('path');
const fs = require('fs');
const CHROME = process.env.CHROME_BIN || [
  '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
  '/opt/pw-browsers/chromium/chrome-linux/chrome',
].find(p => { try { fs.accessSync(p); return true; } catch { return false; } });

const browser = await chromium.launch(CHROME ? { executablePath: CHROME } : {});
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
const page = await ctx.newPage();
await page.addInitScript(() => {
  window.Telegram = { WebApp: { ready(){}, expand(){}, initData:'', initDataUnsafe:{ user:{ id:1, first_name:'Test' } }, colorScheme:'dark', themeParams:{}, MainButton:{show(){},hide(){},setText(){},onClick(){}}, BackButton:{show(){},hide(){},onClick(){}}, HapticFeedback:{impactOccurred(){},notificationOccurred(){}}, onEvent(){}, offEvent(){}, openTelegramLink(){}, showAlert(){}, showConfirm(){}, close(){}, setHeaderColor(){}, setBackgroundColor(){} } };
  // Ответы НАРОЧНО скудные: один элемент без необязательных полей. Так ведёт
  // себя старая запись в базе, у которой колонку добавили позже.
  const ok = (o) => new Response(JSON.stringify(o), { status: 200, headers: { 'Content-Type': 'application/json' } });
  window.fetch = async (url) => {
    // Первичные ключи у записи ЕСТЬ (в базе они обязательны), а необязательные
    // поля отсутствуют — так выглядит старая запись, которой колонку добавили
    // позже. Подставлять записи без ключей нельзя: подпись «Бот #undefined» —
    // артефакт замера, а не дефект продукта.
    const one = [{ id: 1, bot_id: 1, channel_id: 1, user_id: 1, account_id: 1,
                   op_id: 1, chat_id: 1, msg_id: 1, node_id: 1, keyword_id: 1,
                   seg_id: 1, rule_id: 1, group_id: 1, plan_id: 1, acc_id: 1,
                   contact_id: 1, wf_id: 1, net_id: 1, did: 1 }];
    return ok({ ok: true, token: 'T', accounts: one, bots: one, channels: one,
      operations: one, items: one, rows: one, networks: one, workflows: one,
      keywords: one, alerts: one, suggestions: [], bot_suggestions: [],
      presets: [], recent: [], history: [], top_channels: [],
      recent_activity: [], by_stage: [], stats: {}, counts: {}, total: 1 });
  };
});
const errors = [];
page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
await page.goto('file://' + path.resolve(process.cwd(), 'mini_app/index.html'),
                { waitUntil: 'load', timeout: 30000 });
await page.waitForTimeout(1500);

// Экраны рисуются по требованию: вызываем каждый загрузчик, иначе мы меряем
// пустую разметку, а не то, что человек увидит.
const called = await page.evaluate(async () => {
  // Только то, что вызывается БЕЗ аргументов: иначе мы сами подсовываем
  // undefined вместо id и читаем «Бот #undefined» как находку, хотя это
  // артефакт замера. render*(data) по той же причине не зовём.
  const names = Object.keys(window).filter(k =>
    /^(load|open|refresh)[A-Z]/.test(k) && typeof window[k] === 'function'
    && window[k].length === 0);
  let ok = 0, failed = [];
  for (const n of names) {
    try {
      const r = window[n]();
      if (r && typeof r.then === 'function') await r;
      ok += 1;
    } catch (e) { failed.push(n + ': ' + (e && e.message)); }
  }
  return { tried: names.length, ok, failed: failed.slice(0, 15) };
});
await page.waitForTimeout(2500);

const screens = await page.$$eval('[id^="s-"]', els => els.map(e => e.id));
const bad = [];
for (const id of screens) {
  const found = await page.evaluate((sid) => {
    const el = document.getElementById(sid);
    if (!el) return [];
    // Экран может быть скрыт — показываем на время замера.
    const prev = el.style.display;
    el.style.display = 'block';
    const out = [];
    const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walk.nextNode())) {
      const t = (n.nodeValue || '').trim();
      if (/\b(undefined|NaN|null)\b/.test(t)) out.push(t.slice(0, 90));
    }
    el.style.display = prev;
    return out;
  }, id);
  for (const t of found) bad.push(`${id}: ${t}`);
}
console.log(JSON.stringify({ screens: screens.length, called, errors: errors.slice(0, 12), bad: bad.slice(0, 40) }, null, 1));
await browser.close();
