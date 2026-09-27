"""Галочку можно нажать пальцем, а не только курсором.

Замер в Chromium на экране 360×780: 23 переключателя, сама галочка — 13×13
точек, а вся строка-цель у тринадцати из них 17–21 точку по высоте. Восемь из
этих тринадцати стоят на экране массового инвайта, где переключатель решает,
КОГО позовут: промах там меняет состав аудитории у самой баноопасной операции
продукта.

Лечится строкой целиком: `.chk-row` растягивает цель на всю ширину и доводит
её до 40 точек по высоте, галочка внутри — 18×18. После правки строк ниже
32 точек не осталось (тот же замер).

Проверка исходником, а не браузером: браузерный прогон здесь стоил бы минуты
на каждый запуск, а ломается это ровно одним способом — новой галочкой без
класса.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_source

SRC = miniapp_source()

# Компоненты со своей геометрией: строка списка и город-чип уже крупные,
# растягивать их до 40 точек значит сломать их же раскладку.
OWN_GEOMETRY = ("row tap", "gp-city-chip")

LABEL_WITH_CHECKBOX = re.compile(r'<label\b([^>]*)>(\s*)<input type="checkbox"')


def test_every_checkbox_row_is_thumb_sized():
    bad = []
    for m in LABEL_WITH_CHECKBOX.finditer(SRC):
        attrs = m.group(1)
        if any(c in attrs for c in OWN_GEOMETRY):
            continue
        if "chk-row" not in attrs:
            bad.append(SRC[m.start():m.start() + 120].replace("\n", " "))
    assert not bad, (
        "строка-переключатель без класса chk-row — цель останется в 17 точек:\n"
        + "\n".join(bad)
    )


def test_there_are_checkbox_rows_at_all():
    """Страховка от вырождения: если разметка переедет и регулярка перестанет
    что-либо находить, верхний тест станет зелёным, ничего не проверяя."""
    found = LABEL_WITH_CHECKBOX.findall(SRC)
    assert len(found) >= 30, f"нашлось всего {len(found)} строк с галочкой — проверка смотрит не туда"


def test_the_rule_actually_enlarges_the_target():
    m = re.search(r"\.chk-row\{([^}]*)\}", SRC)
    assert m, "правило .chk-row исчезло"
    mh = re.search(r"min-height:(\d+)px", m.group(1))
    assert mh and int(mh.group(1)) >= 40, f"цель ниже 40 точек: {m.group(1)}"
    box = re.search(r"\.chk-row input\[type=checkbox\]\{([^}]*)\}", SRC)
    assert box, "правило на размер галочки исчезло"
    for dim in ("width", "height"):
        d = re.search(dim + r":(\d+)px", box.group(1))
        assert d and int(d.group(1)) >= 18, f"галочка меньше 18 точек: {box.group(1)}"


def test_no_inline_override_shrinks_the_checkbox_again():
    """Инлайновый стиль сильнее класса: вернётся `width:16px` на input — и
    правило перестанет действовать молча."""
    shrunk = []
    for m in LABEL_WITH_CHECKBOX.finditer(SRC):
        if "chk-row" not in m.group(1):
            continue  # у компонентов со своей геометрией размер их собственный
        tag = SRC[m.end() - len('<input type="checkbox"'):]
        tag = tag[:tag.index(">") + 1]
        if re.search(r'style="[^"]*(?:width|height):1[0-7]px', tag):
            shrunk.append(tag[:120])
    assert not shrunk, f"инлайновый размер гасит .chk-row: {shrunk[:3]}"
