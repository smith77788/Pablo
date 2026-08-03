"""Залипающий pool-прокси по ТЕЛЕФОНУ — корень AUTH_KEY_DUPLICATED.

Жалоба: «я переавторизовал сессии у нас перед началом» — и всё равно все аккаунты
падают на AUTH_KEY_DUPLICATED. Значит две сессии с двух IP создаём МЫ.

Две причины, обе про НЕСТАБИЛЬНЫЙ exit IP свежей сессии:
  1. _get_pool_proxy_url() был round-robin → аккаунт получал РАЗНЫЙ прокси на каждый
     коннект (op↔op рассинхрон IP).
  2. Логин (start_login) не знал account_id и брал pool round-robin (IP-A), а
     операции — по account_id (IP-B): сессия авторизуется с одного IP, первая
     операция идёт с другого → ключ убивается на ПЕРВОЙ же операции.

Фикс: залипание по ТЕЛЕФОНУ — он есть и на логине (device['phone']), и у аккаунта
(a.phone в _ACC_COLS). Один и тот же ключ → один и тот же прокси → один exit IP на
логине и в операциях.
"""
from __future__ import annotations

import inspect

from services import account_manager as am


def _reset(pool):
    am.set_pool_proxy_cache(pool)
    am._acc_pool_proxy.clear()


def test_same_key_gets_same_proxy_across_connects():
    _reset(["socks5://p1", "socks5://p2", "socks5://p3"])
    phone = "+79990001122"
    first = am._get_pool_proxy_url(key=phone)
    for _ in range(10):
        assert am._get_pool_proxy_url(key=phone) == first, (
            "залипший прокси обязан быть стабильным — иначе AUTH_KEY_DUPLICATED"
        )


def test_login_and_ops_share_ip_via_phone_key():
    """Ключевое: логин и операции берут ОДИН прокси, т.к. ключ — телефон."""
    _reset(["socks5://p1", "socks5://p2", "socks5://p3", "socks5://p4"])
    phone = "+79995554433"
    login_proxy = am._get_pool_proxy_url(key=phone)          # как на логине
    op_proxy = am._get_pool_proxy_url(key=phone)             # как в операции
    assert login_proxy == op_proxy, (
        "логин и операция обязаны выходить с одного IP (иначе ключ умрёт на 1-й опе)"
    )


def test_dead_proxy_reassigned_on_cache_refresh():
    _reset(["socks5://p1", "socks5://p2"])
    phone = "+70001112233"
    old = am._get_pool_proxy_url(key=phone)
    assert old in ("socks5://p1", "socks5://p2")
    # пул обновился, старый прокси пропал → залипание самоочищается и переназначает
    survivor = "socks5://p1" if old == "socks5://p2" else "socks5://p2"
    am.set_pool_proxy_cache([survivor, "socks5://p9"])
    new = am._get_pool_proxy_url(key=phone)
    assert new in (survivor, "socks5://p9"), "мёртвый прокси должен переназначиться"
    assert new != old or old == survivor


def test_no_key_keeps_round_robin():
    _reset(["socks5://p1", "socks5://p2"])
    seq = [am._get_pool_proxy_url() for _ in range(4)]
    assert seq == ["socks5://p1", "socks5://p2", "socks5://p1", "socks5://p2"]


def test_make_client_keys_pool_by_phone():
    src = inspect.getsource(am._make_client)
    assert 'device.get("phone")' in src and "_get_pool_proxy_url(_pool_key)" in src, (
        "фолбэк в пул обязан залипать по телефону (общему для логина и операций)"
    )


def test_start_login_puts_phone_in_device():
    src = inspect.getsource(am.start_login)
    assert 'device["phone"] = phone' in src, (
        "логин обязан класть телефон в device — иначе pool на логине не залипнет"
    )


def test_acc_cols_selects_phone():
    from services import op_worker
    assert "a.phone" in op_worker._ACC_COLS, (
        "операции должны получать phone, чтобы залипать по тому же ключу, что логин"
    )


def test_empty_pool_returns_empty():
    _reset([])
    assert am._get_pool_proxy_url(key="+79990001122") == ""
    assert am._get_pool_proxy_url() == ""
