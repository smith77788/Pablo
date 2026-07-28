"""Каталог объектов присутствия: запрос со скоупом по владельцу + фильтры.

Единая база созданных объектов (по всем планам-проектам) с фильтром по типу и
статусу. Скоуп по owner_id обязателен (класс #1: межтенантная утечка).
"""
from __future__ import annotations

import pathlib

import pytest

from database import db


class RecPool:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        return self.rows


@pytest.mark.asyncio
async def test_objects_query_is_owner_scoped_and_filters():
    pool = RecPool(rows=[])
    await db.get_owner_presence_objects(pool, 777, asset_type="channel", status="done", limit=15)
    q, args = pool.calls[-1]
    assert "gpp.owner_id=$1" in q, "нет скоупа по владельцу"
    assert args[0] == 777
    assert "JOIN global_presence_plans" in q, "объекты должны джойниться к планам владельца"
    # фильтры дошли в аргументы
    assert "done" in args and "channel" in args
    # только базовые колонки (без v159-only, чтобы не падать при лаге миграции)
    for v159col in ("final_username", "gpt.role", "project_name"):
        assert v159col not in q, f"каталог не должен зависеть от v159-колонки {v159col}"


@pytest.mark.asyncio
async def test_objects_plan_filter():
    pool = RecPool()
    await db.get_owner_presence_objects(pool, 1, plan_id=42)
    q, args = pool.calls[-1]
    assert "gpt.plan_id=" in q and 42 in args


@pytest.mark.asyncio
async def test_objects_default_status_done():
    pool = RecPool()
    await db.get_owner_presence_objects(pool, 1)
    _, args = pool.calls[-1]
    assert "done" in args, "по умолчанию показываем созданные (status=done)"


@pytest.mark.asyncio
async def test_objects_status_none_no_status_filter():
    pool = RecPool()
    await db.get_owner_presence_objects(pool, 1, status=None)
    q, args = pool.calls[-1]
    assert "gpt.status=" not in q, "status=None → без фильтра по статусу"


@pytest.mark.asyncio
async def test_type_counts_owner_scoped():
    pool = RecPool(rows=[{"asset_type": "channel", "n": 5}, {"asset_type": "group", "n": 2}])
    counts = await db.get_owner_presence_type_counts(pool, 42)
    assert counts == {"channel": 5, "group": 2}
    q, args = pool.calls[-1]
    assert "gpp.owner_id=$1" in q and args[0] == 42


def test_catalog_handler_wired():
    src = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "bot", "handlers", "global_presence.py").read_text("utf-8")
    # кросс-проектный обзор всех объектов на action="objects" (per-plan каталог
    # агента — на action="catalog"; действия не пересекаются)
    assert 'F.action == "objects"' in src, "нет обработчика обзора всех объектов"
    assert 'action="objects"' in src, "нет кнопки «Все объекты» в меню"
    assert "get_owner_presence_objects" in src and "get_owner_presence_type_counts" in src
    assert "get_owner_presence_regions" in src, "фильтр по региону не задействован"
