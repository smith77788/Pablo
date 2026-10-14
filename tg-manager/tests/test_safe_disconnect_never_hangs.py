"""Закрытие сокета аккаунта не морозит операцию.

ЧТО ЛОМАЛОСЬ. `client.disconnect()` Telethon на заклиненном сокете (мёртвый
прокси, сеть пропала посреди запроса) висел бесконечно. А вызывается он в
`finally` почти каждой операции — после отправки, инвайта, удара. Один зависший
teardown морозил ВСЮ операцию: цикл по целям стоял, слот и арендованные
аккаунты держались до стоп-гвардии застоя (90 минут), после чего операция
уходила в повтор — хотя сломалось лишь закрытие сокета. Для владельца это
«операция зависла на ответе Telegram или прокси» (жалоба по операции #512).

ЧТО ТЕПЕРЬ. `account_manager.safe_disconnect` ограничивает закрытие потолком и
глушит любые ошибки (отключение — best-effort), а все 76 точек
`await client.disconnect()` в account_manager.py переведены на него. Зависшее
закрытие возвращает управление за секунды, операция идёт дальше.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


async def test_safe_disconnect_returns_fast_on_a_wedged_socket():
    from services import account_manager

    class _Wedged:
        async def disconnect(self):
            await asyncio.sleep(3600)  # заклиненный сокет: висел бы вечно

    t0 = time.monotonic()
    await account_manager.safe_disconnect(_Wedged(), timeout=0.2)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, (
        f"safe_disconnect висел {elapsed:.1f}s на заклиненном сокете — "
        "операция снова будет мёрзнуть до стоп-гвардии"
    )


async def test_safe_disconnect_swallows_errors_and_accepts_none():
    from services import account_manager

    class _Boom:
        async def disconnect(self):
            raise RuntimeError("socket gone")

    # Ошибка закрытия не должна всплывать в operation finally.
    await account_manager.safe_disconnect(_Boom())
    # None-клиент (ещё не создан) — тоже не падает.
    await account_manager.safe_disconnect(None)


async def test_safe_disconnect_handles_sync_disconnect():
    """В разных версиях Telethon disconnect() то корутина, то нет."""
    from services import account_manager

    calls = {"n": 0}

    class _Sync:
        def disconnect(self):  # не корутина
            calls["n"] += 1
            return None

    await account_manager.safe_disconnect(_Sync())
    assert calls["n"] == 1


def test_no_unbounded_disconnect_remains_in_account_manager():
    """Храповик: ни одной голой `await client.disconnect()` — только через
    safe_disconnect (или уже обёрнутый asyncio.wait_for). Иначе один зависший
    teardown снова заморозит операцию, а точечный тест этого не поймает."""
    src = (ROOT / "services" / "account_manager.py").read_text(encoding="utf-8")
    bare = re.findall(r"await\s+\w+\.disconnect\(\)", src)
    assert not bare, (
        "в account_manager.py осталось небуферизованное закрытие сокета "
        f"({len(bare)} шт.) — заверните в safe_disconnect: {bare[:3]}"
    )
