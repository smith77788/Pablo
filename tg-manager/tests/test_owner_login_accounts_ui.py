"""Входы владельца видны в списке аккаунтов без ложных тревог по флоту."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_owner_login_summary_handles_count_failure_paging_and_races():
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const box={dataset:{}},status={textContent:''},pending=[];
const fleet=[{id:1,foreign_sessions:0}];
const ctx={document:{getElementById:id=>id==='accOwnerSessions'?box:status},CUR_ACCS:fleet,
  api:path=>{assert.equal(path,'/api/miniapp/owner/sessions');return new Promise((resolve,reject)=>pending.push({resolve,reject}))}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const run=()=>vm.runInContext('loadAccOwnerSessions()',ctx);
(async()=>{
let p=run();pending.shift().resolve({tracked:true,sessions:[{current:true},{current:false},{revoked:true}],limit:50});await p;
assert(status.textContent.includes('Других незавершённых входов: 1.'));
assert.equal(fleet[0].foreign_sessions,0,'owner login never marks a fleet account');
p=run();pending.shift().reject(new Error('503'));await p;
assert(status.textContent.includes('Не удалось проверить'));
assert(!status.textContent.includes(': 0'));
p=run();pending.shift().resolve({tracked:false,sessions:[{},{}],limit:2});await p;
assert(status.textContent.includes('не менее 2'));
assert(status.textContent.includes('Текущий вход не распознан'));
assert(!status.textContent.includes('Других'));
p=run();pending.shift().resolve({});await p;
assert(status.textContent.includes('Не удалось проверить'));
const old=run(),oldRequest=pending.shift(),fresh=run();
pending.shift().resolve({tracked:true,sessions:[{},{}]});await fresh;
oldRequest.resolve({tracked:true,sessions:[]});await old;
assert(status.textContent.includes(': 2.'));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(
        ['node', '-e', script, str(ROOT / 'mini_app/screens/accounts_list.js')],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_accounts_entry_and_existing_owner_screen_have_honest_failure():
    html = (ROOT / 'mini_app/index.html').read_text()
    accounts = html[html.index('id="s-accounts"'):html.index('id="accTools"')]
    assert 'id="accOwnerSessions"' in accounts
    assert 'onclick="openDevices()"' in accounts
    assert 'Устройства Telegram-аккаунтов' in accounts
    assert "owner/sessions').catch(() => ({unavailable: true}))" in html
    owner_screen = html[html.index('function ownSessHtml('):html.index('function ownGuardHtml(')]
    assert 's.unavailable' in owner_screen and 'Не удалось проверить входы' in owner_screen
    source = (ROOT / 'mini_app/screens/accounts_list.js').read_text()
    assert 'async function loadAccounts() {\n  loadAccOwnerSessions();' in source
    assert 'async function reloadAccounts() {\n  loadAccOwnerSessions();' in source


def test_successful_revocation_refreshes_accounts_summary_but_failure_does_not():
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const start=html.indexOf('async function ownSessRevoke(id) {');
const end=html.indexOf('// ── Секретное слово',start);
let refreshed=0,opened=0,confirmed=true,fail=false,requests=0;
const context={askConfirm:async()=>confirmed,tg:{HapticFeedback:{notificationOccurred:()=>{}}},toast:()=>{},errRu:()=>'',
  loadAccOwnerSessions:()=>refreshed++,openDevices:()=>opened++,
  api:async()=>{requests++;if(fail)throw new Error('503');return {revoked:1}}};
const deviceStart=html.indexOf('async function revokeDevice(id) {');
const deviceEnd=html.indexOf('// ── Сводка',deviceStart);
vm.createContext(context);vm.runInContext(html.slice(start,end)+html.slice(deviceStart,deviceEnd),context);
(async()=>{
await vm.runInContext('ownSessRevoke(7)',context);
await vm.runInContext('ownSessRevokeOthers()',context);
await vm.runInContext('revokeDevice(8)',context);
assert.equal(refreshed,3,'all success paths refresh hidden Accounts banner');
assert.equal(opened,3);
fail=true;
await vm.runInContext('ownSessRevoke(7)',context);
await vm.runInContext('ownSessRevokeOthers()',context);
await vm.runInContext('revokeDevice(8)',context);
assert.equal(refreshed,3,'failed revocation cannot refresh as success');
assert.equal(opened,3);
confirmed=false;
await vm.runInContext('ownSessRevoke(7)',context);
assert.equal(requests,6,'cancel does not submit or refresh');
assert.equal(refreshed,3);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(
        ['node', '-e', script, str(ROOT / 'mini_app/index.html')],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
