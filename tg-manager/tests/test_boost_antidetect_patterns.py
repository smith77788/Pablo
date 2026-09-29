"""Накрутка не должна оставлять машинных следов на свежем канале.

Аудит следов автоматизации:
  • boost_reactions ставил ОДИН и тот же ❤ со всех аккаунтов (дефолт emoji) —
    реальная аудитория даёт разнобой реакций;
  • boost_subscribers сеял подписчиков с пейсингом 3–7с — «0→N за минуты»
    силами связанного флота = сигнатура накрутки.

Здесь фиксируем: реакции раздаются из набора случайно на аккаунт (явный emoji
уважается), а посев идёт шире и с поправкой на время суток.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services import op_worker

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _body(fn: str) -> str:
    i = WORKER.index(f"async def {fn}(")
    j = WORKER.index("\nasync def ", i + 1)
    return WORKER[i:j]


class _FakePool:
    async def execute(self, q, *a):
        return "UPDATE 1"

    async def fetch(self, q, *a):
        return []

    async def fetchval(self, q, *a):
        return 0

    async def fetchrow(self, q, *a):
        return None


def _run(coro):
    return asyncio.run(coro)


def _accs(n):
    return [{"id": i, "session_str": "s", "first_name": f"A{i}", "phone": "+1"} for i in range(1, n + 1)]


# ── F2: разнобой реакций ──────────────────────────────────────────────────────

def test_reactions_default_set_has_variety():
    body = _body("_exec_boost_reactions")
    assert 'params.get("emojis")' in body
    assert "random.choice(_emojis)" in body, "реакция выбирается случайно на аккаунт"
    # дефолтный набор — не один ❤, а несколько реакций
    m = re.search(r'_emojis = \["❤"[^\]]*\]', body)
    assert m and m.group(0).count(",") >= 4, "дефолтный набор реакций должен быть разнообразным"


def test_reactions_spread_across_accounts_not_all_same():
    calls = []

    async def _fake_reaction(session, acc, channel, msg_id, emoji):
        calls.append(emoji)
        return {"ok": True}

    with patch("services.boost_engine.boost_reaction", _fake_reaction), \
         patch.object(op_worker.resource_selector, "select_all_active",
                      AsyncMock(side_effect=lambda *a, **k: _accs(12))), \
         patch.object(op_worker, "_claim_available_accounts",
                      AsyncMock(side_effect=lambda op_id, accs, owner: accs)), \
         patch.object(op_worker, "_filter_quarantined_accounts",
                      AsyncMock(side_effect=lambda pool, op_id, accs: (accs, 0))), \
         patch.object(op_worker, "completed_targets", AsyncMock(return_value=set())), \
         patch.object(op_worker, "_is_cancelled", AsyncMock(return_value=False)), \
         patch.object(op_worker, "_record_boost_flood", AsyncMock()), \
         patch("asyncio.sleep", new=AsyncMock()):
        res = _run(op_worker._exec_boost_reactions(
            _FakePool(), None, 5, 99,
            {"channel": "@c", "msg_id": 7, "account_ids": list(range(1, 13))}))
    assert res["status"] == "done"
    # 12 аккаунтов из набора 8 реакций — практически наверняка больше одной разной
    assert len(set(calls)) > 1, "реакции не должны быть все одинаковыми"


def test_reactions_explicit_emoji_respected():
    calls = []

    async def _fake_reaction(session, acc, channel, msg_id, emoji):
        calls.append(emoji)
        return {"ok": True}

    with patch("services.boost_engine.boost_reaction", _fake_reaction), \
         patch.object(op_worker.resource_selector, "select_all_active",
                      AsyncMock(side_effect=lambda *a, **k: _accs(4))), \
         patch.object(op_worker, "_claim_available_accounts",
                      AsyncMock(side_effect=lambda op_id, accs, owner: accs)), \
         patch.object(op_worker, "_filter_quarantined_accounts",
                      AsyncMock(side_effect=lambda pool, op_id, accs: (accs, 0))), \
         patch.object(op_worker, "completed_targets", AsyncMock(return_value=set())), \
         patch.object(op_worker, "_is_cancelled", AsyncMock(return_value=False)), \
         patch.object(op_worker, "_record_boost_flood", AsyncMock()), \
         patch("asyncio.sleep", new=AsyncMock()):
        _run(op_worker._exec_boost_reactions(
            _FakePool(), None, 5, 99,
            {"channel": "@c", "msg_id": 7, "emoji": "🔥", "account_ids": [1, 2, 3, 4]}))
    assert set(calls) == {"🔥"}, "явно заданный пользователем emoji обязан уважаться"


# ── F3: пейсинг посева подписчиков ────────────────────────────────────────────

def test_seed_pacing_wider_and_time_of_day_scaled():
    body = _body("_exec_boost_subscribers")
    assert "random.uniform(8.0, 25.0)" in body, "разброс посева должен быть шире 3–7с"
    assert "time_of_day_factor()" in body, "пейсинг посева масштабируется временем суток"
