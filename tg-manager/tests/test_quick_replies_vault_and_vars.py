"""Быстрые ответы в переписке от своего имени и кнопки-переменные в шаблоне.

Во «Входящих» чипы шаблонов над полем ответа уже были, а в чате архива
диалогов (ответ от вашего имени) ответ печатался руками каждый раз. Теперь:
чипы быстрых ответов и там, «частые первыми», с подстановкой имени
собеседника; в редакторе шаблона — кнопки {name}/{username}/варианты, которые
вставляют подстановку туда, где курсор.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
QR = (ROOT / "mini_app" / "screens" / "quick_replies.js").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
INBOX = (ROOT / "mini_app" / "screens" / "crm_inbox.js").read_text(encoding="utf-8")

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")


def _node(js: str):
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _fn(name: str) -> str:
    start = QR.index(f"function {name}(")
    nxt = QR.find("\nfunction ", start + 1)
    nxt2 = QR.find("\nasync function ", start + 1)
    ends = [e for e in (nxt, nxt2) if e > 0]
    return QR[start:min(ends) if ends else len(QR)]


def test_script_is_loaded_after_vault_dialogs():
    a = HTML.index('<script src="screens/vault_dialogs.js"></script>')
    b = HTML.index('<script src="screens/quick_replies.js"></script>')
    assert b > a


def test_vault_chat_has_quick_reply_row_and_it_is_rendered_on_open():
    assert 'id="vchatQuick"' in HTML
    # Ряд стоит над полем ответа, а не где-то ещё на экране.
    assert HTML.index('id="vchatQuick"') < HTML.index('id="vchatComposer"')
    start = HTML.index("async function openVaultChat(")
    body = HTML[start:HTML.index("\n}\n", start)]
    assert "vaultQuickReplies()" in body
    for fn in ("vaultQuickReplies", "vaultUseTpl", "vaultPickTpl", "vaultSaveTpl"):
        assert f"function {fn}(" in QR, fn
    assert "/api/miniapp/templates?limit=100" in QR


def test_template_editor_has_variable_buttons():
    assert 'id="tplVars"' in HTML
    start = HTML.index("function openTplModal(")
    assert "tplRenderVarsBar()" in HTML[start:HTML.index("\n}\n", start)]
    for tok in ("{name}", "{first_name}", "{last_name}", "{username}"):
        assert f"token: '{tok}'" in QR, tok


def test_more_composers_offer_insert_template():
    for target in ("schText", "netbcText", "chPostText"):
        assert f"openTemplates('{target}')" in HTML, target


def test_inserting_a_template_is_counted_everywhere():
    start = HTML.index("function useTpl(")
    assert "tplNoteUse(id)" in HTML[start:HTML.index("\n}\n", start)]
    assert "tplNoteUse(t.id)" in INBOX
    assert "tplByUse(_ibTpl)" in INBOX


@needs_node
def test_fill_substitutes_person_and_resolves_variants():
    js = _fn("tplFill") + """
console.log(JSON.stringify([
  tplFill('Привет, {name}! {Спасибо|Благодарим} за {вопрос|интерес}.', {name: 'Анна Петрова', username: 'anna'}),
  tplFill('{first_name} {last_name}, {username}', {name: 'Анна Петрова', username: '@anna'}),
  tplFill('Привет, {name}!', {name: '', username: 'bob'}),
  tplFill('Привет, {name}!', {}, 'друг'),
  tplFill('скидка {10%}, {a|{b|c}}', {name: 'X'}),
]));"""
    out = _node(js)
    assert out[0].startswith("Привет, Анна! ") and "{" not in out[0] and "|" not in out[0]
    assert out[1] == "Анна Петрова, @anna"
    assert out[2] == "Привет, @bob!", "без имени — ник"
    assert out[3] == "Привет, друг!", "ни имени, ни ника — запасное слово"
    assert out[4].startswith("скидка {10%}, ") and out[4][-1] in "abc"


@needs_node
def test_frequent_templates_go_first_and_ties_keep_server_order():
    js = _fn("tplByUse") + """
const list = [{id: 1}, {id: 2}, {id: 3}, {id: 4}];
console.log(JSON.stringify([
  tplByUse(list, {3: 5, 2: 1}).map(t => t.id),
  tplByUse(list, {}).map(t => t.id),
  list.map(t => t.id),
]));"""
    out = _node(js)
    assert out[0] == [3, 2, 1, 4]
    assert out[1] == [1, 2, 3, 4]
    assert out[2] == [1, 2, 3, 4], "исходный массив не меняется"


@needs_node
def test_insert_at_cursor_keeps_the_rest_of_the_text():
    js = "class Event { constructor(t) { this.type = t; } }\n" + _fn("tplInsertAtCursor") + """
const el = {value: 'Привет, !', selectionStart: 8, selectionEnd: 8, fired: 0,
  focus() {}, setSelectionRange(a) { this.pos = a; }, dispatchEvent() { this.fired++; }};
tplInsertAtCursor(el, '{name}');
console.log(JSON.stringify([el.value, el.pos, el.fired]));"""
    assert _node(js) == ["Привет, {name}!", 14, 1]


@needs_node
def test_traits_detect_personal_and_variants():
    js = _fn("tplTraits") + """
console.log(JSON.stringify([tplTraits('Привет, {name}'), tplTraits('{А|Б}'), tplTraits('скидка {10%}')]));"""
    out = _node(js)
    assert out[0] == {"personal": True, "variants": False}
    assert out[1] == {"personal": False, "variants": True}
    assert out[2] == {"personal": False, "variants": False}
