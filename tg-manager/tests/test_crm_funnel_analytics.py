"""Аналитика воронки: чистая часть (services/crm_funnel_analytics.analyze).

Что здесь доказывается — и почему именно это.

* Перескок стадии не выглядит провалом воронки: «Лид → Переговоры» значит, что
  «Контакт» и «Предложение» пройдены (накопительная конверсия).
* Время в стадии не занижается: сделки, которые ЕЩЁ стоят в стадии, входят в
  оценку как незавершённые (Каплан–Мейер). Наивная медиана по вышедшим здесь
  дала бы 1 день — а половина сделок стоит больше десяти.
* Восстановленная при накате строка (время перехода неизвестно) в длительности
  не идёт, а сделка с ней не объявляется «застрявшей».
* Вероятность выигрыша стадии не бывает 0%/100% на одной сделке.
* Прогноз не складывает рубли с долларами.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services import crm_funnel_analytics as fa

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


def _t(key, frm, to, days_ago, backfilled=False, kind="contact"):
    return {"deal_kind": kind, "deal_key": key, "from_stage": frm, "to_stage": to,
            "changed_at": NOW - timedelta(days=days_ago), "backfilled": backfilled}


def _d(key, stage, value=0, currency="RUB", kind="contact", name=""):
    return {"deal_kind": kind, "deal_key": key, "stage": stage, "value": value,
            "currency": currency, "created_at": NOW - timedelta(days=60), "name": name or key}


def _stage(rep, s):
    return next(r for r in rep["stages"] if r["stage"] == s)


def test_a_skipped_stage_counts_as_passed():
    deals = [_d("a", "negotiation")]
    hist = [_t("a", None, "lead", 10), _t("a", "lead", "negotiation", 5)]
    rep = fa.analyze(deals, hist, now=NOW)
    assert [_stage(rep, s)["reached"] for s in ("lead", "contact", "proposal", "negotiation", "won")] \
        == [1, 1, 1, 1, 0]


def test_time_in_stage_counts_deals_still_inside():
    # 4 сделки вышли из «Лида» за 1 день, 6 стоят в нём уже 20 дней.
    deals, hist = [], []
    for i in range(4):
        deals.append(_d(f"q{i}", "contact"))
        hist += [_t(f"q{i}", None, "lead", 30), _t(f"q{i}", "lead", "contact", 29)]
    for i in range(6):
        deals.append(_d(f"s{i}", "lead"))
        hist.append(_t(f"s{i}", None, "lead", 20))
    lead = _stage(fa.analyze(deals, hist, now=NOW), "lead")
    assert lead["exits"] == 4 and lead["now_inside"] == 6
    # больше половины ещё не вышли: честной медианы НЕТ, а не «1 день»
    assert lead["days_median"] is None, lead


def test_km_median_with_censoring():
    curve = fa.kaplan_meier([(1, True), (2, True), (3, False), (4, True), (5, False)])
    assert fa.km_quantile(curve, 0.5) == 4


def test_backfilled_rows_give_no_durations_and_no_stuck():
    deals = [_d("a", "proposal")]
    hist = [_t("a", None, "proposal", 300, backfilled=True)]
    rep = fa.analyze(deals, hist, now=NOW)
    assert _stage(rep, "proposal")["now_inside"] == 0
    assert rep["stuck"] == [] and rep["history_since"] is None


def test_stuck_uses_fallback_when_data_is_thin_and_says_so():
    deals = [_d("a", "proposal", 1000, name="Иван")]
    hist = [_t("a", None, "lead", 40), _t("a", "lead", "proposal", 30)]
    rep = fa.analyze(deals, hist, now=NOW)
    assert rep["stuck_total"] == 1
    s = rep["stuck"][0]
    assert s["name"] == "Иван" and s["threshold_own"] is False
    assert s["limit_days"] == fa.FALLBACK_STUCK_DAYS


def test_stuck_threshold_is_learned_per_stage():
    deals, hist = [], []
    for i in range(6):  # обычно в «Предложении» 2 дня
        deals.append(_d(f"w{i}", "won"))
        hist += [_t(f"w{i}", None, "proposal", 20), _t(f"w{i}", "proposal", "won", 18)]
    deals.append(_d("slow", "proposal"))
    hist.append(_t("slow", None, "proposal", 5))
    deals.append(_d("fresh", "proposal"))
    hist.append(_t("fresh", None, "proposal", 1))
    rep = fa.analyze(deals, hist, now=NOW)
    keys = [x["deal_key"] for x in rep["stuck"]]
    assert keys == ["slow"], rep["stuck"]
    assert rep["stuck"][0]["threshold_own"] is True


def test_win_probability_is_smoothed_and_rates_have_intervals():
    deals = [_d("w", "won", 100), _d("l", "lost")]
    hist = [_t("w", None, "lead", 20), _t("w", "lead", "negotiation", 10), _t("w", "negotiation", "won", 5),
            _t("l", None, "lead", 20), _t("l", "lead", "lost", 3)]
    rep = fa.analyze(deals, hist, now=NOW)
    neg = _stage(rep, "negotiation")["win_probability"]
    assert 0 < neg < 1, neg   # одна выигранная сделка — не «100%»
    assert rep["win_rate"] == 0.5 and rep["won"] == 1 and rep["lost"] == 1
    lo, hi = rep["win_rate_ci"]
    assert lo < 0.5 < hi and hi - lo > 0.5  # две сделки — широкий интервал


def test_forecast_keeps_currencies_apart():
    deals = [_d("a", "proposal", 1000, "RUB"), _d("b", "proposal", 10, "USD")]
    hist = [_t("a", None, "proposal", 1), _t("b", None, "proposal", 1)]
    rep = fa.analyze(deals, hist, now=NOW)
    assert set(rep["pipeline_by_currency"]) == {"RUB", "USD"}
    assert rep["pipeline_by_currency"]["RUB"] == 1000.0
    assert 0 < rep["forecast_by_currency"]["USD"] < 10


def test_wilson_bounds():
    assert fa.wilson(0, 0) is None
    lo, hi = fa.wilson(300, 400)
    assert 0.70 < lo < 0.75 < hi < 0.80
