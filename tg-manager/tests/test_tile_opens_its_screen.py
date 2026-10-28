"""Плитка, которая запускает функцию, но не приводит на экран, — немая кнопка.

ЧТО БЫЛО СЛОМАНО. Плитка «🔍 Дубликаты» в управлении контактами вызывала
`detectDuplicates()`. Функция уходила в API, получала список дублей и писала HTML
в `#dupBody` — элемент внутри экрана `s-uchduplicates`, который в этот момент был
СКРЫТ и никем не открывался. Пользователь нажимал плитку и не видел ровным счётом
ничего: ни экрана, ни ошибки, ни тоста.

Существующие гейты этого не ловили по устройству: `test_no_dead_onclick_handlers`
проверяет, что функция существует (она существовала), `test_no_dead_api_routes` —
что роут есть (он есть), `test_no_stuck_spinner` — что крутилка снимается (она
снималась, просто на невидимом экране). Дефект жил ровно в зазоре между ними.

Соседние плитки того же блока (`openUchConflicts`, `openUchGraph`,
`openUchSmartTags`) `push` делали — выпадала одна, и заметить это можно было
только сверив их между собой.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
JS = "\n".join(p.read_text(encoding="utf-8")
               for p in sorted((ROOT / "mini_app" / "screens").glob("*.js")))
ALL = HTML + "\n" + JS

_DIV = re.compile(r"<(/?)div\b", re.I)


def _screen_bodies() -> dict[str, str]:
    """Тело каждого экрана по РЕАЛЬНОЙ вложенности тегов.

    Наивное «резать по следующему <div class="screen">» ломается на последнем
    экране: он проглатывает все модалки и скрипты после себя, и любой детектор
    на такой разметке выдаёт десятки ложных срабатываний.
    """
    out = {}
    for m in re.finditer(r'<div class="screen" id="(s-[a-z0-9_-]+)"', HTML):
        sid, start = m.group(1), m.start()
        depth, end = 0, start
        for t in _DIV.finditer(HTML, start):
            depth += -1 if t.group(1) else 1
            if depth == 0:
                end = t.end()
                break
        out[sid] = HTML[start:end]
    return out


def _fn_bodies() -> dict[str, str]:
    out = {}
    for m in re.finditer(r"(?:async\s+)?function\s+([A-Za-z0-9_]+)\s*\([^)]*\)\s*\{", ALL):
        name, start = m.group(1), m.end() - 1
        depth, i = 0, start
        while i < len(ALL):
            if ALL[i] == "{":
                depth += 1
            elif ALL[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out[name] = ALL[start:i]
    return out


SCREENS = _screen_bodies()
FNS = _fn_bodies()
ELEM_SCREEN = {}
for _sid, _body in SCREENS.items():
    for _eid in re.findall(r'id="([a-zA-Z0-9_-]+)"', _body):
        ELEM_SCREEN.setdefault(_eid, _sid)

_NAVS = re.compile(r"\bpush\(|\bshow\(|\bnavGo\(|\bgoTab\(|\bopenModal\(")


def test_no_mute_tiles():
    """Функция, вызываемая ИЗВНЕ экрана и пишущая только в этот экран, обязана
    его открыть — иначе нажатие не даёт пользователю ничего."""
    mute = []
    for name in sorted(set(re.findall(r'onclick="([A-Za-z0-9_]+)\(', ALL))):
        body = FNS.get(name)
        if not body or _NAVS.search(body):
            continue
        targets = set(re.findall(r"txt\('([a-zA-Z0-9_-]+)'", body))
        screens = {ELEM_SCREEN[t] for t in targets if t in ELEM_SCREEN}
        if len(screens) != 1:
            continue
        target_screen = screens.pop()
        # Вызов изнутри своего же экрана (кнопка «обновить») — законен: экран
        # уже открыт. Проблема только у вызовов СНАРУЖИ.
        called_outside = any(
            f"{name}(" in b for sid, b in SCREENS.items() if sid != target_screen
        )
        if called_outside:
            mute.append((name, target_screen))
    assert not mute, (
        "нажатие уходит в API и пишет в скрытый экран — пользователь не видит ничего:\n"
        + "\n".join(f"  {n}() → пишет в {s}, но не открывает его" for n, s in mute)
    )


def test_duplicates_tile_navigates():
    """Точечная защита конкретного дефекта: плитка «Дубликаты» была немой."""
    body = FNS.get("detectDuplicates")
    assert body, "функция плитки «Дубликаты» исчезла"
    assert "push('s-uchduplicates')" in body, (
        "без push экран дублей недостижим: тайл молча пишет в невидимый DOM"
    )


def test_duplicates_screen_is_reachable():
    """Экран, на который нет ни одного перехода, — мёртвый вес в бандле."""
    assert "s-uchduplicates" in SCREENS, "экран дублей пропал"
    assert re.search(r"push\('s-uchduplicates'\)", ALL), "к экрану нет ни одного входа"


def test_sibling_tiles_stay_consistent():
    """Плитки одного блока должны вести себя одинаково: расхождение между ними и
    было единственным способом заметить дефект."""
    for fn in ("openUchConflicts", "openUchGraph", "openUchSmartTags", "detectDuplicates"):
        body = FNS.get(fn)
        assert body, f"{fn} не найдена"
        assert _NAVS.search(body), f"{fn} не открывает экран — снова немая плитка"
