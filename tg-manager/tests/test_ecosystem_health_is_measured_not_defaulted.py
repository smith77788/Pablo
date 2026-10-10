"""Здоровье экосистемы: умолчания не выдаются за измерение.

ЗАЧЕМ. У `EcosystemHealth` умолчания — 1.0 («всё отлично»), и расчёт целиком
обёрнут в один `try`. Поэтому ЛЮБОЙ сбой внутри означал: показатели, которые
считаются после места сбоя, остаются на умолчаниях, ошибка уходит в лог, а
владелец видит «Стабильность 100%, Надёжность 100%, Восстановление 100%,
Успешность операций 100%» у экосистемы, где все аккаунты в бане.

Так и было: запрос успешности операций считал `WHERE owner_id=$2` при двух
аргументах — `$1` в тексте не упоминался, Postgres не мог вывести его тип и
отказывал на подготовке, то есть запрос падал при каждом вызове.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


class _StubPool:
    """Записывает запросы и аргументы; данные отдаёт правдоподобные."""

    def __init__(self, fail_on: str | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self.fail_on = fail_on

    def _note(self, sql, args):
        self.calls.append((sql, args))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("запрос не подготовился")

    async def fetch(self, sql, *args):
        self._note(sql, args)
        return []

    async def fetchrow(self, sql, *args):
        self._note(sql, args)
        return {"done": 7, "total": 10, "eligible": 0, "installed": 0}

    async def fetchval(self, sql, *args):
        self._note(sql, args)
        return 0


def _health(pool):
    from services import ecosystem_brain as eb
    return asyncio.new_event_loop().run_until_complete(
        eb.compute_health(pool, 11, 22))


def test_every_query_gets_exactly_the_arguments_it_numbers():
    """$N в тексте и позиционные аргументы обязаны совпадать по счёту.

    Это и есть та поломка: текст знал только `$2`, а аргументов передавали два.
    """
    pool = _StubPool()
    h = _health(pool)
    assert pool.calls, "расчёт не сделал ни одного запроса"
    wrong = []
    for sql, args in pool.calls:
        nums = {int(x) for x in re.findall(r"\$(\d+)", sql)}
        if not nums and not args:
            continue
        expected = set(range(1, len(args) + 1))
        if nums != expected:
            wrong.append(f"{' '.join(sql.split())[:90]} — в тексте "
                         f"{sorted(nums)}, аргументов {len(args)}")
    assert not wrong, (
        "запрос расчёта здоровья нумерует параметры не так, как получает "
        "аргументы (падает при каждом вызове):\n  " + "\n  ".join(wrong))
    assert h.partial is False, "расчёт на здоровой базе не должен быть частичным"


def test_the_operations_rate_is_actually_computed():
    """Успешность операций — измерена (7 из 10), а не умолчание 1.0."""
    h = _health(_StubPool())
    assert h.recent_op_success_rate == 0.7, (
        f"успешность операций {h.recent_op_success_rate} — это умолчание, "
        "значит запрос до неё упал")


def test_an_interrupted_computation_says_so():
    """Сбой внутри расчёта обязан поднять флаг, а не притвориться здоровьем."""
    h = _health(_StubPool(fail_on="operation_queue"))
    assert h.partial is True, (
        "расчёт прервался, но объект здоровья об этом не говорит — экран "
        "покажет умолчания 1.0 как измеренные показатели")
    assert h.stability_score == 1.0  # именно умолчание: считается после сбоя


def test_the_screen_shows_the_warning():
    """Флаг без экрана бесполезен: экран обязан его прочитать."""
    src = (_ROOT / "bot" / "handlers" / "ecosystems.py").read_text(
        encoding="utf-8")
    assert "health.partial" in src, (
        "экран здоровья не смотрит на partial — прерванный расчёт снова "
        "выглядит как здоровая экосистема")
    assert "по умолчанию, а не измерена" in src


def test_the_screen_names_what_it_counts():
    """«Ограничений: N» обещало журнал ограничений, а считались аккаунты."""
    src = (_ROOT / "bot" / "handlers" / "ecosystems.py").read_text(
        encoding="utf-8")
    assert "⛔ Ограничений" not in src, (
        "подпись обещает журнал ограничений, а restrictions_count — это "
        "аккаунты в кулдауне или под флудвейтом")
    assert "На паузе или под флудвейтом" in src


def test_fallbacks_do_not_fake_perfect_health():
    """Там, где упавший расчёт подменяют пустым объектом, он тоже частичный."""
    src = (_ROOT / "services" / "ecosystem_brain.py").read_text(
        encoding="utf-8")
    bare = re.findall(r"health = EcosystemHealth\(\s*\)", src)
    assert not bare, (
        "упавший расчёт подменяется объектом с умолчаниями 1.0 без "
        "partial=True — это «100% здоровья» на пустом месте")
