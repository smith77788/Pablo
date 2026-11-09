"""Сторож прокси: сам замечает, что прокси умер, и говорит об этом.

Разрыв, который это закрывает. Фоновой проверки прокси не было ВООБЩЕ:
`user_proxies.is_alive` обновлялся только когда пользователь вручную жал
«проверить». Прокси, умерший молча (кончился трафик, провайдер снял адрес,
сменился пароль), оставался в базе живым, а отбор аккаунтов смотрит на
`is_active` — ручной флаг, а не на реальную живость. В итоге:

  • аккаунты на мёртвом прокси продолжали выбираться в каждую операцию;
  • каждая попытка подключения падала сетевой ошибкой;
  • снаружи это выглядело как «аккаунты сломались» или «операции не работают»,
    и человек шёл чинить аккаунты — самое дорогое, что у него есть, — вместо
    того чтобы заменить дешёвый прокси.

Сторож раз в полчаса проверяет активные прокси, честно проставляет живость и
ОДИН раз сообщает владельцу о смерти, называя, сколько аккаунтов из-за неё
простаивает. Когда прокси оживает — метка снимается, и о следующей смерти
предупредим снова (тот же приём, что у сторожа Хранилища).

Решения вынесены в чистые функции (`decide_transition`, `build_alert`) —
проверяются без сети и без базы.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

CHECK_EVERY = 30 * 60          # период полного обхода
CONCURRENCY = 8                # одновременных проверок (не забиваем сеть и сокеты)
# Одна неудачная проверка — ещё не смерть: публичные прокси регулярно моргают.
# Тревожим только после устойчивой серии, иначе уведомления станут шумом и их
# перестанут читать.
FAIL_STREAK_TO_ALERT = 3


def decide_transition(alive: bool, was_alive, streak: int,
                      notified: bool, assigned: int) -> dict:
    """Что делать с прокси по результату проверки. Чистая функция.

    Возвращает {"is_alive", "streak", "notify", "clear_notice"}:
      • notify — пора сообщить о смерти (устойчивая серия неудач, к прокси
        привязаны аккаунты, и мы ещё не сообщали);
      • clear_notice — прокси ожил, метку «уже сообщили» снимаем, чтобы о
        следующей смерти предупредить снова.

    Молчим, если к прокси не привязано ни одного аккаунта: смерть запасного
    прокси никому не мешает, и дёргать из-за неё человека незачем.
    """
    if alive:
        return {"is_alive": True, "streak": 0,
                "notify": False, "clear_notice": bool(notified)}
    new_streak = int(streak or 0) + 1
    should = (new_streak >= FAIL_STREAK_TO_ALERT
              and int(assigned or 0) > 0
              and not notified)
    return {"is_alive": False, "streak": new_streak,
            "notify": should, "clear_notice": False}


def build_alert(label: str, assigned: int, streak: int) -> str:
    """Текст уведомления. Называем цену простоя в аккаунтах — иначе человек
    пойдёт чинить аккаунты вместо замены прокси."""
    return (
        "🔌 <b>Прокси не отвечает</b>\n\n"
        f"<b>{label}</b> не отвечает {streak} проверки подряд.\n"
        f"На нём {assigned} аккаунт(ов) — все их операции сейчас падают по сети.\n\n"
        "Аккаунты при этом ЦЕЛЫ: дело в прокси, а не в них. "
        "Замените прокси или переназначьте аккаунты на рабочий — "
        "и операции пойдут дальше."
    )


def plan_auto_failover(stranded: list, free: list) -> dict:
    """Куда автоматически переселить аккаунты с умершего прокси. Чистая функция.

    stranded — [(account_id, страна прокси, на котором он сидел)], free —
    [(proxy_id, страна)] по прокси владельца, которые живы В ЭТОМ обходе и на
    которых нет ни одного аккаунта. Автомат, в отличие от ручного переезда,
    берёт ТОЛЬКО свободные прокси: человек рядом с кнопкой может решить, что
    два аккаунта на одном выходе лучше простоя, а фоновый цикл молча ломать
    изоляцию «один аккаунт — один IP» не вправе. Сначала прокси той же
    страны: смена страны выхода — заметное для Telegram событие, смена
    адреса внутри страны — нет.

    Возвращает {"moves": [(account_id, proxy_id)], "stranded": [account_id]}.
    """
    pool_by_geo: dict = {}
    order: list = []
    for pid, geo in free or []:
        if pid is None:
            continue
        key = (geo or "").strip().upper()
        pool_by_geo.setdefault(key, []).append(int(pid))
        order.append(int(pid))
    taken: set = set()
    moves, left = [], []
    for acc_id, geo in stranded or []:
        key = (geo or "").strip().upper()
        pick = next((p for p in pool_by_geo.get(key, []) if p not in taken), None) if key else None
        if pick is None:
            pick = next((p for p in order if p not in taken), None)
        if pick is None:
            left.append(acc_id)
            continue
        taken.add(pick)
        moves.append((acc_id, pick))
    return {"moves": moves, "stranded": left}


def build_failover_note(moved: int, stranded: int) -> str:
    """Дописка к уведомлению о смерти прокси: что автомат сделал сам."""
    if moved and not stranded:
        return (f"\n\n✅ Автопереезд: все {moved} аккаунт(ов) переселены на свободные "
                "живые прокси, операции идут дальше.")
    if moved:
        return (f"\n\n✅ Автопереезд: {moved} аккаунт(ов) переселены на свободные живые "
                f"прокси. {stranded} остались на мёртвом: свободных живых прокси не "
                "хватило — добавьте прокси.")
    return ("\n\n⚠️ Автопереезд включён, но свободных живых прокси нет — "
            "добавьте прокси, и следующий переезд пройдёт сам.")


async def auto_failover_enabled(pool, owner_id: int) -> bool:
    """Включил ли владелец автопереезд (settings_json.proxy_auto_failover).
    По умолчанию выключено: переезд меняет IP аккаунта, это решение владельца."""
    try:
        raw = await pool.fetchval(
            "SELECT settings_json FROM platform_users WHERE user_id=$1", owner_id)
    except Exception:
        return False
    if not raw:
        return False
    try:
        import json as _json
        data = _json.loads(raw) if isinstance(raw, str) else raw
        return bool((data or {}).get("proxy_auto_failover"))
    except Exception:
        return False


async def _auto_failover(pool, owner_id: int, dead_proxy_id: int,
                         alive_now: set) -> dict:
    """Переселить аккаунты с подтверждённо мёртвого прокси на свободные живые."""
    stranded = await pool.fetch(
        """SELECT a.id, p.geo_country FROM tg_accounts a
             JOIN user_proxies p ON p.id = a.proxy_id
            WHERE a.owner_id=$1 AND a.proxy_id=$2 AND a.is_active
            ORDER BY a.id""", owner_id, dead_proxy_id)
    if not stranded:
        return {"moved": 0, "stranded": 0}
    free = await pool.fetch(
        """SELECT p.id, p.geo_country FROM user_proxies p
            WHERE p.owner_id=$1 AND p.is_active AND p.is_alive IS TRUE
              AND p.id = ANY($2::bigint[])
              AND NOT EXISTS (SELECT 1 FROM tg_accounts a WHERE a.proxy_id = p.id)
            ORDER BY p.latency_avg_ms NULLS LAST, p.id""",
        owner_id, list(alive_now))
    plan = plan_auto_failover([(r["id"], r["geo_country"]) for r in stranded],
                              [(r["id"], r["geo_country"]) for r in free])
    moved = 0
    if plan["moves"]:
        # Повторная проверка «свободен» внутри UPDATE: между выборкой и записью
        # прокси мог занять ручной переезд, и двое на одном IP — ровно то, чего
        # автомат обещает не делать.
        rows = await pool.fetch(
            """UPDATE tg_accounts a SET proxy_id = m.pid
                 FROM unnest($1::bigint[], $2::bigint[]) AS m(acc_id, pid)
                WHERE a.id = m.acc_id AND a.owner_id = $3 AND a.proxy_id = $4
                  AND NOT EXISTS (SELECT 1 FROM tg_accounts b WHERE b.proxy_id = m.pid)
             RETURNING a.id""",
            [int(a) for a, _p in plan["moves"]], [int(p) for _a, p in plan["moves"]],
            owner_id, dead_proxy_id)
        moved = len(rows or [])
    log.info("proxy_watchdog: автопереезд owner=%s proxy=%s переселено %d из %d",
             owner_id, dead_proxy_id, moved, len(stranded))
    return {"moved": moved, "stranded": len(stranded) - moved}


async def _open_outage(pool, owner_id: int, proxy_id: int, accounts: int) -> None:
    """Открыть строку простоя (schema_v316). Без аккаунтов простоя нет.

    История простоя — надстройка: её сбой (схема ещё не применена) не мешает
    ни уведомлению, ни записи живости прокси.
    """
    if accounts <= 0:
        return
    try:
        await pool.execute(
            "INSERT INTO proxy_outages(owner_id, proxy_id, accounts) VALUES ($1, $2, $3)",
            owner_id, proxy_id, accounts)
    except Exception:
        log.debug("proxy_watchdog: простой не записан proxy=%s", proxy_id)


async def _close_outage(pool, proxy_id: int) -> None:
    """Прокси ожил — закрыть открытый простой."""
    try:
        await pool.execute(
            "UPDATE proxy_outages SET ended_at=now() WHERE proxy_id=$1 AND ended_at IS NULL",
            proxy_id)
    except Exception:
        log.debug("proxy_watchdog: простой не закрыт proxy=%s", proxy_id)


async def check_proxy_alive(proxy_url: str, timeout_s: float = 10.0) -> dict:
    """Доступен ли прокси. {alive, latency_ms}.

    Проверяем реальным запросом через прокси, а не только TCP-коннектом:
    сокет к нему может открываться и у прокси, который уже ничего не проксирует.
    """
    import time as _time

    if not proxy_url:
        return {"alive": False, "latency_ms": None}
    try:
        import importlib

        import aiohttp

        socks_module = importlib.import_module("aiohttp_socks")
        connector = socks_module.ProxyConnector.from_url(proxy_url)
        t0 = _time.monotonic()
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                "https://api.telegram.org",
                timeout=aiohttp.ClientTimeout(total=timeout_s),
                # ssl=False намеренно: проверяем заведомо недоверенный прокси,
                # а не боевой API — нас интересует только проходимость.
                ssl=False,
            ) as resp:
                return {"alive": resp.status < 500,
                        "latency_ms": int((_time.monotonic() - t0) * 1000)}
    except Exception:
        return {"alive": False, "latency_ms": None}


async def _notify(pool, bot, owner_id: int, proxy_id: int, text: str) -> bool:
    try:
        from database import db as _db

        # dedup_key на КАЖДЫЙ прокси. notify_if_enabled глушит повторы по тройке
        # (владелец, тип, ключ) раз в минуту и молча отбрасывает остальное. Без
        # ключа все мёртвые прокси владельца делили один слот с сообщениями
        # движка операций: когда падает целый пул (а падает он обычно целиком —
        # у провайдера или датацентра), владелец узнавал ровно про ОДИН прокси.
        # Про остальные он не узнавал уже никогда: dead_notified_at им
        # проставлен, и сторож считает их отработанными.
        await _db.notify_if_enabled(
            pool, bot, owner_id, "op_complete", text,
            dedup_key=f"proxy_dead:{proxy_id}",
        )
        return True
    except Exception:
        log.debug("proxy_watchdog: уведомление не отправлено owner=%s", owner_id)
        return False


async def check_once(pool, bot) -> dict:
    """Один обход. Возвращает {checked, dead, alerted} — для тестов и логов."""
    rows = await pool.fetch(
        """SELECT p.id, p.owner_id, p.proxy_url, p.label, p.is_alive,
                  COALESCE(p.consecutive_failures, 0) AS streak,
                  p.dead_notified_at,
                  (SELECT COUNT(*) FROM tg_accounts a
                    WHERE a.proxy_id = p.id AND a.is_active) AS assigned
             FROM user_proxies p
            WHERE p.is_active = TRUE""")
    if not rows:
        return {"checked": 0, "dead": 0, "alerted": 0}

    sem = asyncio.Semaphore(CONCURRENCY)

    # proxy_url хранится зашифрованным. Раньше сюда уходил шифротекст:
    # ProxyConnector.from_url("ENC:…") бросает, check_proxy_alive возвращает
    # alive=False, и сторож объявлял мёртвым КАЖДЫЙ живой прокси — с письмом
    # владельцу и пометкой в базе. Снаружи это выглядело как «все прокси
    # умерли разом». Для старых незашифрованных строк расшифровка — passthrough.
    from services.proxy_hygiene import proxy_plain_url

    async def _one(r):
        async with sem:
            return r, await check_proxy_alive(proxy_plain_url(r["proxy_url"]))

    results = await asyncio.gather(*[_one(r) for r in rows], return_exceptions=True)

    alive_now = {r["id"] for r, res in (i for i in results if not isinstance(i, BaseException))
                 if res.get("alive")}
    checked = dead = alerted = 0
    for item in results:
        if isinstance(item, BaseException):
            continue
        r, res = item
        checked += 1
        d = decide_transition(
            bool(res.get("alive")), r["is_alive"], r["streak"],
            r["dead_notified_at"] is not None, r["assigned"])
        if not d["is_alive"]:
            dead += 1
        try:
            await pool.execute(
                """UPDATE user_proxies
                      SET is_alive=$1, last_check=now(), last_checked_at=now(),
                          consecutive_failures=$2,
                          latency_avg_ms=COALESCE($3, latency_avg_ms),
                          dead_notified_at = CASE
                              WHEN $4 THEN NULL
                              WHEN $5 THEN now()
                              ELSE dead_notified_at END
                    WHERE id=$6""",
                d["is_alive"], d["streak"], res.get("latency_ms"),
                d["clear_notice"], d["notify"], r["id"])
        except Exception:
            log.warning("proxy_watchdog: не удалось записать состояние прокси id=%s", r["id"])
            continue
        if d["clear_notice"]:
            await _close_outage(pool, r["id"])
        if d["notify"]:
            # Полный адрес прокси содержит логин и пароль — в сообщение он
            # попадать не должен. И маскировать надо расшифрованный адрес:
            # у шифротекста нет ни «://», ни «@», маска его не трогала, и в
            # письме владельцу оказывалась строка «ENC:…».
            from services.proxy_hygiene import proxy_display

            label = proxy_display(r["proxy_url"], r["label"])
            text = build_alert(label, r["assigned"], d["streak"])
            stranded_after = int(r["assigned"] or 0)
            # Автопереезд — только по включённой владельцем настройке и только в
            # момент подтверждённой смерти (та же серия, что для уведомления).
            # Любой сбой здесь не мешает сообщить о смерти прокси.
            try:
                if await auto_failover_enabled(pool, r["owner_id"]):
                    fo = await _auto_failover(pool, r["owner_id"], r["id"], alive_now)
                    text += build_failover_note(fo["moved"], fo["stranded"])
                    stranded_after = int(fo["stranded"])
            except Exception:
                log.warning("proxy_watchdog: автопереезд не удался proxy=%s", r["id"])
            await _open_outage(pool, r["owner_id"], r["id"], stranded_after)
            if await _notify(pool, bot, r["owner_id"], r["id"], text):
                alerted += 1

    if dead or alerted:
        log.info("proxy_watchdog: проверено %d, мёртвых %d, уведомлений %d",
                 checked, dead, alerted)
    return {"checked": checked, "dead": dead, "alerted": alerted}


async def run(pool, bot) -> None:
    log.info("proxy_watchdog: starting")
    while True:
        try:
            await check_once(pool, bot)
        except Exception:
            log.exception("proxy_watchdog: обход не удался")
        await asyncio.sleep(CHECK_EVERY)
