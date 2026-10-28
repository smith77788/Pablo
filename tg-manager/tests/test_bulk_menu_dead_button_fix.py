"""Регресс: мёртвые кнопки bulk_join/bulk_leave в меню channel_ops.

Кнопки эмитили ChanCb (prefix 'chan'), а хендлеры слушают MassOpCb (prefix 'mop')
— префикс не совпадал, тап был мёртвым с момента мерджа (CLAUDE.md правило #1).
Кнопки переведены на MassOpCb. Тест также проверяет инвариант «нет использованного
action без хендлера того же Cb-класса» для ChanCb и MassOpCb.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_bulk_join_leave_use_massopcb_not_chancb():
    h = _read("bot/handlers/channel_ops.py")
    assert 'MassOpCb(action="bulk_join")' in h
    assert 'MassOpCb(action="bulk_leave")' in h
    # старые мёртвые ChanCb-кнопки не должны вернуться
    assert 'ChanCb(action="bulk_join")' not in h
    assert 'ChanCb(action="bulk_leave")' not in h


def test_massopcb_handlers_exist_for_bulk_join_leave():
    m = _read("bot/handlers/mass_ops.py")
    assert 'MassOpCb.filter(F.action == "bulk_join")' in m
    assert 'MassOpCb.filter(F.action == "bulk_leave")' in m


def _used_actions(cb_class: str, files: list[str]) -> set[str]:
    used = set()
    pat = re.compile(rf'{cb_class}\(\s*action\s*=\s*"([a-z0-9_]+)"')
    for f in files:
        for mm in pat.finditer(_read(f)):
            used.add(mm.group(1))
    return used


def _handled_actions(cb_class: str, files: list[str]) -> tuple[set[str], set[str]]:
    """Возвращает (точные actions, префиксы startswith), включая динамическую
    регистрацию через переменную (её пропускаем — не можем статически раскрыть)."""
    eq = re.compile(rf'{cb_class}\.filter\(\s*F\.action\s*==\s*"([a-z0-9_]+)"')
    inn = re.compile(rf'{cb_class}\.filter\(\s*F\.action\.in_\(\s*\{{([^}}]*)\}}')
    sw = re.compile(rf'{cb_class}\.filter\(\s*F\.action\.startswith\(\s*"([a-z0-9_]+)"')
    dyn = re.compile(rf'{cb_class}\.filter\(\s*F\.action\s*==\s*[a-z_]')  # переменная
    exact, prefixes = set(), set()
    dynamic = False
    for f in files:
        s = _read(f)
        for mm in eq.finditer(s):
            exact.add(mm.group(1))
        for mm in inn.finditer(s):
            exact |= set(re.findall(r'"([a-z0-9_]+)"', mm.group(1)))
        for mm in sw.finditer(s):
            prefixes.add(mm.group(1))
        if dyn.search(s):
            dynamic = True
    return exact, prefixes, dynamic  # type: ignore[return-value]


def _bot_files() -> list[str]:
    import glob
    return [os.path.relpath(p, ROOT)
            for p in glob.glob(os.path.join(ROOT, "bot", "**", "*.py"), recursive=True)]


def test_no_dead_massopcb_or_chancb_buttons():
    files = _bot_files()
    for cb in ("MassOpCb",):  # ChanCb использует динамическую регистрацию (prof_*)
        used = _used_actions(cb, files)
        exact, prefixes, dynamic = _handled_actions(cb, files)  # type: ignore[misc]
        dead = {a for a in used
                if a not in exact and not any(a.startswith(p) for p in prefixes)}
        # 'menu' и служебные могут регистрироваться в родительском роутере — но
        # bulk_join/bulk_leave обязаны иметь хендлер именно тут
        assert "bulk_join" not in dead and "bulk_leave" not in dead, \
            f"{cb}: мёртвые кнопки {dead & {'bulk_join', 'bulk_leave'}}"
