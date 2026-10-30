"""Промах по словарю не печатает человеку сырое значение.

ЧТО СЛУЧИЛОСЬ. По всему мини-аппу стоял один и тот же оборот:

    ${EXP_STATUS[e.status]||e.status}

Словарь переводит код в русскую подпись, а запасной вариант — сам код. Пока
код есть, это разумно: незнакомый статус хотя бы виден. Но если поля нет —
старая запись, NULL в колонке, частичный ответ, — запасным вариантом
становится `undefined`, и ровно это слово владелец и видит: «цена ⭐undefined»,
«Окно активности undefined:00–undefined:00», «задержка undefined минут».
Машинное слово в тексте для человека читается как поломка, причём непонятно
чья — и позвонить по этому поводу некуда.

Найдено обходом экранов (`deploy/scripts/render_miniapp.mjs`), который ищет в
показанном экране «undefined» и «[object Object]». Мест оказалось пятнадцать —
один и тот же оборот, размноженный копированием.

ЧТО ДЕРЖИТ ЭТА ПРОВЕРКА. Запасной вариант обязан иметь свой запасной: либо
`||x.y||'—'`, либо что-то ещё осмысленное. Оборот `${СЛОВАРЬ[x.y]||x.y}` без
третьего звена в разметке больше не встречается.

Проверка текстовая и дешёвая: сам оборот — это текст, а не поведение, и
сломать его обратно можно только написав его заново.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
_INDEX = ROOT / "mini_app" / "index.html"
_SCREENS = ROOT / "mini_app" / "screens"

#: ${СЛОВАРЬ[x.поле]||x.поле} — и НИЧЕГО после: ровно тот оборот, который
#: печатает `undefined`, когда поля нет.
_RAW_FALLBACK = re.compile(r"\$\{([A-Z_]+)\[([a-z]+)\.([a-z_]+)\]\|\|\2\.\3\}")

#: тот же оборот, но с третьим звеном — так и надо
_WITH_DASH = re.compile(r"\$\{([A-Z_]+)\[([a-z]+)\.([a-z_]+)\]\|\|\2\.\3\|\|")


def _sources() -> list[tuple[str, str]]:
    out = [(_INDEX.name, _INDEX.read_text(encoding="utf-8"))]
    out += [(f.name, f.read_text(encoding="utf-8")) for f in sorted(_SCREENS.glob("*.js"))]
    return out


def test_no_lookup_falls_back_to_a_field_that_can_be_absent():
    bad = []
    for name, src in _sources():
        for m in _RAW_FALLBACK.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            bad.append(f"{name}:{line} {m.group(0)}")
    assert not bad, (
        "запасной вариант у промаха по словарю — само поле, и без поля "
        "владелец увидит слово undefined:\n  " + "\n  ".join(bad[:10]))


def test_the_measure_still_sees_the_shape_it_forbids():
    """Мерка на заведомо больном образце: иначе «ноль находок» ничего не значит."""
    probe = "<div>${EXP_STATUS[e.status]||e.status}</div>"
    assert _RAW_FALLBACK.search(probe), "мерка перестала видеть запрещённый оборот"
    good = "<div>${EXP_STATUS[e.status]||e.status||'—'}</div>"
    assert not _RAW_FALLBACK.search(good), "мерка ругается на исправленный оборот"


def test_the_fixed_shape_is_actually_in_the_app():
    """Исправление не должно быть «удалили оборот вместе с подписью»."""
    src = _INDEX.read_text(encoding="utf-8")
    n = len(_WITH_DASH.findall(src))
    assert n >= 15, (
        f"оборотов с честным запасным вариантом осталось {n} — похоже, их не "
        "починили, а вырезали вместе с русскими подписями статусов")
