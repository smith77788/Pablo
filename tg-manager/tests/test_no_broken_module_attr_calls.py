"""Храповик: никаких вызовов/чтений `alias.attr` внутреннего модуля, которого нет.

Класс бага (дважды пойман вживую):
  • mass_report звал account_manager.get_channel_intel — такой функции нет;
  • _global_error_handler звал ErrorCode.from_exception — from_exception это
    модуль-функция, а не метод Enum.
Оба падали AttributeError, но внутри try/except или в редком пути — тихо ломали
гейт (разведку, глобальный обработчик). Синтаксис такое не ловит.

Метод (нулевой ложняк): собрать псевдонимы импортов внутренних модулей, найти
обращения `alias.attr`, ИМПОРТНУТЬ целевой модуль (ground truth) и проверить
hasattr. Флагуем ТОЛЬКО когда модуль реально импортнулся и атрибута нет и это не
подмодуль. Псевдонимы, затенённые внешним импортом/присваиванием, пропускаем.

В минимальном CI без aiogram/telethon целевой модуль может не импортнуться — тогда
он просто не проверяется (без ложной тревоги); там, где импорт есть, защита
работает.
"""
from __future__ import annotations

import ast
import importlib
import os
import sys
import types

os.environ.setdefault("MANAGER_BOT_TOKEN", "1:test")
os.environ.setdefault("DATABASE_URL", "postgres://test")
os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "x")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "test-encryption-secret")
os.environ.setdefault("ADMIN_IDS", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if "asyncpg" not in sys.modules:
    try:
        import asyncpg  # noqa
    except Exception:
        _fake = types.ModuleType("asyncpg")
        _fake.Pool = object
        _fake.Connection = object
        _fake.exceptions = types.SimpleNamespace()
        sys.modules["asyncpg"] = _fake

INTERNAL_PREFIXES = ("services", "bot", "config", "utils", "database")
_mod_cache: dict[str, object] = {}


def _load(modname: str):
    if modname in _mod_cache:
        return _mod_cache[modname]
    m = None
    try:
        m = importlib.import_module(modname)
    except Exception:
        if "." in modname:
            parent, name = modname.rsplit(".", 1)
            try:
                pm = importlib.import_module(parent)
                m = getattr(pm, name) if hasattr(pm, name) else ("ERR",)
            except Exception:
                m = ("ERR",)
        else:
            m = ("ERR",)
    _mod_cache[modname] = m
    return m


def _aliases(tree):
    aliases: dict[str, set] = {}
    external: set = set()
    assigned: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                key = a.asname or top
                if top in INTERNAL_PREFIXES:
                    aliases.setdefault(key, set()).add(a.name if a.asname else top)
                else:
                    external.add(key)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            top = base.split(".")[0]
            for a in node.names:
                key = a.asname or a.name
                if top in INTERNAL_PREFIXES or base in INTERNAL_PREFIXES:
                    aliases.setdefault(key, set()).add(f"{base}.{a.name}")
                else:
                    external.add(key)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    assigned.add(t.id)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                assigned.add(node.target.id)
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            assigned.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for a in list(node.args.args) + list(node.args.kwonlyargs):
                assigned.add(a.arg)
    return aliases, external, assigned


def _scan(path: str):
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), path)
    except Exception:
        return []
    aliases, external, assigned = _aliases(tree)
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
            continue
        if not isinstance(node.ctx, ast.Load):
            continue
        alias, attr = node.value.id, node.attr
        if alias not in aliases or alias in external or alias in assigned:
            continue
        if len(aliases[alias]) != 1:
            continue
        modname = next(iter(aliases[alias]))
        m = _load(modname)
        if isinstance(m, tuple):
            continue
        if hasattr(m, attr):
            continue
        sub = _load(f"{modname}.{attr}")
        if not isinstance(sub, tuple):
            continue
        out.append((os.path.relpath(path, ROOT), node.lineno, modname, attr))
    return out


def test_no_calls_to_nonexistent_module_attrs():
    findings = []
    for base in ("services", "bot"):
        for dp, _d, files in os.walk(os.path.join(ROOT, base)):
            if "__pycache__" in dp:
                continue
            for f in files:
                if f.endswith(".py"):
                    findings += _scan(os.path.join(dp, f))
    for f in os.listdir(ROOT):
        if f.endswith(".py"):
            findings += _scan(os.path.join(ROOT, f))

    assert not findings, (
        "обращение к несуществующему атрибуту внутреннего модуля "
        "(тихий AttributeError, как get_channel_intel / ErrorCode.from_exception):\n"
        + "\n".join(f"  {p}:{ln} → {mod}.{attr}" for p, ln, mod, attr in findings)
    )
