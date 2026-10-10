"""Точечный повтор не должен молча терять цели у операций «каждый аккаунт × цель».

Разрыв. `collect_failed_targets` исключает цель, у которой есть хотя бы одна
успешная запись в `operation_log`. Для публикации это правильно и прямо
объяснено: один канал мог упасть на одном аккаунте и пройти на другом, и
повторять его — значит опубликовать второй раз.

Но `bulk_join` и `bulk_leave` устроены иначе: там цикл accounts × targets, и
цель отрабатывает КАЖДЫЙ аккаунт операции. В журнал по одному каналу идёт
строка на каждый аккаунт. С тем же правилом получалось так: из пяти аккаунтов в
канал вступил один, четверо упали — канал считался закрытым, в повтор не
попадал, и четыре аккаунта так и оставались снаружи. Причём операция
отчитывалась пользователю, что повторять нечего.

Дубль в обратную сторону безобиден: аккаунт, который уже вступил или вышел,
получает no-op, а упавший доделывает работу. Поэтому для таких типов успех
одного аккаунта цель не закрывает.
"""
from __future__ import annotations

import inspect

import pytest

from services import operation_bus
from services.operation_bus import OP_REGISTRY, collect_failed_targets, retry_targets_meta


class _FakePool:
    """Пул, отдающий заранее заданные строки operation_log."""

    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *_args, **_kwargs):
        return self._rows


def _row(target, status):
    return {"target": target, "status": status}


# ── Разметка типов ───────────────────────────────────────────────────────────

def test_per_account_types_are_marked():
    """Оба типа с циклом accounts × targets обязаны нести признак."""
    for op_type in ("bulk_join", "bulk_leave"):
        meta = retry_targets_meta(op_type)
        assert meta, f"{op_type}: точечный повтор не объявлен"
        assert meta.get("per_account") is True, (
            f"{op_type}: цель отрабатывает каждый аккаунт — успех одного "
            f"не закрывает её для остальных"
        )


def test_one_target_one_action_types_are_not_marked():
    """Публикация и SEO — одно действие на цель: там правило обязано остаться."""
    meta = retry_targets_meta("bulk_seo_apply")
    assert meta and not meta.get("per_account"), (
        "bulk_seo_apply делает одно действие на канал — повтор успешной цели "
        "был бы лишней работой"
    )


# ── Поведение отбора ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_per_account_keeps_target_that_one_account_completed():
    """Главный регресс: один аккаунт вступил, четверо упали."""
    rows = [
        _row("https://t.me/+abc", "ok"),
        _row("https://t.me/+abc", "error"),
        _row("https://t.me/+abc", "error"),
        _row("https://t.me/+abc", "error"),
        _row("https://t.me/+abc", "error"),
    ]
    got = await collect_failed_targets(_FakePool(rows), 1, "bulk_join")
    assert got == ["https://t.me/+abc"], (
        "цель, где упали четыре аккаунта из пяти, обязана попасть в повтор"
    )


@pytest.mark.asyncio
async def test_per_account_target_fully_successful_is_not_retried():
    """Если не упал никто, повторять действительно нечего."""
    rows = [_row("https://t.me/+abc", "ok"), _row("https://t.me/+abc", "ok")]
    got = await collect_failed_targets(_FakePool(rows), 1, "bulk_join")
    assert got == []


@pytest.mark.asyncio
async def test_per_account_dedupes_repeated_failures():
    """Цель попадает в повтор ОДИН раз, сколько бы аккаунтов на ней ни упало."""
    rows = [_row("-100123", "error")] * 4
    got = await collect_failed_targets(_FakePool(rows), 1, "bulk_leave")
    assert got == ["-100123"]


@pytest.mark.asyncio
async def test_per_account_preserves_input_order():
    rows = [
        _row("-1", "error"),
        _row("-2", "error"),
        _row("-3", "error"),
    ]
    got = await collect_failed_targets(_FakePool(rows), 1, "bulk_leave")
    assert got == ["-1", "-2", "-3"]


@pytest.mark.asyncio
async def test_publish_style_rule_is_unchanged():
    """Регресс в другую сторону: у одно-действие-на-цель правило обязано жить.

    Канал, упавший на одном аккаунте и прошедший на другом, повторять нельзя —
    это вторая публикация.
    """
    rows = [_row("ch#55", "error"), _row("ch#55", "ok")]
    got = await collect_failed_targets(_FakePool(rows), 1, "bulk_seo_apply")
    assert got == [], "успешная цель не должна попадать в повтор"


@pytest.mark.asyncio
async def test_publish_style_still_returns_genuinely_failed():
    rows = [_row("ch#55", "error"), _row("ch#56", "ok")]
    got = await collect_failed_targets(_FakePool(rows), 1, "bulk_seo_apply")
    assert got == [55], got


@pytest.mark.asyncio
async def test_unparseable_targets_are_dropped_for_both_modes():
    """Мусор в списке целей хуже короткого списка — исполнитель ударит не туда."""
    rows = [_row("не число", "error"), _row("ch#7", "error")]
    got = await collect_failed_targets(_FakePool(rows), 1, "bulk_seo_apply")
    assert got == [7], got


@pytest.mark.asyncio
async def test_unsupported_type_returns_nothing():
    rows = [_row("что-то", "error")]
    assert await collect_failed_targets(_FakePool(rows), 1, "mass_publish") == []


# ── Смысловая связка ─────────────────────────────────────────────────────────

def test_rule_is_explained_where_it_is_applied():
    """Правило неочевидное; без объяснения его снова «упростят» до одного случая."""
    src = inspect.getsource(operation_bus.collect_failed_targets)
    assert "per_account" in src
    assert "accounts × targets" in src or "каждый аккаунт" in src


def test_registry_documents_the_flag():
    src = inspect.getsource(operation_bus)
    header = src[:src.index("OP_REGISTRY: dict[str, dict] = {")]
    assert '"per_account"' in header, (
        "признак обязан быть описан в шапке реестра, рядом с retry_targets"
    )


def test_flag_only_on_types_that_declare_retry_targets():
    """Признак без объявленного точечного повтора ничего не значит — это опечатка."""
    for op_type, meta in OP_REGISTRY.items():
        rt = meta.get("retry_targets")
        if isinstance(rt, dict) and rt.get("per_account"):
            assert rt.get("param"), f"{op_type}: per_account без param"
