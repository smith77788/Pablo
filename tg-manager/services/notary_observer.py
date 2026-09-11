"""«Нотариус»: наблюдатель — чтение канала панелью и ведение протокола.

Здесь всё, что касается БД и Telegram; правила вывода живут в `services/notary.py`
и ничего об этом не знают.

Три вещи, ради которых модуль написан именно так:

* **Наблюдение пассивно.** Мы читаем один пост и уходим. Ни подписок «на ходу»,
  ни реакций, ни сообщений — флот от наблюдения не изнашивается. Это и делает
  панель активом, а не расходником.
* **Аккаунт захватывается.** Чтение — такая же живая сессия: без захвата она
  могла бы подняться параллельно с операцией или прогревом на том же auth-key
  (AUTH_KEY_DUPLICATED убивает сессию). Освобождаем в `finally` всегда.
* **Ошибка чтения записывается как `unknown`, а не как «поста нет».** Отличить
  «канал снял пост» от «у нас упал прокси» — единственное, ради чего существует
  этот продукт; перепутать их значит продавать ложные обвинения.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone

import asyncpg

from services import notary

log = logging.getLogger(__name__)

# Как часто просыпается цикл. Сам момент проверки каждого наблюдения задаёт
# notary.plan_next_check — цикл лишь забирает то, что уже подошло.
TICK_SECONDS = 60
# Сколько наблюдений обрабатываем за один тик: панель не должна занимать флот.
BATCH = 20


def _secret() -> bytes:
    s = (os.getenv("COMPLIANCE_SECRET") or os.getenv("ADMIN_SECRET")
         or "botmother-compliance")
    return s.encode()


def sign(payload: str) -> str:
    """Та же схема подписи, что у журнала комплаенса: один секрет — одна
    проверяемость. Отдельный секрет здесь означал бы вторую систему доверия."""
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def verify(payload: str, signature: str) -> bool:
    """Проверка подписи протокола. Сравнение постоянное по времени."""
    if not signature:
        return False
    return hmac.compare_digest(sign(payload), signature)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Одно наблюдение ────────────────────────────────────────────────────────

async def read_post(acc: dict, channel_ref: str, msg_id: int | None) -> dict:
    """Заглянуть в канал. Возвращает {state, views, detail, title, msg_id}.

    Никогда не поднимает исключение наружу: любая неудача — это `unknown`,
    а не приговор каналу.
    """
    from services import account_manager

    client = None
    try:
        client = await asyncio.wait_for(
            account_manager.connect_client(acc.get("session_str") or "", acc,
                                           action_type="parse", low_risk=True),
            timeout=40,
        )
        ref = (channel_ref or "").strip()
        entity = await client.get_entity(ref if not ref.lstrip("-").isdigit()
                                         else int(ref))
        title = getattr(entity, "title", None)
        if not msg_id:
            # Поста ещё нет: проверяем, не появился ли. Берём последний пост —
            # его id нужен, чтобы дальше следить именно за ним.
            async for m in client.iter_messages(entity, limit=1):
                return {"state": notary.PRESENT, "views": getattr(m, "views", None),
                        "detail": None, "title": title, "msg_id": m.id}
            return {"state": notary.ABSENT, "views": None,
                    "detail": "канал пуст", "title": title, "msg_id": None}
        msgs = await client.get_messages(entity, ids=[int(msg_id)])
        m = msgs[0] if msgs else None
        if m is None:
            return {"state": notary.ABSENT, "views": None, "detail": None,
                    "title": title, "msg_id": msg_id}
        return {"state": notary.PRESENT, "views": getattr(m, "views", None),
                "detail": None, "title": title, "msg_id": msg_id}
    except Exception as e:                         # noqa: BLE001 — см. докстринг
        return {"state": notary.UNKNOWN, "views": None,
                "detail": str(e)[:160], "title": None, "msg_id": msg_id}
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log.debug("notary: disconnect failed", exc_info=True)


async def observe(pool: asyncpg.Pool, watch: dict) -> str:
    """Одно наблюдение за одним размещением. Возвращает итоговое состояние."""
    from services import op_worker, resource_selector

    owner_id = int(watch["owner_id"])
    acc = None
    claimed: list[int] = []
    try:
        # Одна дверь: наблюдателя выбираем флуд-осознанно, а не первым попавшимся.
        acc = await resource_selector.select_account_rotated(
            pool, owner_id, action_type="default")
        if acc and await op_worker.try_claim_account(int(acc["id"])):
            claimed = [int(acc["id"])]
        elif acc:
            # Аккаунт занят — это НЕ отсутствие поста. Пропускаем такт.
            acc = None
        if not acc:
            res = {"state": notary.UNKNOWN, "views": None,
                   "detail": "нет свободного наблюдателя", "title": None,
                   "msg_id": watch.get("msg_id")}
        else:
            res = await read_post(acc, watch["channel_ref"], watch.get("msg_id"))
    finally:
        if claimed:
            try:
                await op_worker.release_accounts(claimed)
            except Exception:
                log.warning("notary: release failed acc=%s", claimed)

    await _record(pool, watch, acc, res)
    return res["state"]


async def _record(pool: asyncpg.Pool, watch: dict, acc: dict | None,
                  res: dict) -> None:
    """Записать наблюдение и пересобрать вердикт. Журнал только дописывается."""
    at = _now()
    payload = (f"{watch['id']}|{watch['channel_ref']}|{res.get('msg_id')}"
               f"|{res['state']}|{res.get('views')}|{at.isoformat()}")
    try:
        await pool.execute(
            """INSERT INTO notary_observations
               (watch_id, observed_at, state, views, acc_id, geo, detail, sig)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
            int(watch["id"]), at, res["state"], res.get("views"),
            int(acc["id"]) if acc else None,
            (acc or {}).get("geo_country"), res.get("detail"), sign(payload),
        )
    except Exception:
        log.exception("notary: не удалось записать наблюдение watch=%s", watch["id"])
        return
    await refresh(pool, int(watch["id"]))


async def refresh(pool: asyncpg.Pool, watch_id: int) -> dict | None:
    """Пересобрать факты и вердикт наблюдения из журнала."""
    row = await pool.fetchrow(
        "SELECT * FROM notary_watches WHERE id=$1", watch_id)
    if not row:
        return None
    watch = dict(row)
    obs = [dict(r) for r in await pool.fetch(
        "SELECT observed_at, state, views FROM notary_observations "
        "WHERE watch_id=$1 ORDER BY observed_at", watch_id)]
    now = _now()
    folded = notary.fold(obs, promised_from=watch["promised_from"],
                         promised_until=watch["promised_until"], now=now)
    nxt = notary.plan_next_check(now=now, promised_from=watch["promised_from"],
                                 promised_until=watch["promised_until"])
    done = nxt is None
    sig = None
    if done:
        watch_for_sig = {**watch, "msg_id": watch.get("msg_id")}
        sig = sign(notary.certificate_payload(watch_for_sig, folded))
    await pool.execute(
        """UPDATE notary_watches
              SET verdict=$2, first_seen_at=$3, last_seen_at=$4, absent_since=$5,
                  views_first=$6, views_last=$7, checks_done=checks_done+1,
                  next_check_at=COALESCE($8, next_check_at),
                  status=CASE WHEN $9 THEN 'done' ELSE status END,
                  cert_sig=COALESCE($10, cert_sig),
                  cert_issued_at=CASE WHEN $10 IS NULL THEN cert_issued_at ELSE now() END,
                  updated_at=now()
            WHERE id=$1""",
        watch_id, folded["verdict"], folded["first_seen_at"],
        folded["last_seen_at"], folded["absent_since"],
        folded["views_first"], folded["views_last"], nxt, done, sig,
    )
    return folded


# ── Фоновый цикл ───────────────────────────────────────────────────────────

async def run(pool: asyncpg.Pool, bot=None) -> None:
    """Наблюдает за всеми активными размещениями. Вечный цикл, fail-open."""
    log.info("notary_observer: старт")
    while True:
        try:
            await tick(pool, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("notary_observer: сбой такта")
        await asyncio.sleep(TICK_SECONDS)


async def tick(pool: asyncpg.Pool, bot=None) -> int:
    """Один такт: обработать подошедшие наблюдения. Возвращает их число."""
    try:
        rows = await pool.fetch(
            "SELECT * FROM notary_watches WHERE status='active' "
            "AND next_check_at <= now() ORDER BY next_check_at LIMIT $1", BATCH)
    except Exception:
        log.exception("notary_observer: не удалось выбрать очередь")
        return 0
    n = 0
    for r in rows:
        w = dict(r)
        before = w.get("verdict")
        try:
            await observe(pool, w)
            n += 1
        except Exception:
            log.exception("notary_observer: наблюдение упало watch=%s", w["id"])
            continue
        if bot is not None:
            await _maybe_alert(pool, bot, int(w["id"]), before)
    return n


async def _maybe_alert(pool: asyncpg.Pool, bot, watch_id: int,
                       before: str | None) -> None:
    """Сообщить владельцу, когда вердикт впервые стал обвинительным.

    Досрочное снятие имеет смысл только пока о нём можно предъявить претензию,
    а узнавать об этом, зайдя в мини-апп через неделю, поздно.
    """
    try:
        row = await pool.fetchrow(
            "SELECT owner_id, channel_ref, channel_title, verdict "
            "FROM notary_watches WHERE id=$1", watch_id)
        if not row:
            return
        v = row["verdict"]
        if v == before or v not in (notary.V_EARLY, notary.V_NEVER):
            return
        name = row["channel_title"] or row["channel_ref"]
        await bot.send_message(
            int(row["owner_id"]),
            f"⚖️ <b>Нотариус</b>: {notary.VERDICT_LABEL.get(v, v)}\n"
            f"Канал: {name}\n\n{notary.VERDICT_HINT.get(v, '')}\n\n"
            f"Протокол подписан — откройте «Нотариус», чтобы выгрузить его.",
            parse_mode="HTML")
    except Exception:
        log.debug("notary: алерт не отправлен watch=%s", watch_id, exc_info=True)
