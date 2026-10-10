"""Бренд-бот @MEXAHI3MBOT в свежих каналах — только free-tier и не мгновенно.

Аудит следов автоматизации: create_channel безусловно добавлял сторонний бот
@MEXAHI3MBOT админом с полными правами и пинил рекламу в ту же секунду в КАЖДЫЙ
созданный канал. Один bot_id админом в сотнях свежих каналов = межканальный
граф-след «фабрики», по которому антиспам Telegram уводит их в теневой бан.

Фикс: create_channel получил brand_promo (по умолчанию True — совместимость).
Фабрики передают brand_promo=is_user_free_tier(owner): платным бренд-след не
ставится вовсе, free-tier — ставится, но после паузы (не в ту же секунду).
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services import account_manager

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


class _Ch:
    id = 123
    access_hash = 456
    title = "T"
    username = ""


class _Res:
    chats = [_Ch()]


class _FakeClient:
    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def __call__(self, req):
        return _Res()


def _run(coro):
    return asyncio.run(coro)


def test_create_channel_skips_brand_when_disabled():
    with patch.object(account_manager, "_make_client", return_value=_FakeClient()), \
         patch("services.brand_injection.add_botmother_as_channel_admin", AsyncMock()) as adm, \
         patch("services.brand_injection.post_welcome_and_pin", AsyncMock()) as pin, \
         patch("asyncio.sleep", new=AsyncMock()):
        res = _run(account_manager.create_channel("s" * 20, "Канал", brand_promo=False))
    assert res.get("channel_id") == 123
    assert adm.await_count == 0
    assert pin.await_count == 0


def test_create_channel_adds_brand_for_free_tier_after_pause():
    with patch.object(account_manager, "_make_client", return_value=_FakeClient()), \
         patch("services.brand_injection.add_botmother_as_channel_admin", AsyncMock()) as adm, \
         patch("services.brand_injection.post_welcome_and_pin", AsyncMock()) as pin, \
         patch("asyncio.sleep", new=AsyncMock()) as slp:
        res = _run(account_manager.create_channel("s" * 20, "Канал", brand_promo=True))
    assert res.get("channel_id") == 123
    assert adm.await_count == 1
    assert pin.await_count == 1
    # пауза перед бренд-залпом — не в ту же секунду, что создан канал
    assert slp.await_count >= 1


def test_all_factory_callers_gate_brand_by_tier():
    # Каждый фабричный вызов create_channel передаёт brand_promo=_brand_free,
    # а _brand_free вычисляется через is_user_free_tier. Иначе бренд-след
    # вернётся к платным и к мгновенному залпу.
    assert WORKER.count("brand_promo=_brand_free") == 6, (
        "все 6 вызовов create_channel в фабриках обязаны гейтить бренд по тарифу"
    )
    # тариф считается в каждой из четырёх фабричных функций
    assert WORKER.count("is_user_free_tier(pool, owner_id)") >= 4
    # «голых» вызовов create_channel без brand_promo в op_worker не осталось
    naked = re.findall(r"create_channel\([^)]*_acc=acc\s*\)", WORKER)
    assert not naked, f"остались вызовы create_channel без гейта бренда: {naked}"
