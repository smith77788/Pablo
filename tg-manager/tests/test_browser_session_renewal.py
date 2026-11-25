"""Browser login must renew through its paired device, not empty initData."""
import re
import subprocess
from pathlib import Path


def test_browser_session_renews_through_pairing():
    source = (Path(__file__).resolve().parents[1] / 'mini_app/index.html').read_text()
    function = re.search(r'function _reAuth\(\) \{[\s\S]*?\n\}', source).group()
    script = '''
const vm=require('vm'),assert=require('assert');
const ctx={tg:{initData:''}, TK:'expired',_reAuthInFlight:null,
  calls:0,opened:0,acquireSessionToken:async()=>{ctx.calls++;return 'fresh'},
  openSSE:()=>ctx.opened++,fetchT:()=>{throw Error('unexpected Telegram auth')}};
vm.createContext(ctx);
vm.runInContext(process.argv[1],ctx);
(async()=>{await Promise.all([ctx._reAuth(),ctx._reAuth()]);
assert.equal(ctx.TK,'fresh');assert.equal(ctx.calls,1);assert.equal(ctx.opened,1);
assert.equal(ctx._reAuthInFlight,null);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script, function], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
