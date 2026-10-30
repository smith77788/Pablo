"""Background loop: verify crypto payments on-chain and activate subscriptions.

Supports TON and TRON (USDT) payments. Checks pending payments every 60 seconds
and activates subscriptions upon successful verification.

Usage:
    from services.payment_checker import run

    # Starts the background payment verification loop
    await run(pool, http, bot)
"""

from __future__ import annotations
import asyncio
import logging
import os
import aiohttp
import asyncpg
from aiogram import Bot

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
NANOTON = 1_000_000_000
_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def _TON_WALLET() -> str:
    # Тот же источник, что и приём оплаты в UI (БД-override с fallback на env).
    # Раньше здесь читался только env: кошелёк, заданный в «⚙️ Настройка
    # оплаты», после рестарта жил лишь в override, и автопроверка TON молча
    # выходила по пустому адресу — платёж показывался, но не подтверждался.
    try:
        from bot.handlers.subscription import _ton_wallet as _w
        return _w()
    except Exception:
        return os.getenv("TON_WALLET", "")


def _TRON_WALLET() -> str:
    # Тот же источник, что и приём оплаты в UI (БД-override с fallback на env),
    # чтобы автопроверка сверяла тот же кошелёк, что показан пользователю.
    try:
        from bot.handlers.subscription import _tron_wallet as _w
        return _w()
    except Exception:
        return os.getenv("TRON_WALLET", "")


def _TON_API_KEY() -> str:
    return os.getenv("TON_API_KEY", "")


# Допуск расхождения часов блокчейна и базы при сверке «перевод не раньше
# счёта». Перевод, сделанный до создания счёта, к этому счёту не относится.
_CLOCK_SKEW_S = 300

# Отказ платёжного API подряд столько циклов (по 30 с) — повод написать админу,
# а не только в лог: пока API лежит, ни одна оплата не подтверждается.
_API_ALERT_AFTER = 10
_ALERT_REPEAT_S = 3 * 3600
_api_fail_streak: dict[str, int] = {}


class _ApiError(Exception):
    """Платёжный API не ответил данными. Текст — для админа, по-русски."""


# Напоминания о сроке подписки — раз в ~час (цикл 30 с).
_NOTICE_EVERY = 120


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession, bot: Bot) -> None:
    tick = 0
    while True:
        try:
            await _check_pending(pool, http, bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("payment_checker error: %s", e)
        if tick % _NOTICE_EVERY == 0:
            try:
                from services import subscription_notices

                await subscription_notices.run_once(pool, bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                log_exc_swallow(log, "payment_checker: напоминания о подписке")
        tick += 1
        await asyncio.sleep(30)


async def _check_pending(
    pool: asyncpg.Pool, http: aiohttp.ClientSession, bot: Bot
) -> None:
    from services import usdt_invoice

    # Expire old pending payments
    await pool.execute(
        "UPDATE payments SET status='expired' WHERE status='pending' AND expires_at < now()"
    )
    # Check active + recently expired — user may have paid after initial expiry.
    # Окно то же, в котором usdt_invoice держит сумму счёта занятой.
    rows = await pool.fetch(
        f"""SELECT * FROM payments
           WHERE status IN ('pending','confirming')
              OR (status = 'expired'
                  AND expires_at > now() - INTERVAL '{usdt_invoice.GRACE_HOURS} hours'
                  AND confirmed_at IS NULL)
           ORDER BY created_at"""
    )
    if not rows:
        return

    ton_rows = [r for r in rows if r["currency"] == "TON" and _payable(r)]
    trc_rows = [r for r in rows if r["currency"] == "USDT_TRC20" and _payable(r)]

    if ton_rows:
        await _check_ton(pool, http, bot, ton_rows)
    if trc_rows:
        await _check_trc20(pool, http, bot, trc_rows)


def _payable(payment) -> bool:
    """Можно ли этот счёт подтверждать автоматически.

    Счёт с нулевой суммой подтверждается любым переводом, а подписка со сроком
    вне списка — подделанная кнопка (`months=0` давал цену 0 и месяц подписки).
    Такие счета, созданные до серверной проверки срока, не подтверждаем никогда.
    """
    from bot.handlers.subscription import ALLOWED_PERIODS
    from services import billing

    try:
        amount = float(payment["amount_crypto"])
    except (TypeError, ValueError, KeyError):
        return False
    if amount <= 0:
        log.warning("payment_checker: счёт %s с нулевой суммой — не подтверждаем",
                    payment.get("reference"))
        return False
    plan = payment.get("plan")
    if plan in ("strike", "host_server"):
        return True
    if not billing.is_subscription_plan(plan):
        return False
    try:
        months = int(payment.get("period_months") or 0)
    except (TypeError, ValueError):
        return False
    if months not in ALLOWED_PERIODS:
        log.warning("payment_checker: счёт %s со сроком %r — не подтверждаем",
                    payment.get("reference"), payment.get("period_months"))
        return False
    return True


def _epoch(dt) -> float | None:
    if dt is None:
        return None
    try:
        if dt.tzinfo is None:
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def _not_before_invoice(payment, tx_epoch: float | None) -> bool:
    """Перевод сделан не раньше счёта (с допуском на часы)."""
    created = _epoch(payment.get("created_at"))
    if created is None or tx_epoch is None:
        return True  # нечем сверить — решает сумма/комментарий
    return tx_epoch >= created - _CLOCK_SKEW_S


async def _fetch_json(http, provider: str, url: str, **kw) -> dict:
    try:
        async with http.get(url, timeout=aiohttp.ClientTimeout(total=15), **kw) as r:
            if r.status == 401:
                raise _ApiError("неверный API-ключ (401)")
            if r.status == 429:
                raise _ApiError("превышен лимит запросов (429)")
            if r.status != 200:
                raise _ApiError(f"ответ {r.status}")
            return await r.json()
    except asyncio.TimeoutError:
        raise _ApiError("нет ответа за 15 с") from None
    except _ApiError:
        raise
    except Exception as e:
        raise _ApiError(f"ошибка запроса: {type(e).__name__}") from None


async def _api_failed(pool, bot, provider: str, why: str) -> None:
    """Отказ платёжного API: лог сразу, админу — если отказ держится."""
    streak = _api_fail_streak.get(provider, 0) + 1
    _api_fail_streak[provider] = streak
    log.warning("%s API: %s (подряд: %d)", provider, why, streak)
    if streak == _API_ALERT_AFTER:
        hint = {
            "TON": "Проверьте TON API ключ в «⚙️ Настройка оплаты» (tonconsole.com).",
            "TRON": "Это сторонний сервис tronscan — обычно восстанавливается сам.",
        }.get(provider, "")
        await _alert_admins(
            pool, bot, f"payapi:{provider}",
            f"⚠️ <b>Автопроверка оплаты {provider} не работает</b>\n\n"
            f"API {provider}: {why}, уже около "
            f"{_API_ALERT_AFTER * 30 // 60} мин подряд. Пока это так, оплаты в "
            f"{provider} не подтверждаются, и покупатели ждут подписку.\n\n{hint}",
        )


def _api_ok(provider: str) -> None:
    _api_fail_streak.pop(provider, None)


async def _alert_admins(pool, bot, key: str, text: str) -> None:
    """Сообщение админам с персистентным анти-повтором (переживает рестарт)."""
    if bot is None:
        return
    try:
        from bot.utils.subscription import platform_alert_recipients
        from database import db as _db

        # Получатели берутся ОДНИМ способом со всеми платформенными алертами:
        # `ADMIN_IDS` плюс живые сессии админки. Прежний `_admin_ids()` знал
        # только переменную окружения, и при незаполненной (обычная установка)
        # алерт о платежах не уходил никому, хотя мини-апп этих людей админами
        # считает.
        admin_ids = await platform_alert_recipients(pool)
    except Exception:
        log_exc_swallow(log, "payment_checker: список админов недоступен")
        return
    if not admin_ids:
        log.critical(
            "payment_checker: сообщить некому (ни ADMIN_IDS, ни живых сессий "
            "админки): %s", key)
        return
    for aid in admin_ids:
        try:
            if not await _db.notify_dedup_ok(pool, int(aid), key, _ALERT_REPEAT_S):
                continue
            await bot.send_message(aid, text, parse_mode="HTML")
        except Exception:
            log_exc_swallow(log, f"payment_checker: алерт админу {aid} не ушёл")
            try:
                await _db.notify_dedup_forget(pool, int(aid), key)
            except Exception:
                pass


async def _check_ton(pool, http, bot, payments) -> None:
    wallet = _TON_WALLET()
    if not wallet:
        return
    headers = {}
    api_key = _TON_API_KEY()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        data = await _fetch_json(
            http, "TON",
            f"https://tonapi.io/v2/blockchain/accounts/{wallet}/transactions",
            params={"limit": 50},
            headers=headers,
        )
    except _ApiError as e:
        await _api_failed(pool, bot, "TON", str(e))
        return
    _api_ok("TON")

    ref_map = {p["reference"]: p for p in payments}
    for tx in data.get("transactions", []):
        try:
            if tx.get("success") is False or tx.get("aborted") is True:
                continue
            in_msg = tx.get("in_msg", {})
            if not in_msg:
                continue
            # Try multiple locations for the comment
            decoded = in_msg.get("decoded_body") or {}
            comment = decoded.get("text", "") or in_msg.get("comment", "") or ""
            comment = comment.strip()
            if not comment or comment not in ref_map:
                continue
            payment = ref_map[comment]
            if not _not_before_invoice(payment, tx.get("utime")):
                continue
            value_nano = int(in_msg.get("value", 0))
            expected_nano = int(float(payment["amount_crypto"]) * NANOTON)
            if expected_nano <= 0:
                continue
            # Accept if ≥98% of expected amount
            if value_nano < expected_nano * 0.98:
                log.info(
                    "TON partial payment: ref=%s got=%d expected=%d",
                    comment,
                    value_nano,
                    expected_nano,
                )
                continue
            tx_hash = tx.get("hash", "")
            if not tx_hash:
                continue
            await _confirm(pool, bot, payment, tx_hash)
        except Exception as e:
            log.debug("TON tx parse error: %s", e)
            continue


def _transfer_ok(tx: dict, wallet: str) -> bool:
    """Перевод USDT дошёл на наш кошелёк и окончателен.

    Поля проверяются, только если API их прислал: формат tronscan меняется, а
    отказ от проверки суммы из-за нового поля хуже, чем лишняя строгость.
    """
    to = tx.get("to_address")
    if to and wallet and to != wallet:
        return False
    if tx.get("confirmed") is False:
        return False  # ещё не окончателен — подберём в следующем цикле
    if tx.get("revert") is True:
        return False
    for key in ("contractRet", "finalResult"):
        val = tx.get(key)
        if val and str(val).upper() != "SUCCESS":
            return False
    token = (tx.get("tokenInfo") or {}).get("tokenId")
    if token and token != _USDT_CONTRACT:
        return False
    return True


def _transfer_value(tx: dict) -> float:
    decimals = (tx.get("tokenInfo") or {}).get("tokenDecimal")
    try:
        decimals = int(decimals) if decimals is not None else 6
    except (TypeError, ValueError):
        decimals = 6
    return int(tx.get("quant", 0)) / (10 ** decimals)


def match_trc20(payments, transfers, wallet: str, used_txids: set[str]):
    """Сопоставить переводы USDT со счетами. Чистая функция.

    Возвращает (matches, ambiguous): matches — [(payment, txid)], ambiguous —
    [(txid, [reference, ...])] для перевода, который подходит больше чем одному
    счёту. Такой перевод не подтверждает НИКОГО: угадать, чей он, нельзя, а
    ошибка выдаёт подписку чужому человеку.

    Перевод подходит счёту, когда:
      • сумма совпадает до цента (usdt_invoice даёт каждому открытому счёту
        свою сумму; прежний допуск ±0,02 накрывал соседние счета);
      • счёт выставлен на тот же кошелёк;
      • перевод сделан не раньше счёта (старый перевод из истории кошелька не
        подтверждает новый счёт).
    """
    from services import usdt_invoice

    matches: list = []
    ambiguous: list = []
    taken_payments: set = set()
    used = set(used_txids)
    for tx in transfers:
        try:
            txid = tx.get("transaction_id", "")
            if not txid or txid in used or not _transfer_ok(tx, wallet):
                continue
            cents = usdt_invoice.to_cents(_transfer_value(tx))
            ts = tx.get("block_ts")
            tx_epoch = (int(ts) / 1000) if ts else None
        except (TypeError, ValueError) as e:
            log.debug("TRON tx parse error: %s", e)
            continue
        candidates = [
            p for p in payments
            if p["id"] not in taken_payments
            and usdt_invoice.to_cents(p["amount_crypto"]) == cents
            and (p.get("wallet_address") or wallet) == wallet
            and _not_before_invoice(p, tx_epoch)
        ]
        if not candidates:
            continue
        if len(candidates) > 1:
            ambiguous.append((txid, [p["reference"] for p in candidates]))
            continue
        matches.append((candidates[0], txid))
        taken_payments.add(candidates[0]["id"])
        used.add(txid)
    return matches, ambiguous


def unmatched_trc20(payments, transfers, wallet: str, used_txids: set[str]):
    """Переводы на кошелёк, не подошедшие ни одному счёту. Чистая функция.

    Чаще всего это человек, который перевёл не ту сумму (округлил центы или
    биржа удержала комиссию из суммы). Автоматически такой перевод не
    подтверждается — и раньше об этом не знал никто: покупатель ждал подписку,
    админ не видел перевода. Берём только переводы не старше самого раннего
    открытого счёта: старая история кошелька к покупкам не относится.
    Возвращает [(txid, сумма)].
    """
    from services import usdt_invoice

    created = [_epoch(p.get("created_at")) for p in payments]
    created = [c for c in created if c is not None]
    if not created:
        return []
    since = min(created) - _CLOCK_SKEW_S
    open_cents = {usdt_invoice.to_cents(p["amount_crypto"]) for p in payments}
    out = []
    for tx in transfers:
        try:
            txid = tx.get("transaction_id", "")
            if not txid or txid in used_txids or not _transfer_ok(tx, wallet):
                continue
            ts = tx.get("block_ts")
            if not ts or int(ts) / 1000 < since:
                continue
            value = _transfer_value(tx)
        except (TypeError, ValueError):
            continue
        if usdt_invoice.to_cents(value) in open_cents:
            continue  # подошёл счёту (или спорный — о нём отдельный алерт)
        out.append((txid, round(value, 6)))
    return out


async def _check_trc20(pool, http, bot, payments) -> None:
    wallet = _TRON_WALLET()
    if not wallet:
        return
    try:
        data = await _fetch_json(
            http, "TRON",
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "contractAddress": _USDT_CONTRACT,
                "toAddress": wallet,
                "limit": 50,
                "sort": "-timestamp",
            },
        )
    except _ApiError as e:
        await _api_failed(pool, bot, "TRON", str(e))
        return
    _api_ok("TRON")

    used_txids: set[str] = set()
    # Pre-collect already-used tx hashes from DB to avoid re-confirming
    try:
        existing = await pool.fetch(
            "SELECT tx_hash FROM payments WHERE status='confirmed' AND tx_hash IS NOT NULL"
        )
        used_txids = {r["tx_hash"] for r in existing if r["tx_hash"]}
    except Exception:
        log_exc_swallow(log, "Сбой получения used_txids из payments")

    matches, ambiguous = match_trc20(
        payments, data.get("token_transfers", []) or [], wallet, used_txids)
    for txid, refs in ambiguous:
        log.error("TRON: перевод %s подходит нескольким счетам %s — не подтверждаем",
                  txid, refs)
        await _alert_admins(
            pool, bot, f"payamb:{txid}",
            "⚠️ <b>Перевод USDT подходит нескольким счетам</b>\n\n"
            f"Транзакция <code>{txid}</code>, счета: "
            f"{', '.join(f'<code>{r}</code>' for r in refs)}.\n"
            "Автоматически не подтверждён никто — выясните у покупателей и "
            "выдайте подписку вручную.",
        )
    for payment, txid in matches:
        try:
            await _confirm(pool, bot, payment, txid)
        except Exception:
            log_exc_swallow(log, "TRON: подтверждение платежа не прошло")
    for txid, value in unmatched_trc20(
            payments, data.get("token_transfers", []) or [], wallet, used_txids):
        log.warning("TRON: перевод %s на %.6f USDT не подошёл ни одному счёту",
                    txid, value)
        await _alert_admins(
            pool, bot, f"payunmatched:{txid}",
            "⚠️ <b>Перевод USDT не подошёл ни одному счёту</b>\n\n"
            f"Пришло <b>{value:.6f} USDT</b>, транзакция <code>{txid}</code>.\n"
            "Скорее всего, покупатель перевёл не ту сумму (без центов или за "
            "вычетом комиссии биржи). Автоматически подписка не выдана — найдите "
            "покупателя по сумме и времени и выдайте вручную.",
        )


async def _confirm(pool, bot: Bot, payment, tx_hash: str) -> None:
    """Подтвердить платёж и выдать оплаченное — одной транзакцией.

    Раньше отметка «confirmed» и выдача шли раздельными запросами. Между ними
    помещается смерть контейнера (Railway передеплоивает часто) или обрыв
    соединения с базой — и тогда платёж навсегда остаётся подтверждённым без
    выданной подписки: клейм ниже ловит только статусы pending/confirming/
    expired, так что следующий цикл этот платёж уже не подберёт. Человек
    заплатил и не получил ничего, а в системе всё «успешно».

    Дверь вебхука (`services/payment_webhook`) делает ровно это одной
    транзакцией — здесь было иначе, хотя платёж тот же. Теперь одинаково: если
    выдача не прошла, откатывается и отметка, и следующий цикл повторит.

    Уведомления и реферальные начисления остаются снаружи: внешние вызовы в
    транзакции держат соединение и к целостности оплаты отношения не имеют.
    """
    user_id = payment["user_id"]
    plan = payment["plan"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Одна транзакция блокчейна — один счёт. Проверка «хэш ещё не
            # использован» в цикле выше читает базу ДО подтверждения, а соседняя
            # реплика или следующий цикл могли успеть подтвердить им другой
            # счёт. Блокировка по хэшу + проверка внутри транзакции закрывают
            # это окно.
            if tx_hash:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))", tx_hash)
                other = await conn.fetchval(
                    "SELECT reference FROM payments WHERE tx_hash=$1 AND id<>$2 "
                    "LIMIT 1",
                    tx_hash,
                    payment["id"],
                )
                if other:
                    log.error(
                        "payment_checker: транзакция %s уже подтвердила счёт %s — "
                        "счёт %s ею не подтверждаем",
                        tx_hash[:16], other, payment["reference"],
                    )
                    return
            updated = await conn.fetchval(
                """UPDATE payments SET status='confirmed', tx_hash=$1, confirmed_at=now()
                   WHERE id=$2 AND status IN ('pending','confirming','expired')
                   RETURNING id""",
                tx_hash,
                payment["id"],
            )
            if not updated:
                return  # уже подтверждён — повторно не выдаём

            if plan == "strike":
                # Таблица заводится миграцией schema_v44, создавать её здесь
                # не нужно: DDL внутри транзакции лишний, а «таблица из
                # рантайма» не оставляет следа в схеме.
                await conn.execute(
                    """INSERT INTO strike_access (user_id, payment_ref)
                       VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING""",
                    user_id,
                    payment["reference"],
                )
            elif plan == "host_server":
                # Разовая лицензия на модуль Host-Server (как strike) — не подписка.
                from services import host_server

                await host_server.grant_access(conn, user_id, payment["reference"])
            else:
                period_months = int(payment["period_months"] or 1)
                await _activate_subscription(conn, user_id, plan, period_months)

    log.info(
        "Payment confirmed: user=%s plan=%s months=%s ref=%s tx=%s",
        user_id,
        payment["plan"],
        payment["period_months"],
        payment["reference"],
        tx_hash[:16] if tx_hash else "",
    )

    # Referral system: mark paid + check rewards for referrer
    try:
        from database import db as _db

        referrer_id = await _db.mark_referral_paid(pool, user_id)
        if referrer_id:
            await _db.check_and_grant_rewards(pool, referrer_id, bot)
            # Начислить комиссию если тир позволяет
            try:
                amb = await _db.get_ambassador_status(pool, referrer_id)
                tier = amb.get("current_tier")
                comm_pct = float(tier["commission_pct"]) if tier and tier.get("commission_pct") else 0.0
                if comm_pct > 0:
                    amount_usd = float(payment.get("amount_usd") or 0)
                    if amount_usd > 0:
                        earned = await _db.record_commission(
                            pool, referrer_id, user_id, amount_usd, comm_pct
                        )
                        if earned > 0:
                            try:
                                await bot.send_message(
                                    referrer_id,
                                    f"💰 <b>Начислена комиссия ${earned:.2f}!</b>\n\n"
                                    f"Реферал оплатил ${amount_usd:.2f} → ваша доля {comm_pct:.0f}%.\n"
                                    f"Баланс пополнен. /growth — управление выплатами.",
                                    parse_mode="HTML",
                                )
                            except Exception as e:
                                log_exc_swallow(log, "_confirm")
            except Exception as _ce:
                log.warning("commission accrual error: %s", _ce)
            # Notify referrer about the paid conversion
            try:
                await bot.send_message(
                    referrer_id,
                    "💳 <b>Один из ваших рефералов оплатил подписку!</b>\n\n"
                    "Проверьте прогресс и награды: /referral",
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(
                    log, "Сбой уведомления реферера о платеже", referrer_id=referrer_id
                )
    except Exception as e:
        log.warning("Referral paid hook error: %s", e)

    try:
        em = {"paid": "💎", "starter": "💎", "pro": "💎", "enterprise": "💎",
              "strike": "⚔️", "host_server": "🖥️"}.get(payment["plan"], "💳")
        if payment["plan"] == "strike":
            msg = (
                "⚔️ <b>Strike Module активирован!</b>\n\n"
                "Вы получили пожизненный доступ к модулю массовой зачистки нелегального контента.\n\n"
                "Перейти: /menu → ⚔️ Strike"
            )
        elif payment["plan"] == "host_server":
            msg = (
                "🖥️ <b>Модуль Host-Server активирован!</b>\n\n"
                "Теперь вы можете сдавать свою инфраструктуру в аренду, брать "
                "чужую и (с вашего согласия) зарабатывать на своих вычислительных "
                "мощностях.\n\nПерейти: /menu → 🖥️ Host-Server"
            )
        else:
            msg = (
                f"🎉 <b>Оплата подтверждена!</b>\n\n"
                f"{em} Подписка на {payment['period_months']} мес. активирована!\n\n"
                f"Управление: /subscription"
            )
        await bot.send_message(
            payment["user_id"],
            msg,
            parse_mode="HTML",
        )
    except Exception:
        log_exc_swallow(
            log, "Сбой уведомления пользователя о платеже", user_id=payment["user_id"]
        )


async def _activate_subscription(executor, user_id: int, plan: str, months: int) -> None:
    # Единая точка активации (see services/billing.py) — та же логика, что у
    # webhook, чтобы подписка/synced platform_users никогда не расходились.
    # executor — пул ИЛИ открытое соединение: подтверждение платежа вызывает
    # это внутри своей транзакции.
    from services import billing

    await billing.activate_subscription(executor, user_id, plan, months)
