"""Регресс: «Мать-Дочка» — одноразовые дочерние группы поглощают бан-риск инвайта.

Первопричина: массовый инвайт напрямую в боевую группу означает, что при
закрытии Telegram чата (ChatWriteForbiddenError/ChannelPrivateError — уже
классифицируется как "group error" в mass_inviter_engine) сгорает БОЕВОЙ
канал. Решение — вставить одноразовую дочернюю группу между инвайтом и целью:
закрылась дочерняя — сгорел расходник, op_worker подставляет следующую и
продолжает прогон (см. services/op_worker.py::_exec_mass_invite,
_rotate_daughter).

Два уровня проверки:
  - статические assert'ы по исходникам — прогоняются всегда, без БД;
  - реальный Postgres (INFRAGRAM_TEST_DSN) — DB-логика get_or_create_active/
    mark_burned, включая идемпотентность и пересоздание после сгорания.
"""
from __future__ import annotations

import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ── статика: подключение к исполнителю и защитный предохранитель ────────────

def test_exec_mass_invite_reads_use_daughter_groups_and_rotates_on_group_error():
    src = _read("services/op_worker.py")
    i = src.index("async def _exec_mass_invite")
    seg = src[i:src.index("\nasync def _exec_", i + 1)]
    assert 'params.get("use_daughter_groups")' in seg
    assert "daughter_groups.get_or_create_active" in seg
    assert "daughter_groups.mark_burned" in seg
    # Ротация должна пробоваться ИМЕННО на сигнале «чат закрыт», не на любой
    # ошибке — иначе можно сжечь дочернюю группу по временной, не относящейся
    # к чату причине.
    assert "await _rotate_daughter(" in seg


def test_rotation_has_a_bounded_retry_count():
    """Без потолка сбой классификации плодил бы каналы без остановки."""
    src = _read("services/daughter_groups.py")
    assert "MAX_ROTATIONS_PER_RUN" in src
    seg = src[src.index("async def _create_new"):]
    # значение — небольшое целое, а не 0/бесконечность
    import re
    m = re.search(r"MAX_ROTATIONS_PER_RUN\s*=\s*(\d+)", _read("services/daughter_groups.py"))
    assert m and 1 <= int(m.group(1)) <= 10


def test_rotate_daughter_checked_before_giving_up_in_both_abort_paths():
    """Оба места, где раньше был безусловный `group_broken = True` (safe-режим
    и «group error»), теперь сначала пробуют ротацию — иначе фича молча не
    работает в одном из двух путей остановки."""
    src = _read("services/op_worker.py")
    i = src.index("async def _exec_mass_invite")
    seg = src[i:src.index("\nasync def _exec_", i + 1)]
    assert seg.count("await _rotate_daughter(") == 2


def test_does_not_reuse_the_fixed_brand_billboard_pin():
    """Редирект на мать — НЕ переиспользует brand_injection.post_welcome_and_pin
    (это фиксированный рекламный pin продукта, не связанный с конкретной
    кампанией) — иначе смена одного сломала бы другое."""
    src = _read("services/daughter_groups.py")
    body = src.split('"""', 2)[-1]  # без вводного докстринга модуля (упоминает его для контекста)
    assert "import brand_injection" not in body
    assert "post_welcome_and_pin" not in body


def test_creates_an_invite_link_not_a_bare_id():
    """group_ref обязан резолвиться ЛЮБЫМ инвайтящим аккаунтом, а не только
    создателем: голый channel_id без access_hash в кеше сессии другого
    аккаунта не резолвится (get_entity по id требует уже виденную entity)."""
    src = _read("services/daughter_groups.py")
    assert "create_channel_invite_link" in src
    seg = src[src.index("async def _create_new"):src.index("async def _pin_mother_redirect")]
    assert "group_ref = link_res" in seg, "group_ref обязан браться из выпущенной ссылки, не из id"


def test_ui_and_api_expose_the_toggle():
    from tests.miniapp_source import miniapp_source
    ui = miniapp_source()
    api = _read("services/mini_app_api.py")
    assert 'id="invDaughterGroups"' in ui
    assert "body.use_daughter_groups" in ui
    assert '"use_daughter_groups"' in api


# ── реальный Postgres: DB-логика get_or_create_active / mark_burned ──────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark_db = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pg_pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        await conn.execute(_read("schema_v200_daughter_groups.sql"))
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=2)

    try:
        pool = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield pool
    _run(pool.execute("DELETE FROM daughter_groups WHERE owner_id IN (990101, 990102)"))
    _run(pool.close())
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


def _patch_telethon_calls(dg, monkeypatch):
    import services.account_manager as am

    seq = {"n": 0}

    async def fake_create_channel(session_str, title, about, megagroup, _acc):
        seq["n"] += 1
        return {"channel_id": 1000 + seq["n"], "access_hash": 1, "title": title,
                "username": "", "type": "group", "invite_link": ""}

    async def fake_create_channel_invite_link(session_str, channel_id, _acc, access_hash, **kw):
        return {"ok": True, "link": f"https://t.me/+FAKE{channel_id}"}

    async def fake_pin(*a, **kw):
        return True

    monkeypatch.setattr(am, "create_channel", fake_create_channel)
    monkeypatch.setattr(am, "create_channel_invite_link", fake_create_channel_invite_link)
    monkeypatch.setattr(dg, "_pin_mother_redirect", fake_pin)


def test_get_or_create_active_reuses_the_existing_row(pg_pool, monkeypatch):
    from services import daughter_groups as dg
    _patch_telethon_calls(dg, monkeypatch)
    creator = {"session_str": "x", "id": 1}

    r1 = _run(dg.get_or_create_active(pg_pool, 990101, "@mother_a", creator))
    r2 = _run(dg.get_or_create_active(pg_pool, 990101, "@mother_a", creator))
    assert r1["ok"] and r2["ok"]
    assert r1["id"] == r2["id"], "активная дочерняя группа должна переиспользоваться, а не плодиться"


def test_mark_burned_makes_the_next_call_create_a_new_group(pg_pool, monkeypatch):
    from services import daughter_groups as dg
    _patch_telethon_calls(dg, monkeypatch)
    creator = {"session_str": "x", "id": 1}

    r1 = _run(dg.get_or_create_active(pg_pool, 990101, "@mother_b", creator))
    _run(dg.mark_burned(pg_pool, r1["id"], "ChatWriteForbiddenError"))
    r2 = _run(dg.get_or_create_active(pg_pool, 990101, "@mother_b", creator))
    assert r2["id"] != r1["id"], "после сгорания активная группа должна пересоздаться"

    row = _run(pg_pool.fetchrow(
        "SELECT status, burn_reason FROM daughter_groups WHERE id=$1", r1["id"]))
    assert row["status"] == "burned"
    assert row["burn_reason"] == "ChatWriteForbiddenError"


def test_different_owners_never_share_a_daughter_group(pg_pool, monkeypatch):
    from services import daughter_groups as dg
    _patch_telethon_calls(dg, monkeypatch)
    creator = {"session_str": "x", "id": 1}

    r1 = _run(dg.get_or_create_active(pg_pool, 990101, "@same_mother", creator))
    r2 = _run(dg.get_or_create_active(pg_pool, 990102, "@same_mother", creator))
    assert r1["id"] != r2["id"]


def test_create_failure_is_reported_not_swallowed(pg_pool, monkeypatch):
    from services import daughter_groups as dg
    import services.account_manager as am

    async def failing_create_channel(session_str, title, about, megagroup, _acc):
        return {"error": "FloodWait 3600с — Telegram ограничил создание"}

    monkeypatch.setattr(am, "create_channel", failing_create_channel)
    creator = {"session_str": "x", "id": 1}
    r = _run(dg.get_or_create_active(pg_pool, 990101, "@mother_fail", creator))
    assert r["ok"] is False and "FloodWait" in r["error"]
