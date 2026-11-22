"""Вход по номеру: код с пробелами и дефисами принимается как цифры, а
полные пять цифр подтверждаются сами — один раз на значение."""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "mini_app/screens/account_login.js").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app/index.html").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def test_digits_and_single_autosubmit():
    js = ("let sent=0;function accPhoneCode(){sent++;}" + _fn("accCodeDigits") + _fn("accCodeTyped")
          + "const d=[accCodeDigits('1 2-3 4 5'),accCodeDigits(null)];"
            "accCodeTyped({value:'123'});accCodeTyped({value:'1 2 3 4 5'});accCodeTyped({value:'12345'});"
            "const once=sent;accCodeTyped.last='';accCodeTyped({value:'12345'});"
            "console.log(JSON.stringify({d,once,sent}))")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r == {"d": ["12345", ""], "once": 1, "sent": 2}


def test_wired_into_field_and_flow():
    assert 'autocomplete="one-time-code" id="accPhoneCode"' in HTML and 'oninput="accCodeTyped(this)"' in HTML
    assert "const code=accCodeDigits(document.getElementById('accPhoneCode').value);" in SRC
    assert SRC.count("accCodeTyped.last='';") >= 2


def test_enter_submits_number_and_passwords():
    for idv, fn in (("accPhoneNum", "accPhoneStart"), ("accPhone2fa", "accPhone2fa"), ("accQr2fa", "accQr2fa")):
        assert f"id=\"{idv}\" onkeydown=\"if(event.key==='Enter'){{event.preventDefault();{fn}()}}\"" in HTML, idv
