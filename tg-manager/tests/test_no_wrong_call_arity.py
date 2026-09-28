"""Храповик: вызов своей же функции с неверными аргументами.

Это `TypeError` в момент вызова строки — ровно та же порода, что закрытые
`test_no_undefined_names` (имени нет) и `test_no_shadowed_module_imports`
(имя стало локальным). Синтаксис такое не ловит, импорт модуля тоже: сигнатура
сверяется только при исполнении, поэтому падает у владельца, а не в прогоне.

Разбираются два случая, которыми написан почти весь продукт:

* вызов функции своего модуля по имени — `helper(a, b)`;
* вызов функции другого модуля репозитория — `db.add_bot(...)`,
  `story_manager.fetch_stories(...)`.

Проверяются три вещи: лишний позиционный аргумент, неизвестный именованный
(опечатка в имени) и незаполненный обязательный.

ЧТО СОЗНАТЕЛЬНО ПРОПУСКАЕТСЯ. Детектор обязан молчать там, где он не уверен:
храповик с ложными срабатываниями отключают целиком, и тогда он не держит
ничего. Поэтому мимо идут: функции с декоратором (декоратор меняет сигнатуру),
методы классов, `*args`/`**kwargs` на вызове, имена, перекрытые локально или на
уровне модуля, и любые непростые цели вызова.

Внизу — САМОПРОВЕРКА на заведомо битом примере. Без неё сломанный детектор
остаётся зелёным и выглядит как доказательство, которым он не является.
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", "tests", "node_modules"}


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    a = fn.args
    pos = [p.arg for p in a.posonlyargs] + [p.arg for p in a.args]
    ndef = len(a.defaults)
    return {
        "pos": pos,
        "required": pos[: len(pos) - ndef] if ndef else list(pos),
        "kwonly": [p.arg for p in a.kwonlyargs],
        "kwreq": [p.arg for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is None],
        "vararg": a.vararg is not None,
        "kwarg": a.kwarg is not None,
        "lineno": fn.lineno,
    }


def _module_level(tree: ast.Module) -> tuple[dict, set]:
    """Функции уровня модуля без декораторов + имена, переопределённые не-функцией."""
    fns, rebound = {}, set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not n.decorator_list:
                fns[n.name] = _signature(n)
        elif isinstance(n, ast.Assign):
            rebound |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            rebound |= {(al.asname or al.name).split(".")[0] for al in n.names}
        elif isinstance(n, ast.ClassDef):
            rebound.add(n.name)
    return {k: v for k, v in fns.items() if k not in rebound}, rebound


def _shadowed_inside(fn) -> set:
    """Имена, перекрытые внутри функции — такой вызов может быть не нашим."""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            out |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out |= {(al.asname or al.name).split(".")[0] for al in n.names}
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn:
            out.add(n.name)
    return out


def _mismatch(call: ast.Call, sig: dict) -> str | None:
    """Почему этот вызов упадёт. None — всё в порядке или судить не берёмся."""
    if any(isinstance(a, ast.Starred) for a in call.args):
        return None
    if any(k.arg is None for k in call.keywords):        # **kwargs на вызове
        return None
    npos = len(call.args)
    kws = [k.arg for k in call.keywords]
    if not sig["vararg"] and npos > len(sig["pos"]):
        return f"передано {npos} позиционных, принимает {len(sig['pos'])}"
    if not sig["kwarg"]:
        unknown = [k for k in kws if k not in sig["pos"] and k not in sig["kwonly"]]
        if unknown:
            return f"неизвестный аргумент {', '.join(sorted(unknown))}"
    filled = set(sig["pos"][:npos]) | set(kws)
    missing = [r for r in sig["required"] if r not in filled]
    missing += [r for r in sig["kwreq"] if r not in filled]
    if missing:
        return f"не передан обязательный {', '.join(missing)}"
    return None


def _py_files(root: str):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(dirpath, f)
                yield path, os.path.relpath(path, root).replace(os.sep, "/")


def _parse(path: str):
    try:
        return ast.parse(open(path, encoding="utf-8", errors="ignore").read())
    except SyntaxError:
        return None


def detect(root: str = ROOT) -> list[str]:
    """['файл:строка вызов — причина'] — все разобранные несовпадения сигнатур."""
    # ── карта модулей: "services.story_manager" → {имя: сигнатура}
    mods: dict[str, dict] = {}
    rels: dict[str, str] = {}
    trees: dict[str, ast.Module] = {}
    for path, rel in _py_files(root):
        tree = _parse(path)
        if tree is None:
            continue
        dotted = rel[:-3].replace("/", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        mods[dotted], _ = _module_level(tree)
        rels[dotted] = rel
        trees[dotted] = tree

    out: list[str] = []
    for dotted, tree in trees.items():
        rel = rels[dotted]
        own, rebound = _module_level(tree), None
        own_fns = mods[dotted]
        # псевдоним → модуль репозитория (только модульные импорты)
        alias: dict[str, str] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                for al in n.names:
                    cand = f"{n.module}.{al.name}"
                    if cand in mods:
                        alias[al.asname or al.name] = cand
            elif isinstance(n, ast.Import):
                for al in n.names:
                    if al.name in mods:
                        alias[al.asname or al.name.split(".")[0]] = al.name

        for holder in ast.walk(tree):
            if not isinstance(holder, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local = _shadowed_inside(holder)
            for call in ast.walk(holder):
                if not isinstance(call, ast.Call):
                    continue
                target_sig = target_desc = declared = None
                if isinstance(call.func, ast.Name):
                    name = call.func.id
                    if name in own_fns and name not in local:
                        target_sig = own_fns[name]
                        target_desc = f"{name}()"
                        declared = f"{rel}:{target_sig['lineno']}"
                elif isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
                    holder_name = call.func.value.id
                    if holder_name in alias and holder_name not in local:
                        mod = alias[holder_name]
                        sig = mods[mod].get(call.func.attr)
                        if sig:
                            target_sig = sig
                            target_desc = f"{holder_name}.{call.func.attr}()"
                            declared = f"{rels[mod]}:{sig['lineno']}"
                if target_sig is None:
                    continue
                why = _mismatch(call, target_sig)
                if why:
                    out.append(
                        f"{rel}:{call.lineno} {holder.name}() зовёт {target_desc} — "
                        f"{why} [объявлена {declared}]")
    return sorted(set(out))


# Вызов, который СПЕЦИАЛЬНО сделан неверным: тест проверяет, что аргумент
# обязателен, то есть падение там и есть предмет проверки.
KNOWN_INTENTIONAL: set[str] = set()


def test_no_wrong_call_arity():
    offenders = [o for o in detect() if o not in KNOWN_INTENTIONAL]
    assert not offenders, (
        "вызов упадёт с TypeError, когда до этой строки дойдёт исполнение — "
        "сигнатура сверяется только в момент вызова, поэтому это видно не в "
        "прогоне, а у владельца:\n  " + "\n  ".join(offenders[:40])
    )


def test_detector_catches_a_broken_call(tmp_path):
    """САМОПРОВЕРКА. Без неё сломанный детектор молча зелёный."""
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "services" / "thing.py").write_text(
        "async def do(pool, a, b=1, *, flag):\n    return a\n", encoding="utf-8")
    (tmp_path / "caller.py").write_text(
        "from services import thing\n"
        "\n"
        "def helper(a, b, c=1):\n"
        "    return a\n"
        "\n"
        "async def ok(pool):\n"
        "    helper(1, 2)\n"
        "    helper(1, b=2, c=3)\n"
        "    await thing.do(pool, 1, flag=True)\n"
        "\n"
        "async def too_many(pool):\n"
        "    helper(1, 2, 3, 4)\n"
        "\n"
        "async def typo_in_name(pool):\n"
        "    await thing.do(pool, 1, flagg=True)\n"
        "\n"
        "async def missing_required(pool):\n"
        "    await thing.do(pool, 1)\n"
        "\n"
        "def locally_shadowed():\n"
        "    helper = min\n"
        "    helper(1, 2, 3, 4, 5)\n",
        encoding="utf-8")

    found = detect(str(tmp_path))
    joined = "\n".join(found)
    assert "too_many()" in joined, f"не поймал лишний позиционный: {found}"
    assert "flagg" in joined, f"не поймал опечатку в имени аргумента: {found}"
    assert "missing_required()" in joined, f"не поймал незаполненный обязательный: {found}"
    # и НЕ должен ругаться на исправное и на локально перекрытое имя
    assert "ok()" not in joined, f"ложное срабатывание на исправном вызове: {found}"
    assert "locally_shadowed()" not in joined, f"локальное имя принято за нашу функцию: {found}"
