"""Планировщик кампании: цель → ёмкостная раскладка (чистое ядро compute_plan)."""
from __future__ import annotations

from services.campaign_planner import compute_plan, JOIN_YIELD


def test_feasible_when_capacity_covers_deadline():
    # цель 600, yield direct 0.6 → нужно 1000 инвайтов; ёмкость 300/сут, 10 акк.
    p = compute_plan(600, 5, daily_capacity=300, n_accounts=10, method="direct")
    assert p["invites_needed"] == 1000
    assert p["days_at_capacity"] == 4          # ceil(1000/300)
    assert p["feasible"] is True               # 4 <= 5
    assert p["per_account_cap"] == 30
    assert "✅" in p["verdict"]


def test_infeasible_recommends_extra_accounts():
    # цель 6000 direct → 10000 инвайтов; ёмкость 300/сут, 10 акк; срок 5 дней.
    p = compute_plan(6000, 5, daily_capacity=300, n_accounts=10, method="direct")
    assert p["feasible"] is False
    # нужно 2000/сут, каждый аккаунт тянет ~30 → всего ~67 акк → +57
    assert p["needed_daily"] == 2000
    assert p["extra_accounts"] >= 50
    assert "не хватает" in p["verdict"].lower()


def test_method_yield_changes_invites_needed():
    direct = compute_plan(1000, 7, 500, 10, "direct")
    admin = compute_plan(1000, 7, 500, 10, "admin")
    link = compute_plan(1000, 7, 500, 10, "link")
    # admin даёт выше конверсию → меньше инвайтов; link — сильно больше
    assert admin["invites_needed"] < direct["invites_needed"] < link["invites_needed"]
    assert admin["join_yield"] == JOIN_YIELD["admin"]


def test_no_accounts_or_capacity_guard():
    assert compute_plan(100, 3, 0, 0, "direct")["feasible"] is False
    assert "Нет активных аккаунтов" in compute_plan(100, 3, 0, 0, "direct")["verdict"]
    # есть аккаунты, но нулевая ёмкость (холодный старт/флуды)
    v = compute_plan(100, 3, 0, 5, "direct")["verdict"]
    assert "ёмкости" in v.lower()


def test_launch_config_carries_settings():
    p = compute_plan(600, 5, 300, 10, "admin")
    assert p["launch"]["invite_method"] == "admin"
    assert p["launch"]["per_account_limit"] == p["recommended_per_account"]
    assert 1 <= p["recommended_per_account"] <= 50
