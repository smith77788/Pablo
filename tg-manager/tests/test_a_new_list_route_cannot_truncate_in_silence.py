"""Новая ручка-список не может молча обрезать выдачу. Храповик.

РАЗРЫВ. За две итерации десяток списков переведён на страницы с честным
итогом (`_list_limit` + `_page_meta`), но ничто не мешает завести одиннадцатый
с жёстким `LIMIT 50` в тексте запроса. Именно так и появились все найденные:
никто не выбирал «обрезать молча» — потолок ставился как «чтобы не тащить всё»,
и экран потом выдавал кусок за целое («Посты (50)», «Всего планов 200» над
пятьюдесятью строками).

ПОЧЕМУ СЧЁТ, А НЕ СПИСОК ИМЁН. Реестр имён руками ловит только то, что кто-то
вспомнил в него дописать — в этом проекте так дважды проходили мимо проверки
целые пути (`auto_merge_with_confidence`, реестр дверей идемпотентности).
Здесь храповик: число молчаливых потолков в слое мини-аппа может только
уменьшаться. Новый — красный, и красным становится у того, кто его завёл.

Потолок остаётся законным там, где он не про список владельца: `LIMIT 1`,
`LIMIT 10` под «частые причины», выборки под сводку. Поэтому считаются только
потолки от двадцати: меньше — это про виджет, больше — почти всегда про
список, который человек листает.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Столько молчаливых потолков от 20 было на 25.11.2026. Число может только
# убывать: перевели список на страницы — уменьшите его тем же коммитом.
BASELINE = 75

_LIMIT = re.compile(r"\bLIMIT\s+(\d+)")


def _silent_ceilings() -> list[str]:
    """Потолок без конверта страницы рядом: окно ±40/25 строк — это тело ручки."""
    out = []
    for path in sorted((ROOT / "services").glob("mini_app*.py")):
        lines = path.read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(lines):
            m = _LIMIT.search(line)
            if not m or int(m.group(1)) < 20:
                continue
            window = "\n".join(lines[max(0, i - 40):i + 25])
            if "_page_meta(" in window:
                continue
            out.append(f"{path.name}:{i + 1} LIMIT {m.group(1)}")
    return out


def test_the_number_of_silent_ceilings_only_goes_down():
    found = _silent_ceilings()
    assert len(found) <= BASELINE, (
        "появился новый молчаливый потолок в списке мини-аппа: выдача обрежется, "
        "а экран выдаст кусок за целое. Поставьте _list_limit/_list_offset и "
        "_page_meta, либо, если это не список владельца, оставьте LIMIT меньше "
        "двадцати.\n" + "\n".join(found[-12:]))


def test_the_baseline_is_not_stale():
    """Опустили потолков больше, чем записано — обновите число, иначе храповик встал."""
    found = _silent_ceilings()
    assert len(found) >= BASELINE - 5, (
        f"молчаливых потолков осталось {len(found)}, а храповик держит {BASELINE}: "
        "запишите новое число, иначе он перестанет ловить новые")


def test_the_measurer_sees_a_known_unhealthy_case():
    """Пробник на заведомо больном примере: детектор без него доверия не стоит."""
    src = "\n".join([
        "    async def bad_list(request):",
        '        rows = await pool.fetch("SELECT * FROM t WHERE owner_id=$1 LIMIT 50", uid)',
        '        return _json_resp({"items": rows})',
    ])
    assert _LIMIT.search(src) and int(_LIMIT.search(src).group(1)) >= 20
    assert "_page_meta(" not in src, "пробник обязан быть именно больным примером"


def test_the_measurer_does_not_flag_a_paged_route():
    """И на заведомо здоровом: ручка со страницей не считается находкой."""
    src = "\n".join([
        "    async def good_list(request):",
        "        limit = _list_limit(request, 20, 200)",
        '        rows = await pool.fetch("SELECT * FROM t LIMIT $2 OFFSET $3", uid, limit, 0)',
        '        return _json_resp({"items": rows, "page": _page_meta(0, limit, len(rows), 7)})',
    ])
    assert not _LIMIT.search(src), "в здоровой ручке потолок приходит параметром, а не числом"
