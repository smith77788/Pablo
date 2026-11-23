"""Каждая кнопка бота ведёт в обработчик.

Кнопка с `XCb(action="foo")`, для которой нет `XCb.filter(F.action == "foo")`,
в Telegram выглядит живой: нажатие крутит часики и не делает ничего. Ни один
тест этого не видел — обработчики и кнопки живут в разных файлах, а на
синтаксисе опечатка в action не ломается.

Формы обработчика, которые измеритель понимает:
  * `F.action == "foo"`, `F.action.in_({...})`, `F.action.startswith("fo")`;
  * `XCb.filter()` без условия — ловит всё;
  * динамические (`F.action == _prof_action` в цикле,
    `F.action.in_(set(_MAP.keys()))`): тогда action считается обработанным,
    если та же строка лежит в коллекции-литерале того же модуля;
  * псевдонимы импорта (`from bot.callbacks import AccCb as _AccCb`).
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "bot"


def _scan(files: dict[str, str]):
    uses: dict[tuple[str, str], list[str]] = defaultdict(list)
    handled: dict[str, set[str]] = defaultdict(set)
    prefixes: dict[str, set[str]] = defaultdict(set)
    catchall: set[str] = set()
    dynamic: dict[str, set[str]] = defaultdict(set)  # cb -> строки из литералов модулей
    for name, src in files.items():
        tree = ast.parse(src)
        alias = {a.asname: a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                 for a in n.names if a.asname}
        cb_of = lambda ident: alias.get(ident, ident)  # noqa: E731
        literals: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
                elts = n.keys if isinstance(n, ast.Dict) else n.elts
                literals |= {e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if isinstance(f, ast.Name) and cb_of(f.id).endswith("Cb"):
                for k in n.keywords:
                    if k.arg == "action" and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str):
                        uses[(cb_of(f.id), k.value.value)].append(f"{name}:{n.lineno}")
            if (isinstance(f, ast.Attribute) and f.attr == "filter" and isinstance(f.value, ast.Name)
                    and cb_of(f.value.id).endswith("Cb")):
                cb = cb_of(f.value.id)
                if not n.args:
                    catchall.add(cb)
                    continue
                cond = ast.unparse(n.args[0])
                if "F.action" not in cond:
                    catchall.add(cb)
                    continue
                handled[cb] |= set(re.findall(r"F\.action == '([^']+)'", cond))
                m = re.search(r"F\.action\.in_\((.*)\)", cond)
                if m:
                    handled[cb] |= set(re.findall(r"'([^']+)'", m.group(1)))
                prefixes[cb] |= set(re.findall(r"F\.action\.startswith\('([^']+)'\)", cond))
                if re.search(r"F\.action == [A-Za-z_]", cond) or (m and not re.search(r"'", m.group(1))):
                    dynamic[cb] |= literals
    dead = []
    for (cb, action), where in sorted(uses.items()):
        if cb in catchall or action in handled[cb] or action in dynamic[cb]:
            continue
        if any(action.startswith(p) for p in prefixes[cb]):
            continue
        dead.append(f"{cb}(action={action!r}) — {where[0]}")
    return dead


def test_every_button_action_has_a_handler():
    files = {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8") for p in sorted(BOT.rglob("*.py"))}
    dead = _scan(files)
    assert not dead, "кнопки без обработчика (нажатие ничего не делает):\n" + "\n".join(dead)


def test_probe_sees_a_dead_button_and_spares_live_forms():
    files = {
        "a.py": (
            "from bot.callbacks import XCb as _XCb\n"
            "kb.button(text='a', callback_data=XCb(action='live'))\n"
            "kb.button(text='b', callback_data=_XCb(action='dead'))\n"
            "kb.button(text='c', callback_data=XCb(action='dyn_one'))\n"
            "kb.button(text='d', callback_data=XCb(action='pre_x'))\n"
            "@router.callback_query(XCb.filter(F.action == 'live'))\nasync def h(c): ...\n"
            "@router.callback_query(XCb.filter(F.action.startswith('pre_')))\nasync def p(c): ...\n"
            "_MAP = {'dyn_one': 1}\n"
            "@router.callback_query(XCb.filter(F.action.in_(set(_MAP.keys()))))\nasync def d(c): ...\n"
        ),
    }
    assert _scan(files) == ["XCb(action='dead') — a.py:3"]
