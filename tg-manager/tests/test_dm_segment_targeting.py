"""Сегмент CRM как аудитория DM-кампании.

Разрыв, который это закрывает: единственным CRM-таргетом был «все контакты».
Пользователь строил срез в контактах («Горячие», «Молчуны 30д»), но написать
именно ему не мог — приходилось слать всей базе. Движок сегментов при этом
существовал и использовался списком контактов.

Тесты гоняют реальную ветку dm_engine._get_targets с подставным пулом и
подставным репозиторием сегментов (asyncio.run — pytest-asyncio в окружении
нет, см. test_managed_channels_partial_import).
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from services import dm_engine


class _FakePool:
    """Пул, отдающий пустой dm_campaign_log (никому ещё не слали)."""

    def __init__(self, sent_rows=None):
        self._sent_rows = sent_rows or []

    async def fetch(self, query, *args):
        if "dm_campaign_log" in query:
            return self._sent_rows
        return []

    async def execute(self, *a, **kw):
        return "OK"


def _install_repo(monkeypatch, *, filters, rows):
    """Подменяет services.contacts_hub.repository на заглушку с нужным срезом."""
    mod = types.ModuleType("services.contacts_hub.repository")

    async def get_segment_filters(pool, owner_id, segment_id):
        return filters

    async def resolve_segment(pool, owner_id, f, limit=5000):
        assert f == filters, "резолвер обязан получить фильтры именно этого сегмента"
        return rows

    mod.get_segment_filters = get_segment_filters
    mod.resolve_segment = resolve_segment
    pkg = sys.modules.get("services.contacts_hub")
    if pkg is None:
        pkg = types.ModuleType("services.contacts_hub")
        monkeypatch.setitem(sys.modules, "services.contacts_hub", pkg)
    monkeypatch.setitem(sys.modules, "services.contacts_hub.repository", mod)
    monkeypatch.setattr(pkg, "repository", mod, raising=False)
    return mod


def _campaign(target_id=5):
    return {
        "id": 1,
        "owner_id": 42,
        "target_type": "segment",
        "target_id": target_id,
        "params": {},
    }


def test_segment_targets_resolved_through_segment_engine(monkeypatch):
    _install_repo(
        monkeypatch,
        filters={"crm_stage": "hot"},
        rows=[
            {"telegram_user_id": 100, "username": "alice"},
            {"telegram_user_id": 200, "username": None},
        ],
    )
    targets = asyncio.run(dm_engine._get_targets(_FakePool(), _campaign()))
    assert targets == [
        {"user_id": 100, "username": "alice"},
        {"user_id": 200, "username": None},
    ]


def test_segment_skips_contacts_without_any_address(monkeypatch):
    """Контакт только с телефоном адресовать в ЛС нечем — он не должен попасть
    в аудиторию и не должен считаться ошибкой отправки."""
    _install_repo(
        monkeypatch,
        filters={},
        rows=[
            {"telegram_user_id": 0, "username": None, "phones": ["+79990000000"]},
            {"telegram_user_id": 0, "username": "bob"},
            {"telegram_user_id": 300, "username": None},
        ],
    )
    targets = asyncio.run(dm_engine._get_targets(_FakePool(), _campaign()))
    assert {t["username"] for t in targets} == {"bob", None}
    assert len(targets) == 2


def test_segment_excludes_already_sent(monkeypatch):
    """Возобновление кампании не должно писать тем, кому уже отправили."""
    _install_repo(
        monkeypatch,
        filters={},
        rows=[
            {"telegram_user_id": 100, "username": "alice"},
            {"telegram_user_id": 200, "username": "bob"},
        ],
    )
    pool = _FakePool(sent_rows=[{"tg_user_id": 100}])
    targets = asyncio.run(dm_engine._get_targets(pool, _campaign()))
    assert [t["user_id"] for t in targets] == [200]


def test_segment_deduplicates_repeated_contacts(monkeypatch):
    _install_repo(
        monkeypatch,
        filters={},
        rows=[
            {"telegram_user_id": 100, "username": "alice"},
            {"telegram_user_id": 100, "username": "alice"},
        ],
    )
    targets = asyncio.run(dm_engine._get_targets(_FakePool(), _campaign()))
    assert len(targets) == 1


def test_segment_strips_at_sign_from_username(monkeypatch):
    _install_repo(
        monkeypatch, filters={}, rows=[{"telegram_user_id": 100, "username": "@alice"}]
    )
    targets = asyncio.run(dm_engine._get_targets(_FakePool(), _campaign()))
    assert targets[0]["username"] == "alice"


def test_missing_or_foreign_segment_raises_not_silent(monkeypatch):
    """Попытка сломать: сегмент удалён или принадлежит другому владельцу.

    Тихий пустой список означал бы «кампания успешно отправлена никому» —
    пользователь не поймёт, почему рассылки не было. Ждём явную ошибку,
    которую run_campaign переведёт в status='failed'.
    """
    _install_repo(monkeypatch, filters=None, rows=[])
    with pytest.raises(ValueError):
        asyncio.run(dm_engine._get_targets(_FakePool(), _campaign(target_id=999)))


def test_segment_without_target_id_yields_nothing(monkeypatch):
    """Без выбранного сегмента ветка не активна — общий фолбэк пустого списка."""
    _install_repo(monkeypatch, filters={}, rows=[{"telegram_user_id": 1}])
    assert asyncio.run(dm_engine._get_targets(_FakePool(), _campaign(target_id=None))) == []


def test_segment_has_its_own_pacing():
    """У сегмента должен быть класс темпа CRM, а не безымянный дефолт."""
    assert dm_engine._DELAYS_BY_TARGET_TYPE.get("segment") == (45.0, 110.0)


def test_segment_target_limit_is_bounded():
    """Аудитория грузится в память целиком — потолок обязан существовать."""
    assert 0 < dm_engine._SEGMENT_TARGET_LIMIT <= 50000


def test_api_allows_segment_target_type():
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    assert '"segment"' in src and "get_segment_filters" in src, (
        "эндпоинт создания кампании обязан принимать сегмент и проверять владение им"
    )
