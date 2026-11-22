"""Почтовые ящики: у работающего ящика с паролем приложения есть «🔌 Проверить»
(та же ручка, что «Включить» — вход по SMTP без письма, сброс счётчика ошибок);
у входа через Google кнопки нет, а способ входа подписан в строке."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/mailboxes.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def _run(body):
    js = (
        "const calls=[],toasts=[];let reopened=0;"
        "const document={createElement:()=>({style:{},disabled:false,textContent:''})};"
        "const toast=m=>toasts.push(m);const errRu=(e,f)=>f;"
        "async function openMailboxes(){reopened++;}"
        "async function api(p,o){calls.push([p,o.method]);}"
        + _fn("mailboxAuthLabel") + _fn("mailboxCanCheck") + _fn("mailboxCheckButton") + body
    )
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_only_active_password_mailboxes_get_the_check():
    r = _run(
        "console.log(JSON.stringify([mailboxCanCheck({is_active:true,auth_type:'password'}),"
        "mailboxCanCheck({is_active:true}),mailboxCanCheck({is_active:true,auth_type:'oauth'}),"
        "mailboxCanCheck({is_active:false,auth_type:'password'}),"
        "mailboxAuthLabel({auth_type:'oauth'}),mailboxAuthLabel({})]))"
    )
    assert r == [True, True, False, False, " · вход через Google", " · пароль приложения"]


def test_check_calls_enable_and_reports_reset():
    r = _run(
        "const b=mailboxCheckButton({id:7,email:'a@b.ru',fail_count:2,is_active:true});"
        "b.onclick().then(()=>console.log(JSON.stringify({calls,toasts,reopened})))"
    )
    assert r["calls"] == [["/api/miniapp/mailboxes/7/enable", "POST"]]
    assert r["toasts"] == ["✅ Вход в a@b.ru работает, счётчик ошибок сброшен"]
    assert r["reopened"] == 1


def test_row_uses_check_for_active_mailboxes():
    assert "else if (mailboxCanCheck(mailbox)) row.append(mailboxCheckButton(mailbox));" in SRC
