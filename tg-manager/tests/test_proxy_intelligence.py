"""Proxy Intelligence — регрессия: статистика прокси должна учитывать УСПЕХ.

Раньше в реальных операциях писались только провалы прокси (_record_proxy_fail),
а успех коннекта не фиксировался нигде. Из-за этого success_rate любого активно
используемого прокси стремился к 0, и proxy_selector штрафовал рабочие прокси.
А админ-панель (get_proxy_stats) читала отдельное хранилище _proxy_stats, которое
в реальных операциях вообще не заполнялось → всегда нули.

Эти тесты фиксируют, что:
- infra_memory видит и успех, и провал прокси (success_rate реальный);
- get_proxy_summary агрегирует по всем типам действий;
- get_proxy_stats отдаёт реальные операционные данные, а не нули;
- account_manager реально пишет успех прокси через _connect_and_track.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_proxy_memory():
    from services import infra_memory

    infra_memory._proxy_memory.clear()
    yield
    infra_memory._proxy_memory.clear()


def test_success_rate_reflects_successes_not_only_failures():
    from services import infra_memory

    url = "socks5://1.2.3.4:1080"
    for _ in range(9):
        infra_memory.record_proxy_op(url, "join", success=True, latency_ms=120)
    infra_memory.record_proxy_op(url, "join", success=False)
    # 9 успехов из 10 → высокий score, а не 0 как раньше
    assert infra_memory.get_proxy_score(url, "join") == pytest.approx(0.9)


def test_summary_aggregates_across_action_types():
    from services import infra_memory

    url = "socks5://5.6.7.8:1080"
    infra_memory.record_proxy_op(url, "join", success=True, latency_ms=100)
    infra_memory.record_proxy_op(url, "post", success=True, latency_ms=300)
    infra_memory.record_proxy_op(url, "post", success=False)
    s = infra_memory.get_proxy_summary(url)
    assert s is not None
    assert s["success"] == 2
    assert s["fail"] == 1
    assert s["total"] == 3
    assert s["success_rate"] == pytest.approx(66.7, abs=0.1)
    # средняя задержка взвешена по успехам (100 и 300) → 200
    assert s["avg_latency_ms"] == 200


def test_summary_none_when_no_data():
    from services import infra_memory

    assert infra_memory.get_proxy_summary("socks5://9.9.9.9:1080") is None
    assert infra_memory.get_proxy_summary("") is None


def test_get_proxy_stats_uses_real_operational_data():
    from services import infra_memory
    from services import account_manager

    url = "socks5://10.0.0.1:1080"
    # реальные операции: 8 успехов, 2 провала
    for _ in range(8):
        infra_memory.record_proxy_op(url, "join", success=True, latency_ms=150)
    for _ in range(2):
        infra_memory.record_proxy_op(url, "join", success=False)
    stats = account_manager.get_proxy_stats(url)
    # раньше здесь были бы нули (пустой _proxy_stats) — теперь реальные данные
    assert stats["total_checks"] == 10
    assert stats["success_rate"] == pytest.approx(80.0)
    assert stats["avg_latency_ms"] == 150


def test_record_proxy_ok_writes_success():
    from services import infra_memory
    from services import account_manager

    url = "socks5://11.0.0.1:1080"
    account_manager._record_proxy_ok({"proxy_url": url}, "leave", latency_ms=90)
    s = infra_memory.get_proxy_summary(url)
    assert s is not None and s["success"] == 1 and s["fail"] == 0


def test_record_proxy_ok_noop_without_proxy():
    from services import infra_memory
    from services import account_manager

    account_manager._record_proxy_ok(None, "leave")
    account_manager._record_proxy_ok({}, "leave")
    account_manager._record_proxy_ok({"proxy_url": ""}, "leave")
    # ничего не записалось
    assert not infra_memory._proxy_memory


def test_operations_wired_to_track_proxy_success():
    """Операции должны писать успех прокси через _connect_and_track,
    иначе success_rate снова будет только по провалам."""
    import inspect
    from services import account_manager

    for fn_name, action in [
        ("join_channel", "join"),
        ("leave_channel", "leave"),
        ("post_to_channel", "post"),
        ("send_message_via_account", "send_message"),
        ("send_bot_start", "bot_start"),
        ("get_dialogs", "dialogs"),
    ]:
        src = inspect.getsource(getattr(account_manager, fn_name))
        # Успех прокси трекается либо напрямую _connect_and_track, либо через
        # connect_client (fallback-обёртка, внутри вызывает _connect_and_track).
        tracked = (
            f'_connect_and_track(client, _acc, "{action}")' in src
            or f'connect_client(session_string, _acc, "{action}")' in src
        )
        assert tracked, (
            f"{fn_name} не трекает успех прокси (ни _connect_and_track, ни connect_client)"
        )
