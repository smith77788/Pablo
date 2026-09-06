"""Храповик: никаких вызовов внутренних функций/классов с keyword-аргументом,
которого нет в их сигнатуре.

Класс бага: operation_bus.submit(scheduled_for=...) — submit не принимал такой
kwarg → TypeError в рантайме, убивший ВСЕ отложенные операции продукта. Синтаксис
и обычные тесты это не ловят (заглушка пула типы/сигнатуры не проверяет).

Метод (ground truth, нулевой ложняк): резолвим целевой объект (alias.func / прямо
импортированное имя func / Класс(...)), берём inspect.signature. Если объект
принимает **kwargs — пропускаем. Иначе каждый ЯВНЫЙ keyword-арг обязан быть среди
параметров. Флагуем только когда объект реально резолвится и сигнатура читается;
псевдонимы, затенённые внешним импортом/присваиванием, пропускаются.

Покрытие на момент добавления: ~4880 вызовов проверено строго, 0 находок. В
минимальном CI без aiogram/telethon недоступный модуль просто не проверяется.
"""
from __future__ import annotations

import ast
import importlib
import inspect
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
        _f = types.ModuleType("asyncpg")
        _f.Pool = object; _f.Connection = object
        _f.exceptions = types.SimpleNamespace()
        sys.modules["asyncpg"] = _f

INTERNAL = ("services", "bot", "config", "utils", "database")
_cache: dict = {}


def _imp(modname):
    if modname in _cache:
        return _cache[modname]
    try:
        m = importlib.import_module(modname)
    except Exception:
        m = None
    _cache[modname] = m
    return m


def _params(obj):
    try:
        sig = inspect.signature(obj)
    except (ValueError, TypeError):
        return None
    names, var_kw = set(), False
    for p in sig.parameters.values():
        if p.kind == inspect.Parameter.VAR_KEYWORD:
            var_kw = True
        elif p.kind != inspect.Parameter.VAR_POSITIONAL:
            names.add(p.name)
    return names, var_kw


def _scan(path):
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), path)
    except Exception:
        return []
    alias_mod: dict = {}
    direct: dict = {}
    external: set = set()
    assigned: set = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                top = a.name.split(".")[0]
                key = a.asname or top
                if top in INTERNAL:
                    alias_mod.setdefault(key, set()).add(a.name if a.asname else top)
                else:
                    external.add(key)
        elif isinstance(n, ast.ImportFrom):
            base = n.module or ""
            top = base.split(".")[0]
            for a in n.names:
                key = a.asname or a.name
                if top in INTERNAL or base in INTERNAL:
                    alias_mod.setdefault(key, set()).add(f"{base}.{a.name}")
                    direct.setdefault(key, set()).add((base, a.name))
                else:
                    external.add(key)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    assigned.add(t.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for a in list(n.args.args) + list(n.args.kwonlyargs):
                assigned.add(a.arg)

    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        kwargs = [k.arg for k in n.keywords if k.arg is not None]
        if not kwargs:
            continue
        obj = label = None
        f = n.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            alias = f.value.id
            if (alias in external or alias in assigned or alias not in alias_mod
                    or len(alias_mod[alias]) != 1):
                continue
            mod = _imp(next(iter(alias_mod[alias])))
            if mod is None or not hasattr(mod, f.attr):
                continue
            obj = getattr(mod, f.attr)
            label = f"{next(iter(alias_mod[alias]))}.{f.attr}"
        elif isinstance(f, ast.Name):
            name = f.id
            if (name in assigned or name in external or name not in direct
                    or len(direct[name]) != 1):
                continue
            base, sym = next(iter(direct[name]))
            mod = _imp(base)
            if mod is None or not hasattr(mod, sym):
                continue
            obj = getattr(mod, sym)
            label = f"{base}.{sym}"
        if obj is None or inspect.ismodule(obj):
            continue
        pr = _params(obj)
        if pr is None:
            continue
        names, var_kw = pr
        if var_kw:
            continue
        for kw in kwargs:
            if kw not in names:
                out.append((os.path.relpath(path, ROOT), n.lineno, label, kw))
    return out


def test_no_bad_kwargs_to_internal_callables():
    findings = []
    for base in ("services", "bot"):
        for dp, _d, files in os.walk(os.path.join(ROOT, base)):
            if "__pycache__" in dp:
                continue
            for fn in files:
                if fn.endswith(".py"):
                    findings += _scan(os.path.join(dp, fn))
    for fn in os.listdir(ROOT):
        if fn.endswith(".py"):
            findings += _scan(os.path.join(ROOT, fn))

    assert not findings, (
        "keyword-аргумент, которого нет в сигнатуре внутренней функции/класса "
        "(TypeError в рантайме, как submit(scheduled_for=...)):\n"
        + "\n".join(f"  {p}:{ln} → {label}(... {kw}=...)"
                    for p, ln, label, kw in findings)
    )
