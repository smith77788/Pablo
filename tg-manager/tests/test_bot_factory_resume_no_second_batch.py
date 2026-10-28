"""Возобновление фабрики ботов не должно создавать второй комплект.

Разрыв. Повтор запускает исполнителя заново с `done_items=0` — после сетевого
сбоя (`_maybe_requeue`), после сброса зависшей операции сторожем, после
перезапуска контейнера (ветка едет на Railway, перезапуск — штатное событие
деплоя). Фабрика проходила `range(count)` сначала и создавала ВТОРОЙ комплект
ботов.

Цена конкретная и неустранимая: у BotFather жёсткий предел 20 ботов на аккаунт,
и он выгорал на работе, которая уже сделана. Лишние боты остаются в Telegram
навсегда, удалять их владельцу придётся руками. У многоаккаунтной фабрики
шагов `accounts × bot_count` — до сотни лишних ботов за один повтор.

Второй разрыв, из-за которого первый нельзя было закрыть: `_exec_bot_factory_multi`
не писал в `operation_log` НИ ОДНОЙ строки. «📋 Лог» операции оставался пустым
(владелец видел только «Создано: N» и не мог узнать, какие боты завелись), а
опереться пропуску было не на что.

Ключ — номер шага, а не имя: в режиме ключевых слов имена генерируются. Так же
устроено массовое создание каналов, см. `completed_steps`.

op_worker импортирует telethon и в тестовой среде не поднимается — связку
проверяем по исходнику, как и соседние тесты очереди.
"""
from __future__ import annotations

import os
import re

import pytest

from tests import op_journal_probe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FACTORIES = ("_exec_bot_factory", "_exec_bot_factory_multi")


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _fn(src: str, name: str) -> str:
    start = src.index(f"async def {name}(")
    m = re.search(r"\n(?:async )?def ", src[start + 10:])
    return src[start:start + 10 + m.start()] if m else src[start:]


@pytest.fixture(scope="module")
def ow() -> str:
    return _read("services/op_worker.py")


# ── Пропуск сделанного ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name", FACTORIES)
def test_factory_reads_the_journal_before_creating(ow, name):
    body = _fn(ow, name)
    assert "await completed_steps(pool, op_id)" in body, (
        f"{name}: повтор создаст второй комплект ботов")


@pytest.mark.parametrize("name", FACTORIES)
def test_skip_key_matches_the_key_written_to_the_journal(ow, name):
    """Пропуск по номеру шага бессмыслен, если в журнал пишется другой номер."""
    body = _fn(ow, name)
    m = re.search(r"if (\w+) in _done_steps:", body)
    assert m, f"{name}: пропуска по журналу нет"
    skip_key = m.group(1)

    # Записи читаются по дереву разбора (tests/op_journal_probe.py): успех
    # пишется через дверь `_journal_done`, и поиск литерала
    # `INSERT INTO operation_log` его не видит.
    journal = op_journal_probe.writes(name)
    assert journal, f"{name}: записи в журнал не найдены"
    steps = {w.step for w in journal}
    assert steps == {skip_key}, (
        f"{name}: пропускаем по {skip_key}, а пишем {steps}")


@pytest.mark.parametrize("name", FACTORIES)
def test_skip_happens_after_the_name_is_generated(ow, name):
    """Главная ловушка этого фикса.

    @юзернеймы в SEO-режиме берутся из генератора, который вращается на каждом
    шаге. Пропуск ДО обращения к генератору сдвинул бы имена всех оставшихся
    ботов — то есть идемпотентность сломала бы сам результат операции.
    """
    body = _fn(ow, name)
    skip_at = body.index("in _done_steps:")
    gen_at = body.index("_bf_ugen is not None")
    assert gen_at < skip_at, (
        f"{name}: пропуск идёт до генератора имён — имена остальных ботов съедут")


@pytest.mark.parametrize("name", FACTORIES)
def test_skipped_step_still_counts_as_created(ow, name):
    """Иначе владелец видит «1 из 10» на отработавшей операции и жмёт повтор."""
    body = _fn(ow, name)
    seg = body[body.index("in _done_steps:"):]
    seg = seg[:seg.index("continue")]
    assert "created_count += 1" in seg
    assert "done_items=done_items+1" in seg


# ── Журнал, на который опирается пропуск ─────────────────────────────────────

def test_multi_factory_writes_a_per_bot_journal(ow):
    """Раньше не писала ни строки: «📋 Лог» пустой, опереться пропуску не на что."""
    journal = op_journal_probe.writes("_exec_bot_factory_multi")
    assert len(journal) >= 3, (
        f"нужны записи и об успехе, и об обеих ветках неудачи, а их {len(journal)}")
    statuses = {w.status for w in journal}
    assert statuses >= {"ok", "error"}, f"в журнал попадает не всякий исход: {statuses}"
    assert op_journal_probe.UNKNOWN not in {w.target for w in journal}, (
        "запись в журнал не разобрана — проверка ничего не проверяет")


def test_created_bot_is_never_logged_as_a_step_to_redo(ow):
    """Бот создан, но getMe не ответил — шаг обязан считаться закрытым.

    Иначе каждое возобновление создаёт ещё одного бота взамен того, которого
    мы просто не смогли прочитать: предел BotFather выгорает на пустом месте.
    """
    body = _fn(ow, "_exec_bot_factory_multi")
    seg = body[body.index("if not bot_id:"):]
    seg = seg[:seg.index("continue")]
    # Статус 'ok' даёт сама дверь журнала, поэтому ищем вызов двери, а не
    # литерал: литерал тут нашёлся бы и в соседнем комментарии.
    assert f"await {op_journal_probe.JOURNAL_DOOR}(" in seg, (
        "шаг с уже созданным ботом не закрыт успехом — повтор создаст второго")
    assert "getMe" in seg, "причина обязана быть видна владельцу в логе операции"


@pytest.mark.parametrize("name", FACTORIES)
def test_journal_write_cannot_kill_the_operation(ow, name):
    """Бот уже создан — падать на записи о нём нельзя."""
    body = _fn(ow, name)
    tail = body[body.index("in _done_steps:"):]
    assert "await pool.execute(\n                \"INSERT INTO operation_log" not in tail
    for m in re.finditer(r"await pool\.execute\(\s*\n?\s*\"INSERT INTO operation_log", body):
        raise AssertionError(f"{name}: запись в журнал идёт мимо _safe_execute")


# ── Сторож самой проверки ────────────────────────────────────────────────────

def test_known_good_executor_passes_the_same_check(ow):
    """Детектор, ничего не находящий на здоровом коде, сломан.

    Массовое создание каналов получило ту же защиту раньше — на нём проверка
    обязана быть зелёной.
    """
    body = _fn(ow, "_exec_bulk_create_channels_multi")
    assert "await completed_steps(pool, op_id)" in body


def test_helper_counts_only_successful_steps(ow):
    """Пропуск по неудачному шагу означал бы молча недоделанную работу."""
    body = _fn(ow, "completed_steps")
    assert "status='ok'" in body
    assert "journal_op_ids(pool, op_id)" in body
