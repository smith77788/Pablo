"""Аналитика продаж: чистая часть (services/crm_sales_insights.analyze).

Что здесь доказывается — и почему именно это.

* Сравнение с прошлым периодом считает новые сделки по первому появлению, а
  закрытые — по дате закрытия, и не складывает рубли с долларами.
* «Где теряем» берёт стадию, из которой сделка ушла в «Проиграно»; проигрыш,
  известный только по восстановленной строке (время и путь неизвестны), не
  приписывается «Лиду», а честно идёт как «стадия неизвестна».
* Источник — аккаунт, нашедший клиента первым; доля выигранных по нему идёт с
  интервалом, «без источника» не заслоняет аккаунты.
* «Без следующего шага»: напоминание на будущее снимает сделку из списка,
  просроченное — нет; свежая смена стадии — тоже касание; закрытые сделки
  брошенными не бывают.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services import crm_sales_insights as si

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


def _t(key, frm, to, days_ago, backfilled=False, kind="contact"):
    return {"deal_kind": kind, "deal_key": key, "from_stage": frm, "to_stage": to,
            "changed_at": NOW - timedelta(days=days_ago), "backfilled": backfilled}


def _d(key, stage, value=0, currency="RUB", kind="contact", created_days=60):
    return {"deal_kind": kind, "deal_key": key, "stage": stage, "value": value,
            "currency": currency, "created_at": NOW - timedelta(days=created_days),
            "name": key}


def _run(deals, hist, meta=None, **kw):
    return si.analyze(deals, hist, meta or {}, now=NOW, **kw)


def test_period_compare_counts_new_and_closed_by_their_own_dates():
    deals = [_d("a", "won", 100), _d("b", "won", 50, "USD"), _d("c", "lost"),
             _d("old", "won", 300)]
    hist = [
        _t("a", None, "lead", 20), _t("a", "lead", "won", 10),
        # появилась в прошлом окне, выиграна в текущем
        _t("b", None, "lead", 45), _t("b", "lead", "won", 5),
        _t("c", None, "lead", 40), _t("c", "lead", "lost", 35),
        _t("old", None, "lead", 50), _t("old", "lead", "won", 40),
    ]
    rep = _run(deals, hist, window_days=30)
    assert rep["period"]["new"] == 1 and rep["previous"]["new"] == 3
    assert rep["period"]["won"] == 2 and rep["period"]["lost"] == 0
    assert rep["previous"]["won"] == 1 and rep["previous"]["lost"] == 1
    assert rep["period"]["won_by_currency"] == {"RUB": 100.0, "USD": 50.0}
    assert rep["previous"]["won_by_currency"] == {"RUB": 300.0}
    assert rep["compare"]["won"] == 1.0                     # 2 против 1
    assert rep["compare"]["win_rate_pp"] == 0.5             # 100% против 50%
    assert rep["compare"]["won_by_currency"]["USD"] is None  # было 0 — сравнивать не с чем


def test_monthly_has_six_months_ending_now():
    rep = _run([_d("a", "won", 10)], [_t("a", None, "lead", 3), _t("a", "lead", "won", 1)])
    months = [m["month"] for m in rep["monthly"]]
    assert months == ["2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11"]
    nov = rep["monthly"][-1]
    assert nov["new"] == 1 and nov["won"] == 1 and nov["won_by_currency"] == {"RUB": 10.0}


def test_losses_are_attributed_to_the_stage_they_left():
    deals = [_d("a", "lost", 100), _d("b", "lost", 5, "USD"), _d("c", "lost"),
             _d("bf", "lost")]
    hist = [
        _t("a", None, "lead", 20), _t("a", "lead", "proposal", 15), _t("a", "proposal", "lost", 10),
        _t("b", None, "lead", 20), _t("b", "lead", "proposal", 15), _t("b", "proposal", "lost", 9),
        _t("c", None, "lead", 20), _t("c", "lead", "lost", 8),
        # известна только восстановленная строка: откуда ушла — неизвестно
        _t("bf", None, "lost", 5, backfilled=True),
    ]
    rep = _run(deals, hist)
    by = {r["stage"]: r for r in rep["losses"]}
    assert by["proposal"]["count"] == 2
    assert by["proposal"]["value_by_currency"] == {"RUB": 100.0, "USD": 5.0}
    assert by["lead"]["count"] == 1
    assert by[si.UNKNOWN_STAGE]["count"] == 1, "восстановленный проигрыш приписан стадии"
    assert rep["losses_total"] == 4
    assert rep["worst_loss_stage"] == "proposal"
    assert [r["stage"] for r in rep["losses"]] == ["lead", "proposal", si.UNKNOWN_STAGE]


def test_sources_rank_accounts_and_carry_an_interval():
    deals = [_d("a", "won", 100), _d("b", "lost"), _d("c", "won", 7, "USD"),
             _d("d", "proposal"), _d("e", "won", 1)]
    hist = [_t(k, None, "lead", 20) for k in "abcde"] + [
        _t("a", "lead", "won", 2), _t("b", "lead", "lost", 2),
        _t("c", "lead", "won", 2), _t("e", "lead", "won", 2)]
    meta = {("contact", "a"): {"source": 7}, ("contact", "b"): {"source": 7},
            ("contact", "c"): {"source": 9}, ("contact", "d"): {"source": 9},
            ("contact", "e"): {"source": None}}
    rep = _run(deals, hist, meta)
    src = rep["sources"]
    assert [s["source"] for s in src] == ["7", "9", si.NO_SOURCE]
    s7 = src[0]
    assert (s7["leads"], s7["won"], s7["lost"], s7["open"]) == (2, 1, 1, 0)
    assert s7["win_rate"] == 0.5 and s7["win_rate_ci"][0] < 0.5 < s7["win_rate_ci"][1]
    s9 = src[1]
    assert s9["open"] == 1 and s9["won_by_currency"] == {"USD": 7.0}
    assert s9["win_rate"] == 1.0 and s9["win_rate_ci"][0] < 1.0


def test_sources_only_count_deals_that_appeared_in_the_window():
    rep = _run([_d("a", "won")], [_t("a", None, "lead", 200), _t("a", "lead", "won", 5)],
               {("contact", "a"): {"source": 1}}, window_days=90)
    assert rep["sources"] == []


def test_neglected_deals_need_no_future_reminder_and_no_recent_touch():
    deals = [_d("idle", "proposal", 500), _d("planned", "proposal", 900),
             _d("overdue", "lead", 10), _d("fresh", "contact"), _d("moved", "negotiation"),
             _d("closed", "won"), _d("never", "lead", created_days=40)]
    hist = [
        _t("idle", None, "lead", 50), _t("idle", "lead", "proposal", 30),
        _t("planned", None, "proposal", 50),
        _t("overdue", None, "lead", 40),
        _t("fresh", None, "contact", 40),
        _t("moved", None, "lead", 40), _t("moved", "lead", "negotiation", 2),
        _t("closed", None, "won", 40),
        _t("never", None, "lead", 40, backfilled=True),
    ]
    meta = {
        ("contact", "planned"): {"next_reminder_at": NOW + timedelta(days=1)},
        ("contact", "overdue"): {"next_reminder_at": NOW - timedelta(days=3)},
        ("contact", "fresh"): {"last_touch_at": NOW - timedelta(days=1)},
    }
    rep = _run(deals, hist, meta)
    got = {x["deal_key"]: x for x in rep["neglected"]}
    assert set(got) == {"idle", "overdue", "never"}
    assert got["idle"]["idle_days"] == 30.0
    assert got["overdue"]["reminder_overdue"] is True
    assert got["never"]["touch_known"] is False and got["never"]["idle_days"] == 40.0
    assert [x["deal_key"] for x in rep["neglected"]][0] == "idle"  # дороже — выше
    assert rep["neglected_total"] == 3 and rep["open"] == 6


def test_empty_owner_gets_a_well_formed_report():
    rep = _run([], [])
    assert rep["period"]["win_rate"] is None and rep["compare"]["new"] is None
    assert rep["losses"] == [] and rep["sources"] == [] and rep["neglected"] == []
    assert len(rep["monthly"]) == si.MONTHS


def test_account_label_never_shows_the_full_phone():
    assert si.account_label({"first_name": "Маша", "phone": "+79991234567"}) == "Маша"
    assert si.account_label({"username": "shop", "phone": "+79991234567"}) == "@shop"
    lbl = si.account_label({"id": 3, "phone": "+79991234567"})
    assert "4567" in lbl and "7999123" not in lbl
    assert si.account_label({"id": 3}) == "Аккаунт #3"
