"""Промоут-трюк не оставляет посторонних админами канала.

Трюк добавляет человека, выдавая ему анонимную админку с правом приглашать, и
тут же её снимает. Снятие шло в одном try с выдачей: флуд или обрыв на снятии
разбирались как «цель не добавлена». Человек при этом уже был в канале — и
оставался анонимным админом с правом приглашать в канале владельца, а оператор
об этом не узнавал. Цель к тому же возвращалась в очередь, хотя её добавили.
"""
from __future__ import annotations

import asyncio

import pytest
from telethon.errors import FloodWaitError  # стаб-класс из conftest

from services import account_manager
from services import mass_inviter_engine as mie
from tests.test_invite_flood_keeps_targets import _privacy_all, promoter_stand  # noqa: F401
from tests.test_invite_queue_scheduler import (  # noqa: F401 — фикстуры стенда
    _Pool,
    _reset_account_claims,
    _run,
    stand,
)


def _flood(seconds: int) -> FloodWaitError:
    e = FloodWaitError("flood")
    e.seconds = seconds
    return e


class _Client:
    """Каждый вызов берёт следующий исход из очереди: None — успех."""

    def __init__(self, effects):
        self.effects = list(effects)
        self.calls = 0

    async def get_entity(self, ref):
        return ref

    async def __call__(self, request):
        self.calls += 1
        eff = self.effects.pop(0) if self.effects else None
        if eff is not None:
            raise eff

    async def disconnect(self):
        return None


@pytest.fixture
def engine(monkeypatch):
    def _install(client):
        async def _connect(*a, **k):
            return client

        async def _group(c, ref, acc_id=None):
            return "GRP"

        async def _fast(*a, **k):
            return None

        monkeypatch.setattr(account_manager, "connect_client", _connect)
        monkeypatch.setattr(mie, "_resolve_group_entity", _group)
        monkeypatch.setattr(asyncio, "sleep", _fast)
        return client
    return _install


@pytest.mark.asyncio
async def test_long_flood_on_revoke_counts_added_and_reports_admin(engine):
    # @a: выдача ок, снятие — длинная пауза (дважды: без повтора на длинной)
    engine(_Client([None, _flood(900)]))
    res = await mie.add_via_promote("s", {"id": 1}, "@g", ["@a", "@b"])
    assert res["ok"] == 1, "человек уже в канале — он добавлен"
    assert res["admins_left"] == ["@a"], "оператор должен узнать, что права не сняты"
    assert res["untried"] == ["@b"]
    assert res["flood_wait"] == 900
    assert "@a" not in res["still_blocked"], "добавленного не зовём повторно ссылкой"


@pytest.mark.asyncio
async def test_short_flood_on_revoke_is_waited_out(engine):
    c = engine(_Client([None, _flood(10), None, None, None]))
    res = await mie.add_via_promote("s", {"id": 1}, "@g", ["@a", "@b"])
    assert res["ok"] == 2 and res["admins_left"] == []
    assert c.calls == 5, "выдача, снятие (пауза), снятие, выдача, снятие"


@pytest.mark.asyncio
async def test_revoke_promoted_reports_who_is_left(engine):
    engine(_Client([None, RuntimeError("x"), RuntimeError("x")]))
    res = await mie.revoke_promoted("s", {"id": 1}, "@g", ["@a", "@b"])
    assert res == {"revoked": ["@a"], "left": ["@b"]}


# ── исполнитель ──────────────────────────────────────────────────────────────

def _setup(promoter_stand, monkeypatch, left_after_retry):
    s = promoter_stand(lambda acc_id, refs, dry=False: _privacy_all(refs))
    s.revokes = []

    async def _trick(sess, acc, group, refs):
        return {"ok": len(refs), "failed": 0, "errors": [], "still_blocked": [],
                "untried": [], "admins_left": list(refs[:1])}
    monkeypatch.setattr(mie, "add_via_promote", _trick)

    async def _revoke(sess, acc, group, refs):
        s.revokes.append((int(acc["id"]), list(refs)))
        return {"revoked": [r for r in refs if r not in left_after_retry],
                "left": [r for r in refs if r in left_after_retry]}
    monkeypatch.setattr(mie, "revoke_promoted", _revoke)
    return s


def test_unrevoked_admin_is_retried_by_promoter_then_reported(promoter_stand, monkeypatch):
    s = _setup(promoter_stand, monkeypatch, left_after_retry={"u1"})
    res = _run(_Pool(), ["u1", "u2"], account_ids=[1, 2, 3])
    assert s.revokes == [(1, ["u1"])], "повторное снятие — промоутером"
    assert "не сняты права админа" in res["summary"] and "u1" in res["summary"]


def test_successful_retry_leaves_no_warning(promoter_stand, monkeypatch):
    s = _setup(promoter_stand, monkeypatch, left_after_retry=set())
    res = _run(_Pool(), ["u1", "u2"], account_ids=[1, 2, 3])
    assert s.revokes
    assert "не сняты права админа" not in res["summary"]
