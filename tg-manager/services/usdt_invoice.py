"""Счёт в USDT (TRC-20): у каждого открытого счёта своя сумма до цента.

ЗАЧЕМ. Перевод TRC-20 не несёт комментария, как TON, поэтому привязать его к
счёту можно только суммой. Раньше сумма была «цена + сдвиг 0,01–0,49 по
контрольной сумме референса», а проверка — «любой перевод ±0,02 от суммы».
Сдвиг не проверялся на занятость, допуск накрывал соседние центы, а Strike и
Host-Server сдвига не имели вовсе (ровно 250 USDT). Итог: два одновременных
счёта одного тарифа были неразличимы, и перевод одного человека подтверждал
подписку другому.

Теперь сумма выделяется здесь, под общей блокировкой: среди всех счетов,
которые автопроверка ещё может подтвердить (`OPEN_PAYMENT_SQL`), такой суммы
на этот кошелёк нет. Автопроверка сверяет сумму точно до цента
(`payment_checker.match_trc20`), так что перевод подходит ровно одному счёту.
"""

from __future__ import annotations

import random

import asyncpg

CURRENCY = "USDT_TRC20"

# Сколько счёт ждёт оплату сам и сколько ещё после истечения его подбирает
# автопроверка (человек мог заплатить позже). Пока счёт в этом окне, его сумма
# занята: иначе поздний перевод подошёл бы двум счетам.
TTL_HOURS = 24
GRACE_HOURS = 24

# Счета, которые автопроверка ещё может подтвердить. Тот же набор, что
# выбирает `payment_checker._check_pending`.
OPEN_PAYMENT_SQL = (
    "currency = 'USDT_TRC20' AND confirmed_at IS NULL AND ("
    "status IN ('pending','confirming') OR "
    f"(status = 'expired' AND expires_at > now() - INTERVAL '{GRACE_HOURS} hours'))"
)

# Ключ advisory-блокировки: два счёта, создаваемые одновременно, не должны
# получить одну сумму. 0x55534454 = 'USDT'.
_LOCK_KEY = 0x55534454

# Сдвиг от цены в центах: 0,01–0,99. Ноль не берём — «ровная» сумма чаще всего
# приходит переводом, не относящимся к счёту.
_OFFSETS = range(1, 100)


class NoFreeAmountError(RuntimeError):
    """Все 99 сумм вокруг цены заняты открытыми счетами — новый счёт не создать."""

    def __init__(self) -> None:
        super().__init__(
            "Сейчас открыто слишком много счетов в USDT на эту сумму. "
            "Попробуйте через час или оплатите в TON."
        )


def to_cents(amount) -> int:
    return int(round(float(amount) * 100))


def pick_amount(base_usdt: float, taken_cents: set[int]) -> float:
    """Свободная сумма «цена + 0,01..0,99», которой нет среди занятых."""
    base = to_cents(base_usdt)
    free = [k for k in _OFFSETS if base + k not in taken_cents]
    if not free:
        raise NoFreeAmountError()
    return (base + random.choice(free)) / 100


async def create_payment(
    pool: asyncpg.Pool,
    *,
    user_id: int,
    plan: str,
    period_months: int,
    base_usdt: float,
    amount_usd: float,
    wallet: str,
    reference: str,
    ttl_hours: int = TTL_HOURS,
) -> float:
    """Завести счёт в USDT с уникальной суммой. Возвращает сумму к оплате.

    Бросает NoFreeAmountError, когда свободной суммы нет.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _LOCK_KEY)
            rows = await conn.fetch(
                "SELECT amount_crypto FROM payments WHERE wallet_address = $1 AND "
                + OPEN_PAYMENT_SQL,
                wallet,
            )
            taken = {to_cents(r["amount_crypto"]) for r in rows}
            amount = pick_amount(base_usdt, taken)
            await conn.execute(
                """INSERT INTO payments (user_id, plan, period_months, currency,
                                         amount_crypto, amount_usd, wallet_address,
                                         reference, expires_at)
                   VALUES ($1, $2, $3, 'USDT_TRC20', $4, $5, $6, $7,
                           now() + make_interval(hours => $8::int))""",
                user_id,
                plan,
                period_months,
                amount,
                amount_usd,
                wallet,
                reference,
                ttl_hours,
            )
    return amount
