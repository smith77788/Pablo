"""Основная валюта владельца и пересчёт выручки в неё.

Зачем. Заказы ботов-продавцов приходят в той валюте, в какой владелец завёл
прайс (рубли, доллары, звёзды), а расход на кампании — в своей. Пока валюты
не пересчитывались, аналитика честно отказывалась складывать рубли с
долларами, и «итого» с окупаемостью оставались пустыми у любого, кто продаёт
больше чем в одной валюте. Здесь — одна валюта отчётов на владельца и курс
пересчёта.

Откуда курс, по приоритету:

1. ручной курс владельца («1 USD = 95 ₽») — он знает, по какому курсу реально
   выводит деньги;
2. курс ЦБ РФ (cbr-xml-daily.ru), общий для всех, обновляется не чаще чем раз
   в REFRESH_HOURS и хранится в fx_rates — сбой источника не ломает отчёт,
   остаётся последний сохранённый курс;
3. звёзды Telegram (XTR) — по выплатному курсу Telegram STARS_USD за звезду,
   через доллар.

Сумма в валюте, для которой курса нет, не пересчитывается и не прячется:
отчёт показывает её отдельной строкой «не пересчитано».
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

MAIN_CURRENCIES = ("RUB", "USD", "EUR", "KZT", "UAH", "BYN", "UZS", "TRY", "GBP", "CNY")
CURRENCY_LABEL = {
    "RUB": "₽", "USD": "$", "EUR": "€", "KZT": "₸", "UAH": "₴", "BYN": "Br",
    "UZS": "сум", "TRY": "₺", "GBP": "£", "CNY": "¥", "XTR": "⭐",
}
STARS_USD = 0.013           # выплатный курс Telegram за одну звезду
REFRESH_HOURS = 12
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
DEFAULT_MAIN = "RUB"


def _norm(cur) -> str:
    return str(cur or "").strip().upper()[:8]


class Converter:
    """Пересчёт сумм в основную валюту владельца.

    rub_per: {валюта: сколько рублей стоит 1 единица} — курс ЦБ;
    manual:  {валюта: сколько основной валюты стоит 1 единица} — курс владельца.
    """

    def __init__(self, main: str, rub_per: dict | None = None, manual: dict | None = None):
        self.main = _norm(main) or DEFAULT_MAIN
        self.rub_per = {_norm(k): float(v) for k, v in (rub_per or {}).items()
                        if v and float(v) > 0}
        self.rub_per["RUB"] = 1.0
        if "XTR" not in self.rub_per and "USD" in self.rub_per:
            self.rub_per["XTR"] = STARS_USD * self.rub_per["USD"]
        self.manual = {_norm(k): float(v) for k, v in (manual or {}).items()
                       if v and float(v) > 0}

    def rate(self, cur) -> float | None:
        """Сколько основной валюты стоит 1 единица `cur`; None — курса нет."""
        cur = _norm(cur) or self.main
        if cur == self.main:
            return 1.0
        if cur in self.manual:
            return self.manual[cur]
        if cur == "XTR" and "USD" in self.manual:
            return STARS_USD * self.manual["USD"]
        a, b = self.rub_per.get(cur), self.rub_per.get(self.main)
        if a and b:
            return a / b
        return None

    def convert(self, amount, cur) -> float | None:
        r = self.rate(cur)
        if r is None or amount is None:
            return None
        return float(amount) * r


def parse_cbr(payload: dict) -> dict:
    """Ответ cbr-xml-daily → {валюта: рублей за 1 единицу}."""
    out = {}
    for code, v in ((payload or {}).get("Valute") or {}).items():
        try:
            nominal = float(v.get("Nominal") or 1)
            value = float(v.get("Value"))
        except (TypeError, ValueError, AttributeError):
            continue
        if nominal > 0 and value > 0:
            out[_norm(code)] = value / nominal
    return out


def clean_manual_rates(raw, main: str) -> dict:
    """Проверить ручные курсы владельца: положительные числа, известные коды."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("Курсы должны быть списком «валюта → число»")
    out = {}
    for k, v in raw.items():
        code = _norm(k)
        if not code or code == _norm(main):
            continue
        if not code.isalpha() or len(code) != 3:
            raise ValueError(f"Неизвестный код валюты: {k}")
        if v in (None, ""):
            continue
        try:
            val = float(str(v).replace(",", "."))
        except ValueError:
            raise ValueError(f"Курс {code} должен быть числом") from None
        if not (0 < val < 1e9):
            raise ValueError(f"Курс {code} вне допустимых пределов")
        out[code] = val
    return out


# ── База ──────────────────────────────────────────────────────────────────

async def _fetch_cbr() -> dict:
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=6)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        async with http.get(CBR_URL) as r:
            if r.status != 200:
                raise RuntimeError(f"ЦБ ответил {r.status}")
            return parse_cbr(json.loads(await r.text()))


async def stored_rates(pool) -> tuple[dict, datetime | None]:
    try:
        rows = await pool.fetch("SELECT currency, rub_per_unit, fetched_at FROM fx_rates")
    except Exception:
        log.debug("owner_currency: fx_rates недоступна", exc_info=True)
        return {}, None
    rates = {r["currency"]: float(r["rub_per_unit"]) for r in rows}
    at = max((r["fetched_at"] for r in rows), default=None)
    return rates, at


async def refresh_rates(pool, fetch=None, now: datetime | None = None) -> tuple[dict, datetime | None]:
    """Вернуть курсы ЦБ, обновив их, если сохранённым больше REFRESH_HOURS.

    Сбой источника не ошибка отчёта: остаются последние сохранённые курсы."""
    rates, at = await stored_rates(pool)
    now = now or datetime.now(timezone.utc)
    if at is not None and now - at < timedelta(hours=REFRESH_HOURS):
        return rates, at
    try:
        fresh = await (fetch or _fetch_cbr)()
    except Exception as exc:
        log.info("owner_currency: курс ЦБ не обновлён: %s", str(exc)[:120])
        return rates, at
    if not fresh:
        return rates, at
    try:
        await pool.execute(
            """INSERT INTO fx_rates (currency, rub_per_unit, fetched_at)
               SELECT * FROM unnest($1::text[], $2::numeric[], $3::timestamptz[])
               ON CONFLICT (currency) DO UPDATE
                  SET rub_per_unit = EXCLUDED.rub_per_unit, fetched_at = EXCLUDED.fetched_at""",
            list(fresh), [fresh[k] for k in fresh], [now] * len(fresh))
    except Exception:
        log.warning("owner_currency: курсы не сохранены", exc_info=True)
    return fresh, now


async def get_settings(pool, owner_id: int) -> dict:
    row = None
    try:
        row = await pool.fetchrow(
            "SELECT currency, manual_rates FROM owner_money_settings WHERE owner_id=$1",
            int(owner_id))
    except Exception:
        log.debug("owner_currency: настройки недоступны", exc_info=True)
    manual = {}
    if row and row["manual_rates"]:
        m = row["manual_rates"]
        manual = json.loads(m) if isinstance(m, str) else dict(m)
    return {"currency": (row["currency"] if row else None) or DEFAULT_MAIN,
            "manual_rates": manual}


async def set_settings(pool, owner_id: int, currency, manual_rates=None) -> dict:
    cur = _norm(currency) or DEFAULT_MAIN
    if cur not in MAIN_CURRENCIES:
        raise ValueError("Эта валюта не поддерживается как основная")
    manual = clean_manual_rates(manual_rates, cur)
    await pool.execute(
        """INSERT INTO owner_money_settings (owner_id, currency, manual_rates, updated_at)
           VALUES ($1, $2, $3::jsonb, now())
           ON CONFLICT (owner_id) DO UPDATE
              SET currency=EXCLUDED.currency, manual_rates=EXCLUDED.manual_rates,
                  updated_at=now()""",
        int(owner_id), cur, json.dumps(manual))
    return {"currency": cur, "manual_rates": manual}


async def load(pool, owner_id: int, fetch=None) -> tuple[Converter, dict]:
    """Конвертер владельца и сведения о курсе для подписи на экране."""
    st = await get_settings(pool, owner_id)
    rates, at = await refresh_rates(pool, fetch=fetch)
    conv = Converter(st["currency"], rates, st["manual_rates"])
    return conv, {
        "currency": conv.main,
        "symbol": CURRENCY_LABEL.get(conv.main, conv.main),
        "rates_at": at.isoformat() if at else None,
        "manual_rates": st["manual_rates"],
        "choices": list(MAIN_CURRENCIES),
    }
