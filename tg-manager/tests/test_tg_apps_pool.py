"""Регресс: пул Telegram-приложений (api_id) и стабильное назначение.

Первопричина: весь флот подключался под ОДНОЙ парой TG_API_ID/TG_API_HASH —
для Telegram это прямой корреляционный признак связности когорты, который не
маскируется ни прокси, ни устройствами, ни гео.
"""
from __future__ import annotations

import os
from collections import Counter

os.environ.setdefault("MANAGER_BOT_TOKEN", "x")
os.environ.setdefault("TG_API_ID", "111")
os.environ.setdefault("TG_API_HASH", "defhash")
os.environ.setdefault("DATABASE_URL", "postgresql://x/x")

import pytest  # noqa: E402

from services import tg_apps  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POOL3 = [(222, "h2"), (333, "h3"), (444, "h4")]


@pytest.fixture(autouse=True)
def _clean_env():
    old = os.environ.pop("TG_API_POOL", None)
    yield
    if old is not None:
        os.environ["TG_API_POOL"] = old
    else:
        os.environ.pop("TG_API_POOL", None)


def test_parse_pool_formats_and_garbage():
    assert tg_apps.parse_pool("222:h2,333:h3") == [(222, "h2"), (333, "h3")]
    assert tg_apps.parse_pool("222:h2; 333:h3 ") == [(222, "h2"), (333, "h3")]
    # мусор пропускается, не роняет
    assert tg_apps.parse_pool("nope,:,123:,:h,444:h4") == [(444, "h4")]
    # дубли по api_id схлопываются
    assert tg_apps.parse_pool("222:h2,222:other") == [(222, "h2")]
    assert tg_apps.parse_pool(None) == []
    assert tg_apps.parse_pool("") == []


def test_no_pool_means_previous_behaviour():
    """Пул не задан → ровно одна пара из config, поведение прежнее."""
    from config import TG_API_ID, TG_API_HASH

    expected = (int(TG_API_ID), str(TG_API_HASH))
    apps = tg_apps.pool()
    assert apps == [expected], f"без пула должна быть одна пара из config: {apps}"
    # любой аккаунт получает ту же (единственную) пару — регрессии нет
    assert tg_apps.for_account(5) == expected
    assert tg_apps.for_account(999999) == expected


def test_distributes_across_pool():
    dist = Counter(tg_apps.for_account(a, apps=POOL3)[0] for a in range(1, 301))
    assert len(dist) == 3, f"использованы не все приложения: {dist}"
    # ни одно приложение не забирает подавляющее большинство
    assert max(dist.values()) < 200, f"перекос распределения: {dist}"


def test_assignment_is_stable_for_same_account():
    """Смена приложения у живого аккаунта сама по себе палевна — назначение
    обязано быть стабильным."""
    first = tg_apps.for_account(777, apps=POOL3)
    for _ in range(10):
        assert tg_apps.for_account(777, apps=POOL3) == first


def test_stored_api_id_wins():
    """Если за аккаунтом уже закреплено приложение — оно и используется."""
    assert tg_apps.for_account(1, stored_api_id=444, apps=POOL3) == (444, "h4")
    # закреплённое, но отсутствующее в пуле → откат на детерминированный выбор
    got = tg_apps.for_account(1, stored_api_id=999, apps=POOL3)
    assert got in POOL3


def test_adding_app_moves_only_a_fraction():
    """Rendezvous-хеширование: добавление приложения не перетасовывает весь флот."""
    accs = list(range(1, 401))
    before = {a: tg_apps.for_account(a, apps=POOL3)[0] for a in accs}
    pool4 = POOL3 + [(555, "h5")]
    after = {a: tg_apps.for_account(a, apps=pool4)[0] for a in accs}
    moved = sum(1 for a in accs if before[a] != after[a])
    assert moved < len(accs) * 0.45, f"переехало слишком много: {moved}/{len(accs)}"


def test_string_key_supported_for_import_time_assignment():
    """При импорте id ещё не существует — ключом служит fingerprint сессии,
    иначе вся пачка сядет на одно приложение."""
    dist = Counter(tg_apps.for_account(f"fp_{i}", apps=POOL3)[0] for i in range(300))
    assert len(dist) == 3, f"строковый ключ не распределяет: {dist}"
    assert tg_apps.for_account("fp_7", apps=POOL3) == tg_apps.for_account("fp_7", apps=POOL3)


def test_empty_pool_returns_none():
    assert tg_apps.for_account(1, apps=[]) is None
    assert tg_apps.assign_api_id is not None


def test_wired_into_client_and_import():
    with open(os.path.join(ROOT, "services/account_manager.py"), encoding="utf-8") as f:
        am = f.read()
    # клиент берёт пару аккаунта, с откатом на config
    assert 'int(d.get("api_id") or TG_API_ID)' in am
    assert 'd.get("api_hash") or TG_API_HASH' in am
    assert "tg_apps.for_account(" in am
    with open(os.path.join(ROOT, "services/session_importer.py"), encoding="utf-8") as f:
        imp = f.read()
    assert "_assign_api_id(_fp)" in imp, "при импорте приложение не закрепляется"
    assert "api_id" in imp


def test_migration_adds_column():
    with open(os.path.join(ROOT, "schema_v191_tg_app_pool.sql"), encoding="utf-8") as f:
        sql = f.read()
    assert "ADD COLUMN IF NOT EXISTS api_id" in sql
