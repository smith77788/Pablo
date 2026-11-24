"""Догрузка списка: одна дверь на фронте и один конверт страницы на сервере.

РАЗРЫВ. Почти каждый список мини-аппа просил у сервера первые 30–100 строк и
рисовал их БЕЗ единого слова о том, что строк больше. «Показано 30» и «всего
30» выглядят на экране одинаково: владелец с восьмьюдесятью ботами видел
тридцать и считал, что у него тридцать. Измерено до правки: из 55 файлов
экранов кнопку догрузки имели три, при этом лимит в запросе стоял у восемнадцати.

Правило: подпись «показано N из M» обязательна, кнопка — только когда звать
есть куда, а форма ответа у всех списков одна (`_page_meta`).
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
KIT = (ROOT / "mini_app" / "screens" / "ui_kit.js").read_text(encoding="utf-8")
CSS = (ROOT / "mini_app" / "ui_kit.css").read_text(encoding="utf-8")
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")


def _run(js: str):
    src = KIT + "\n" + js
    return json.loads(subprocess.check_output(["node", "-e", src], text=True))


def test_label_never_passes_a_capped_list_off_as_the_whole_list():
    out = _run("""
console.log(JSON.stringify({
  capped: UI.more({ shown: 30, total: 80, fn: 'loadMoreX' }),
  whole: UI.more({ shown: 80, total: 80, fn: 'loadMoreX' }),
  nothing: UI.more({ shown: 0, total: 0, fn: 'loadMoreX' }),
  unknown_more: UI.more({ shown: 30, total: null, fn: 'loadMoreX', maybeMore: true }),
  unknown_end: UI.more({ shown: 12, total: null, fn: 'loadMoreX', maybeMore: false }),
}));
""")
    assert "Показано 30 из 80" in out["capped"], "обрезанный список обязан назвать итог"
    assert "Загрузить ещё" in out["capped"]
    assert "Загрузить ещё" not in out["whole"], "звать некуда — кнопки быть не должно"
    assert "Показано всё: 80" in out["whole"]
    assert out["nothing"] == "", "у пустого списка подпись о количестве не нужна"
    # Сервер не всегда может посчитать итог дёшево. Тогда выдумывать его нельзя.
    assert "из" not in out["unknown_more"] and "Показано 30" in out["unknown_more"]
    assert "Загрузить ещё" in out["unknown_more"]
    assert "Загрузить ещё" not in out["unknown_end"]


def test_the_handler_name_is_a_name_and_not_code_from_data():
    out = _run("""
console.log(JSON.stringify({
  bad: UI.more({ shown: 1, total: 9, fn: "x();alert('я')" }),
  good: UI.more({ shown: 1, total: 9, fn: 'loadMoreX' }),
}));
""")
    assert "alert(" not in out["bad"], "имя обработчика пришло кодом и было подставлено"
    assert "loadMoreX()" in out["good"]


def test_the_label_text_from_data_is_escaped():
    out = _run("""
console.log(JSON.stringify({
  h: UI.more({ shown: 5, total: 5, allLabel: '<b>всё</b>' }),
}));
""")
    assert "&lt;b&gt;" in out["h"], "подпись из данных обязана остаться текстом"


def test_the_styles_exist_so_the_door_is_not_invisible():
    for cls in (".ui-more{", ".ui-more-note{", ".ui-more-btn{"):
        assert cls in CSS, cls


def test_server_envelope_is_one_shape():
    assert "def _page_meta(" in API, "конверта страницы нет — каждая ручка снова своя"
    body = API[API.index("def _page_meta("):]
    body = body[:body.index("\ndef ", 1)]
    assert '"has_more"' in body and '"shown"' in body and '"offset"' in body
    # total необязателен: не у каждого списка счёт по всей выборке дёшев.
    assert "if total is not None" in body
    assert "shown >= limit" in body, (
        "без total «есть ещё» определяется полной страницей, иначе догрузка "
        "обрывается на первой же странице")
