"""Массовый инвайт: темп, способ, режим объёма, размер захода, лимит на
аккаунт и текст ссылки возвращаются с прошлого удачного запуска, а
рискованные переключатели не запоминаются никогда."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/invite.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def _consts():
    m = re.search(r"\nconst _INV_PREF_FIELDS = \{[\s\S]*?\};\n", SRC)
    assert m
    return m.group(0)


def _run(body):
    js = (
        "const prefs={};const saveFormPrefs=(k,o)=>{prefs[k]=o;};const loadFormPrefs=k=>prefs[k]||{};"
        "function sel(v,opts){return {tagName:'SELECT',value:v,options:opts.map(x=>({value:x}))};}"
        "const els={massInvitePace:sel('auto',['auto','slow','normal','fast']),"
        "massInviteMethod:sel('direct',['direct','link','admin']),massInviteVolMode:sel('',['','progressive']),"
        "massInviteBatch:{tagName:'INPUT',value:''},massInvitePerAcc:{tagName:'INPUT',value:''},"
        "massInviteLinkMsg:{tagName:'TEXTAREA',value:''}};"
        "const document={getElementById:id=>els[id]};"
        + _consts() + _fn("_invSavePrefs") + _fn("_invApplyPrefs") + body
    )
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_safe_settings_round_trip_and_risky_are_not_kept():
    r = _run(
        "_invSavePrefs({pace:'slow',invite_method:'link',volume_mode:'progressive',batch_size:3,"
        "per_account_limit:15,link_message:'Привет {link}',one_pass:true,include_risky:true,skip_invited:false});"
        "_invApplyPrefs();const v={};for(const k in els)v[k]=els[k].value;"
        "console.log(JSON.stringify({v,saved:prefs.mass_invite}))"
    )
    assert r["v"] == {"massInvitePace": "slow", "massInviteMethod": "link", "massInviteVolMode": "progressive",
                      "massInviteBatch": "3", "massInvitePerAcc": "15", "massInviteLinkMsg": "Привет {link}"}
    for risky in ("one_pass", "include_risky", "skip_invited"):
        assert risky not in r["saved"]


def test_unknown_select_value_is_not_applied():
    r = _run("prefs.mass_invite={pace:'warp'};_invApplyPrefs();console.log(JSON.stringify(els.massInvitePace.value))")
    assert r == "auto"


def test_wired_into_open_and_successful_launch():
    open_fn = _fn("openMassInvite")
    assert open_fn.index("'massInvitePerAcc'") < open_fn.index("_invApplyPrefs();")
    launch = _fn("submitMassInvite")
    assert launch.index("_invSavePrefs(body);") > launch.index("api('/api/miniapp/mass_invite'")
    assert "аккаунт(ов)" not in SRC and "прохода(ов)" not in SRC
