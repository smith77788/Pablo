"""Регресс: канонический регистратор FloodWait обязан растить flood_count_7d.

Жалоба владельца (со скриншотов): аккаунты с флудом показываются «Под риском»,
но здоровье у них 100%, и «здоровье сети» — тоже 100%.

Корень — расхождение двух органов пульса, питающихся из РАЗНЫХ таблиц:

  • риск-пульс (`infra_memory.get_account_health`) читает `account_flood_log`
    — туда событие FloodWait попадало, поэтому бейдж «Под риском» был верным;
  • `trust_engine` читает денормализованный счётчик `tg_accounts.flood_count_7d`
    (`penalty = _FLOOD_PENALTY * flood_count_7d`) — а он НЕ рос.

`record_flood` (канонический путь, его зовут strike/warmup/parser/chat_warmup
через `note_flood`) ставил паузу через `apply_cooldown`, но без
`bump_flood_count=True`. UPDATE делал `flood_count_7d = COALESCE(...,0) + 0`,
то есть не инкрементировал счётчик вовсе. Значит доверие не падало, и приборный
щиток (dashboard `acc_health = AVG(trust)*100`) светил «здоровье 100%» на флоте,
который на деле во флуде. Прямые вызовы `apply_cooldown` в dm_engine/op_worker
флаг уже ставили — расходился именно канонический путь.
"""
from __future__ import annotations

import asyncio

from services import flood_engine


class _CapPool:
    """Ловит pool.execute — как _CapPool в test_flood_op_impact."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "OK"


def _flood_count_update_args(pool: _CapPool):
    for q, a in pool.calls:
        if "UPDATE tg_accounts" in q and "flood_count_7d" in q:
            return a
    return None


def test_record_flood_increments_flood_count_7d():
    pool = _CapPool()
    asyncio.run(flood_engine.record_flood(pool, 777, 120, "strike"))

    args = _flood_count_update_args(pool)
    assert args is not None, (
        "record_flood не обновил flood_count_7d — trust_engine не узнает о флуде")
    # apply_cooldown: `flood_count_7d = COALESCE(flood_count_7d, 0) + $3::int`,
    # $3 — третий позиционный аргумент (индекс 2); bump_flood_count=True → 1.
    assert args[2] == 1, (
        "flood_count_7d инкрементируется на 0: record_flood вызвал apply_cooldown "
        "без bump_flood_count=True. trust_engine не увидит флуд — доверие и "
        f"здоровье аккаунта останутся 100%. Аргументы UPDATE: {args!r}")


def test_record_flood_still_writes_the_event_log():
    """Инкремент счётчика не должен вытеснить запись самого события.

    Оба органа пульса обязаны получить флуд: account_flood_log (риск-пульс) и
    flood_count_7d (доверие). Проверяем, что первый на месте тоже.
    """
    pool = _CapPool()
    asyncio.run(flood_engine.record_flood(pool, 777, 120, "strike"))
    assert any("INSERT INTO account_flood_log" in q for q, _ in pool.calls), (
        "record_flood перестал писать в account_flood_log — риск-пульс ослеп")
