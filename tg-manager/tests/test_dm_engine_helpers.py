"""Регрессия для углублённых настроек DM-рассылок.

- parse_import_list: тип таргета import_list (движок его умел, UI не давал).
- pick_account_under_cap: дневной лимит отправок на аккаунт (защита от бана) —
  аккаунт, достигший лимита, выбывает из ротации; когда все выбыли — None.
"""
from __future__ import annotations

from services.dm_engine import parse_import_list, pick_account_under_cap


# ── parse_import_list ────────────────────────────────────────────────────────

def test_import_list_numeric_and_username():
    out = parse_import_list("123456789\n@alice\nt.me/bob")
    assert {"user_id": 123456789, "username": None} in out
    assert {"user_id": 0, "username": "alice"} in out
    assert {"user_id": 0, "username": "bob"} in out


def test_import_list_dedup_by_id_and_username():
    out = parse_import_list(["111", "111", "@x_user", "@X_User"])
    ids = [o["user_id"] for o in out]
    assert ids.count(111) == 1
    # дедуп username регистронезависимо
    assert sum(1 for o in out if (o["username"] or "").lower() == "x_user") == 1


def test_import_list_rejects_garbage():
    assert parse_import_list(["", "!!", "ab", "0", "-5"]) == []


def test_import_list_accepts_dicts():
    out = parse_import_list([{"user_id": 5, "username": "@u"}, {"username": "onlyname"}])
    assert out[0] == {"user_id": 5, "username": "u"}
    assert out[1] == {"user_id": 0, "username": "onlyname"}


def test_import_list_limit():
    out = parse_import_list([str(i) for i in range(1, 100)], limit=10)
    assert len(out) == 10


# ── pick_account_under_cap ───────────────────────────────────────────────────

def _accs(*ids):
    return [{"id": i} for i in ids]


def test_no_cap_round_robins():
    cycle = _accs(1, 2, 3)
    acc, idx = pick_account_under_cap(cycle, 0, {}, None)
    assert acc["id"] == 1 and idx == 1
    acc, idx = pick_account_under_cap(cycle, idx, {}, None)
    assert acc["id"] == 2 and idx == 2


def test_cap_skips_exhausted_account():
    cycle = _accs(1, 2)
    sent = {1: 5}  # аккаунт 1 достиг лимита 5
    acc, idx = pick_account_under_cap(cycle, 0, sent, 5)
    assert acc["id"] == 2  # пропустил 1, выбрал 2


def test_cap_all_exhausted_returns_none():
    cycle = _accs(1, 2)
    sent = {1: 5, 2: 5}
    acc, idx = pick_account_under_cap(cycle, 0, sent, 5)
    assert acc is None


def test_empty_cycle_returns_none():
    acc, idx = pick_account_under_cap([], 0, {}, 5)
    assert acc is None


def test_under_cap_still_selected():
    cycle = _accs(7)
    acc, _ = pick_account_under_cap(cycle, 0, {7: 4}, 5)
    assert acc["id"] == 7  # 4 < 5 → ещё можно
