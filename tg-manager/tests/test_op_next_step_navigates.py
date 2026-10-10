"""Блок «⚡ Что сделать дальше» должен вести туда, что обещает.

Что было
--------
Карточка подсказки рисовалась так:

    onclick="<действие>();back()"

`back()` стоял ПОСЛЕ действия. А почти каждое действие из этого блока —
навигация: `openHealth`, `openWarmup`, `openChannels`, `openProxies`,
`openRehab`, `openFolders`, `openMassInvite` начинаются с `push()` нового
экрана. `back()` тут же его снимал. Нажатие срабатывало, экран открывался и
закрывался в том же кадре — со стороны это выглядело как мёртвая кнопка:
подсказка с шевроном «›», которая никуда не ведёт.

Отдельно: `loadBots` стоял в карте следующих шагов как «🤖 Открыть ботов», но
он ничего не открывает — только перерисовывает содержимое вкладки ботов,
которая в этот момент скрыта.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import op_quick_actions as qa  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _ui() -> str:
    src = (ROOT / "mini_app/index.html").read_text(encoding="utf-8")
    for p in sorted((ROOT / "mini_app/screens").glob("*.js")):
        src += "\n" + p.read_text(encoding="utf-8")
    return src


def _quick_action_row() -> str:
    """Разметка строки подсказки — та самая, что рисует блок.

    Комментарии отброшены: объяснение прежнего дефекта цитирует его буквально,
    и тест ловил бы собственный комментарий вместо кода.
    """
    ui = (ROOT / "mini_app/index.html").read_text(encoding="utf-8")
    i = ui.index("Что сделать дальше")
    chunk = ui[i:i + 1800]
    return "\n".join(
        ln for ln in chunk.splitlines() if not ln.lstrip().startswith("//")
    )


# ── навигация не отменяется сама собой ───────────────────────────────────────


def test_next_step_does_not_undo_its_own_navigation():
    row = _quick_action_row()
    assert "${call};back()" not in row, (
        "back() снова стоит ПОСЛЕ действия — открытый экран будет тут же закрыт"
    )
    assert "back();${call}" in row, (
        "экран деталей операции не закрывается перед переходом"
    )


def test_every_target_actually_opens_something():
    """Подсказка обязана открывать экран или переключать вкладку. Функция,
    которая лишь подгружает данные в скрытый экран, выглядит мёртвой кнопкой."""
    ui = _ui()
    bad = []
    for name in _all_action_fns():
        if name == "goTab":
            continue
        m = re.search(
            r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
            ui,
        )
        assert m, f"{name}: функции нет в мини-аппе"
        body = ui[m.end(): m.end() + 900]
        navigates = "push(" in body or "goTab(" in body
        acts = "api(" in body or "askConfirm(" in body or "toast(" in body
        if not navigates and not acts:
            bad.append(name)
    assert not bad, f"подсказки ведут в никуда: {bad}"


def _all_action_fns() -> set[str]:
    names = set()
    for v in qa._AFTER_DONE.values():
        names.add(v[1])
    src = (ROOT / "services/op_quick_actions.py").read_text(encoding="utf-8")
    names |= set(re.findall(r'_add\(\s*"[^"]+",\s*f?"[^"]*",\s*"([A-Za-z_$][\w$]*)"', src))
    return {n for n in names if n}


def test_open_bots_really_opens_bots():
    """`loadBots` перерисовывает скрытую вкладку и ничего не показывает."""
    targets = [v[1] for v in qa._AFTER_DONE.values()]
    assert "loadBots" not in targets, (
        "«Открыть ботов» снова вызывает загрузчик вместо перехода на вкладку"
    )
    for key in ("bot_factory", "connect_discovered_bots"):
        entry = qa._AFTER_DONE[key]
        assert entry[1] == "goTab" and entry[2] == "bots", entry


# ── карта следующих шагов остаётся исполнимой ────────────────────────────────


def test_after_done_entries_are_well_formed():
    for op_type, entry in qa._AFTER_DONE.items():
        assert 2 <= len(entry) <= 3, f"{op_type}: {entry}"
        assert entry[0].strip(), f"{op_type}: пустая подпись"
        assert entry[1].strip(), f"{op_type}: нет функции"


def test_argument_reaches_the_action():
    """Третий элемент карты — аргумент. Раньше карта его не умела, и переход на
    вкладку записать было нечем."""
    out = qa.suggest("bot_factory", "done", {})
    step = [a for a in out if a["id"].startswith("next_")]
    assert step, "следующий шаг не предложен"
    assert step[0]["fn"] == "goTab" and step[0]["arg"] == "bots"


def test_actions_without_argument_stay_without_one():
    out = qa.suggest("mass_invite", "done", {})
    step = [a for a in out if a["id"].startswith("next_")][0]
    assert step["fn"] == "openHealth"
    assert "arg" not in step, "лишний аргумент сломает вызов функции без параметров"


def test_failed_operation_is_offered_a_retry_not_a_next_step():
    out = qa.suggest("mass_invite", "failed", {}, op_id=5)
    ids = [a["id"] for a in out]
    assert "retry" in ids
    assert not [i for i in ids if i.startswith("next_")]
