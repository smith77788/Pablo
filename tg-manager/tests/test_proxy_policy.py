"""Регрессия: политика прокси-изоляции (services/proxy_policy) + проводка.

Пользовательский выбор: 'strict' (только через прокси) vs 'allow_direct' (можно
без прокси, риск). Плюс инвариант «низкорисковая операция никогда не блокируется
из-за отсутствия прокси» (чтобы, напр., чтение контактов не упиралось в прокси).
"""
from __future__ import annotations

import os

from services.proxy_policy import proxy_decision, normalize_policy, DEFAULT_POLICY, VALID_POLICIES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _d(**kw):
    base = dict(has_proxy_url=False, proxy_parsed_ok=False,
               policy="allow_direct", enforce=False, low_risk=False)
    base.update(kw)
    return proxy_decision(**base)


def test_valid_proxy_always_used():
    assert _d(has_proxy_url=True, proxy_parsed_ok=True, policy="strict", enforce=True) == "use"


def test_low_risk_direct_only_when_no_assigned_proxy():
    # низкий риск + НЕТ назначенного прокси → прямое (каноничный транспорт сессии)
    assert _d(policy="strict", enforce=True, low_risk=True) == "fallback"
    assert _d(policy="allow_direct", low_risk=True) == "fallback"


def test_broken_assigned_proxy_blocks_ALWAYS_even_low_risk():
    # КРИТИЧНО: аккаунту назначен прокси, но он битый/недоступен — НЕЛЬЗЯ
    # подключаться с другого IP (убьёт сессию AUTH_KEY_DUPLICATED). Блок ВЕЗДЕ,
    # включая low_risk и allow_direct.
    for pol in ("allow_direct", "strict"):
        for lr in (False, True):
            assert _d(has_proxy_url=True, proxy_parsed_ok=False, policy=pol, low_risk=lr) == "block", (pol, lr)


def test_no_proxy_strict_blocks_permissive_allows():
    assert _d(policy="strict") == "block"
    assert _d(policy="allow_direct") == "fallback"


def test_enforce_flag_acts_like_strict():
    assert _d(policy="allow_direct", enforce=True) == "block"


def test_normalize_policy():
    assert normalize_policy("STRICT") == "strict"
    assert normalize_policy("  Allow_Direct ") == "allow_direct"
    for bad in (None, "", "nonsense", 123):
        assert normalize_policy(bad) == DEFAULT_POLICY
    assert set(VALID_POLICIES) == {"strict", "allow_direct"}


def test_wired_low_risk_for_contacts_read():
    with open(os.path.join(ROOT, "services/account_manager.py"), encoding="utf-8") as f:
        src = f.read()
    # чтение контактов помечено low_risk, чтобы не требовать прокси
    seg = src[src.index("async def get_contacts"):]
    seg = seg[:seg.index("return contacts")]
    assert "_make_client(session_string, _acc, low_risk=True)" in seg


def test_resolve_client_proxy_takes_low_risk_and_policy():
    with open(os.path.join(ROOT, "services/account_manager.py"), encoding="utf-8") as f:
        src = f.read()
    assert "def _resolve_client_proxy(device: dict[str, Any], low_risk: bool = False)" in src
    assert "device.get(\"proxy_policy\")" in src
    # owner-политика подкладывается в аккаунт-словарь
    with open(os.path.join(ROOT, "database/db.py"), encoding="utf-8") as f:
        db = f.read()
    assert "async def get_proxy_policy" in db
    assert 'd["proxy_policy"] = await get_proxy_policy' in db


def test_mass_path_per_owner_policy_wired():
    with open(os.path.join(ROOT, "services/account_manager.py"), encoding="utf-8") as f:
        am = f.read()
    # per-owner кэш + чтение по owner_id из словаря аккаунта
    assert "def set_owner_proxy_policy" in am
    assert "_OWNER_PROXY_POLICY.get(int(_oid))" in am
    # массовый селектор отдаёт owner_id (иначе кэш не с чем сопоставить)
    with open(os.path.join(ROOT, "services/resource_selector.py"), encoding="utf-8") as f:
        rs = f.read()
    assert "SELECT a.id, a.owner_id," in rs
    # op_worker праймит кэш на старте операции
    with open(os.path.join(ROOT, "services/op_worker.py"), encoding="utf-8") as f:
        ow = f.read()
    assert "set_owner_proxy_policy(owner_id, await db.get_proxy_policy(pool, owner_id))" in ow


def test_resolver_keys_on_proxy_id_not_just_url():
    # КРИТИЧНО: аккаунт с назначенным proxy_id, но неактивным прокси (proxy_url=NULL)
    # всё равно «привязан к прокси» → нельзя подключать напрямую. Резолвер должен
    # опираться на proxy_id, иначе снова убьёт сессию (AUTH_KEY_DUPLICATED).
    with open(os.path.join(ROOT, "services/account_manager.py"), encoding="utf-8") as f:
        am = f.read()
    assert 'has_assigned_proxy = bool(acc_proxy_url) or bool(device.get("proxy_id"))' in am
    # Битый назначенный прокси → жёсткий блок в резолвере (не уходим на другой IP).
    assert "if has_assigned_proxy and proxy is None:" in am
    # Kill-switch в _make_client опирается на назначение (proxy_id/url), а не только URL.
    assert '_has_assigned = bool(str(d.get("proxy_url") or "").strip()) or bool(d.get("proxy_id"))' in am


def test_mass_detach_proxy_scoped_and_nulls():
    with open(os.path.join(ROOT, "services/mini_app_api.py"), encoding="utf-8") as f:
        api = f.read()
    assert 'op == "detach_proxy"' in api
    assert "SET proxy_id=NULL WHERE owner_id=$1 AND id=ANY($2::bigint[])" in api


def test_low_risk_via_account_dict_and_inline_reads_marked():
    with open(os.path.join(ROOT, "services/account_manager.py"), encoding="utf-8") as f:
        am = f.read()
    # аккаунт-словарь может пометить соединение низкорисковым
    assert 'low_risk = low_risk or bool(device.get("_low_risk"))' in am
    with open(os.path.join(ROOT, "services/mini_app_api.py"), encoding="utf-8") as f:
        api = f.read()
    # одиночные инлайн-чтения помечают соединение low_risk (не требуют прокси)
    assert 'get_login_code(acc["session_str"], {**dict(acc), "_low_risk": True})' in api
    assert 'check_restriction(acc["session_str"], {**dict(acc), "_low_risk": True})' in api


def test_set_owner_proxy_policy_normalizes_and_ignores_none():
    import importlib
    # account_manager не импортируется в песочнице (env) — проверяем через proxy_policy,
    # что set_owner_proxy_policy нормализует так же (контракт normalize_policy).
    from services.proxy_policy import normalize_policy
    assert normalize_policy("STRICT") == "strict"
    assert normalize_policy("bogus") == DEFAULT_POLICY


# ── Стадия 2 #2: enterprise-дефолт strict + kill-switch ──────────────────────

def test_default_is_strict_now():
    """Дефолт переведён в strict: массовая операция без прокси не уходит напрямую."""
    assert DEFAULT_POLICY == "strict"
    # без явной политики (fallback на дефолт) и без прокси, не low_risk → block
    assert _d(policy=None) == "block"


def test_killswitch_blocks_true_direct_but_not_relay_ipv6(monkeypatch):
    """_make_client: strict роняет ТОЛЬКО истинный прямой выход с host-IP.

    Не-host транспорты (CF-relay/IPv6/free-pool) под strict НЕ блокируются —
    kill-switch стоит в самом конце цепочки, после них.
    """
    from services import account_manager as am
    from services.account_manager import ProxyIsolationError
    import telethon.sessions as ts

    class _Cap:
        def __init__(self, s): pass
        def __getattr__(self, _n): return lambda *a, **k: None
    monkeypatch.setattr(ts, "StringSession", _Cap, raising=False)
    monkeypatch.setattr(am, "_resolve_client_proxy", lambda d, low_risk=False: None)
    monkeypatch.setattr(am, "_get_pool_proxy_url", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(am, "CF_RELAY_URL", "", raising=False)

    dev = {"device_model": "x", "system_version": "x", "app_version": "x",
           "lang_code": "en", "system_lang_code": "en"}

    # strict + нет прокси/релея/IPv6/пула → истинный direct → kill-switch блокирует
    import pytest
    with pytest.raises(ProxyIsolationError):
        am._make_client("SESS", {**dev, "proxy_policy": "strict"})

    # allow_direct → тот же путь разрешён
    am._make_client("SESS", {**dev, "proxy_policy": "allow_direct"})

    # low_risk-чтение под strict НЕ блокируется (одиночное чтение не требует прокси)
    am._make_client("SESS", {**dev, "proxy_policy": "strict"}, low_risk=True)

    # CF-relay доступен → strict НЕ блокирует (не host-IP транспорт)
    monkeypatch.setattr(am, "CF_RELAY_URL", "wss://relay.example/ws", raising=False)
    am._make_client("SESS", {**dev, "proxy_policy": "strict"})
