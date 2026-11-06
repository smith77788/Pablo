"""Вторая рассылка тем же текстом по тому же боту не уходит, пока идёт первая.

Окно идемпотентности постановки (`operation_bus.submit`, 20 секунд) на этот
случай не работает ВООБЩЕ, и не из-за окна: немедленная рассылка создаётся в
два шага — сперва строка в `broadcasts`, потом постановка операции с её id в
params. Дедуп сравнивает params целиком, а в них у каждого нажатия свой
`broadcast_id`, то есть два сабмита не сливаются никогда, даже сделанные
подряд.

Цена ошибки — максимальная из всех, что бывают в продукте: у каждой строки
рассылки свой журнал доставок, поэтому вторая уходит ВСЕЙ аудитории бота
заново. Подписчики получают одно и то же сообщение дважды, а бот — повторяющуюся
автоматическую активность в глазах Telegram.

Путь к этому короткий и обычный: запрос на отправку ушёл, ответ не вернулся
(деплой, холодный старт, сеть), мини-апп показал ошибку, владелец нажал
«Отправить» ещё раз.

Поэтому проверка — в исполнителе: это одна дверь для всех путей постановки
(мини-апп, виджеты, повторы из `broadcaster`). Правило узкое, и у него две
половины. Залп на ВСЮ аудиторию блокирует живого двойника с тем же текстом и
тоже на всю аудиторию. Подмножество (догонка недоставленным, вариант A/B)
сперва не проверялось вовсе — а двойное нажатие по догонке вредит ровно так
же: два нажатия подряд считают ОДИН И ТОТ ЖЕ список недоставленных (первая
отправка ещё никому не успела доставить), и эти люди получают сообщение
дважды. Поэтому для подмножества двойник — живая рассылка с тем же текстом и
ТЕМ ЖЕ списком получателей; рассылка по другому списку законна и уходит.
"""
from __future__ import annotations

import asyncio

import pytest

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


class _Bot:
    pass


class _Pool:
    """Стенд базы: отвечает на запрос о живом двойнике так, как попросили.

    Стенд обязан различать ДВА запроса, потому что их различает правило: один
    ищет двойника на всю аудиторию, другой — на тот же список получателей.
    Стенд, который на оба отвечает одним и тем же двойником, зеленит проверку
    догонки ни о чём: он отдаёт ей двойника, которого случай не имел в виду.

    `live_subset` — список получателей ЖИВОЙ рассылки. Сравнение множеств
    стенд повторяет руками: так видно, что запрос ищет равенство списков, а не
    просто «есть живая рассылка с таким текстом».
    """

    def __init__(self, twin: dict | None = None, twin_raises: bool = False,
                 live_subset: list[int] | None = None):
        self.twin = twin
        self.twin_raises = twin_raises
        self.live_subset = live_subset
        self.twin_asked = 0
        self.twin_args: tuple = ()

    async def execute(self, sql, *args):
        return "UPDATE 1"

    async def fetchrow(self, sql, *args):
        if "FROM broadcasts" in sql and "status='running'" in sql:
            self.twin_asked += 1
            self.twin_sql = sql
            self.twin_args = args
            if self.twin_raises:
                raise RuntimeError("стенд: база моргнула")
            if "jsonb_typeof(target_user_ids)" in sql:
                if self.live_subset is None:
                    return None
                asked = list(args[4] or [])
                return ({"id": 4100}
                        if sorted({int(u) for u in self.live_subset}) == asked
                        else None)
            return self.twin
        if "FROM managed_bots" in sql:
            return {"token": "123456789:AAH-stand", "bot_id": 777}
        return None

    async def fetch(self, sql, *args):
        if "FROM bot_users" in sql:
            return [{"user_id": 1001}, {"user_id": 1002}]
        return []

    async def fetchval(self, sql, *args):
        return None


@pytest.fixture
def stand(monkeypatch):
    """Телеграма нет, рассылка не стартует — смотрим на решение исполнителя."""
    from database import db
    from services import broadcaster, op_worker

    started: list[int] = []
    abandoned: list[int] = []

    async def _create(pool, bot_id, text, total, owner_id, **kw):
        return 4242

    def _start(*a, **kw):
        started.append(int(a[2]))

    async def _abandon(pool, broadcast_id):
        abandoned.append(int(broadcast_id))

    async def _await_bc(*a, **kw):
        return {"status": "done", "summary": "стенд: дождались"}

    async def _instant(_s):
        return None

    monkeypatch.setattr(db, "create_broadcast", _create)
    monkeypatch.setattr(broadcaster, "start", _start)
    monkeypatch.setattr(broadcaster, "abandon_unstarted", _abandon)
    monkeypatch.setattr(broadcaster, "is_running", lambda _i: False)
    monkeypatch.setattr(op_worker, "_await_broadcast", _await_bc)
    monkeypatch.setattr(asyncio, "sleep", _instant)
    return {"started": started, "abandoned": abandoned}


def _go(pool, op_id=950, params=None):
    from services import op_worker

    return _run(op_worker._exec_run_broadcast(
        pool, _Bot(), op_id, 55, params or {"bot_id": 777, "text": "Привет"}))


def test_a_live_twin_stops_the_second_blast(stand):
    """Двойник идёт — вторая рассылка не стартует и снимается."""
    pool = _Pool(twin={"id": 4100})
    res = _go(pool)

    assert stand["started"] == [], (
        "вторая рассылка тем же текстом ушла всей аудитории заново")
    assert pool.twin_asked == 1, "двойника не искали вовсе"
    assert stand["abandoned"] == [4242], (
        "строка второй рассылки осталась живой: старт процесса поднимет её "
        "и отправит мимо проверки (см. broadcaster.abandon_unstarted)")
    assert res["status"] == "failed", res
    assert "уже идёт" in res["summary"], res["summary"]
    assert "дважды" in res["summary"], (
        "владельцу не сказано, ПОЧЕМУ отказ: он увидит «не отправилось» и "
        "нажмёт ещё раз")


def test_without_a_twin_the_broadcast_goes_out(stand):
    """Обратная сторона: проверка не должна глушить обычную рассылку."""
    pool = _Pool(twin=None)
    res = _go(pool)
    assert stand["started"] == [4242], stand
    assert stand["abandoned"] == [], "здоровую рассылку сняли"
    assert res["status"] == "done", res


def test_a_catch_up_to_the_undelivered_is_not_blocked(stand):
    """Догонка недоставленным — подмножество, и двойником не считается.

    `broadcaster.resend_undelivered` заводит новую рассылку с тем же текстом по
    тому же боту сознательно: она идёт по списку тех, кто не получил. Запретить
    её значило бы сломать штатный путь.
    """
    pool = _Pool(twin={"id": 4100})
    res = _go(pool, op_id=951,
              params={"bot_id": 777, "text": "Привет", "user_ids": [1001]})
    assert stand["started"] == [4242], (
        "догонку недоставленным приняли за второй залп")
    assert res["status"] == "done", res


def test_two_taps_on_the_same_catch_up_send_once(stand):
    """Второе нажатие по той же догонке — те же люди, и письмо им одно.

    Это вторая половина правила. Два нажатия подряд считают один и тот же
    список недоставленных: первая отправка ещё никому не успела доставить,
    поэтому список второй совпадает со списком первой, и без запрета эти люди
    получают сообщение дважды.
    """
    pool = _Pool(live_subset=[1002, 1001])  # порядок внутри списка ничем не обещан
    res = _go(pool, op_id=955,
              params={"bot_id": 777, "text": "Привет", "user_ids": [1001, 1002]})
    assert stand["started"] == [], (
        "повтор догонки ушёл тем же людям во второй раз")
    assert stand["abandoned"] == [4242], (
        "строка повтора осталась живой: старт процесса поднимет её и отправит "
        "мимо проверки")
    assert res["status"] == "failed", res
    assert "дважды" in res["summary"], res["summary"]


def test_a_different_recipient_list_is_not_a_twin(stand):
    """Другой список получателей — другая рассылка, и она уходит.

    Обратная сторона запрета выше. Брать пересечение вместо равенства нельзя:
    две честные рассылки по разным сегментам пересекаются людьми, и правило
    запретило бы вторую — то есть сломало бы варианты A/B и сегментные залпы.
    """
    pool = _Pool(live_subset=[1001, 1002])
    res = _go(pool, op_id=956,
              params={"bot_id": 777, "text": "Привет", "user_ids": [1001]})
    assert stand["started"] == [4242], (
        "рассылку по другому списку получателей приняли за двойника")
    assert res["status"] == "done", res


def test_an_unreadable_twin_check_defers_instead_of_sending(stand):
    """База моргнула — откладываем, а не отправляем вслепую.

    Отказ был бы потерей сообщения (владельцу пришлось бы нажимать снова), а
    отправка — возможным вторым залпом. Поэтому возврат в очередь: решение
    примет следующий прогон.
    """
    pool = _Pool(twin_raises=True)
    res = _go(pool, op_id=952)
    assert stand["started"] == [], "при нечитаемой проверке рассылка ушла"
    assert res["status"] == "requeue", res
    assert stand["abandoned"] == [], (
        "рассылку сняли, хотя она ещё может уйти после возврата в очередь")


def test_the_twin_check_has_a_statute_of_limitations(stand):
    """Запрет обязан сниматься сам: иначе одна мёртвая строка запретит навсегда.

    Строка рассылки может остаться в 'running', если процесс умер, а поднять
    её больше нельзя (токен бота отозван). Без срока давности владелец не смог
    бы отправить рассылку с этим текстом уже никогда и не узнал бы, почему.
    Поэтому у запроса о двойнике есть окно по времени.
    """
    from services import op_worker

    pool = _Pool(twin=None)
    _go(pool, op_id=953)
    assert "created_at >" in getattr(pool, "twin_sql", ""), (
        "у проверки двойника нет срока давности: "
        f"{getattr(pool, 'twin_sql', '<запроса не было>')}")
    assert op_worker._TWIN_LOOKBACK_S >= 3600, (
        "срок давности меньше часа оборвал бы запрет посреди долгой рассылки")
    assert op_worker._TWIN_LOOKBACK_S <= 24 * 3600, (
        "срок давности в сутки держал бы запрет сильно дольше любой рассылки")


def test_a_row_without_an_id_is_not_treated_as_a_twin(stand):
    """Блокировать залп по ответу без id двойника — значит верить непонятно чему."""
    pool = _Pool(twin={"status": "running"})
    res = _go(pool, op_id=954)
    assert stand["started"] == [4242], (
        "рассылку запретила строка, в которой нет даже id")
    assert res["status"] == "done", res

