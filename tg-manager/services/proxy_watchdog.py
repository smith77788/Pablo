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


async def _notify(pool, bot, owner_id: int, text: str) -> bool:
    try:
        from database import db as _db

        await _db.notify_if_enabled(pool, bot, owner_id, "op_complete", text)
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
        if d["notify"]:
            # Полный адрес прокси содержит логин и пароль — в сообщение он
            # попадать не должен. И маскировать надо расшифрованный адрес:
            # у шифротекста нет ни «://», ни «@», маска его не трогала, и в
            # письме владельцу оказывалась строка «ENC:…».
            from services.proxy_hygiene import proxy_display

            label = proxy_display(r["proxy_url"], r["label"])
            if await _notify(pool, bot, r["owner_id"],
                             build_alert(label, r["assigned"], d["streak"])):
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
