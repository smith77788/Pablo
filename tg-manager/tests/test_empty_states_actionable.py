"""Пустые экраны на пути «контент/сообщения» ведут к следующему шагу (не тупик).

Жалоба «нету информации / MVP»: пустые экраны показывали «Нет …» без действия.
empty() поддерживает CTA-кнопку {label, fn}. Довели ключевые on-journey пустые
состояния до «объяснение + кнопка создать», а не тупик.

Тест держит ПОВЕДЕНИЕ, а не текст и не глиф. Дважды он краснел на безобидных
правках: заголовок «Нет DM-кампаний» переписали на «Кампаний в ЛС пока нет»
(в UI не должно быть латиницы), а глиф кнопки повтора свели к одному виду
(`↻` → `↺`). Защита при этом стояла на месте. Поэтому пустое состояние ищем по
CTA-функции, которая за ним стоит, а ветку ошибки — по границам функции, а не
по окну фиксированной длины: комментарий внутри empty() дорос, и код уехал за
границу окна в 1400 символов.
"""
from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def _empty_call_with(marker):
    # находит вызов empty(...), содержащий marker, в одной строке
    for line in HTML.splitlines():
        if "empty(" in line and marker in line:
            return line
    return ""


def _fn_src(name):
    """Исходник функции верхнего уровня: от её заголовка до следующего объявления.

    Границы функции не зависят от того, сколько внутри кода и комментариев, —
    в отличие от среза «первые N символов».
    """
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\(", HTML, re.M)
    assert m, f"{name} не определена"
    nxt = re.compile(r"^(?:async )?function \w+\(", re.M).search(HTML, m.end())
    return HTML[m.start():nxt.start() if nxt else len(HTML)]


def _cta_empty(fn):
    """Пустое состояние с кнопкой, ведущей в `fn`: есть подпись и живая функция."""
    line = _empty_call_with(fn + "()")
    assert line, f"пустого состояния с кнопкой {fn}() нет — экран стал тупиком"
    assert re.search(r"label\s*:\s*'[^']+'", line), (
        f"кнопка {fn}() без подписи:\n{line.strip()}")
    return line


def test_autoreplies_empty_has_cta():
    _cta_empty("openArModal")


def test_dm_campaigns_empty_has_cta():
    _cta_empty("openCmpModal")


def test_templates_empty_has_cta():
    _cta_empty("openTplModal")


def test_cta_targets_are_real_functions():
    for fn in ("openArModal", "openCmpModal", "openTplModal"):
        assert re.search(rf"function {fn}\(", HTML), f"{fn} не определена"


def test_empty_error_states_offer_retry_at_root():
    # На корневой вкладке ошибка загрузки должна давать «Повторить» (reload),
    # а не только «Назад» — 73 error-состояния раньше упирались в текст ошибки.
    assert re.search(r"function reloadView\(", HTML), "reloadView не определена"
    seg = _fn_src("empty")
    # условие срабатывает только для error-иконки на корневом экране
    assert "isErr && atRoot" in seg, seg[:400]
    branch = seg[seg.index("isErr && atRoot"):]
    # ветка ошибки на корне зовёт reloadView и подписана по-русски
    assert "reloadView()" in branch, branch[:400]
    assert "Повторить" in branch, branch[:400]


def test_reloadview_is_safe_on_substacks():
    # reloadView не перезагружает вкладку, когда открыт вложенный экран
    # (иначе обновился бы не тот экран). Гейт против регрессии.
    seg = _fn_src("reloadView")
    assert "STACK.length) return" in seg, seg[:300]
