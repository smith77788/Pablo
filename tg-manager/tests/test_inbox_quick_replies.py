"""Входящие: быстрые ответы из шаблонов.

Оператор отвечал каждому руками, хотя половина ответов — одни и те же фразы,
а шаблоны у владельца уже лежат (экран «Шаблоны»). Теперь над полем ответа —
чипы шаблонов, «Все шаблоны» и «💾 В шаблоны». В личный ответ подставляется
имя человека, а варианты {а|б} раскрываются: скобки к человеку не уходят.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "mini_app" / "screens" / "crm_inbox.js").read_text(encoding="utf-8")


def test_reply_box_has_quick_replies():
    assert "id=\"ibQuick\"" in SRC.replace("\\'", "'").replace("'", '"')
    for fn in ("_ibQuickReplies", "inboxUseTpl", "inboxPickTpl", "inboxSaveTpl"):
        assert f"function {fn}(" in SRC, fn
    assert "/api/miniapp/templates?limit=100" in SRC


@pytest.mark.skipif(not shutil.which("node"), reason="нужен node")
def test_fill_substitutes_the_person_and_resolves_variants():
    start = SRC.index("function _ibFill(")
    end = SRC.index("\nfunction inboxUseTpl(")
    # Подстановка общая с чатом архива — screens/quick_replies.js::tplFill.
    qr = (ROOT / "mini_app" / "screens" / "quick_replies.js").read_text(encoding="utf-8")
    qs = qr.index("function tplFill(")
    fill = qr[qs:qr.index("\nfunction ", qs + 1)]
    js = ("let _ibPeer = {name: 'Анна Петрова', username: 'anna'};\n" + fill + "\n" + SRC[start:end]
          + "\nconsole.log(JSON.stringify(["
            "_ibFill('Привет, {name}! {Спасибо|Благодарим} за {вопрос|интерес}.'),"
            "_ibFill('{username}, {a|{b|c}}'),"
            "_ibFill('Цена {от 5|до 9} руб, скидка {10%}')]));")
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True,
                                    check=True).stdout)
    assert out[0].startswith("Привет, Анна! ") and "{" not in out[0] and "|" not in out[0]
    assert out[1].startswith("@anna, ") and out[1][-1] in "abc"
    assert out[2].endswith("скидка {10%}"), "скобки без вариантов — не спинтакс, не трогаем"
