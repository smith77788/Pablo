"""Exercise real settings UI: explicit patching, failures, and stale responses."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_settings_ui_preserves_unknown_and_unchanged_values():
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements=new Map();const element=id=>{if(!elements.has(id))elements.set(id,{textContent:'',innerHTML:'',disabled:false,style:{}});return elements.get(id)};
let pending=[],posts=[],confirmations=[],toasts=[];
const ctx={document:{getElementById:element},showModal:()=>{},toast:x=>toasts.push(x),copyText:()=>{},errRu:(e,f)=>e.message||f,esc:x=>String(x??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),askConfirm:async text=>{confirmations.push(text);return true},api:(url,opts)=>{if(opts){posts.push({url,body:JSON.parse(opts.body)});return Promise.resolve({ok:true})}return new Promise((resolve,reject)=>pending.push({resolve,reject,url}))}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const run=x=>vm.runInContext(x,ctx);
const data={bot:{first_name:'<bad>',active_usernames:['mybot','aliasbot']},invite_link:'https://t.me/botinvite/mybot',settings:{can_join_groups:null,inline_query_placeholder:'old',callback_query_mode:null},fields:[{key:'can_join_groups',label:'Группы',type:'boolean'},{key:'inline_query_placeholder',label:'Подсказка',type:'string'},{key:'callback_query_mode',label:'Нажатия',type:'enum',choices:['default','auto','inline']},{key:'inline_feedback_share_denominator',label:'Результаты',type:'integer',choices:[0,1,10]}]};
(async()=>{
let p=run('openBotSettings(7)');pending.shift().resolve(data);await p;
assert.equal(run('Object.keys(BOT_SETTINGS.changed).length'),0);
assert(element('botSettingsFields').innerHTML.includes('неизвестно'));
assert(element('botSettingsFields').innerHTML.includes('value="false"'));
assert.equal(element('botSettingsIdentity').textContent,'<bad> · @mybot · @aliasbot');
assert.equal(element('botSettingsInvite').href,'https://t.me/botinvite/mybot');
await run('saveBotSettings()');assert.equal(posts.length,0,'opening form must not change settings');
run("botSettingsChange(0,'false');botSettingsChange(1,'');botSettingsChange(3,'0')");
p=run('saveBotSettings()');await new Promise(setImmediate);
assert.deepEqual(posts[0].body,{settings:{can_join_groups:false,inline_query_placeholder:'',inline_feedback_share_denominator:0}});
assert(!Object.hasOwn(posts[0].body.settings,'callback_query_mode'));
pending.shift().reject(new Error('offline'));await p;
assert(element('botSettingsStatus').textContent.includes('Настройки применены'));
const old=run('openBotSettings(7)');const request=pending.shift();
const fresh=run('openBotSettings(8)');pending.shift().resolve({...data,bot:{first_name:'Другой'}});await fresh;
request.resolve(data);await old;
assert.equal(element('botSettingsIdentity').textContent,'Другой');
p=run('openBotSettings(9)');pending.shift().reject(new Error('недоступно'));await p;
assert.equal(element('botSettingsSave').disabled,true);
assert.equal(element('botSettingsInviteBlock').style.display,'none');
assert(element('botSettingsStatus').textContent.includes('недоступно'));
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script, str(ROOT/'mini_app/screens/bot_settings.js')], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_bot_profile_has_settings_entry_and_script():
    detail = (ROOT/'mini_app/screens/bot_detail.js').read_text()
    html = (ROOT/'mini_app/index.html').read_text()
    assert 'openBotSettings(${Number(bid)})' in detail
    assert 'screens/bot_settings.js' in html
    assert 'id="mo-bot-settings"' in html
