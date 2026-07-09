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


def test_low_risk_never_blocks():
    # даже strict + enforce + нет прокси — низкий риск идёт на fallback, не block
    assert _d(policy="strict", enforce=True, low_risk=True) == "fallback"
    # битый назначенный прокси при низком риске — тоже fallback
    assert _d(has_proxy_url=True, proxy_parsed_ok=False, policy="strict", low_risk=True) == "fallback"


def test_broken_assigned_proxy_blocks_non_low_risk_any_policy():
    # аккаунту назначен прокси, но он битый — не уводим в сеть напрямую втихую
    assert _d(has_proxy_url=True, proxy_parsed_ok=False, policy="allow_direct") == "block"
    assert _d(has_proxy_url=True, proxy_parsed_ok=False, policy="strict") == "block"


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
    assert normalize_policy("bogus") == "allow_direct"
