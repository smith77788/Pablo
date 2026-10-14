"""У идущей операции не показывается причина от прошлой попытки.

ЧТО ЛОМАЛОСЬ. Операция, упавшая по таймауту/ошибке и ушедшая в автоповтор
(`op_worker._maybe_retry_op`), получает `status='pending'` и текст причины в
`last_error`. Поллер по расписанию снова запускает её — `status='running'`, — а
`last_error` при этом НЕ очищается. Общее выражение причины
`op_status.sql_error_reason()` читало `COALESCE(error_msg, last_error,
result->>'reason')` без оглядки на статус, поэтому экран деталей показывал блок
«Ошибка: операция была прервана…» ПОВЕРХ статуса «▶ Работает». Владелец видел
одновременно «работает» и «прервана» — противоречие (жалоба со скриншота
операции #512: 4%, «Работает», и тут же «Операция была прервана»).

ЧТО ТЕПЕРЬ. Для `status='running'` выражение причины возвращает NULL: идущая
операция не ждёт и не провалилась. Причина ОЖИДАНИЯ (last_error у 'pending' —
флуд-пауза, «продолжим завтра») и терминальная причина (error_msg у
'failed'/'partial') по-прежнему видны — именно ради них выражение и писалось.

Проверка идёт по РЕАЛЬНОМУ SQL выражению в живом Postgres (а не по строке): так
ловится и логика статуса, и то, что выражение остаётся валидным SQL.
"""
from __future__ import annotations

import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")


async def _reasons() -> dict[str, str | None]:
    import asyncpg

    from services import op_status

    expr = op_status.sql_error_reason("t.")
    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            f"""
            SELECT t.tag, {expr} AS reason
            FROM (VALUES
              -- #512: операция снова идёт после автоповтора; причина прошлой
              -- попытки осела в last_error. Показывать её как «Ошибка» нельзя.
              ('running_last_error', 'running', NULL::text, 'Операция была прервана'::text, NULL::jsonb),
              -- Идущая операция, у которой прошлый result ещё несёт reason.
              ('running_result',      'running', NULL::text, NULL::text, '{{"reason":"старое"}}'::jsonb),
              -- Ждёт флуд-паузу: причина ОЖИДАНИЯ должна остаться видимой.
              ('pending_flood',       'pending', NULL::text, 'Отложено Telegram на час'::text, NULL::jsonb),
              -- Терминальный провал: причина должна остаться видимой.
              ('failed_terminal',     'failed',  'Нет активных аккаунтов'::text, NULL::text, NULL::jsonb)
            ) AS t(tag, status, error_msg, last_error, result)
            """
        )
        return {r["tag"]: r["reason"] for r in rows}
    finally:
        await conn.close()


async def test_running_operation_shows_no_stale_reason():
    by = await _reasons()
    # Суть бага: идущая операция НЕ показывает причину прошлой попытки.
    assert by["running_last_error"] is None, (
        "идущая операция показывает last_error прошлой попытки — "
        "это «Ошибка» поверх «Работает»"
    )
    assert by["running_result"] is None, (
        "идущая операция показывает result.reason прошлой попытки"
    )


async def test_waiting_and_terminal_reasons_stay_visible():
    by = await _reasons()
    # Регресс-страховка: фикс не должен погасить то, ради чего причина и
    # показывается — ожидание (флуд-пауза) и терминальный провал.
    assert by["pending_flood"] == "Отложено Telegram на час"
    assert by["failed_terminal"] == "Нет активных аккаунтов"
