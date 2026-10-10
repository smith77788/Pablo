"""Экономика флота: радар утечки бюджета (прокси/аккаунты).

Проверяем чистую классификацию утечек, подсчёт денег, маскировку кредов,
сортировку «сначала дорогое», сбор отчёта на FakePool и разводку в бота.
"""
from __future__ import annotations

import os

import pytest

from tests.test_executors import FakePool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from services import budget_radar as br  # noqa: E402

COST = {
    "proxy_monthly": 100.0, "account_unit": 20.0, "currency": "₽",
    "idle_days": 7, "fast_death_days": 3, "burn_window_days": 30,
}


# ── Чистые функции ──────────────────────────────────────────────────────────────

def test_num_parses_and_guards():
    assert br._num("50", 1) == 50.0
    assert br._num("", 7) == 7.0        # пусто → дефолт
    assert br._num("abc", 7) == 7.0     # мусор → дефолт
    assert br._num("-5", 7) == 7.0      # отрицательное → дефолт


def test_fmt_money_int_vs_fraction():
    assert br.fmt_money(100, "₽") == "100 ₽"      # целое без копеек
    assert br.fmt_money(99.5, "$") == "99.50 $"   # дробное — 2 знака
    assert br.fmt_money("junk", "₽") == "0 ₽"     # не падает


def test_short_proxy_hides_credentials():
    # логин:пароль в URL не должны утечь на экран
    out = br._short_proxy(None, "socks5://user:secret@1.2.3.4:1080")
    assert out == "1.2.3.4:1080"
    assert "secret" not in out and "user" not in out
    # метка приоритетнее URL
    assert br._short_proxy("MTS-mobile", "socks5://x:y@9.9.9.9:1") == "MTS-mobile"
    assert br._short_proxy(None, "") == "—"


def test_classify_dead_proxy_assigned_is_top():
    # мёртвый прокси, но аккаунт всё ещё привязан — и опасно, и деньги на ветер
    assert br.classify_proxy(
        {"is_alive": False, "assigned_total": 2, "assigned_dead": 0, "assigned_idle": 0}
    ) == "dead_proxy_assigned"


def test_classify_dead_weight_only_dead_accounts():
    assert br.classify_proxy(
        {"is_alive": True, "assigned_total": 3, "assigned_dead": 3, "assigned_idle": 0}
    ) == "dead_weight"


def test_classify_idle_empty_proxy():
    assert br.classify_proxy(
        {"is_alive": True, "assigned_total": 0, "assigned_dead": 0, "assigned_idle": 0}
    ) == "idle"


def test_classify_stale_all_live_idle():
    assert br.classify_proxy(
        {"is_alive": True, "assigned_total": 2, "assigned_dead": 0, "assigned_idle": 2}
    ) == "stale"


def test_classify_healthy_returns_none():
    # есть хотя бы один живой работающий аккаунт → прокси окупается
    assert br.classify_proxy(
        {"is_alive": True, "assigned_total": 3, "assigned_dead": 1, "assigned_idle": 1}
    ) is None


def test_leak_from_row_carries_money_and_fix():
    row = {"id": 5, "label": "p5", "proxy_url": "socks5://a:b@1.1.1.1:1",
           "is_alive": True, "assigned_total": 2, "assigned_dead": 2, "assigned_idle": 0}
    leak = br.leak_from_row(row, COST)
    assert leak["kind"] == "dead_weight"
    assert leak["proxy_id"] == 5 and leak["proxy"] == "p5"
    assert leak["monthly_waste"] == 100.0
    assert leak["fix"]  # есть готовое действие


def test_rank_puts_costliest_and_hottest_first():
    leaks = [
        {"kind": "stale", "monthly_waste": 100},
        {"kind": "dead_proxy_assigned", "monthly_waste": 10},
        {"kind": "idle", "monthly_waste": 100},
    ]
    ranked = br.rank_leaks(leaks)
    assert ranked[0]["kind"] == "dead_proxy_assigned"  # приоритет типа важнее денег
    assert br.total_monthly_waste(leaks) == 210.0
    assert br.summarize(leaks)["idle"] == 1


# ── Async: сбор отчёта ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scan_builds_report_and_ranks():
    rows = [
        # окупается — не утечка
        {"id": 1, "label": None, "proxy_url": "socks5://1.1.1.1:1", "is_alive": True,
         "assigned_total": 2, "assigned_dead": 0, "assigned_idle": 0},
        # пустой прокси — утечка idle
        {"id": 2, "label": "empty", "proxy_url": "socks5://2.2.2.2:2", "is_alive": True,
         "assigned_total": 0, "assigned_dead": 0, "assigned_idle": 0},
        # мёртвый прокси под аккаунтом — топ-утечка
        {"id": 3, "label": "dead", "proxy_url": "socks5://3.3.3.3:3", "is_alive": False,
         "assigned_total": 1, "assigned_dead": 0, "assigned_idle": 0},
    ]
    pool = FakePool(fetch=rows, fetchval=4)  # fetchval → сгоревшие регистрации
    rep = await br.scan(pool, owner_id=1, cost=COST)

    assert rep["proxies_total"] == 3
    assert rep["leak_count"] == 2                      # прокси #1 окупается
    assert rep["leaks"][0]["kind"] == "dead_proxy_assigned"
    assert rep["monthly_waste"] == 200.0              # две утечки × 100
    assert rep["annual_waste"] == 2400.0
    assert rep["burned"]["count"] == 4
    assert rep["burned"]["cost"] == 80.0             # 4 × 20
    assert rep["currency"] == "₽"


@pytest.mark.asyncio
async def test_scan_failsoft_on_db_error():
    class Boom(FakePool):
        async def fetch(self, q, *a):
            raise RuntimeError("db down")

    rep = await br.scan(Boom(fetchval=0), owner_id=1, cost=COST)
    assert rep["leak_count"] == 0 and rep["monthly_waste"] == 0
    assert rep["proxies_total"] == 0


# ── Разводка в бота ─────────────────────────────────────────────────────────────

def test_wired_into_bot():
    m = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "budget_radar_handler.router" in m, "роутер экономики флота не подключён"
    assert 'command="budget"' in m, "команды /budget нет в меню бота"

    h = open(os.path.join(ROOT, "bot", "handlers", "budget_radar.py"),
             encoding="utf-8").read()
    assert 'Command("budget")' in h
    assert 'F.action == "budget"' in h

    k = open(os.path.join(ROOT, "bot", "keyboards.py"), encoding="utf-8").read()
    assert 'action="budget"' in k, "кнопки «Экономика флота» нет в главном меню"
