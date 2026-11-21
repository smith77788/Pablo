"""Кампания в ЛС: копия с тем же текстом и настройками, но без аудитории.

Удачный текст переносили в новую кампанию руками, а темп, лимит на
аккаунт, тихие часы и запасное имя набирали заново. Аудитория при этом
копироваться НЕ должна: та же аудитория второй раз — повторная ЛС тем же
людям. Проверяем настоящую функцию в node на заглушках DOM.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\nfunction " + name + r"\([^)]*\)[\s\S]*?\n}\n", HTML)
    assert m, name
    return m.group(0)


def test_list_row_offers_copy():
    assert 'onclick="copyDm(${c.id})"' in HTML


def test_copy_carries_text_and_settings_but_not_audience():
    if not shutil.which("node"):
        pytest.skip("нет node")
    camp = {"id": 5, "name": "Весна", "text_template": "Привет, {name}!", "target_type": "bot_users",
            "params": {"pace": "slow", "per_account_daily": 7, "media_url": "https://x/y.jpg",
                       "quiet_hours": False, "name_fallback": "друг"}}
    js = (
        "const els = {}; const el = id => els[id] || (els[id] = {id, value:'', checked:true, textContent:'', style:{}});\n"
        "const document = {getElementById: el};\n"
        "let CUR_DM_CAMPAIGNS = [" + json.dumps(camp) + "]; let CMP_EDIT_ID = 'x';\n"
        "function toast(){}\n"
        "function openCmpModal(){ CMP_EDIT_ID = null; el('cmpTargetType').value = 'all_bots'; }\n"
        + _fn("copyDm") +
        "\ncopyDm(5);\n"
        "const g = id => els[id] || {};\n"
        "console.log(JSON.stringify({name:g('cmpName').value, text:g('cmpText').value,"
        " pace:g('cmpPace').value, per:g('cmpPerAcc').value, media:g('cmpMedia').value,"
        " quiet:g('cmpQuiet').checked, fb:g('cmpNameFb').value, tt:g('cmpTargetType').value,"
        " edit:CMP_EDIT_ID, note:g('cmpErr').textContent}));"
    )
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True,
                                    check=True).stdout)
    assert out["text"] == "Привет, {name}!" and "копия" in out["name"]
    assert (out["pace"], out["per"], out["media"], out["quiet"], out["fb"]) == (
        "slow", 7, "https://x/y.jpg", False, "друг")
    # Аудитория сброшена на выбор по умолчанию, а не взята из исходной.
    assert out["tt"] == "all_bots"
    # Это НОВАЯ кампания, а не правка исходной.
    assert out["edit"] is None
    assert "ещё раз" in out["note"]
