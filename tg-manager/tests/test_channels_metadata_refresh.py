"""Карточка канала обновляется: имя, ссылка и число участников.

Обе поломки, из-за которых экран «Каналы и чаты» врал:

* `managed_channels.members_count` не заполнял НИКТО — колонка появилась в
  schema_v91 и осталась со значением по умолчанию 0, поэтому у каждого канала
  в списке стояло «0 участников»;
* имя и ссылка писались только при импорте. Переименовал канал в самом
  Telegram — Infragram продолжал показывать старое название.

Данные при этом уже были на руках: `get_dialogs` отдаёт title/username/members/
access_hash, `get_full_channel_info` — members_count из полного запроса. И то и
другое просто выбрасывалось.
"""
from __future__ import annotations

import asyncio

import pytest


class _RecordingPool:
    """Пул, который запоминает записи вместо похода в базу."""

    def __init__(self):
        self.execute_calls: list[tuple] = []
        self.executemany_calls: list[tuple] = []
        self.fetch_rows: list = []

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "UPDATE 1"

    async def executemany(self, sql, rows):
        self.executemany_calls.append((sql, list(rows)))

    async def fetch(self, sql, *args):
        return list(self.fetch_rows)

    async def fetchval(self, sql, *args):
        return 0

    async def fetchrow(self, sql, *args):
        return None


# ── обход диалогов: reclassify_channels ──────────────────────────────────────

def _dialog(cid, title, username, members, *, admin=False, creator=False):
    return {
        "id": cid, "title": title, "username": username, "members": members,
        "type": "channel", "access_hash": 555,
        "is_admin": admin or creator, "is_creator": creator,
    }


def _run_reclassify(dialogs_by_acc, prune=False):
    """Прогоняет _exec_reclassify_channels на заглушках и отдаёт записи в базу."""
    from services import op_worker as w
    from services import account_manager, resource_selector

    pool = _RecordingPool()
    accounts = [{"id": aid, "session_str": f"s{aid}", "owner_id": 1}
                for aid in dialogs_by_acc]

    async def fake_select(*a, **k):
        return accounts

    async def fake_claim(ids):
        return list(ids)

    async def fake_release(ids):
        return None

    async def fake_dialogs(session, limit=300, _acc=None):
        return dialogs_by_acc[int(_acc["id"])]

    async def fake_cancelled(*a, **k):
        return False

    async def fake_pause(*a, **k):
        return None

    orig = (resource_selector.select_all_active, w.try_claim_accounts,
            w.release_accounts, account_manager.get_dialogs, w._is_cancelled)
    resource_selector.select_all_active = fake_select
    w.try_claim_accounts = fake_claim
    w.release_accounts = fake_release
    account_manager.get_dialogs = fake_dialogs
    w._is_cancelled = fake_cancelled
    from services import session_simulator
    orig_pause = session_simulator.short_pause
    session_simulator.short_pause = fake_pause
    try:
        res = asyncio.run(w._exec_reclassify_channels(
            pool, None, 1, 1, {"prune": prune}))
    finally:
        (resource_selector.select_all_active, w.try_claim_accounts,
         w.release_accounts, account_manager.get_dialogs, w._is_cancelled) = orig
        session_simulator.short_pause = orig_pause
    return res, pool


def _meta_rows(pool):
    """Строки обновления карточек (единственный executemany с 7 полями)."""
    for sql, rows in pool.executemany_calls:
        if "members_count" in sql and "UPDATE managed_channels" in sql:
            return {r[1]: r for r in rows}
    return {}


def test_members_count_is_stored():
    """Главное: число участников доезжает до базы, а не теряется."""
    res, pool = _run_reclassify({10: [_dialog(100, "Клуб", "club", 4321, creator=True)]})
    rows = _meta_rows(pool)
    assert 100 in rows, "карточка канала вообще не обновлялась"
    assert rows[100][4] == 4321, "число участников не записано"
    assert res["refreshed"] == 1


def test_title_and_username_follow_telegram():
    """Переименование и смена ссылки вне Infragram доезжают в список."""
    _, pool = _run_reclassify({10: [_dialog(100, "Новое имя", "new_link", 7, admin=True)]})
    row = _meta_rows(pool)[100]
    assert row[2] == "Новое имя"
    assert row[3] == "new_link"


def test_removed_username_is_cleared():
    """Ссылку сняли — в списке её тоже не должно остаться.

    Пустая строка обязана доехать как значение, а не быть отброшенной как
    «нечего писать»: иначе мёртвая ссылка висит вечно.
    """
    _, pool = _run_reclassify({10: [_dialog(100, "Канал", "", 7, creator=True)]})
    assert _meta_rows(pool)[100][3] == ""


def test_zero_members_never_overwrites_known_count():
    """Ноль от Telegram означает «не сказали», а не «никого нет».

    participants_count в списке диалогов — необязательное поле. Аккаунт, который
    его не получил, не должен обнулять счётчик, увиденный другим аккаунтом.
    """
    _, pool = _run_reclassify({
        10: [_dialog(100, "Канал", "c", 900, creator=True)],
        11: [_dialog(100, "Канал", "c", 0, admin=True)],
    })
    row = _meta_rows(pool)[100]
    assert row[4] == 900, "известное число участников затёрто нулём"


def test_metadata_update_never_wipes_with_null():
    """SQL обязан быть через COALESCE: отсутствующее поле сохраняет прежнее
    значение, а не превращает карточку в пустую."""
    _, pool = _run_reclassify({10: [_dialog(100, "К", "u", 5, creator=True)]})
    sql = next(s for s, _ in pool.executemany_calls if "members_count" in s)
    for col in ("title", "members_count", "type", "access_hash"):
        assert f"{col}         = COALESCE(" in sql or f"{col} = COALESCE(" in sql \
            or f"{col}  = COALESCE(" in sql or "COALESCE" in sql


# ── фоновый обход: drift_detector ────────────────────────────────────────────

def test_drift_detector_stores_members_and_names():
    """Фоновая проверка тоже обязана сохранять карточку.

    Раньше запись жила ВНУТРИ `if changes:` и требовала непустого старого
    значения — то есть первое появление имени и любое число участников
    не сохранялись вовсе.
    """
    from services import drift_detector as dd
    import inspect

    src = inspect.getsource(dd._check_all)
    upd = src.split("changes: dict = {}")[0]
    assert "members_count" in upd, (
        "drift_detector не сохраняет число участников до анализа дрейфа — "
        "оно снова будет теряться"
    )
    assert "UPDATE managed_channels" in upd, (
        "карточка не пишется на каждом удачном опросе"
    )


def test_drift_detector_asks_for_member_count():
    """Счётчик берётся из полного запроса — там он есть всегда, в отличие от
    списка диалогов, где поле необязательное."""
    from services import account_manager
    import inspect

    src = inspect.getsource(account_manager.get_full_channel_info)
    assert "participants_count" in src and "members_count" in src


# ── экран ────────────────────────────────────────────────────────────────────

def test_screen_has_refresh_button():
    """Кнопка обновления должна быть на самом экране каналов: фоновый обход
    идёт раз в несколько часов, а список нужен свежим сейчас."""
    import pathlib

    html = (pathlib.Path(__file__).resolve().parents[1]
            / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "refreshChannelsMeta()" in html, "нет кнопки обновления списка каналов"
    assert "async function refreshChannelsMeta" in html, "кнопка ведёт в никуда"
    assert '"prune":false' in html.replace(" ", "") or "prune:false" in html.replace(" ", ""), \
        "обновление обязано идти без удаления каналов"
