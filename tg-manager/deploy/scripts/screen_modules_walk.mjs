// Обход всех модулей с вкладками (SCREEN_MODULES): каждая вкладка открывает
// свой экран, «назад» не копит вкладки, шапка называет модуль, подсвечена
// ровно нужная вкладка. Печатает JSON {bad, errors}. Пути к Playwright и
// Chromium — как у screens_smoke.mjs (PLAYWRIGHT_JS, CHROME_BIN).
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const path = require('path');
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..');
const { LAUNCH, chromiumOf } = await import('./_chrome.mjs');
const chromium = await chromiumOf();
const b = await chromium.launch(LAUNCH); const p = await b.newPage({viewport:{width:390,height:800}});
const errs=[]; p.on('pageerror',e=>errs.push(e.message));
await p.goto('file://'+ROOT+'/mini_app/index.html'); await p.waitForTimeout(1500);
const r = await p.evaluate(async()=>{document.querySelectorAll('body > div').forEach(d=>{if(/Вход в Infragram/.test(d.textContent)&&!d.querySelector('#s-ranking'))d.remove();});
 const w=()=>new Promise(r=>setTimeout(r,250)); const bad=[];
 for (const [mod,m] of Object.entries(SCREEN_MODULES)) {
   for (const t of m.tabs) {
     modTab(mod,t.key); await w();
     const top=STACK[STACK.length-1]; const el=document.getElementById(top);
     const chips=el?el.querySelectorAll('.mod-tabs .chip'):[];
     const on=[...(el?el.querySelectorAll('.mod-tabs .chip.on'):[])].map(c=>c.dataset.key);
     const title=el?.querySelector('.hdr-title')?.textContent.trim();
     if (top!==t.sid||STACK.length!==1||chips.length!==m.tabs.length||on.join()!==t.key||title!==m.title)
       bad.push({mod,key:t.key,top,len:STACK.length,chips:chips.length,on,title});
   }
   while(STACK.length) back();
 }
 return bad;});
console.log(JSON.stringify({bad: r, errors: errs})); await b.close();
