"""Сбой в приложении показывался человеку по-английски.

Любой JS-сбой в мини-аппе («Cannot read properties of undefined (reading
'slice')», «NetworkError when attempting to fetch resource») попадал прямо в
тост или в блок ошибки:

    catch(e) { toast('⚠️ '+(e.message||'Ошибка')); }

Владелец английского не читает — для него это строка мусора вместо объяснения.
При этом сообщения СЕРВЕРА приходят по-русски (их бросает `_apiRaw` из тела
ответа), и терять их нельзя: «Укажите название сети» человеку как раз нужно.

Поэтому показ ошибки идёт через `errRu`: русский текст пропускается как есть,
всё остальное заменяется русской фразой, а подробность уходит в консоль. Здесь
берётся НАСТОЯЩАЯ функция из index.html и исполняется в node.
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


def _fn_source(name: str) -> str:
    src = INDEX.read_text("utf-8")
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\(", src, re.M)
    assert m, f"функция {name} не найдена"
    i = src.index("{", m.end() - 1)
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


HARNESS = r"""
'use strict';
__ERRRU__
const out = {};
// Сообщение сервера — по-русски, его показываем как есть.
out.server = errRu(new Error('Укажите название сети'), 'Ошибка');
// Сбой JS — английский, человеку он ничего не говорит.
out.js = errRu(new TypeError("Cannot read properties of undefined (reading 'slice')"), 'Не загрузилось');
// Без запасной фразы — всё равно по-русски.
out.bare = errRu(new TypeError('boom'));
// Пустая ошибка и мусор вместо ошибки не должны ронять показ.
out.empty = errRu(new Error(''), 'Пусто');
out.nothing = errRu(null, 'Нет ошибки');
out.notAnError = errRu('строка вместо ошибки', 'Запасная');
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_errru_keeps_the_server_message_and_hides_the_js_one():
    src = HARNESS.replace("__ERRRU__", _fn_source("errRu"))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(src)
        path = fh.name
    res = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:800]
    out = json.loads(res.stdout)

    assert out["server"] == "Укажите название сети", (
        "русское сообщение сервера потерялось — человек не узнает, что исправить")
    for key in ("js", "bare", "empty", "nothing", "notAnError"):
        got = out[key]
        assert re.search("[а-яёА-ЯЁ]", got), f"{key}: не по-русски: {got!r}"
        assert not re.search("[A-Za-z]{4}", got), f"{key}: английские слова: {got!r}"


def test_no_screen_shows_a_raw_exception_text():
    """Иначе один забытый `catch` возвращает английский текст на экран."""
    src = INDEX.read_text("utf-8")
    raw = []
    for m in re.finditer(r"(toast|errHtml|empty|textContent\s*=|innerHTML\s*=)"
                         r"[^;\n]{0,80}?\b(e|err|ex|exc)\.message", src):
        line = src[:m.start()].count("\n") + 1
        frag = " ".join(m.group(0).split())
        if "errRu(" in frag:
            continue
        raw.append(f"{line}: {frag}")
    assert not raw, (
        "показ ошибки идёт мимо errRu — на экран снова попадёт английский текст "
        "исключения:\n" + "\n".join(raw[:20]))


def test_the_helper_exists_in_one_place():
    src = INDEX.read_text("utf-8")
    assert src.count("function errRu(") == 1
    assert src.count("errRu(") > 300, (
        "показ ошибок перестал ходить через errRu — проверка выше ослепнет")


def test_no_call_is_glued_to_another_call():
    """Массовая замена склеивает имена — и путь ошибки падает молча.

    Я это и сделал: вместо `toast(errRu(e,'Ошибка'))` в 23 местах вышло
    `toasterrRu(e,'Ошибка')` (а в одном — `errHtmlerrRu`). Синтаксис такой
    склейки верный, `node --check` молчит, в браузере она не исполняется,
    пока пользователь не попадёт на ошибку, — и тогда вместо сообщения он
    получает `ReferenceError: toasterrRu is not defined`, то есть пустой
    экран вместо объяснения. Нашлось это случайно, тестом другого экрана.

    Проверяем все помощники показа: имя помощника не может быть склеено с
    предшествующим идентификатором.
    """
    files = [INDEX] + sorted((INDEX.parent / "screens").glob("*.js"))
    helpers = ("errRu", "errHtml", "toast", "esc", "num", "txt")
    glued = []
    for f in files:
        src = f.read_text("utf-8")
        for h in helpers:
            for m in re.finditer(r"[A-Za-z0-9_$]" + h + r"\(", src):
                frag = src[max(0, m.start() - 24):m.end()]
                # Законные случаи: имя функции само кончается на помощника
                # (`_opEsc(`, `window.toast(`) — слева тогда стоит `.` или
                # имя объявлено. Отсекаем только склейку двух ВЫЗОВОВ.
                if frag.rstrip()[-len(h) - 2:-len(h) - 1] == ".":
                    continue
                name = re.search(r"([A-Za-z0-9_$]+)" + h + r"\($", frag)
                if not name:
                    continue
                full = name.group(1) + h
                if re.search(r"(function\s+|const\s+|let\s+|var\s+)" + re.escape(full) + r"\b", src):
                    continue   # такая функция и правда объявлена
                glued.append(f"{f.name}:{src[:m.start()].count(chr(10)) + 1}: {full}(")
    assert not glued, (
        "вызов склеен с другим вызовом — путь ошибки упадёт на ReferenceError:\n"
        + "\n".join(glued[:20]))
