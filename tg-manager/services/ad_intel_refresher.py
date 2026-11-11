"""Автообновление «Разведки рекламы»: каналы пересканируются сами.

Раньше канал сканировался один раз — при добавлении, а пустое состояние
экрана обещало, что «разведка смотрит новые посты и дополнит список сама».
Этого не делал никто.

Как устроено и почему так безопасно для флота:

* Обход раз в час берёт каналы с включённым автообновлением, чей скан старше
  `ad_intelligence.AUTO_RESCAN_HOURS`, и ставит по ним ОБЫЧНУЮ операцию
  `ad_intel_scan` через `operation_bus.submit`. Значит работают все защиты
  очереди: подбор аккаунта одной дверью (статус, кулдаун, мёртвый прокси),
  захват аккаунта под операцию, флуд-паузы, дедуп одинаковых операций.
* На владельца за обход — не больше `PER_OWNER` каналов: длинный список
  растягивается на несколько часов, а не бьёт по флоту разом.
* Операция только читает публичный канал (100 последних постов) — ни одного
  действия от имени аккаунта.
* Отказ по тарифу или сбой постановки не роняет обход и не повторяется
  тут же: канал попадёт в следующий обход.
"""
from __future__ import annotations

import asyncio
import logging

from services import ad_intelligence as _ai

log = logging.getLogger(__name__)

TICK_SEC = 3600
PER_OWNER = 5
MAX_PER_TICK = 200


async def tick(pool) -> dict:
    """Один обход. Возвращает {"due": N, "queued": N, "failed": N}."""
    from services import operation_bus as _obus

    due = await _ai.channels_due_for_rescan(
        pool, hours=_ai.AUTO_RESCAN_HOURS, per_owner=PER_OWNER, limit=MAX_PER_TICK)
    queued = failed = 0
    blocked_owners: set[int] = set()
    for item in due:
        owner, ch = item["owner_id"], item["channel"]
        if owner in blocked_owners:
            continue
        try:
            await _obus.submit(
                pool, owner, "ad_intel_scan", {"channel": ch},
                total_items=1, label=f"Разведка рекламы @{ch} (автообновление)")
            queued += 1
        except PermissionError:
            # Тариф не пускает — остальным каналам этого владельца тоже нет.
            blocked_owners.add(owner)
        except Exception:
            failed += 1
            log.warning("ad_intel_refresher: постановка @%s владельца %s не удалась",
                        ch, owner, exc_info=True)
    if due:
        log.info("ad_intel_refresher: к обновлению %d, поставлено %d, сбоев %d",
                 len(due), queued, failed)
    return {"due": len(due), "queued": queued, "failed": failed}


async def run(pool, bot=None) -> None:
    log.info("ad_intel_refresher: starting")
    # Не в первую секунду после старта: процесс сначала разбирает очередь.
    await asyncio.sleep(120)
    while True:
        try:
            await tick(pool)
        except Exception:
            log.exception("ad_intel_refresher: обход не удался")
        await asyncio.sleep(TICK_SEC)
