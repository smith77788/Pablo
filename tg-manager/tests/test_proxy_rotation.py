"""Регрессия: proxy rotation — планировщик НИКОГДА не ломает изоляцию.

Инвариант (класс багов «изоляция», CLAUDE.md — самый дорогой): после ротации
никакие два аккаунта не делят один прокси. Проверяем на всех краевых случаях,
включая пул меньше числа аккаунтов (где наивный сдвиг дал бы коллизию со
«скипнутым» аккаунтом, удерживающим прокси из пула).
"""
from __future__ import annotations

import os

from services.proxy_rotation import plan_rotation

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _final(assignments, result):
    """Итоговое назначение: план перекрывает старое, скипнутые держат своё."""
    m = {int(a["account_id"]): a.get("proxy_id") for a in assignments}
    for ch in result["plan"]:
        m[ch["account_id"]] = ch["new"]
    return m


def _assert_isolated(m):
    used = [p for p in m.values() if p is not None]
    assert len(used) == len(set(used)), f"НАРУШЕНА ИЗОЛЯЦИЯ (дубль прокси): {m}"


def test_full_rotation_is_permutation_and_isolated():
    accs = [{"account_id": i, "proxy_id": i} for i in range(1, 6)]  # 1..5 на прокси 1..5
    pool = [1, 2, 3, 4, 5]
    r = plan_rotation(accs, pool)
    m = _final(accs, r)
    _assert_isolated(m)
    assert r["skipped_no_proxy"] == 0
    # каждый реально сменил прокси (циклический сдвиг без неподвижных точек)
    assert r["rotated"] == 5
    assert all(ch["new"] != ch["old"] for ch in r["plan"])


def test_small_pool_skip_does_not_collide_with_retained():
    # КРАЕВОЙ: пул [1,2], аккаунт C держит прокси 1 и будет скипнут — 1 нельзя
    # отдать A/B, иначе коллизия. Планировщик обязан это учесть.
    accs = [
        {"account_id": 1, "proxy_id": 5},
        {"account_id": 2, "proxy_id": 6},
        {"account_id": 3, "proxy_id": 1},
    ]
    pool = [1, 2]
    r = plan_rotation(accs, pool)
    m = _final(accs, r)
    _assert_isolated(m)  # главное: никакого дубля прокси 1


def test_more_accounts_than_proxies_rotates_subset_isolated():
    accs = [{"account_id": i, "proxy_id": 100 + i} for i in range(1, 6)]  # прокси вне пула
    pool = [1, 2]
    r = plan_rotation(accs, pool)
    m = _final(accs, r)
    _assert_isolated(m)
    assert r["skipped_no_proxy"] == 3  # 5 аккаунтов, 2 прокси → 3 без ротации


def test_null_current_proxy_gets_assigned_and_isolated():
    accs = [
        {"account_id": 1, "proxy_id": None},
        {"account_id": 2, "proxy_id": None},
        {"account_id": 3, "proxy_id": 7},
    ]
    pool = [7, 8, 9]
    r = plan_rotation(accs, pool)
    m = _final(accs, r)
    _assert_isolated(m)
    # аккаунты без прокси получили назначение
    assert m[1] is not None and m[2] is not None


def test_empty_pool_and_single_cases():
    accs = [{"account_id": 1, "proxy_id": 3}, {"account_id": 2, "proxy_id": 4}]
    r = plan_rotation(accs, [])
    assert r["plan"] == [] and r["skipped_no_proxy"] == 2
    r1 = plan_rotation([{"account_id": 9, "proxy_id": None}], [5])
    m = _final([{"account_id": 9, "proxy_id": None}], r1)
    assert m[9] == 5


def test_isolation_holds_across_many_shapes():
    # перебор форм: n аккаунтов, k прокси. Вход ИЗОЛИРОВАН (текущие прокси уникальны:
    # нечётные держат прокси i из пула, чётные — None) — проверяем, что планировщик
    # НЕ ВНОСИТ коллизий ни при каком соотношении n/k.
    for n in range(0, 7):
        for k in range(0, 7):
            accs = [{"account_id": i, "proxy_id": (i if (i % 2 and i <= k) else None)}
                    for i in range(1, n + 1)]
            pool = list(range(1, k + 1))
            r = plan_rotation(accs, pool)
            _assert_isolated(_final(accs, r))
            # планировщик не отдаёт один прокси двум аккаунтам среди назначенных
            news = [ch["new"] for ch in r["plan"]]
            assert len(news) == len(set(news)), f"дубль в плане: n={n} k={k} {r}"


def test_endpoint_transaction_guard_route_and_ui_wired():
    api = _read("services/mini_app_api.py")
    seg = api[api.index("async def rotate_proxies"):api.index("async def reporter_submit")]
    # безопасность: транзакция + FOR UPDATE + гвард in_operation при апдейте
    assert "conn.transaction()" in seg and "FOR UPDATE" in seg
    assert "COALESCE(in_operation,FALSE)=FALSE" in seg
    assert "proxy_rotation.plan_rotation" in seg
    # резолвер не трогаем — правим только proxy_id
    assert "UPDATE tg_accounts SET proxy_id=" in seg
    assert 'add_post("/api/miniapp/proxy/rotate", rotate_proxies)' in api
    ui = _read("mini_app/index.html")
    assert "rotateProxies" in ui and "/api/miniapp/proxy/rotate" in ui
