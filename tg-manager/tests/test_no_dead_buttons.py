"""Храповик: кнопка бота, у которой нет обработчика — нажал и ничего.

Кнопки строятся фабрикой из `bot/callbacks.py`:

    kb.button(text="📱 Аккаунты", callback_data=AccCb(action="menu"))

а обработчик объявляется фильтром той же фабрики:

    @router.callback_query(AccCb.filter(F.action == "menu"))

Значит набор `action`, которые СТРОЯТ кнопки, обязан лежать внутри набора
`action`, которые КТО-ТО ловит. Если нет — кнопка в интерфейсе есть, нажатие
уходит в пустоту, и бот молчит. Ни синтаксис, ни импорт, ни существующие тесты
этого не видят: и кнопка, и обработчик по отдельности корректны, разошлись
только строки.

Родственный `test_no_dead_end_screens` держит другое — экран без единой кнопки.
Здесь про кнопку без экрана по ту сторону. Эту породу в репозитории находили
поштучно и не раз (`test_bulk_menu_dead_button_fix`,
`test_account_add_button_always_visible`) — храповик закрывает её целиком.

ЧЕГО ХРАПОВИК НЕ ВИДИТ, сознательно:

* `action` из переменной (`AccCb(action=act)`) — значение известно в рантайме;
* фабрику, у которой ХОТЬ ОДИН фильтр не сводится к литералам (`filter()` без
  условия, `startswith`, вычисляемое условие): такой фильтр может ловить что
  угодно, и судить о её кнопках нельзя. Таких фабрик меньшинство, и лучше
  промолчать, чем ругаться зря: храповик с ложными срабатываниями отключают
  целиком.

Внизу — САМОПРОВЕРКА на заведомо мёртвой кнопке.
"""
from __future__ import annotations

import ast
import os
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", "tests", "node_modules"}


def _factories(root: str) -> set[str]:
    """Классы-фабрики callback_data из bot/callbacks.py."""
    path = os.path.join(root, "bot", "callbacks.py")
    if not os.path.exists(path):
        return set()
    tree = ast.parse(open(path, encoding="utf-8").read())
    return {
        n.name
        for n in tree.body
        if isinstance(n, ast.ClassDef)
        and any(isinstance(b, ast.Name) and b.id == "CallbackData" for b in n.bases)
    }


def _literal_actions(call: ast.Call) -> tuple[set[str], bool]:
    """Какие action ловит этот filter(...). Второе значение — «свелось к литералам»."""
    if not call.args and not call.keywords:
        return set(), False                     # filter() без условия ловит всё
    got: set[str] = set()
    literal_only = True
    for node in list(call.args) + [k.value for k in call.keywords]:
        # F.action == "x"
        if (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], ast.Eq)
                and isinstance(node.left, ast.Attribute) and node.left.attr == "action"
                and isinstance(node.comparators[0], ast.Constant)
                and isinstance(node.comparators[0].value, str)):
            got.add(node.comparators[0].value)
            continue
        # F.action.in_({"a", "b"})
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "in_"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "action"):
            for arg in node.args:
                if isinstance(arg, (ast.Set, ast.List, ast.Tuple)):
                    for el in arg.elts:
                        if isinstance(el, ast.Constant) and isinstance(el.value, str):
                            got.add(el.value)
                        else:
                            literal_only = False
                else:
                    literal_only = False
            continue
        literal_only = False
    return got, literal_only and bool(got)


def detect(root: str = ROOT) -> list[str]:
    """['файл:строка Фабрика(action="x") — обработчика нет']."""
    facts = _factories(root)
    if not facts:
        return []
    buttons: dict[str, set[str]] = defaultdict(set)
    where: dict[tuple[str, str], str] = {}
    handled: dict[str, set[str]] = defaultdict(set)
    unjudgeable: set[str] = set()

    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dirpath, f)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            try:
                tree = ast.parse(open(path, encoding="utf-8", errors="ignore").read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                # обработчик: Фабрика.filter(...)
                if (isinstance(node.func, ast.Attribute) and node.func.attr == "filter"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in facts):
                    actions, ok = _literal_actions(node)
                    if ok:
                        handled[node.func.value.id] |= actions
                    else:
                        unjudgeable.add(node.func.value.id)
                    continue
                # кнопка: Фабрика(action="x", ...)
                if isinstance(node.func, ast.Name) and node.func.id in facts:
                    for kw in node.keywords:
                        if kw.arg != "action":
                            continue
                        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                            buttons[node.func.id].add(kw.value.value)
                            where.setdefault((node.func.id, kw.value.value), f"{rel}:{node.lineno}")
                        break

    dead = []
    for factory in sorted(buttons):
        if factory in unjudgeable:
            continue
        for action in sorted(buttons[factory] - handled[factory]):
            dead.append(
                f'{where[(factory, action)]}  {factory}(action="{action}") — обработчика нет')
    return dead


def test_no_dead_buttons():
    dead = detect()
    assert not dead, (
        "кнопка есть в интерфейсе, а обработчика под неё нет: нажатие уходит в "
        "пустоту и бот молчит. Либо повесьте обработчик, либо уберите кнопку:\n  "
        + "\n  ".join(dead[:40])
    )


def test_ratchet_actually_looks_at_something():
    """Пустой список фабрик дал бы вечно зелёный тест, ничего не проверяющий."""
    facts = _factories(ROOT)
    assert len(facts) > 50, f"фабрик найдено {len(facts)} — похоже, разбор сломался"


def test_detector_catches_a_dead_button(tmp_path):
    """САМОПРОВЕРКА на заведомо мёртвой кнопке."""
    (tmp_path / "bot" / "handlers").mkdir(parents=True)
    (tmp_path / "bot" / "callbacks.py").write_text(
        "class CallbackData:\n"
        "    def __init_subclass__(cls, prefix='', **kw): pass\n"
        "    @classmethod\n"
        "    def filter(cls, *a, **k): return None\n"
        "\n"
        "class AccCb(CallbackData, prefix='acc'):\n"
        "    action: str\n"
        "\n"
        "class WideCb(CallbackData, prefix='wide'):\n"
        "    action: str\n",
        encoding="utf-8")
    (tmp_path / "bot" / "handlers" / "h.py").write_text(
        "from bot.callbacks import AccCb, WideCb\n"
        "\n"
        "@router.callback_query(AccCb.filter(F.action == 'menu'))\n"
        "async def menu(c): ...\n"
        "\n"
        "@router.callback_query(AccCb.filter(F.action.in_({'add', 'remove'})))\n"
        "async def pair(c): ...\n"
        "\n"
        "@router.callback_query(WideCb.filter())\n"
        "async def wide(c): ...\n"
        "\n"
        "def keyboard():\n"
        "    AccCb(action='menu')\n"
        "    AccCb(action='add')\n"
        "    AccCb(action='export')\n"
        "    WideCb(action='anything')\n",
        encoding="utf-8")

    found = detect(str(tmp_path))
    joined = "\n".join(found)
    assert 'AccCb(action="export")' in joined, f"не поймал мёртвую кнопку: {found}"
    assert "menu" not in joined and "add" not in joined, f"ложное срабатывание: {found}"
    # фабрика с filter() без условия ловит всё — её кнопки мёртвыми не считаются
    assert "WideCb" not in joined, f"кнопка под catch-all названа мёртвой: {found}"
