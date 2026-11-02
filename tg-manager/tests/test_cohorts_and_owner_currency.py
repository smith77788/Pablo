"""Когорты, LTV и пересчёт в основную валюту — чистая логика.

Что стережёт:

* человек попадает в когорту по ПЕРВОМУ касанию из любого канала, и источник —
  канал этого касания; покупатель без касаний не теряется;
* удержание и покупки считаются по периодам от прихода, будущие периоды —
  пустые, а не нули;
* LTV — накопленная выручка на человека когорты; средняя кривая не тянется
  вниз молодыми когортами;
* сумма в валюте без курса не попадает в LTV и не исчезает — она в
  «не пересчитано»;
* курс владельца важнее курса ЦБ, звёзды идут через доллар;
* воронка денег считает окупаемость в основной валюте, когда выручка и расход
  в разных валютах.

Запросы к базе проверяет tests/test_cohorts_postgres.py.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from services import cohorts as co
from services import money_funnel as mf
from services import owner_currency as oc

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def _d(y, m, d):
    return datetime(y, m, d, 12, tzinfo=timezone.utc)


# ── Периоды ────────────────────────────────────────────────────────────────

def test_periods_by_month_and_week():
    assert co.period_start(_d(2026, 10, 17), "month") == date(2026, 10, 1)
    assert co.period_start(_d(2026, 11, 1), "week") == date(2026, 10, 26)  # понедельник
    assert co.period_index(date(2025, 12, 1), _d(2026, 2, 3), "month") == 2
    assert co.period_index(date(2026, 10, 26), _d(2026, 11, 9), "week") == 2
    assert co.shift(date(2026, 11, 1), -5, "month") == date(2026, 6, 1)


# ── Когорты ────────────────────────────────────────────────────────────────

def test_first_touch_wins_across_channels():
    acq = co.acquisition([
        (1, _d(2026, 10, 5), "бот @shop"),
        (1, _d(2026, 9, 20), "рассылка «Осень»"),
        (2, _d(2026, 11, 2), "написал на аккаунт"),
        (3, None, "бот @shop"),
    ])
    assert acq[1][1] == "рассылка «Осень»"
    assert set(acq) == {1, 2}


def test_cohort_retention_and_ltv():
    acq = {
        1: (_d(2026, 9, 3), "бот @shop"),
        2: (_d(2026, 9, 10), "бот @shop"),
        3: (_d(2026, 10, 1), "рассылка «Осень»"),
    }
    orders = [(1, _d(2026, 9, 5), 1000, "RUB"),
              (1, _d(2026, 10, 7), 500, "RUB"),
              (3, _d(2026, 11, 2), 300, "RUB")]
    activity = [(2, _d(2026, 11, 3))]
    out = co.build(acq, orders, activity, grain="month", periods=3, now=NOW)
    by = {c["start"]: c for c in out["cohorts"]}
    sep, octo = by["2026-09-01"], by["2026-10-01"]
    assert sep["size"] == 2 and octo["size"] == 1
    # Сентябрьская: купил 1 из 2 в месяц прихода, 1 из 2 — через месяц,
    # второй человек вернулся написать через два месяца.
    assert sep["buyers"] == [0.5, 0.5, 0.0]
    assert sep["active"] == [0.5, 0.5, 0.5]
    assert sep["ltv"] == [500.0, 750.0, 750.0]
    # Октябрьская прожила только два периода: третий — пусто, не ноль.
    assert octo["ltv"] == [0.0, 300.0, None]
    assert octo["buyers"][2] is None
    # Средняя кривая на +2 — только по сентябрьской когорте.
    assert out["ltv_curve"][2] == 750.0
    assert out["totals"]["people"] == 3 and out["totals"]["buyers"] == 2
    assert out["totals"]["ltv"] == 600.0       # 1800 / 3
    assert out["totals"]["arppu"] == 900.0


def test_ltv_by_source():
    acq = {1: (_d(2026, 10, 1), "бот @shop"), 2: (_d(2026, 10, 2), "бот @shop"),
           3: (_d(2026, 10, 3), "рассылка «Осень»")}
    orders = [(3, _d(2026, 10, 13), 900, "RUB"), (1, _d(2026, 10, 2), 100, "RUB")]
    out = co.build(acq, orders, grain="month", periods=2, now=NOW)
    src = {s["label"]: s for s in out["sources"]}
    assert src["рассылка «Осень»"]["ltv"] == 900.0
    assert src["рассылка «Осень»"]["median_days_to_buy"] == 10.0
    assert src["бот @shop"]["ltv"] == 50.0 and src["бот @shop"]["paying_share"] == 0.5
    # Доходнейший источник — первым.
    assert out["sources"][0]["label"] == "рассылка «Осень»"


def test_buyer_without_touch_is_not_lost():
    out = co.build({}, [(9, _d(2026, 11, 1), 200, "RUB")], grain="month", periods=2, now=NOW)
    assert out["totals"]["revenue"] == 200.0
    assert out["sources"][0]["label"] == co.UNKNOWN_SOURCE


def test_old_people_stay_out_of_the_window():
    out = co.build({1: (_d(2025, 1, 1), "бот")}, [(1, _d(2026, 11, 1), 50, "RUB")],
                   grain="month", periods=3, now=NOW)
    assert out["totals"]["people"] == 0 and out["cohorts"] == []


def test_currency_without_rate_is_shown_not_summed():
    conv = oc.Converter("RUB", {"USD": 90.0}, {})
    out = co.build({1: (_d(2026, 11, 1), "бот")},
                   [(1, _d(2026, 11, 2), 10, "USD"), (1, _d(2026, 11, 3), 5, "ABC")],
                   grain="month", periods=2, now=NOW, convert=conv.convert)
    assert out["totals"]["revenue"] == 900.0
    assert out["unconverted"] == [{"currency": "ABC", "amount": 5.0}]


# ── Валюта ────────────────────────────────────────────────────────────────

def test_converter_priorities():
    c = oc.Converter("USD", {"USD": 90.0, "EUR": 99.0}, {"EUR": 1.2})
    assert c.rate("USD") == 1.0
    assert c.rate("EUR") == 1.2                       # свой курс важнее ЦБ
    assert c.convert(900, "RUB") == pytest.approx(10.0)
    assert c.rate("XTR") == pytest.approx(oc.STARS_USD)
    assert c.rate("ABC") is None
    r = oc.Converter("RUB", {}, {"USD": 100})
    assert r.rate("XTR") == pytest.approx(oc.STARS_USD * 100)
    assert r.rate("EUR") is None


def test_parse_cbr_respects_nominal():
    rates = oc.parse_cbr({"Valute": {"USD": {"Nominal": 1, "Value": 92.5},
                                     "KZT": {"Nominal": 100, "Value": 18.0},
                                     "BAD": {"Nominal": 1, "Value": None}}})
    assert rates == {"USD": 92.5, "KZT": 0.18}


def test_manual_rates_are_validated():
    assert oc.clean_manual_rates({"usd": "95,5", "EUR": "", "RUB": 1}, "RUB") == {"USD": 95.5}
    with pytest.raises(ValueError):
        oc.clean_manual_rates({"USD": -1}, "RUB")
    with pytest.raises(ValueError):
        oc.clean_manual_rates({"доллар": 90}, "RUB")
    with pytest.raises(ValueError):
        oc.clean_manual_rates(["USD"], "RUB")


def test_set_settings_refuses_unknown_main_currency():
    class P:
        async def execute(self, *a):
            raise AssertionError("не должно писаться")

    with pytest.raises(ValueError):
        asyncio.new_event_loop().run_until_complete(oc.set_settings(P(), 1, "XTR"))


class _RatesPool:
    def __init__(self, rows):
        self.rows = rows
        self.written = None

    async def fetch(self, q, *a):
        return self.rows

    async def execute(self, q, *a):
        self.written = a


def test_rates_are_refreshed_only_when_stale_and_failure_keeps_old():
    run = asyncio.new_event_loop().run_until_complete
    fresh = [{"currency": "USD", "rub_per_unit": 90, "fetched_at": NOW - timedelta(hours=1)}]

    async def boom():
        raise AssertionError("свежий курс не перезапрашивается")

    rates, _ = run(oc.refresh_rates(_RatesPool(fresh), fetch=boom, now=NOW))
    assert rates == {"USD": 90.0}

    stale = [{"currency": "USD", "rub_per_unit": 90, "fetched_at": NOW - timedelta(days=2)}]

    async def down():
        raise RuntimeError("ЦБ недоступен")

    rates, _ = run(oc.refresh_rates(_RatesPool(stale), fetch=down, now=NOW))
    assert rates == {"USD": 90.0}

    async def ok():
        return {"USD": 95.0}

    p = _RatesPool(stale)
    rates, at = run(oc.refresh_rates(p, fetch=ok, now=NOW))
    assert rates == {"USD": 95.0} and at == NOW and p.written[0] == ["USD"]


# ── Воронка денег в основной валюте ────────────────────────────────────────

def test_money_funnel_romi_across_currencies_with_rate():
    t0 = _d(2026, 11, 1)
    b = mf.attribute([(1, 1, t0)], orders=[(1, t0 + timedelta(hours=1), 10000, "USD")])
    conv = oc.Converter("RUB", {"USD": 90.0}, {})
    s = mf.summarize(b, spend={1: {"amount": 4500, "currency": "RUB"}}, convert=conv.convert)
    row = s["campaigns"][0]
    assert row["revenue_main"] == 9000.0
    assert row["romi"] == 1.0                       # 9000 ₽ на 4500 ₽
    assert s["revenue_main"] == 9000.0 and s["spend_main"] == 4500.0
    # Без курса — окупаемость не выдумывается.
    s2 = mf.summarize(b, spend={1: {"amount": 4500, "currency": "RUB"}},
                      convert=oc.Converter("RUB", {}, {}).convert)
    assert s2["campaigns"][0]["romi"] is None and s2["revenue_main"] is None
