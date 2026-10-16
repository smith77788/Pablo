"""Исход операции не записался — владельцу об успехе НЕ рапортуют.

РАЗРЫВ. Финал операции (`status`, `result`, `finished_at`) писался одной
попыткой через `_safe_execute`, а тот на сбое БД возвращает строку "ERROR" и
идёт дальше. Проверка ниже ловила только «ноль строк» (кто-то закрыл операцию
раньше нас), а "ERROR" под неё не подходит — поэтому после НЕЗАПИСАННОГО
финала выполнялось всё остальное завершение:

  * владельцу уходило «✅ выполнена» по операции, которая в его списке
    осталась «работает»;
  * исход уходил в метрики и подписанный аудит;
  * а строка так и висела 'running', и через сторож зависших та же операция
    поднималась заново — то есть владелец получал ВТОРОЙ отчёт о той же
    работе, а операция теряла единицу бюджета живучести.

Блип БД на закрытии — это не редкость: именно на закрытии операция делает
больше всего записей, а живёт она часами и переживает деплои.

Теперь финал пишется с одним повтором (как все решения о судьбе операции), а
если не записался и со второго раза — доклад, аудит и расписание
пропускаются: в БД операция честно осталась «работает», её поднимет сторож,
повтор пропустит сделанное по журналу целей и закроется сам. О самом сбое
человек узнаёт из `_watchdog_protective_failures` по счётчику
`infragram_op_close_write_failures_total`.

Нужен живой Postgres: проверяется РЕАЛЬНОЕ состояние строки очереди после
сбоя записи, а заглушка пула состояния не хранит. Рецепт запуска — в docstring
tests/test_account_lease_postgres.py.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 993077

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        # Пул создаётся ВНУТРИ того же цикла, в котором будет работать: иначе
        # asyncpg привязывает его к другому циклу и пул не стартует вовсе.
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(p.close())


class _FlakyPool:
    """Живой пул, у которого ЗАПИСЬ ФИНАЛА падает заданное число раз.

    Всё остальное идёт в настоящую базу: проверяем не арифметику заглушки, а
    состояние строки очереди после сбоя.
    """

    def __init__(self, real, fail_times: int):
        self._real = real
        self._left = int(fail_times)
        self.attempts = 0

    def _is_close(self, query: str) -> bool:
        # Терминальная запись — та, что ставит finished_at: возвраты в очередь
        # и отложенные повторы его не трогают.
        return "operation_queue" in query and "finished_at" in query

    async def execute(self, query, *args, **kw):
        if self._is_close(query):
            self.attempts += 1
            if self._left > 0:
                self._left -= 1
                raise RuntimeError("БД недоступна на записи финала")
        return await self._real.execute(query, *args, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _seed(pool, op_type="contacts_sync") -> int:
    op_id = _run(pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, "
        "                            label, started_at) "
        "VALUES($1,$2,'running',$3,3,'close-write test', now()) RETURNING id",
        OWNER, op_type, json.dumps({})))
    return int(op_id)


def _stand(monkeypatch, result: dict):
    """Исполнитель отработал успешно; доклад владельцу — под наблюдением."""
    from services import op_worker

    announced: list[tuple] = []
    metrics_seen: list[tuple[str, float]] = []

    async def _handler(pool, bot, op_id, owner_id, params):
        return dict(result)

    async def _announce(*a, **kw):
        announced.append(a)

    monkeypatch.setattr(op_worker, "handler_for", lambda op_type: _handler)
    monkeypatch.setattr(op_worker, "_announce_op_outcome", _announce)
    monkeypatch.setattr(
        op_worker, "_reliability_metric",
        lambda name, value=1.0, **lbl: metrics_seen.append((name, float(value))))
    # Повтор не должен удлинять тест на свою паузу.
    monkeypatch.setattr(op_worker, "_JOURNAL_READ_RETRY_DELAY_S", 0.01)
    return announced, metrics_seen


def test_a_blip_on_the_closing_write_does_not_lose_the_outcome(pool, monkeypatch):
    """Один сбой — финал всё равно записан, и отчёт владельцу один."""
    from services import op_worker

    announced, _metrics = _stand(monkeypatch, {"status": "done", "ok": 3, "failed": 0})
    op_id = _seed(pool)
    flaky = _FlakyPool(pool, fail_times=1)
    row = {"id": op_id, "owner_id": OWNER, "op_type": "contacts_sync", "params": "{}"}

    _run(op_worker._run_op_task(flaky, None, row))

    stored = _run(pool.fetchrow(
        "SELECT status, result FROM operation_queue WHERE id=$1", op_id))
    assert stored["status"] == "done", (
        "блип БД на записи финала потерял исход доведённой операции: статус "
        f"{stored['status']!r} (попыток записи: {flaky.attempts})")
    assert len(announced) == 1, f"отчёт владельцу ушёл {len(announced)} раз(а)"


def test_an_unsaved_outcome_is_not_announced_as_done(pool, monkeypatch):
    """Финал не записался даже с повтором — об успехе не рапортуем."""
    from services import op_worker

    announced, metrics_seen = _stand(
        monkeypatch, {"status": "done", "ok": 3, "failed": 0})
    op_id = _seed(pool)
    flaky = _FlakyPool(pool, fail_times=99)
    row = {"id": op_id, "owner_id": OWNER, "op_type": "contacts_sync", "params": "{}"}

    _run(op_worker._run_op_task(flaky, None, row))

    assert flaky.attempts >= 2, (
        f"запись финала не повторили: попыток {flaky.attempts} — решение о "
        "судьбе операции обязано переживать блип БД")
    assert not announced, (
        "владельцу доложили «выполнена» по операции, чей исход в БД не "
        "сохранён: в списке она осталась «работает», её поднимет сторож "
        "зависших, и тот же отчёт придёт второй раз")
    assert _run(pool.fetchval(
        "SELECT status FROM operation_queue WHERE id=$1", op_id)) == "running", (
        "строка очереди не в том состоянии, которое предполагает сценарий — "
        "тест не показателен")
    assert ("infragram_op_close_write_failures_total", 1.0) in metrics_seen, (
        "незаписанный финал не оставил следа, по которому человека "
        f"предупредит сторож защитных записей: {metrics_seen}")


def test_a_cancellation_that_was_not_saved_is_not_announced(pool, monkeypatch):
    """Та же честность на пути отмены — он результат записи не смотрел вовсе.

    Отмена закрывалась одним UPDATE без проверки: и «строку закрыл кто-то
    другой», и «запись не прошла» уходили владельцу как состоявшаяся отмена с
    цифрами. А операция оставалась 'running', её поднимал сторож зависших — и
    она продолжала работу, про которую владельцу уже сказали «остановлено».
    """
    from services import op_worker

    announced, metrics_seen = _stand(monkeypatch, {})
    op_id = _seed(pool)
    flaky = _FlakyPool(pool, fail_times=99)

    _run(op_worker._finish_cancelled_op(
        flaky, op_id, OWNER, "contacts_sync", {}, {"ok": 2, "failed": 0}, 1.0))

    assert flaky.attempts >= 2, (
        f"запись отмены не повторили: попыток {flaky.attempts}")
    assert not announced, (
        "владельцу доложили об отмене, которой в БД нет: операция осталась "
        "«работает» и продолжит работу после сторожа зависших")
    assert _run(pool.fetchval(
        "SELECT status FROM operation_queue WHERE id=$1", op_id)) == "running"
    assert ("infragram_op_close_write_failures_total", 1.0) in metrics_seen, (
        f"незаписанная отмена не оставила следа: {metrics_seen}")


def test_a_cancellation_survives_a_blip(pool, monkeypatch):
    """Один сбой записи — отмена всё равно сохранена и доложена один раз."""
    from services import op_worker

    announced, _metrics = _stand(monkeypatch, {})
    op_id = _seed(pool)
    flaky = _FlakyPool(pool, fail_times=1)

    _run(op_worker._finish_cancelled_op(
        flaky, op_id, OWNER, "contacts_sync", {}, {"ok": 2, "failed": 0}, 1.0))

    assert _run(pool.fetchval(
        "SELECT status FROM operation_queue WHERE id=$1", op_id)) == "cancelled", (
        "блип БД потерял отмену: владелец нажал «Остановить», а операция "
        "осталась в работе")
    assert len(announced) == 1, f"отчёт об отмене ушёл {len(announced)} раз(а)"
