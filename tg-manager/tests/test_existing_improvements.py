"""Улучшения существующего кода: id контакта на конфликте, охват губернатора,
реакция на бан, подсказка о банах в мозге."""
from __future__ import annotations

import asyncio
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── A. upsert_contact возвращает РЕАЛЬНЫЙ id (RETURNING id) ──────────────────
def test_upsert_contact_returns_existing_id_on_conflict():
    from services.contacts_hub import repository

    class _Pool:
        async def fetchrow(self, sql, *a):
            assert "RETURNING id" in sql          # исправление на месте
            return {"id": "EXISTING-UUID"}        # id существующей строки

    got = _run(repository.upsert_contact(_Pool(), 1, {"telegram_user_id": 42}))
    assert got == "EXISTING-UUID"                 # не только что сгенерированный


# ── B. Губернатор покрывает bulk_join ───────────────────────────────────────
def test_governor_covers_bulk_join():
    src = _read("services/op_worker.py")
    join = src[src.index("async def _exec_bulk_join_inner"):]
    join = join[:join.index("\n\nasync def ")]
    assert "_governed_delay(pool, owner_id, pause)" in join


# ── C. Реакция на бан: инвалидация губернатора + событие ────────────────────
def test_ban_reaction_helper_invalidates_and_emits():
    src = _read("services/op_worker.py")
    fn = src[src.index("async def _on_account_banned"):]
    fn = fn[:fn.index("\n\nasync def ")]
    assert "fleet_governor" in fn and "invalidate" in fn
    assert 'spine.emit' in fn and '"ban"' in fn
    # и он вызывается в ban-ветке рассылки
    assert "_on_account_banned(pool, owner_id, acc" in src


# ── Контакты: поиск КОМБИНИРУЕТСЯ с фильтрами (единый путь, не отдельный /search)
def test_contacts_list_unified_search_with_filters():
    ui = _read("mini_app/index.html")
    fn = ui[ui.index("async function loadContacts"):]
    fn = fn[:fn.index("async function searchContacts")]
    # список всегда идёт через uch/contacts (search как параметр), без ветки на /uch/search
    assert "/api/miniapp/uch/contacts?" in fn
    assert "params.set('search', q)" in fn
    assert "/api/miniapp/uch/search?q=" not in fn   # разветвление убрано


# ── Ретеншен журнала событий (защита от разрастания) ────────────────────────
def test_event_retention_prune():
    from services.organism import spine

    class _Pool:
        def __init__(self): self.sql = None
        async def execute(self, sql, *a):
            self.sql = sql
            return "DELETE 42"

    p = _Pool()
    n = _run(spine.prune_events(p, days=30))
    assert n == 42 and "DELETE FROM organism_events" in p.sql
    # раннер вызывает ретеншен с троттлом
    src = _read("services/organism/runner.py")
    assert "prune_events" in src and "_last_prune" in src


# ── Мозг: подсказка о банах из событий ──────────────────────────────────────
def test_brain_surfaces_bans():
    from services.organism.brain import build_suggestions
    snap = {"fleet": {"accounts": 20, "active": 15, "dead": 0, "bans_24h": 3,
                      "governor_level": "green"},
            "ops": {}, "graph": {}, "vault": {"health": "ok"}, "goal": None}
    ids = [s["id"] for s in build_suggestions(snap)]
    assert "bans" in ids
