"""Блип БД на решении о повторе отменял сам повтор — операция падала владельцу.

ЧТО БЫЛО. Ретраевая ошибка (таймаут сети, сбой Telegram, флуд) операцию не
проваливает: `_maybe_requeue` смотрит бюджет повторов и возвращает её в
очередь с бэкофом. Оба обращения к БД на этом пути были «мягкими»
(`_safe_fetchrow` / `_safe_execute`): один неудачный запрос — и

* бюджет не прочитан → `return False`, то есть ретраевая ошибка объявлена
  провалом, и автоматический повтор подменён ручным запуском;
* запись не прошла → функция всё равно отвечала «поставлена», хотя операция
  в очередь не вернулась. Вызывающий, поверив, не докладывал о провале —
  операция исчезала молча: владелец видел «выполняется», потом ничего.

Второе хуже первого: о провале владельцу хотя бы сообщают.

ЧТО ПРОВЕРЯЕМ. Оба обращения переживают один блип (`_retrying_fetchrow` /
`_retrying_execute`), а если не прошли и со второго раза — ответ честный:
«повтор не поставлен», со счётчиком и log.error, а не молчаливая потеря.
"""
from __future__ import annotations

import pytest


class _Pool:
    """Пул, который роняет первые `fail_reads` чтений и `fail_writes` записей."""

    def __init__(self, *, fail_reads: int = 0, fail_writes: int = 0,
                 retry_count: int = 0, max_retries: int | None = 3):
        self.fail_reads = fail_reads
        self.fail_writes = fail_writes
        self.retry_count = retry_count
        self.max_retries = max_retries
        self.reads = 0
        self.writes = 0
        self.updates: list[str] = []

    async def fetchrow(self, query, *args):
        self.reads += 1
        if self.reads <= self.fail_reads:
            raise RuntimeError("БД недоступна")
        return {"retry_count": self.retry_count, "max_retries": self.max_retries}

    async def execute(self, query, *args):
        self.writes += 1
        if self.writes <= self.fail_writes:
            raise RuntimeError("БД недоступна")
        self.updates.append(query)
        return "UPDATE 1"

    async def fetch(self, query, *args):
        return []

    async def fetchval(self, query, *args):
        return 0


@pytest.fixture
def ow(monkeypatch):
    from services import op_worker

    # Пауза между попытками настоящая (полсекунды) — в тесте она не нужна.
    monkeypatch.setattr(op_worker, "_JOURNAL_READ_RETRY_DELAY_S", 0.0)
    return op_worker


async def _requeue(ow, pool):
    return await ow._maybe_requeue(
        pool, 777, RuntimeError("таймаут сети"), {}, "bulk_join")


@pytest.mark.asyncio
async def test_a_blip_on_the_budget_read_still_queues_the_retry(ow):
    pool = _Pool(fail_reads=1)
    assert await _requeue(ow, pool) is True, (
        "один неудачный запрос превратил ретраевую ошибку в провал: владелец "
        "запускает заново то, что автомат должен был поднять сам")
    assert pool.reads == 2, "повтора чтения не было"
    assert pool.updates, "операция в очередь не вернулась"


@pytest.mark.asyncio
async def test_a_blip_on_the_requeue_write_still_queues_the_retry(ow):
    pool = _Pool(fail_writes=1)
    assert await _requeue(ow, pool) is True
    assert pool.updates, "запись решения не повторилась — повтор потерян"


@pytest.mark.asyncio
async def test_an_unwritable_requeue_is_reported_as_not_queued(ow, monkeypatch):
    """Иначе операция исчезает молча: повтора нет и о провале не сказано."""
    seen: list[tuple] = []
    monkeypatch.setattr(ow, "_reliability_metric",
                        lambda name, **labels: seen.append((name, labels)))
    pool = _Pool(fail_writes=99)
    assert await _requeue(ow, pool) is False, (
        "функция обещала повтор, которого не было — вызывающий промолчит о "
        "провале, и владелец не узнает об операции ничего")
    assert any(n == "infragram_requeue_write_failures_total" for n, _ in seen), (
        "потеря повтора не попала на график")


@pytest.mark.asyncio
async def test_an_unreadable_budget_does_not_retry_blindly(ow):
    """Самопроверка: совсем не прочитали бюджет — повтор не ставим.

    Ставить его вслепую значит крутить операцию без предела: как раз этот
    предел и задаёт `max_retries`, а ноль в нём означает «не повторять»
    осознанно (экспорт папки не идемпотентен).
    """
    pool = _Pool(fail_reads=99)
    assert await _requeue(ow, pool) is False
    assert not pool.updates


@pytest.mark.asyncio
async def test_the_retry_budget_is_still_respected(ow):
    """Самопроверка: повтор с повтором чтения не стал бесконечным."""
    pool = _Pool(retry_count=3, max_retries=3)
    assert await _requeue(ow, pool) is False
    assert not pool.updates, "бюджет исчерпан, а операция снова в очереди"
