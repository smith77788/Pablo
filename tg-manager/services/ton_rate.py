"""Курс TON/USD для счетов подписки.

ЗАЧЕМ. Цена подписки в TON считалась по курсу из настройки `TON_RATE`, которую
админ вводит руками (по умолчанию 3.0). Курс TON ходит на десятки процентов за
месяц, и счёт выставлялся по цифре, которую никто не обновлял: при росте TON
покупатель переплачивал, при падении платформа недополучала, и никто этого не
видел. Теперь курс берётся с рынка и кэшируется на несколько минут.

ИСТОЧНИКИ по порядку:
  1. tonapi.io `/v2/rates` — тот же сервис, через который payment_checker
     проверяет TON-переводы (ключ `TON_API_KEY` используется, если задан);
  2. CoinGecko `simple/price` — запасной публичный источник;
  3. последний полученный курс, если ему не больше `STALE_MAX_S`;
  4. ручной `TON_RATE`, только если админ задал его явно.
Нет ни одного — `None`: оплату в TON не предлагаем, а не выставляем счёт по
выдуманной цифре. USDT от курса не зависит и остаётся доступным.

Курс фиксируется в счёте в момент его создания (`payments.amount_crypto`), так
что колебания после выставления на сверку перевода не влияют.
"""
from __future__ import annotations

import logging
import os
import time

import aiohttp

log = logging.getLogger(__name__)

# Сколько живёт свежий курс, прежде чем спросить рынок заново.
FRESH_S = 10 * 60
# Сколько можно пользоваться последним курсом, когда оба источника недоступны.
STALE_MAX_S = 6 * 3600
# Курс вне этих границ — ошибка источника, а не рынок.
_MIN_RATE = 0.05
_MAX_RATE = 1000.0
_TIMEOUT_S = 4.0

_TONAPI_URL = "https://tonapi.io/v2/rates"
_COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"

# (курс, момент получения по time.time(), источник)
_cache: dict[str, object] = {"rate": None, "at": 0.0, "source": ""}


def _sane(value) -> float | None:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if not (_MIN_RATE <= rate <= _MAX_RATE):
        return None
    return rate


def manual_rate() -> float | None:
    """Ручной курс из настройки, только если он задан явно и выглядит как курс."""
    raw = (os.getenv("TON_RATE") or "").strip()
    if not raw:
        return None
    return _sane(raw)


def parse_tonapi(data) -> float | None:
    """Курс из ответа tonapi `/v2/rates?tokens=ton&currencies=usd`. ЧИСТАЯ."""
    try:
        rates = data["rates"]
        node = rates.get("TON") or rates.get("ton") or {}
        prices = node["prices"]
        return _sane(prices.get("USD") or prices.get("usd"))
    except (KeyError, TypeError, AttributeError):
        return None


def parse_coingecko(data) -> float | None:
    """Курс из ответа CoinGecko `simple/price`. ЧИСТАЯ."""
    try:
        return _sane(data["the-open-network"]["usd"])
    except (KeyError, TypeError):
        return None


async def _get_json(http: aiohttp.ClientSession, url: str, params: dict, headers: dict):
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
    async with http.get(url, params=params, headers=headers, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return await resp.json(content_type=None)


async def _fetch_live(http: aiohttp.ClientSession) -> tuple[float | None, str]:
    headers = {}
    key = (os.getenv("TON_API_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        rate = parse_tonapi(await _get_json(
            http, _TONAPI_URL, {"tokens": "ton", "currencies": "usd"}, headers))
        if rate:
            return rate, "tonapi"
    except Exception as exc:
        log.info("ton_rate: tonapi недоступен: %s", str(exc)[:120])
    try:
        rate = parse_coingecko(await _get_json(
            http, _COINGECKO_URL,
            {"ids": "the-open-network", "vs_currencies": "usd"}, {}))
        if rate:
            return rate, "coingecko"
    except Exception as exc:
        log.info("ton_rate: coingecko недоступен: %s", str(exc)[:120])
    return None, ""


def cached(now: float | None = None) -> tuple[float | None, str]:
    """Последний рыночный курс, если он ещё годен, иначе ручной. Без сети."""
    now = time.time() if now is None else now
    rate = _cache.get("rate")
    if rate and now - float(_cache.get("at") or 0) <= STALE_MAX_S:
        return float(rate), str(_cache.get("source") or "")
    manual = manual_rate()
    if manual:
        return manual, "manual"
    return None, ""


async def get_rate(http: aiohttp.ClientSession | None = None,
                   now: float | None = None) -> tuple[float | None, str]:
    """Курс TON/USD и его источник: tonapi | coingecko | manual | ''."""
    now = time.time() if now is None else now
    rate = _cache.get("rate")
    if rate and now - float(_cache.get("at") or 0) <= FRESH_S:
        return float(rate), str(_cache.get("source") or "")
    own = http is None
    session = http or aiohttp.ClientSession()
    try:
        live, source = await _fetch_live(session)
    finally:
        if own:
            await session.close()
    if live:
        _cache.update(rate=live, at=now, source=source)
        return live, source
    return cached(now)


def source_label(source: str) -> str:
    """Источник курса по-русски — для экрана настройки оплаты."""
    return {
        "tonapi": "рынок (tonapi.io)",
        "coingecko": "рынок (CoinGecko)",
        "manual": "ручной, рынок недоступен",
    }.get(source, "нет курса")


def _reset_for_tests() -> None:
    _cache.update(rate=None, at=0.0, source="")
