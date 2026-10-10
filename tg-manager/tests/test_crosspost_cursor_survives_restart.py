"""Перезапуск посередине пачки не должен пересылать посты второй раз.

Механика дефекта. Кросспостинг берёт до 20 новых постов источника и
пересылает их в цель с человеческими паузами — это минута-полторы работы.
Курсор связки (`crosspost_links.last_msg_id`) записывался ОДНОЙ строкой после
возврата пересылки. Перезапуск контейнера случается на каждом деплое рабочей
ветки, и попадание в эту минуту давало ровно такую картину: посты уже уехали в
целевой канал, а курсор остался на прежнем значении. Следующий прогон брал те
же посты заново и отправлял их ВТОРОЙ РАЗ. Подписчики видели дубли, а Telegram
— повтор одинакового содержимого с одного аккаунта.

Защиты от повтора у этого исполнителя нет и не нужно: курсор связки и ЕСТЬ его
журнал. Но журнал, который пишется только в конце, журналом не является.

Проверяем поведением: пересылка обязана отдавать прогресс после КАЖДОГО поста,
и после обрыва посередине курсор обязан стоять на последнем реально отправленном
посте, а не на том, с которого пачка началась.
"""
from __future__ import annotations

import asyncio

import pytest

from services import account_manager


class _Msg:
    def __init__(self, mid: int) -> None:
        self.id = mid
        self.service = False


class _Client:
    """Клиент, который успешно пересылает, пока не дойдёт до `die_at`.

    На `die_at` поднимается `asyncio.CancelledError` — именно так выглядит
    остановка процесса: задачу отменяют, и тело пересылки дальше не идёт.
    Обычное исключение здесь смоделировало бы НЕ перезапуск, а неудачу одного
    поста, которую пересылка намеренно переступает (см. тест про пропуск).
    """

    def __init__(self, ids, die_at=None) -> None:
        self._ids = list(ids)
        self._die_at = die_at
        self.sent: list[int] = []

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    def iter_messages(self, *a, **kw):
        ids = self._ids

        class _It:
            def __aiter__(self):
                self._i = 0
                return self

            async def __anext__(self):
                if self._i >= len(ids):
                    raise StopAsyncIteration
                m = _Msg(ids[self._i])
                self._i += 1
                return m

        return _It()

    async def forward_messages(self, target, msg):
        if self._die_at is not None and msg.id == self._die_at:
            raise asyncio.CancelledError("контейнер перезапущен посередине пачки")
        self.sent.append(msg.id)


@pytest.fixture
def _no_pauses(monkeypatch):
    """Человеческие паузы между пересылками тесту не нужны."""
    monkeypatch.setattr(account_manager.random, "uniform", lambda a, b: 0)
    monkeypatch.setattr(account_manager, "_resolve_channel_peer",
                        lambda *a, **kw: _fake_peer())


async def _fake_peer(*a, **kw):
    return object()


@pytest.mark.asyncio
async def test_cursor_is_reported_after_every_post(_no_pauses, monkeypatch):
    client = _Client([11, 12, 13])
    monkeypatch.setattr(account_manager, "_make_client", lambda *a, **kw: client)

    seen: list[int] = []

    async def _on_forwarded(msg_id):
        seen.append(int(msg_id))

    res = await account_manager.forward_new_posts(
        "sess", 1, 2, since_msg_id=10, on_forwarded=_on_forwarded)

    assert client.sent == [11, 12, 13]
    assert seen == [11, 12, 13], (
        "прогресс приходит не после каждого поста — курсор снова двигается "
        "только в конце, и перезапуск посередине даст дубли")
    assert res["forwarded"] == 3


@pytest.mark.asyncio
async def test_break_in_the_middle_leaves_cursor_on_the_last_sent_post(
        _no_pauses, monkeypatch):
    """Главный случай: оборвались на третьем посте — курсор обязан быть на втором.

    Без прогресса по ходу работы вызывающий знал бы только исходные 10, и посты
    11 и 12 уехали бы в целевой канал повторно.
    """
    client = _Client([11, 12, 13, 14], die_at=13)
    monkeypatch.setattr(account_manager, "_make_client", lambda *a, **kw: client)

    cursor = 10

    async def _on_forwarded(msg_id):
        nonlocal cursor
        cursor = max(cursor, int(msg_id))

    with pytest.raises(asyncio.CancelledError):
        await account_manager.forward_new_posts(
            "sess", 1, 2, since_msg_id=10, on_forwarded=_on_forwarded)

    assert client.sent == [11, 12]
    assert cursor == 12, (
        f"курсор остался на {cursor}: следующий прогон переслал бы посты "
        f"11 и 12 второй раз")


@pytest.mark.asyncio
async def test_failing_callback_does_not_stop_forwarding(_no_pauses, monkeypatch):
    """Сбой записи в базу не должен обрывать уже начатую пачку.

    Отправленное не отменить, а терять из-за одной неудачной записи остаток
    пачки незачем: курсор подстрахован ещё и записью по возврату.
    """
    client = _Client([11, 12])
    monkeypatch.setattr(account_manager, "_make_client", lambda *a, **kw: client)

    async def _boom(_msg_id):
        raise RuntimeError("база недоступна")

    res = await account_manager.forward_new_posts(
        "sess", 1, 2, since_msg_id=10, on_forwarded=_boom)

    assert client.sent == [11, 12]
    assert res["forwarded"] == 2


@pytest.mark.asyncio
async def test_without_callback_behaviour_is_unchanged(_no_pauses, monkeypatch):
    """Колбэк необязателен: остальные вызывающие ничего не меняют."""
    client = _Client([11, 12])
    monkeypatch.setattr(account_manager, "_make_client", lambda *a, **kw: client)

    res = await account_manager.forward_new_posts("sess", 1, 2, since_msg_id=10)
    assert res["forwarded"] == 2 and res["last_msg_id"] == 12


def test_executor_passes_the_callback():
    """Хелпер с колбэком бесполезен, если исполнитель его не передаёт."""
    import ast
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "services", "op_worker.py"), encoding="utf-8") as f:
        src = f.read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_exec_crosspost_run")
    body = "\n".join(src.split("\n")[fn.lineno - 1:fn.end_lineno])
    assert "on_forwarded=" in body
    # GREATEST, а не присваивание: колбэк мог продвинуть курсор дальше, чем
    # знает запись по возврату, и затирать его назад нельзя.
    assert "GREATEST" in body


@pytest.mark.asyncio
async def test_single_failed_post_is_skipped_not_wedging_the_link(
        _no_pauses, monkeypatch):
    """Неудача ОДНОГО поста переступается намеренно — и это осознанный выбор.

    Пост с защищённым содержимым не отправится никогда. Если останавливать
    курсор на нём, связка встанет навсегда и перестанет возить вообще всё.
    Поэтому пересылка идёт дальше, а курсор уходит за неудачный пост: цена —
    один потерянный пост, альтернатива — мёртвая связка. Тест фиксирует именно
    этот выбор, чтобы его не поменяли молча.
    """
    class _Picky(_Client):
        async def forward_messages(self, target, msg):
            if msg.id == 13:
                raise RuntimeError("содержимое защищено от пересылки")
            self.sent.append(msg.id)

    client = _Picky([11, 12, 13, 14])
    monkeypatch.setattr(account_manager, "_make_client", lambda *a, **kw: client)

    res = await account_manager.forward_new_posts("sess", 1, 2, since_msg_id=10)
    assert client.sent == [11, 12, 14]
    assert res["last_msg_id"] == 14 and res["forwarded"] == 3
