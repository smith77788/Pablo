"""Ожидание по Telegram `retry_after` — всегда с верхним пределом.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. `await asyncio.sleep(retry_after)` выглядит безупречно:
платформа попросила подождать — ждём. Но `retry_after` назначает Telegram, и он
бывает и три секунды, и три часа. Фоновый цикл, который спит столько, сколько
попросили, останавливается ЦЕЛИКОМ: рассылка на десять тысяч получателей висит
на одном, у кого сработал лимит; шаг воронки не уходит ни одному подписчику;
пересылка оператору не доходит ни от кого. Снаружи это неотличимо от «сервис
умер», и чинилось только рестартом контейнера.

В `op_worker` это правило уже действует (`bounded_flood_sleep` + храповик
`tests/test_no_unbounded_flood_sleep.py`), но остальные фоновые циклы жили без
него. Здесь — та же дисциплина в виде, пригодном для лёгких модулей: op_worker
тянет telethon и импортировать его из рассылки нельзя.

ЧЕМ ЭТО НЕ ЯВЛЯЕТСЯ. Это не обход лимитов Telegram. Предел ограничивает ровно
то, сколько цикл СТОИТ НА МЕСТЕ; само действие при слишком долгой паузе не
повторяется раньше срока, а ПРОПУСКАЕТСЯ — получатель останется на следующий
круг, когда пауза истечёт сама.

Использование:

    if await wait_for_retry_after(retry_after, where="broadcast"):
        ...  # пауза выдержана целиком — повтор безопасен
    else:
        ...  # пауза слишком длинная — пропускаем цель, цикл идёт дальше
"""

from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger(__name__)


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    """Целое из окружения с зажимом в разумные границы (мусор → default)."""
    try:
        return max(lo, min(int(os.getenv(name, "").strip() or default), hi))
    except (TypeError, ValueError):
        return default


# Сколько секунд лимита имеет смысл переждать ПРЯМО В ЦИКЛЕ.
# Две минуты покрывают обычный 429 от Bot API (единицы-десятки секунд) с
# большим запасом; всё, что дольше, — это уже не «подожди чуть-чуть», а
# «приходи позже», и приходить должен следующий круг цикла, а не текущий.
MAX_RETRY_AFTER_S = _int_env("INFRAGRAM_RETRY_AFTER_MAX_SEC", 120, 5, 3600)


async def wait_for_retry_after(
    retry_after: float | int | None,
    where: str = "",
    *,
    extra_s: float = 0.0,
    max_s: float | None = None,
) -> bool:
    """Переждать `retry_after`, если он укладывается в предел.

    Возвращает True, если пауза выдержана ПОЛНОСТЬЮ и повтор действия
    безопасен; False — если платформа попросила ждать дольше предела и цель
    надо пропустить. Половинчатого сна не бывает намеренно: поспать меньше, чем
    просил Telegram, и тут же повторить — верный способ получить лимит подлиннее.

    extra_s — запас поверх паузы (некоторые вызовы добавляли +5с «на всякий
    случай»); он входит в проверку предела, а не обходит её.
    """
    try:
        want = float(retry_after or 0)
    except (TypeError, ValueError):
        return False
    if want <= 0:
        return False

    limit = float(max_s if max_s is not None else MAX_RETRY_AFTER_S)
    total = want + max(0.0, float(extra_s))
    if total > limit:
        log.warning(
            "flood_sleep%s: Telegram просит ждать %.0fс — дольше предела %.0fс; "
            "цель пропущена, цикл продолжает работу",
            f" {where}" if where else "", want, limit,
        )
        return False
    await asyncio.sleep(total)
    return True
