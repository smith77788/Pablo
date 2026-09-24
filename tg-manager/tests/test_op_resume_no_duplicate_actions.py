"""Возобновление операции не должно повторять уже совершённое действие.

Разрыв. Повтор запускает исполнителя ЗАНОВО с `done_items=0`. Это не редкий
случай и не всегда решение человека: так работает `_maybe_requeue` после
сетевого сбоя, так сбрасывает зависшую операцию сторож, так воскрешает
незавершённые операции старт воркера после перезапуска контейнера (а ветка
едет на Railway, то есть перезапуск — штатное событие деплоя).

Исполнитель при этом проходит ВЕСЬ список сначала. Для операций, которые
что-то СОЗДАЮТ во внешнем мире, второй проход — не лишняя работа, а видимый
всем результат:

  * жалоба (`mass_report`) уходит с того же аккаунта на ту же цель второй раз.
    Повторные жалобы от одного отправителя Telegram считает шумом, а для
    аккаунта это лишнее действие в том же направлении — ровно то давление, от
    которого страйк отдельно защищается правилом «не бить одну цель повторно в
    короткий интервал»;
  * AI-комментарий (`ai_comment`) появляется под тем же каналом второй раз: в
    обсуждении оказывается пара похожих реплик от аккаунтов одного владельца.
    Это самая заметная подпись накрутки — и она остаётся на виду, в отличие от
    сожжённого лимита.

Механизм уже есть и применён в постинге: `completed_targets` читает
`operation_log` за всю цепочку повторов и отдаёт цели, закрытые успехом.
Требовалось не изобретать, а применить — и следить, чтобы ключ, по которому
пропускаем, был ТЕМ ЖЕ, что пишется в журнал: разошедшиеся ключи уже однажды
сделали идемпотентность mass_publish нерабочей (tests/test_op_retry_idempotency).

op_worker импортирует telethon и в тестовой среде не поднимается — связку
проверяем по исходнику, как и соседние тесты очереди.
"""
from __future__ import annotations

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Исполнитель → как выглядит ключ цели в журнале.
GUARDED = {
    "_exec_mass_report": "_acc_key",
    "_exec_ai_comment": "ref",
}


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

@pytest.mark.parametrize("name", sorted(GUARDED))
def test_executor_reads_the_journal_before_acting(ow, name):
    body = _fn(ow, name)
    assert "await completed_targets(pool, op_id)" in body, (
        f"{name}: повтор пройдёт по всем целям заново и совершит действие второй раз"
    )


@pytest.mark.parametrize("name,key", sorted(GUARDED.items()))
def test_skip_uses_the_key_that_is_written(ow, name, key):
    """Ключ пропуска обязан совпадать с ключом записи — иначе пропуск не сработает.

    Ровно на этом однажды сломалась идемпотентность постинга: успех писал
    заголовок канала, провал — id, и сопоставить их было нечем.
    """
    body = _fn(ow, name)
    m = re.search(r"if (\S+) in _already_\w+:", body)
    assert m, f"{name}: пропуска по журналу нет"
    assert m.group(1) == key, f"{name}: пропускаем по {m.group(1)}, а пишем {key}"

    writes = re.findall(
        r"INSERT INTO operation_log\(op_id, step_num, target[^)]*\)"
        r"(?:.*?\n)*?\s*op_id,\s*idx,\s*([^,\n]+),",
        body,
    )
    assert writes, f"{name}: записи в журнал не найдены"
    assert {w.strip() for w in writes} == {key}, (
        f"{name}: в журнал пишутся разные ключи {set(writes)} — сопоставить нечем"
    )


@pytest.mark.parametrize("name", sorted(GUARDED))
def test_skipped_target_still_counts_as_done(ow, name):
    """Счётчик не должен проседать из-за того, что работу сделал прошлый прогон.

    Иначе после возобновления владелец видит «2 из 50» на операции, которая на
    самом деле отработала полностью, и запускает её заново руками — то есть
    идемпотентность приводит ровно к тому дублю, который предотвращает.
    """
    body = _fn(ow, name)
    seg = body[body.index("in _already_"):]
    seg = seg[:seg.index("continue")]
    assert "ok_count += 1" in seg, f"{name}: пропущенная цель не засчитана в успех"
    assert "done_items=done_items+1" in seg, (
        f"{name}: прогресс операции не сдвинулся на пропущенной цели")


@pytest.mark.parametrize("name", sorted(GUARDED))
def test_skip_happens_before_the_pause(ow, name):
    """Межцелевая пауза — анти-детект для ДЕЙСТВИЯ; без действия она пустая трата.

    Пятьдесят пропусков по 20–45 секунд — это до получаса, за которые операция
    не делает ничего, держа слот и арендованные аккаунты.
    """
    body = _fn(ow, name)
    skip_at = body.index("in _already_")
    # Пауза между целями у исполнителей разная: у одного прямой sleep, у
    # другого — темп под губернатором. Берём ту, что есть.
    pauses = [body.rindex(p) for p in ("asyncio.sleep(", "_governed_delay(") if p in body]
    assert pauses, f"{name}: межцелевой паузы нет — проверять нечего"
    sleep_at = max(pauses)
    assert skip_at < sleep_at


# ── Журнал, на который опирается пропуск ─────────────────────────────────────

@pytest.mark.parametrize("name", sorted(GUARDED))
def test_journal_write_cannot_kill_the_operation(ow, name):
    """Действие уже совершено — падать на записи о нём нельзя.

    Сбой записи стоит возможного повтора по одной цели; необработанное
    исключение стоит всей операции, причём уже после внешнего действия.
    """
    body = _fn(ow, name)
    assert "await pool.execute(\n" not in body and "await pool.execute(" not in body, (
        f"{name}: запись в журнал идёт мимо _safe_execute и может уронить операцию"
    )


@pytest.mark.parametrize("name", sorted(GUARDED))
def test_resume_is_explained_where_it_happens(ow, name):
    """Пропуск целей выглядит как потеря работы; без объяснения его снимут."""
    body = _fn(ow, name)
    seg = body[:body.index("await completed_targets(pool, op_id)")]
    tail = seg[-900:]
    assert "Идемпотентность повтора" in tail, (
        f"{name}: пропуск не объяснён рядом с местом, где он делается")


# ── Сторож самой проверки ────────────────────────────────────────────────────

def test_known_good_executor_passes_the_same_check(ow):
    """Детектор, который ничего не находит на заведомо здоровом коде, сломан.

    `_exec_mass_publish` получил ту же защиту раньше — на нём проверка обязана
    быть зелёной.
    """
    body = _fn(ow, "_exec_mass_publish")
    assert "await completed_targets(pool, op_id)" in body


def test_helper_reads_the_whole_retry_chain(ow):
    """Повтор заводит НОВУЮ операцию: читать журнал только своего id мало."""
    body = _fn(ow, "completed_targets")
    assert "journal_op_ids(pool, op_id)" in body
    assert "status='ok'" in body
