"""Загрузка файлов показывала английскую техническую строку сервера.

`api()` уже знает правило: на 401 — про сессию, на 5xx — своя понятная фраза, а
не `error` из ответа. Но три пути ходят своим `fetch` (api() форсит JSON, а
multipart нужен свой Content-Type): отправка файла в чат аккаунта, импорт
`.session`/tdata и выгрузка Хранилища. Они брали `j.message || j.error` при
ЛЮБОМ неуспехе, а сервер кладёт туда «Unauthorized» (619 мест) и «Failed to
delete»/«db error» (500-е, ещё ~20). Владелец английский не читает, и на импорте
аккаунтов — одном из основных путей продукта — он получал «⚠️ Failed to …».
Выгрузка Хранилища говорила «Экспорт 500»: номер кода человеку не говорит ничего.

Правило теперь одно и живёт в `_errText`.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "mini_app" / "index.html"


def _fn(name: str) -> str:
    src = INDEX.read_text("utf-8")
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\(", src, re.M)
    assert m, f"функция {name} не найдена"
    pd, k = 0, m.end() - 1
    while k < len(src):
        if src[k] == "(":
            pd += 1
        elif src[k] == ")":
            pd -= 1
            if pd == 0:
                break
        k += 1
    i = src.index("{", k)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[m.start():j + 1]


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_errtext_never_shows_the_technical_string():
    code = _fn("_errText") + """
const cases = {
  s401: _errText({status:401}, {error:'Unauthorized'}, 'запас'),
  s500: _errText({status:500}, {error:'Failed to delete'}, 'запас'),
  s502: _errText({status:502}, {error:'db error'}, 'запас'),
  s400: _errText({status:400}, {error:'Файл слишком большой'}, 'запас'),
  s400_empty: _errText({status:400}, {}, 'запас'),
  no_json: _errText({status:500}, null, 'запас'),
};
console.log(JSON.stringify(cases));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(code)
        path = fh.name
    res = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:600]
    out = json.loads(res.stdout)
    assert out["s401"] == "Сессия истекла, перезапустите приложение"
    for key in ("s500", "s502", "no_json"):
        assert out[key] == "запас", f"{key}: техническая строка дошла до человека"
    assert out["s400"] == "Файл слишком большой", (
        "осмысленный отказ сервера потерян — человек не узнает причину")
    assert out["s400_empty"] == "запас"


def test_the_three_raw_paths_use_the_shared_rule():
    """Иначе правило снова разойдётся по местам и где-то отстанет."""
    src = INDEX.read_text("utf-8")
    assert src.count("function _errText(") == 1
    # Считаем именно места выброса: сама подпись функции тоже содержит
    # "_errText(r," и давала лишнюю единицу.
    assert src.count("new Error(_errText(") == 3, (
        "не все пути со своим fetch пользуются общим правилом")
    assert "throw new Error(j.message || j.error ||" not in src
    assert "throw new Error(j.message||j.error||" not in src
    assert "'Экспорт '+r.status" not in src, (
        "выгрузка снова показывает человеку номер кода вместо причины")
