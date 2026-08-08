/**
 * Render the Telegram mini-app (mini_app/index.html) headlessly for visual/layout
 * verification — the project previously had NO way to render it (CLAUDE.md:
 * «в песочнице нет рендера Telegram-мини-аппа»), so UI bugs (clipping, dead
 * screens) shipped unverified. This stubs window.Telegram.WebApp + fetch and
 * screenshots any screen at a mobile viewport, and reports horizontal overflow.
 *
 * Usage:  node deploy/scripts/render_miniapp.mjs [screenTab] [outDir]
 *   screenTab: home|accounts|bots|broadcasts|more  (default: home)
 * Requires Playwright + a Chromium build. Resolves both from common locations;
 * override with PLAYWRIGHT_JS and CHROME_BIN env vars.
 */
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const PW = process.env.PLAYWRIGHT_JS
  || ['playwright', '/opt/node22/lib/node_modules/playwright/index.mjs'].find(p => { try { require.resolve(p); return true; } catch { return p.startsWith('/'); } });
const { chromium } = await import(PW);
const path = require('path');
const fs = require('fs');

const tab = process.argv[2] || 'home';
const outDir = process.argv[3] || '.';
const CHROME = process.env.CHROME_BIN || [
  '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
  '/opt/pw-browsers/chromium/chrome-linux/chrome',
].find(p => { try { fs.accessSync(p); return true; } catch { return false; } });

const browser = await chromium.launch(CHROME ? { executablePath: CHROME } : {});
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();
await page.addInitScript(() => {
  window.Telegram = { WebApp: { ready(){}, expand(){}, initData:'', initDataUnsafe:{ user:{ id:1, first_name:'Test' } }, colorScheme:'dark', themeParams:{}, MainButton:{show(){},hide(){},setText(){},onClick(){}}, BackButton:{show(){},hide(){},onClick(){}}, HapticFeedback:{impactOccurred(){},notificationOccurred(){}}, onEvent(){}, offEvent(){}, openTelegramLink(){}, showAlert(){}, showConfirm(){}, close(){}, setHeaderColor(){}, setBackgroundColor(){} } };
  const ok = (o) => new Response(JSON.stringify(o), { status: 200, headers: { 'Content-Type': 'application/json' } });
  window.fetch = async () => ok({ ok:true, accounts:[], bots:[], channels:[], keywords:[], alerts:[], suggestions:[], bot_suggestions:[], rows:[], items:[], stats:{}, total:0, count:0, presets:[], recent:[], history:[], top_channels:[], recent_activity:[], by_stage:[] });
});
const errors = [];
page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
const root = path.resolve(process.cwd(), 'mini_app/index.html');
await page.goto('file://' + root, { waitUntil: 'load', timeout: 20000 });
await page.waitForTimeout(1200);
if (tab !== 'home') {
  await page.evaluate((t) => { if (typeof navGo === 'function') navGo(t, null); else if (typeof goTab === 'function') goTab(t); }, tab);
  await page.waitForTimeout(1000);
}
const shot = path.join(outDir, `miniapp_${tab}.png`);
await page.screenshot({ path: shot });
const m = await page.evaluate(() => ({ docW: document.documentElement.scrollWidth, winW: window.innerWidth }));
console.log(`screen=${tab} -> ${shot}`);
console.log(`h-overflow: docW=${m.docW} winW=${m.winW} => ${m.docW > m.winW ? 'OVERFLOW (bug)' : 'OK'}`);
console.log(`js-errors: ${errors.length}`);
errors.slice(0, 15).forEach(e => console.log('  ' + e));
await browser.close();
