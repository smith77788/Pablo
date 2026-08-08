"""Anti-detection: narrative-движок не публикует через флагнутый аккаунт.

`narrative_engine.execute_pending_posts` крутит фоновый цикл и постит в каналы
владельца через реальный Telethon-аккаунт. Если этот аккаунт под недавним
серьёзным ограничением (риск-пульс), публикация = активность флагнутого аккаунта
→ эскалация к хард-бану (anti-detection слой — дороже обычной фичи).

При этом провал в движке помечает пост `status='failed'` НАВСЕГДА — поэтому
нельзя просто «зафейлить» карантинный пост (потеряем его). Правильно —
ОТЛОЖИТЬ: оставить `pending`, сдвинуть `scheduled_at` вперёд. Опубликуется,
когда аккаунт выйдет из карантина. Гейт fail-open.
"""
from __future__ import annotations

import asyncio
import inspect

from services import narrative_engine


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakePool:
    def __init__(self):
        self.executed: list[tuple] = []

    async def execute(self, q, *a):
        self.executed.append((q, a))
        return "OK"

    async def fetchrow(self, q, *a):
        return None


def test_quarantined_account_defers_not_publishes(monkeypatch):
    """Флагнутый аккаунт → пост отложен (scheduled_at сдвинут), НЕ опубликован,
    НЕ помечен failed, _publish_post НЕ вызван."""
    pool = _FakePool()

    async def _quar(_pool, _aid):
        return True
    monkeypatch.setattr(narrative_engine, "_is_quarantined", _quar)

    published = {"called": False}

    async def _boom_publish(_pool, _post):
        published["called"] = True
        return True, ""
    monkeypatch.setattr(narrative_engine, "_publish_post", _boom_publish)

    async def _noop_completion(_pool, _cid):
        return None
    monkeypatch.setattr(narrative_engine, "_check_campaign_completion", _noop_completion)
    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(narrative_engine.asyncio, "sleep", _no_sleep)

    posts = [{"id": 7, "acc_id": 42, "campaign_id": 1}]
    count = _run(narrative_engine._execute_with_session(pool, posts))

    assert count == 0, "карантинный пост не должен считаться опубликованным"
    assert published["called"] is False, "нельзя постить через флагнутый аккаунт"
    joined = " ".join(q for q, _ in pool.executed)
    assert "scheduled_at" in joined and "interval" in joined, "пост должен быть отложен"
    assert "status='failed'" not in joined, "отложенный пост нельзя терять (failed)"


def test_healthy_account_still_publishes(monkeypatch):
    """Здоровый аккаунт (fail-open, не в карантине) → публикуем как раньше."""
    pool = _FakePool()

    async def _ok(_pool, _aid):
        return False
    monkeypatch.setattr(narrative_engine, "_is_quarantined", _ok)

    published = {"called": False}

    async def _publish(_pool, _post):
        published["called"] = True
        return True, ""
    monkeypatch.setattr(narrative_engine, "_publish_post", _publish)

    async def _noop_completion(_pool, _cid):
        return None
    monkeypatch.setattr(narrative_engine, "_check_campaign_completion", _noop_completion)
    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(narrative_engine.asyncio, "sleep", _no_sleep)

    posts = [{"id": 8, "acc_id": 42, "campaign_id": 1}]
    count = _run(narrative_engine._execute_with_session(pool, posts))

    assert count == 1 and published["called"] is True


def test_quarantine_wrapper_is_fail_open():
    # обёртка обязана глушить ошибку пульса в False (не рушить цикл)
    src = inspect.getsource(narrative_engine._is_quarantined)
    assert "return False" in src and "except" in src
