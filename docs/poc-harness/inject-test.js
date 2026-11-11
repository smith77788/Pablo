// Tests the REAL macOS sendEvent sink: script = 'window.TelegramGameProxy.receiveEvent("'+name+'", '+data+')'
// where data = JSONSerialization-style output of {value: <attacker>}. Node's JSON.stringify
// matches Foundation JSONSerialization escaping for these cases (does NOT escape / < U+2028 U+2029).
const { chromium } = require('playwright');
const payloads = [
  'normal',
  'a"b',                         // quote
  'a\\b',                        // backslash
  '</script><img src=x onerror=alert(1)>',
  '\u2028alert(document.domain)//',   // U+2028 line separator
  '\u2029x',                     // U+2029
  '"});window.__PWNED__=1;({"x":"',  // attempt to break out of the object literal
  '\\");window.__PWNED__=1;//',
];
(async () => {
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const p = await b.newPage();
  await p.addInitScript(() => { window.__log=[]; window.TelegramGameProxy={receiveEvent:(n,d)=>window.__log.push([n,typeof d])}; });
  await p.goto('about:blank');
  for (const val of payloads) {
    // EXACT real sink construction:
    const name = 'secure_storage_key_received';
    const data = JSON.stringify({ req_id: '1', value: val });  // == JSON(dictionary:)?.string
    const script = 'window.TelegramGameProxy.receiveEvent("' + name + '", ' + data + ')';
    let err=null, pwned=false;
    try { await p.evaluate(s => { window.__PWNED__=0; eval(s); window.__R=window.__PWNED__; }, script); pwned = await p.evaluate(()=>window.__R===1); }
    catch(e){ err = String(e).split('\n')[0]; }
    console.log(JSON.stringify({ valuePreview: val.slice(0,30), pwned, err }));
  }
  await b.close();
})();
