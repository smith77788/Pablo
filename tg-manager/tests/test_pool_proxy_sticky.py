"""Залипающий pool-прокси на аккаунт — корень AUTH_KEY_DUPLICATED при свежих сессиях.

Жалоба: «я переавторизовал сессии у нас перед началом» — и всё равно все аккаунты
падают на AUTH_KEY_DUPLICATED. Значит две сессии с двух IP создаём МЫ.

Причина найдена: аккаунт без назначенного прокси уходил в бесплатный пул через
_get_pool_proxy_url() — а тот был ROUND-ROBIN: отдавал РАЗНЫЙ прокси на каждый
коннект. Значит одна и та же сессия выходила с РАЗНЫХ IP: переавторизация — IP-A,
инвайт-батч — IP-B, следующий — IP-C. Telegram видит один ключ с нескольких IP
подряд → AUTH_KEY_DUPLICATED, для всего флота, даже со свежими сессиями.

Фикс: выбор из пула ЗАЛИПАЕТ по account_id — один аккаунт всегда получает один
прокси (стабильный exit IP), пока тот жив в пуле.
"""
from __future__ import annotations

from services import account_manager as am


def _reset(pool):
    am.set_pool_proxy_cache(pool)
    am._acc_pool_proxy.clear()


def test_same_account_gets_same_proxy_across_connects():
    _reset(["socks5://p1", "socks5://p2", "socks5://p3"])
    first = am._get_pool_proxy_url(account_id=42)
    # десять «коннектов» подряд — прокси не должен меняться
    for _ in range(10):
        assert am._get_pool_proxy_url(account_id=42) == first, (
            "залипший прокси обязан быть стабильным — иначе AUTH_KEY_DUPLICATED"
        )


def test_different_accounts_can_get_different_proxies():
    _reset(["socks5://p1", "socks5://p2", "socks5://p3", "socks5://p4"])
    a = am._get_pool_proxy_url(account_id=1)
    b = am._get_pool_proxy_url(account_id=2)
    # распределение по разным аккаунтам (не обязано всегда отличаться, но
    # детерминировано по id — 1 и 2 попадают на разные индексы при пуле>1)
    assert a == am._pool_proxy_cache[1 % len(am._pool_proxy_cache)]
    assert b == am._pool_proxy_cache[2 % len(am._pool_proxy_cache)]


def test_dead_proxy_reassigned_on_cache_refresh():
    _reset(["socks5://p1", "socks5://p2"])
    # аккаунт 3 → p2 (3 % 2 = 1)
    assert am._get_pool_proxy_url(account_id=3) == "socks5://p2"
    # пул обновился, p2 пропал → залипание должно самоочиститься и переназначить
    am.set_pool_proxy_cache(["socks5://p1", "socks5://p9"])
    new = am._get_pool_proxy_url(account_id=3)
    assert new in ("socks5://p1", "socks5://p9"), "мёртвый прокси должен переназначиться"
    assert new != "socks5://p2"


def test_no_account_id_keeps_round_robin():
    _reset(["socks5://p1", "socks5://p2"])
    # обратная совместимость: без account_id — прежний round-robin (разные подряд)
    seq = [am._get_pool_proxy_url() for _ in range(4)]
    assert seq == ["socks5://p1", "socks5://p2", "socks5://p1", "socks5://p2"]


def test_make_client_passes_account_id_to_pool():
    import inspect
    src = inspect.getsource(am._make_client)
    assert "_get_pool_proxy_url(_acc_id)" in src, (
        "фолбэк в пул обязан быть залипающим по account_id, иначе IP скачет"
    )


def test_empty_pool_returns_empty():
    _reset([])
    assert am._get_pool_proxy_url(account_id=1) == ""
    assert am._get_pool_proxy_url() == ""
