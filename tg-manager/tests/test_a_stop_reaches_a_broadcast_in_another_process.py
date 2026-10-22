"""«Стоп» доходит до рассылки, даже если она живёт в другом процессе.

РАЗРЫВ. Остановить рассылку можно было только `broadcaster.cancel()` —
снятием asyncio-задачи ЭТОГО процесса. Но запускают рассылку два разных
процесса (Mini App — web, операция — воркер), а останавливает её
`op_worker._stop_broadcasts`, то есть воркер. Для рассылки, живущей в web,
`cancel()` не находит задачу: в БД остаётся честное 'cancelled', а цикл
отправки эту строку никогда не читал — и досылал сообщения ЖИВЫМ ЛЮДЯМ до
конца аудитории. Владелец при этом видел «остановлено».

То же окно открыто внутри одного процесса: `start_delay` (разбег сетевого
залпа — `order * 2s`) стоит ДО защищённого блока, и ровно в эти десятки
секунд владелец и жмёт «стоп».

ЧТО ПРОВЕРЯЕМ: цикл спрашивает БАЗУ (общая дверь `op_status.stop_requested`,
поэтому удалённая строка — тоже стоп), останавливается на ближайшем
получателе и пишет исход 'cancelled', а не «частично». Блип БД стопом НЕ
считается: рвать живую рассылку из-за сетевого сбоя опаснее, чем заметить
отмену на следующей проверке.
"""
from __future__ import annotations

import asyncio

import pytest

TOKEN = "123456789:AAFqwertyuiopasdfghjklzxcvbnm123456A"
AUDIENCE = list(range(1, 21))


class _Pool:
    """Строка рассылки отдаётся по сценарию: статус меняется на N-м чтении."""

    def __init__(self, statuses: list):
        self.statuses = list(statuses)
        self.status_reads = 0

    async def fetchval(self, *a, **k):
        return 777  # owner_check

    async def fetchrow(self, query, *args):
        if "FROM broadcasts" in query:
            self.status_reads += 1
            i = min(self.status_reads - 1, len(self.statuses) - 1)
            st = self.statuses[i]
            if isinstance(st, Exception):
                raise st
            return None if st is None else {"status": st}
        if "FROM managed_bots" in query:
            return {"username": "bot", "first_name": "bot"}
        return None

    async def fetch(self, *a, **k):
        return []

    async def execute(self, *a, **k):
        return "UPDATE 1"

    async def executemany(self, *a, **k):
        return None


@pytest.fixture
def bc(monkeypatch):
    from services import bot_api, brand_injection, broadcaster
    from database import db

    sent: list[int] = []
    marks: list[tuple[int, int, str]] = []

    async def _send(session, token, uid, text, **kw):
        sent.append(int(uid))
        return True, 0, "ok"

    async def _get_me(session, token):
        return {"id": 1, "username": "bot"}

    async def _delivered(*a, **k):
        return set()

    async def _update(pool, bid, s, f, status):
        marks.append((s, f, status))

    async def _free(*a, **k):
        return False

    monkeypatch.setattr(bot_api, "send_message_classified", _send)
    monkeypatch.setattr(bot_api, "get_me", _get_me)
    monkeypatch.setattr(db, "get_broadcast_delivered_ids", _delivered)
    monkeypatch.setattr(db, "update_broadcast", _update)
    monkeypatch.setattr(brand_injection, "is_free_tier", _free)
    # Пауза между сообщениями и TTL проверки — не предмет этого теста.
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(broadcaster.asyncio, "sleep",
                        lambda *_a, **_k: _real_sleep(0))
    monkeypatch.setattr(broadcaster, "_STOP_CHECK_TTL_S", 0.0, raising=False)
    return broadcaster, sent, marks


def _run(broadcaster, pool):
    asyncio.run(broadcaster.run(
        pool, object(), 4242, TOKEN, 555, "Привет", None, list(AUDIENCE)))


def test_a_stop_written_by_another_process_halts_the_sending(bc):
    broadcaster, sent, marks = bc
    # Первые три проверки — рассылка жива, дальше владелец нажал «стоп».
    pool = _Pool(["running", "running", "running", "cancelled"])
    _run(broadcaster, pool)

    assert sent, "рассылка не начиналась — тест проверяет не то"
    assert len(sent) < len(AUDIENCE), (
        "«стоп» из другого процесса не остановил отправку живым людям")
    assert marks and marks[-1][2] == "cancelled", (
        f"исход записан как {marks[-1][2] if marks else None}, а не как остановка")


def test_a_stop_before_the_first_send_sends_nothing(bc):
    broadcaster, sent, marks = bc
    _run(broadcaster, _Pool(["cancelled"]))
    assert sent == [], "остановленная до старта рассылка всё равно ушла"


def test_a_deleted_broadcast_is_a_stop_too(bc):
    """Общая дверь: пропавшая строка — такое же однозначное «стоп»."""
    broadcaster, sent, marks = bc
    _run(broadcaster, _Pool(["running", None]))
    assert len(sent) < len(AUDIENCE), "удалённая рассылка продолжила отправку"


def test_a_db_blip_does_not_stop_a_live_broadcast(bc):
    broadcaster, sent, marks = bc
    pool = _Pool(["running", RuntimeError("соединение потеряно"), "running"])
    _run(broadcaster, pool)
    assert len(sent) == len(AUDIENCE), (
        "сетевой блип оборвал живую рассылку — это дороже пропущенной отмены")
    assert marks[-1][2] == "done"


def test_a_broadcast_without_a_row_is_not_silently_killed(bc):
    """Пропавшая строка — стоп только после того, как её хотя бы раз видели.

    Наличия строки в `broadcasts` сам `run` не требует: проверка владельца
    смотрит в `managed_bots`. Если бы отсутствие строки считалось стопом
    сразу, такая рассылка молча не отправила бы ничего — новый класс бага
    вместо закрытого.
    """
    broadcaster, sent, marks = bc
    _run(broadcaster, _Pool([None]))
    assert len(sent) == len(AUDIENCE), (
        "рассылка без строки в broadcasts молча не ушла никому")
