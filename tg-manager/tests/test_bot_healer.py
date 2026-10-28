"""Само-лечение сети ботов: вернуть молчащего бота в строй до звонка владельцу.

Проверяем сердце — решение plan_recovery (лечим только CONFLICT и только когда
вебхук реально стоит) — и проход heal_broken_bots на заглушках сети и БД: бот с
зависшим вебхуком чинится и его счётчик поломок обнуляется; конфликт без вебхука
и отозванный токен НЕ трогаем.
"""
from __future__ import annotations

import asyncio

from services import bot_healer as H
from services.bot_health import CONFLICT, UNAUTHORIZED


# ── Чистое решение ───────────────────────────────────────────────────────────

def test_conflict_with_webhook_is_healable():
    assert H.plan_recovery(CONFLICT, "https://evil.example/hook") == H.HEAL_DELETE_WEBHOOK


def test_conflict_without_webhook_not_touched():
    # пустой url при конфликте = второй поллер, вебхук ни при чём
    assert H.plan_recovery(CONFLICT, "") is None
    assert H.plan_recovery(CONFLICT, None) is None
    assert H.plan_recovery(CONFLICT, "   ") is None


def test_unauthorized_never_healed():
    assert H.plan_recovery(UNAUTHORIZED, "https://x/y") is None


def test_unknown_kind_not_healed():
    assert H.plan_recovery("network", "https://x/y") is None


# ── Проход лечения на заглушках ──────────────────────────────────────────────

class _FakeApi:
    """Заглушка bot_api: настраиваемый вебхук-info и лог вызовов deleteWebhook."""

    def __init__(self, webhook_url, delete_ok=True):
        self._url = webhook_url
        self._delete_ok = delete_ok
        self.deleted = []

    async def get_webhook_info(self, http, token):
        return {"url": self._url}

    async def delete_webhook(self, http, token):
        self.deleted.append(token)
        return {"ok": self._delete_ok}


class _Pool:
    def __init__(self, rows):
        self._rows = rows
        self.updated = []
        self.emitted = []

    async def fetch(self, q, *a):
        if "managed_bots" in q:
            return [dict(r) for r in self._rows]
        return []

    async def execute(self, q, *a):
        if "managed_bots" in q and "fail_streak=0" in q:
            self.updated.append(a[0])          # bot_id
        elif "organism_events" in q:
            self.emitted.append(a)
        return "OK"


def _row(bot_id=1, token="tok"):
    return {"bot_id": bot_id, "added_by": 7, "token": token,
            "username": "b", "first_name": "B", "fail_streak": 3}


def _run_with_api(pool, api):
    import services.bot_api as real
    saved = (real.get_webhook_info, real.delete_webhook)
    real.get_webhook_info, real.delete_webhook = api.get_webhook_info, api.delete_webhook
    try:
        return asyncio.run(H.heal_broken_bots(pool, http=None))
    finally:
        real.get_webhook_info, real.delete_webhook = saved


def test_bot_with_stuck_webhook_is_healed():
    pool = _Pool([_row(1)])
    api = _FakeApi("https://leftover.example/hook")
    healed = _run_with_api(pool, api)
    assert healed == 1
    assert api.deleted == ["tok"], "зависший вебхук должен быть снят"
    assert pool.updated == [1], "счётчик поломок бота должен обнулиться"
    assert pool.emitted, "самолечение должно оставить след в памяти организма"


def test_conflict_without_webhook_left_for_human():
    pool = _Pool([_row(2)])
    api = _FakeApi("")                          # вебхука нет — это второй поллер
    healed = _run_with_api(pool, api)
    assert healed == 0
    assert api.deleted == [] and pool.updated == []


def test_failed_delete_does_not_mark_healed():
    pool = _Pool([_row(3)])
    api = _FakeApi("https://x/y", delete_ok=False)
    healed = _run_with_api(pool, api)
    assert healed == 0 and pool.updated == []


def test_no_token_skipped():
    pool = _Pool([_row(4, token=None)])
    api = _FakeApi("https://x/y")
    assert _run_with_api(pool, api) == 0
