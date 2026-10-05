"""Инвайт из хранилища контактов (CRM) как четвёртый источник целей.

Хранилище контактов (unified_contacts) — накопленная база CRM, которая до сих
пор не была источником инвайта. Инвайтер умел парсер, ручной ввод и телефоны, а
контакты — нет. Здесь проверяется, что новый источник:
  • отдаёт цели в ТОТ ЖЕ конвейер mass_invite (вся флуд-защита достаётся даром);
  • считает и материализует цели ОДНИМ предикатом (иначе оператор увидит одно
    число, а пригласит другое);
  • предпочитает дешёвый идентификатор (@username → id → телефон), чтобы не
    тащить лишний импорт контакта там, где хватает username.
"""
from __future__ import annotations

import ast
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HANDLER = os.path.join(ROOT, "bot", "handlers", "mass_inviter.py")

from bot.handlers import mass_inviter as mi  # noqa: E402


# ── чистые функции ───────────────────────────────────────────────────────────

def test_invitable_predicate_requires_an_identifier():
    where, args = mi._crm_invitable_where(42, None)
    assert args == [42]
    # пригоден = есть username ИЛИ id ИЛИ телефон
    assert "telegram_user_id IS NOT NULL" in where
    assert "username IS NOT NULL" in where
    assert "jsonb_array_length" in where


def test_fav_filter_adds_favorite_condition():
    where, args = mi._crm_invitable_where(42, {"kind": "fav"})
    assert "is_favorite = TRUE" in where
    assert args == [42]


def test_tag_filter_is_parameterized_not_interpolated():
    where, args = mi._crm_invitable_where(42, {"kind": "tag", "tag": "vip"})
    # тег обязан идти аргументом, а не в строку (инъекция + корректность)
    assert "= ANY(tags)" in where
    assert args == [42, "vip"]
    assert "vip" not in where


def test_split_prefers_username_then_phone_then_id():
    """Номер раньше голого id: по id пригласит только аккаунт, у кого человек уже
    в контактах, а по номеру — любой аккаунт флота (владелец, 05.10.2026:
    «из хранилища контактов должны инвайтить все аккаунты»)."""
    rows = [
        {"telegram_user_id": 111, "username": "alice", "phones": ["+700"]},
        {"telegram_user_id": 222, "username": None, "phones": ["+711"]},
        {"telegram_user_id": None, "username": None, "phones": ["+722"]},
        {"telegram_user_id": 333, "username": None, "phones": []},
    ]
    user_refs, phones = mi._crm_split_targets(rows)
    assert user_refs == ["@alice", "333"], "id — только когда нет ни username, ни номера"
    assert phones == ["+711", "+722"]


def test_hub_phone_without_plus_is_normalised():
    from services.contacts_hub.invite_target import invite_target
    assert invite_target({"username": None, "phones": '["79001234567"]',
                          "telegram_user_id": 5}) == ("phone", "+79001234567")


def test_split_dedups_across_identifier_kinds():
    rows = [
        {"telegram_user_id": None, "username": "bob", "phones": []},
        {"telegram_user_id": None, "username": "@bob", "phones": []},   # тот же
        {"telegram_user_id": None, "username": None, "phones": ["+7"]},
        {"telegram_user_id": None, "username": None, "phones": ["+7"]}, # тот же
    ]
    user_refs, phones = mi._crm_split_targets(rows)
    assert user_refs == ["@bob"]
    assert phones == ["+7"]


def test_split_handles_phones_as_json_string_and_skips_empty():
    rows = [
        {"telegram_user_id": None, "username": None, "phones": json.dumps(["+790"])},
        {"telegram_user_id": None, "username": None, "phones": "[]"},
        {"telegram_user_id": None, "username": "", "phones": ["", None, "+791"]},
    ]
    user_refs, phones = mi._crm_split_targets(rows)
    assert user_refs == []
    assert phones == ["+790", "+791"], "пустые элементы и '[]' не должны стать целями"


# ── разводка ────────────────────────────────────────────────────────────────

def _src() -> str:
    with open(HANDLER, encoding="utf-8") as f:
        return f.read()


def test_source_button_is_offered():
    src = _src()
    assert 'InviterCb(action="src_crm")' in src, "нет кнопки источника CRM в меню"


def test_handlers_are_registered():
    """Роуты src_crm и pick_crm обязаны существовать, иначе кнопка мертва."""
    src = _src()
    tree = ast.parse(src)
    handled = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            txt = ast.dump(dec)
            for act in ("src_crm", "pick_crm"):
                if f"'{act}'" in txt or f'"{act}"' in txt:
                    handled.add(act)
    assert {"src_crm", "pick_crm"} <= handled, (
        f"не зарегистрированы обработчики: {{'src_crm','pick_crm'}} - {handled}")


def test_confirm_materializes_crm_via_the_same_predicate():
    """Ветка crm в confirm обязана звать _crm_invitable_where — тот же предикат,
    что и подсчёт. Иначе показанное число разойдётся с реально приглашёнными."""
    src = _src()
    tree = ast.parse(src)
    lines = src.split("\n")
    confirm = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "cb_inviter_confirm":
            confirm = "\n".join(lines[n.lineno - 1:n.end_lineno])
    assert confirm, "cb_inviter_confirm не найдена"
    assert 'source_type == "crm"' in confirm, "нет ветки источника crm в confirm"
    assert "_crm_invitable_where" in confirm, (
        "материализация целей CRM идёт не тем предикатом, что подсчёт")
    assert "_crm_split_targets" in confirm, "цели CRM не раскладываются на refs/phones"


def test_source_label_names_crm():
    assert '"crm":' in _src(), "источник CRM не подписан в сводке операции"


# ── живой Postgres: SQL реально исполняется ──────────────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
def test_queries_run_on_real_schema():
    """jsonb_array_length, unnest(tags), $N-тег — реально исполнимы на схеме."""
    import asyncio
    import asyncpg

    async def _run():
        conn = await asyncpg.connect(DSN)
        try:
            with open(os.path.join(ROOT, "schema_v138.sql"), encoding="utf-8") as f:
                await conn.execute(f.read())
            await conn.execute("DELETE FROM unified_contacts WHERE owner_id=$1", 9001)
            await conn.execute(
                "INSERT INTO unified_contacts(owner_id, telegram_user_id, username, phones, tags, is_favorite) "
                "VALUES "
                "($1, 5, 'ada', '[]'::jsonb, ARRAY['vip'], TRUE),"
                "($1, NULL, NULL, '[\"+70000000001\"]'::jsonb, ARRAY['cold'], FALSE),"
                "($1, NULL, NULL, '[]'::jsonb, ARRAY['empty'], FALSE)",   # непригоден
                9001)

            where, args = mi._crm_invitable_where(9001, None)
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM unified_contacts WHERE {where}", *args)
            assert total == 2, "непригодный контакт (без id/username/телефона) попал в счёт"

            fav_where, fav_args = mi._crm_invitable_where(9001, {"kind": "fav"})
            fav = await conn.fetchval(
                f"SELECT COUNT(*) FROM unified_contacts WHERE {fav_where}", *fav_args)
            assert fav == 1

            tag_where, tag_args = mi._crm_invitable_where(9001, {"kind": "tag", "tag": "vip"})
            vip = await conn.fetchval(
                f"SELECT COUNT(*) FROM unified_contacts WHERE {tag_where}", *tag_args)
            assert vip == 1

            # unnest(tags) для топ-тегов
            tag_rows = await conn.fetch(
                f"SELECT t AS tag, COUNT(*) AS cnt FROM unified_contacts, unnest(tags) AS t "
                f"WHERE {where} GROUP BY t ORDER BY cnt DESC, t LIMIT 6", *args)
            assert {r["tag"] for r in tag_rows} == {"vip", "cold"}

            # материализация: ada → @ada (username), безымянный с телефоном → phones
            crows = await conn.fetch(
                f"SELECT telegram_user_id, username, phones FROM unified_contacts "
                f"WHERE {where} LIMIT 100000", *args)
            user_refs, phones = mi._crm_split_targets(crows)
            assert user_refs == ["@ada"]
            assert phones == ["+70000000001"]
        finally:
            await conn.execute("DELETE FROM unified_contacts WHERE owner_id=$1", 9001)
            await conn.close()

    asyncio.run(_run())


def test_segment_source_sends_hub_contact_by_phone_not_bare_id(monkeypatch):
    """Исполнитель (источник «сегмент хранилища») отдаёт номер, а не голый id."""
    from services import mass_inviter_engine as inv
    from services.contacts_hub import repository as crepo
    from tests.test_invite_queue_scheduler import (
        _SourcePool, _ok, _run_source, source_stand as _ss, stand as _st)  # noqa: F401

    async def _seg(pool, owner, filters, limit=None):
        return [{"username": None, "telegram_user_id": 222, "phones": ["79001112233"]},
                {"username": None, "telegram_user_id": 333, "phones": []}]
    monkeypatch.setattr(crepo, "resolve_segment", _seg)
    by_phone: list = []
    by_ref: list = []

    async def _batch(sess, acc, group, refs, pace_mult=1.0, bulk=None):
        by_ref.extend(refs)
        return _ok(len(refs))

    async def _phones(sess, acc, group, refs, skip_keys=None):
        by_phone.extend(refs)
        return {**_ok(len(refs)), "invited_phones": list(refs), "invited_aliases": [],
                "already_phones": []}
    # стенд из соседнего файла: сначала общие заглушки, потом наши движки
    import tests.test_invite_queue_scheduler as qs
    install = qs.stand.__wrapped__(monkeypatch) if hasattr(qs.stand, "__wrapped__") else None
    assert install is not None
    s = qs.source_stand.__wrapped__(install, monkeypatch)(_SourcePool([]), lambda *a, **k: _ok(0))
    del s
    monkeypatch.setattr(inv, "invite_batch", _batch)
    monkeypatch.setattr(inv, "invite_by_phones", _phones)
    _run_source(_SourcePool([]), source="segment", segment_filters={})
    assert "+79001112233" in by_phone, "номер контакта не дошёл до инвайта по номеру"
    assert "222" not in [str(r) for r in by_ref], "голый id ушёл вместо номера"
    assert "333" in [str(r) for r in by_ref], "без номера id всё ещё годится"
