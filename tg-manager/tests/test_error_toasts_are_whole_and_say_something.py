"""Тосты ошибок не обрезают объяснение сервера и не говорят голое «Ошибка».

`errRu(e,'Ошибка').substring(0,60)` стояло в восьми десятках мест: русская
причина отказа от сервера («Сначала добавьте прокси для аккаунтов …»)
резалась посреди слова, хотя тост и так переносит строки и закрывается
крестиком. А при сбое без текста человек видел одно слово «Ошибка» —
без подсказки, что делать дальше.
"""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "mini_app"
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
FILES = [ROOT / "index.html", *sorted((ROOT / "screens").glob("*.js"))]


def _errru():
    m = re.search(r"\nfunction errRu\([\s\S]*?\n}\n", INDEX)
    assert m
    return m.group(0)


def test_short_truncation_is_gone():
    bad = []
    for p in FILES:
        for m in re.finditer(r"errRu\([^()]*\)\.(?:substring|slice)\(0, ?(\d+)\)", p.read_text(encoding="utf-8")):
            if int(m.group(1)) < 120:
                bad.append(f"{p.name}: {m.group(0)}")
    assert not bad, bad


def test_bare_word_fallback_becomes_a_sentence():
    js = _errru() + """
      console.log(JSON.stringify([
        errRu(new TypeError('x is undefined'), 'Ошибка'),
        errRu(null, 'Ошибка'),
        errRu(new Error('Сначала добавьте прокси'), 'Ошибка'),
        errRu(new TypeError('boom'), 'Не удалось сохранить'),
      ]))"""
    out = subprocess.run(["node", "-e", js], check=False, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    a, b, server, own = json.loads(out.stdout)
    assert a == b == "Что-то пошло не так, попробуйте ещё раз"
    assert server == "Сначала добавьте прокси"
    assert own == "Не удалось сохранить"
