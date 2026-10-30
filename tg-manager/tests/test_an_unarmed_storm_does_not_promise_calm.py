"""Защита от волны банов: не включилась — так и скажи.

ЧТО ДЕЛАЕТ ЗАЩИТА. `anti_storm.check_and_arm` вызывается из хука бана, то есть
ровно в те минуты, когда Telegram чистит флот. Замедление включается ОДНОЙ
записью состояния (`organism_state`): по ней считает множитель губернатор, по
ней же темп клэмпится сразу, а не через TTL. Нет записи — нет замедления, и
аккаунты продолжают работать в полном темпе под чисткой.

ЧТО БЫЛО. Сбой этой записи гасился в `log.debug` и дальше не влиял ни на что:
функция возвращала `{"level": "storm"}`, как будто защита стоит, а владельцу
уходило «Флот переведён в глубокий сон (темп ×N) на M мин». Обещание, которого
никто не выполнил, в самую опасную минуту: владелец читает, что всё под
контролем, и не останавливает работу сам.

Второй разрыв того же места: сбой ЧТЕНИЯ прежнего состояния отменял армирование
целиком — волна шла, а защита не включалась из-за неудачного чтения. Писать
вслепую тоже нельзя: своим `watch` можно затереть уже объявленный шторм, то есть
ослабить защиту. Правило теперь такое: на непрочитанном состоянии армируем
только самый сильный уровень — шторм ничего слабее собой не заменит.
"""

import asyncio
import json

import pytest

from services import anti_storm as A

COUNTER = "infragram_anti_storm_arm_write_failures_total"


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, owner_id, text, **k):
        self.sent.append(text)


class _Pool:
    """Пул, у которого ломается ровно одна дверь: чтение или запись состояния."""

    def __init__(self, hit: int, fleet: int, *, break_write=False,
                 break_read=False, state=None):
        self._hit, self._fleet = hit, fleet
        self._state = state
        self.break_write, self.break_read = break_write, break_read
        self.state_writes: list = []
        self.emitted: list = []

    async def fetchval(self, q, *a):
        if "COUNT(DISTINCT account_id)" in q:
            return self._hit
        if "FROM tg_accounts" in q:
            return self._fleet
        return 0

    async def fetchrow(self, q, *a):
        if "organism_state" in q:
            if self.break_read:
                raise RuntimeError("состояние не прочитано")
            return None if self._state is None else {"value": self._state}
        return None

    async def execute(self, q, *a):
        if "organism_state" in q:
            if self.break_write:
                raise RuntimeError("состояние не записано")
            self._state = json.loads(a[2])
            self.state_writes.append(self._state)
        elif "organism_events" in q:
            self.emitted.append(a)
        return "OK"


def _count() -> float:
    from services import metrics

    return float(metrics.snapshot()["counters"].get(COUNTER, 0))


def test_a_lost_arming_write_is_counted_not_whispered():
    from services import metrics

    metrics.reset()
    pool = _Pool(A.STORM_ABS, 50, break_write=True)
    asyncio.run(A.check_and_arm(pool, owner_id=7))

    assert not pool.state_writes, "запись должна была упасть — стенд собран не так"
    assert _count() == 1, (
        "потерянное включение защиты не попало на график: снаружи «защита "
        f"стоит» неотличимо от «защита не включилась» ({metrics.snapshot()['counters']})")


def test_the_owner_is_not_told_the_fleet_is_asleep():
    from services import metrics

    metrics.reset()
    bot = _Bot()
    pool = _Pool(A.STORM_ABS, 50, break_write=True)
    asyncio.run(A.check_and_arm(pool, owner_id=7, bot=bot))

    assert len(bot.sent) == 1, "про волну банов владельцу сказать обязаны"
    text = bot.sent[0]
    assert "глубокий сон" not in text, (
        "владельцу обещан глубокий сон, которого не случилось: по этому тексту "
        f"он решит, что останавливать работу не нужно — {text!r}")
    assert "НЕ включилась" in text and "вручную" in text, (
        f"текст не говорит, что защиты нет и что делать владельцу: {text!r}")


def test_a_successful_arming_still_promises_the_sleep():
    """Обратная сторона: когда защита включилась, текст прежний."""
    bot = _Bot()
    pool = _Pool(A.STORM_ABS, 50)
    asyncio.run(A.check_and_arm(pool, owner_id=7, bot=bot))

    assert pool.state_writes and pool.state_writes[-1]["level"] == A.STORM
    assert "глубокий сон" in bot.sent[0], bot.sent[0]
    assert pool.emitted, "событие шины уходит при успешном армировании"


def test_an_unreadable_state_does_not_cancel_a_storm():
    """Неудачное чтение больше не оставляет волну без защиты."""
    pool = _Pool(A.STORM_ABS, 50, break_read=True)
    d = asyncio.run(A.check_and_arm(pool, owner_id=7))

    assert d["level"] == A.STORM
    assert pool.state_writes, (
        "прежнее состояние не прочиталось, и защита не включилась вовсе — "
        "волна банов идёт, а флот в полном темпе")
    assert pool.state_writes[-1]["level"] == A.STORM


def test_an_unreadable_state_does_not_let_watch_overwrite_a_storm():
    """А вот слабый уровень вслепую не пишем: он затёр бы объявленный шторм."""
    pool = _Pool(A.WATCH_ABS, 50, break_read=True)
    d = asyncio.run(A.check_and_arm(pool, owner_id=7))

    assert d["level"] == A.WATCH
    assert not pool.state_writes, (
        "watch записан поверх непрочитанного состояния: так можно ослабить "
        "уже объявленный шторм")
