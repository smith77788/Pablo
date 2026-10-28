"""Один занятый аккаунт больше не гасит ход всему флоту.

ЧТО ЛОМАЛОСЬ (видно в журнале разогрева на проде). Ход выбирал РОВНО ОДИН
аккаунт из сессии и, если тот оказывался занят операцией, в карантине или
негодным, пропускал ход целиком — до следующего интервала интенсивности. При
флоте в тридцать аккаунтов это значит, что разогрев молчит из-за одного
занятого: за одиннадцать минут в чат ушло одно сообщение, а в журнале рядом —
«Аккаунт занят другой операцией — ход пропущен».

Вторая половина оттуда же: аккаунт, которому чат не даёт писать («You can't
write in this chat»), выбирался снова и снова. Каждый такой выбор — сожжённый
ход, в котором мог бы говорить кто-то другой, и ещё одна одинаковая строка в
журнале.

ЧТО ТЕПЕРЬ. Кандидаты пробуются по очереди до первого годного (перебор
ограничен, чтобы ход не превращался в обход всего флота), а получивший запрет
на письмо выводится из разогрева на несколько часов — ограничение бывает
временным, поэтому потом он пробуется снова.
"""
from __future__ import annotations

import asyncio

import pytest

from services import chat_warmup as cw


# ── Распознавание запрета ────────────────────────────────────────────────────


class ChatWriteForbiddenError(Exception):
    pass


class UserBannedInChannelError(Exception):
    pass


@pytest.mark.parametrize("exc", [
    ChatWriteForbiddenError("You can't write in this chat (caused by SendReactionRequest)"),
    UserBannedInChannelError("banned from sending messages"),
    RuntimeError("CHAT_WRITE_FORBIDDEN"),
])
def test_a_write_ban_is_recognised(exc):
    assert cw._is_write_forbidden(exc), exc


@pytest.mark.parametrize("exc", [
    RuntimeError("Connection reset by peer"),
    RuntimeError("A wait of 300 seconds is required"),
    RuntimeError("database is locked"),
])
def test_an_ordinary_failure_is_not_a_write_ban(exc):
    assert not cw._is_write_forbidden(exc), (
        f"«{exc}» принято за запрет письма — аккаунт зря выведут из разогрева"
    )


# ── Очередь кандидатов ───────────────────────────────────────────────────────


def test_the_queue_holds_every_account_not_just_one():
    order = cw._speaker_order([1, 2, 3, 4, 5], last_speaker=3)
    assert sorted(order) == [1, 2, 3, 4, 5], "кандидаты потерялись"
    assert order[-1] == 3, (
        "говоривший последним снова первый в очереди — флот будет говорить "
        "одним голосом подряд"
    )


def test_a_single_account_session_still_speaks():
    assert cw._speaker_order([7], last_speaker=7) == [7], (
        "в сессии с одним аккаунтом говорить стало некому"
    )


def test_the_turn_is_bounded():
    assert 1 < cw._MAX_SPEAKER_TRIES <= 10, (
        "перебор без потолка превращает один ход в обход всего флота"
    )


# ── Отметка «этому чат писать не даёт» ───────────────────────────────────────


class _Pool:
    def __init__(self, rows=None, raises: bool = False):
        self._rows = rows or []
        self._raises = raises
        self.inserted: list[tuple] = []

    async def fetch(self, sql, *args):
        if self._raises:
            raise RuntimeError("БД недоступна")
        return list(self._rows)

    async def fetchrow(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        if sql.strip().startswith("INSERT"):
            self.inserted.append((sql, args))
        return "INSERT 0 1"


def test_the_ban_is_written_down_under_its_own_kind():
    pool = _Pool()
    asyncio.run(cw._note_write_forbidden(
        pool, {"id": 7, "owner_id": 42}, 5,
        ChatWriteForbiddenError("You can't write in this chat")))

    assert pool.inserted, "запрет письма нигде не отметился"
    args = pool.inserted[0][1]
    assert cw._NO_WRITE_KIND in args, (
        "отметка легла под общим видом — по ней нельзя будет отобрать "
        "аккаунты, выведенные из разогрева"
    )
    assert any("не даёт" in str(a) for a in args)
    assert any(str(cw._NO_WRITE_HOURS) in str(a) for a in args), (
        "владельцу не сказали, на сколько аккаунт выведен"
    )


def test_barred_accounts_are_read_back():
    pool = _Pool(rows=[{"account_id": 5}, {"account_id": 9}])
    got = asyncio.run(cw._write_forbidden_ids(pool, 7))
    assert got == {5, 9}


def test_the_ban_expires_by_itself():
    import inspect

    src = inspect.getsource(cw._write_forbidden_ids)
    assert "make_interval(hours =>" in src, (
        "отметка вечная — временное ограничение (новичок в группе, медленный "
        "режим) навсегда вычеркнет здоровый аккаунт"
    )


def test_a_broken_lookup_does_not_bar_the_whole_fleet():
    assert asyncio.run(cw._write_forbidden_ids(_Pool(raises=True), 7)) == set()


# ── Что видит владелец ───────────────────────────────────────────────────────


def test_the_turn_reports_how_many_it_tried():
    import ast
    import inspect

    src = inspect.getsource(cw._process_session)
    tree = ast.parse(src.lstrip())
    joined = " ".join(
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
        and isinstance(n.value, str))
    assert "Ни один из" in joined, (
        "сводная причина не называется — владелец снова не узнает, почему "
        "весь флот промолчал"
    )


def test_the_screen_names_a_barred_account():
    """Выведенный из разогрева не должен выглядеть на экране свободным.

    Шов переехал: признак читает общее для хода и экрана чтение готовности
    (`fleet_availability`), а подпись даёт `busy_reason_ru`. Поэтому стережём
    всю цепочку, а не текст одной функции; само ПОВЕДЕНИЕ экрана (barred
    аккаунт приходит с причиной и не считается готовым) проверяется в
    test_warmup_picks_the_free_accounts — это строже прежнего грепа, который
    прошёл бы и на функции, упоминающей строку, но не выводящей её.
    """
    import inspect

    assert "fleet_availability" in inspect.getsource(cw.diagnose), (
        "экран судит о флоте сам и про запрет письма снова не знает"
    )
    assert "_write_forbidden_ids" in inspect.getsource(cw.fleet_availability), (
        "общее чтение готовности не спрашивает про запрет письма"
    )
    assert "не даёт ему писать" in inspect.getsource(cw.busy_reason_ru), (
        "причина больше не называется владельцу"
    )
