"""Регрессия: разогрев берёт в сессию и в ход СВОБОДНЫЕ аккаунты.

ЧТО ЛОМАЛОСЬ (слова владельца: «у меня 51 аккаунт флота, многие из которых
заняты другими делами, разогрев берёт максимум 30, и в них могут быть включены
те, которые заняты»). Два места, и оба решали «кого взять» вслепую:

1. Состав сессии. Потолок флота на чат срезал список выражением `[:30]` после
   выборки `ORDER BY id`, то есть по дате заливки аккаунта. Готовность не
   спрашивалась вообще: в тридцать мест попадали держащие операции, стоящие на
   паузе Telegram и вовсе негодные (дверь `_alive_accounts` смотрит только на
   выключатель и строку сессии — ни на статус, ни на кулдаун). Место в сессии
   конечно, а тратилось на тех, кто говорить не может.

2. Очередь хода. Кандидаты перемешивались случайно, а пробовались только первые
   шесть. Во флоте, где из тридцати заняты двадцать, шесть случайных кандидатов
   с большой вероятностью оказываются занятыми ВСЕ — и ход молчит при живых
   свободных аккаунтах.

ЧТО ТЕПЕРЬ. Готовность каждого аккаунта читается ОДНИМ запросом, и занятость —
не флаг, а вес по длительности: аренда под операцию снимается через минуты,
пауза Telegram держится часами, карантин — сутками, негодность не проходит
сама. По этому весу строится и очередь хода (свободные первыми), и отбор под
потолок (негодные не занимают места вовсе). Fail-open сохранён: нечитаемая
готовность возвращает прежнее поведение, а не останавливает разогрев.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from services import chat_warmup as cw
from services import infra_memory


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _acc(acc_id: int, *, leased: bool = False, cooling: bool = False,
         status: str = "active", is_active: bool = True,
         has_session: bool = True, trust: float = 50.0) -> dict:
    return {
        "id": acc_id,
        "label": f"acc{acc_id}",
        "is_active": is_active,
        "has_session": has_session,
        "acc_status": status,
        # Строки параметризации собираются при СБОРЕ тестов, а проверка идёт
        # позже: в полном прогоне дольше получаса «остывание на 30 минут»
        # успевало истечь, и тест краснел сам по себе. Берём заведомо далёкое.
        "cooldown_until": (_now() + dt.timedelta(days=30)) if cooling else None,
        "leased": leased,
        "trust_score": trust,
    }


class _Pool:
    """Пул, отвечающий по таблице в запросе: аккаунты, журнал, всё прочее пусто."""

    def __init__(self, accounts=None, barred=(), raises: bool = False):
        self.accounts = list(accounts or [])
        self.barred = list(barred)
        self.raises = raises
        self.queries: list[str] = []
        self.executed: list[tuple] = []

    async def fetch(self, sql, *args):
        self.queries.append(sql)
        if self.raises:
            raise RuntimeError("БД недоступна")
        if "tg_accounts" in sql:
            ids = {int(i) for i in (args[0] or [])}
            return [a for a in self.accounts if int(a["id"]) in ids]
        if "chat_warmup_log" in sql:
            return [{"account_id": i} for i in self.barred]
        return []

    async def fetchrow(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


@pytest.fixture(autouse=True)
def _no_quarantine(monkeypatch):
    """По умолчанию карантин пуст — тесты, которым он нужен, ставят свой."""
    async def _none(pool, ids, **kw):
        return set()

    monkeypatch.setattr(infra_memory, "quarantined_accounts", _none)


# ── Словарь занятости: вес по длительности ───────────────────────────────────


def test_the_vocabulary_is_ordered_by_how_long_it_lasts():
    assert (cw.busy_weight(cw.FREE)
            < cw.busy_weight(cw.BUSY_LEASED)
            < cw.busy_weight(cw.BUSY_COOLDOWN)
            < cw.busy_weight(cw.BUSY_QUARANTINE)
            < cw.busy_weight(cw.BUSY_NO_WRITE)
            < cw.busy_weight(cw.BUSY_UNFIT)), (
        "порядок весов не отражает длительность: аренда под операцию снимается "
        "за минуты, а негодность не проходит вовсе — выбор участников и очередь "
        "хода строятся именно по этому порядку"
    )


def test_an_unknown_code_is_treated_as_the_worst():
    assert cw.busy_weight("нечто новое") == cw.busy_weight(cw.BUSY_UNFIT), (
        "новый код занятости по умолчанию считается ЛУЧШИМ — такой аккаунт "
        "встанет в начало очереди хода, хотя про него ничего не известно"
    )


# ── Классификация: ровно те же гейты, что у хода ─────────────────────────────


@pytest.mark.parametrize("row,expect", [
    (None, cw.BUSY_UNFIT),
    (_acc(1), cw.FREE),
    (_acc(1, is_active=False), cw.BUSY_UNFIT),
    (_acc(1, has_session=False), cw.BUSY_UNFIT),
    (_acc(1, status="banned"), cw.BUSY_UNFIT),
    (_acc(1, status="spamblock"), cw.BUSY_UNFIT),
    (_acc(1, cooling=True), cw.BUSY_COOLDOWN),
    (_acc(1, leased=True), cw.BUSY_LEASED),
])
def test_the_class_matches_the_gate(row, expect):
    assert cw.busy_class(row, now=_now()) == expect


def test_a_write_ban_outranks_everything_else():
    assert cw.busy_class(_acc(1), now=_now(), barred=True) == cw.BUSY_NO_WRITE


def test_quarantine_is_seen():
    assert (cw.busy_class(_acc(1), now=_now(), quarantined=True)
            == cw.BUSY_QUARANTINE)


def test_an_expired_cooldown_does_not_hold_the_account():
    row = _acc(1)
    row["cooldown_until"] = _now() - dt.timedelta(hours=2)
    assert cw.busy_class(row, now=_now()) == cw.FREE, (
        "прошедшая пауза Telegram держит аккаунт вечно"
    )


# ── Причина по-русски и ДЕЙСТВЕННО ───────────────────────────────────────────


def test_every_reason_is_russian_and_actionable():
    now = _now()
    seen = {
        cw.busy_reason_ru(cw.BUSY_UNFIT, _acc(1, is_active=False)),
        cw.busy_reason_ru(cw.BUSY_UNFIT, _acc(1, has_session=False)),
        cw.busy_reason_ru(cw.BUSY_UNFIT, _acc(1, status="banned")),
        cw.busy_reason_ru(cw.BUSY_UNFIT, None),
        cw.busy_reason_ru(cw.BUSY_COOLDOWN, _acc(1, cooling=True), now=now),
        cw.busy_reason_ru(cw.BUSY_QUARANTINE),
        cw.busy_reason_ru(cw.BUSY_LEASED),
        cw.busy_reason_ru(cw.BUSY_NO_WRITE),
    }
    assert len(seen) == 8, (
        f"причины схлопнулись в одну формулировку ({seen}) — владельцу придётся "
        f"перебирать флот руками: выключенный включается кнопкой, аккаунт без "
        f"сессии перезаливается, забаненный не лечится никак"
    )
    for text in seen:
        # Имена собственные («Telegram») — не английский текст, их владелец
        # читает и так; проверяем, что не осталось слов и кодов статусов.
        bare = text.replace("Telegram", "")
        assert text and not any(c.isascii() and c.isalpha() for c in bare), (
            f"в причине «{text}» есть английские слова — владелец английского "
            f"не понимает"
        )


def test_a_free_account_has_no_reason():
    assert cw.busy_reason_ru(cw.FREE) == ""


# ── Очередь хода: сначала свободные ──────────────────────────────────────────


def test_the_free_accounts_come_first():
    """Случай владельца: 51 аккаунт, свободны двое."""
    ids = list(range(1, 52))
    free = {7, 41}
    busy = {i: {"code": cw.FREE if i in free else cw.BUSY_LEASED} for i in ids}

    order = cw._speaker_order(ids, last_speaker=None, busy=busy)

    assert sorted(order) == ids, "кандидаты потерялись"
    assert set(order[:2]) == free, (
        "свободные аккаунты не в начале очереди — бюджет попыток уйдёт на "
        "занятых, и разогрев промолчит при живом свободном флоте"
    )
    assert set(order[:cw._MAX_SPEAKER_TRIES]) >= free, (
        "свободные не попали в бюджет попыток"
    )


def test_the_order_follows_the_weights():
    ids = [1, 2, 3, 4]
    busy = {1: {"code": cw.BUSY_UNFIT}, 2: {"code": cw.BUSY_COOLDOWN},
            3: {"code": cw.FREE}, 4: {"code": cw.BUSY_LEASED}}
    assert cw._speaker_order(ids, last_speaker=None, busy=busy) == [3, 4, 2, 1]


def test_the_last_speaker_goes_first_if_only_he_is_free():
    """Второй ход подряд лучше тишины."""
    ids = [1, 2, 3]
    busy = {1: {"code": cw.BUSY_LEASED}, 2: {"code": cw.BUSY_LEASED},
            3: {"code": cw.FREE}}
    assert cw._speaker_order(ids, last_speaker=3, busy=busy)[0] == 3


def test_without_readiness_the_order_is_the_old_one():
    order = cw._speaker_order([1, 2, 3, 4, 5], last_speaker=3)
    assert sorted(order) == [1, 2, 3, 4, 5]
    assert order[-1] == 3, (
        "без данных о готовности поведение изменилось — говоривший последним "
        "снова может говорить подряд"
    )


# ── Чтение готовности: один запрос, fail-open ────────────────────────────────


def test_readiness_is_read_in_one_query():
    pool = _Pool([_acc(1), _acc(2, leased=True), _acc(3, status="banned")])
    got = asyncio.run(cw.fleet_availability(pool, [1, 2, 3], owner_id=42))

    assert {k: v["code"] for k, v in got.items()} == {
        1: cw.FREE, 2: cw.BUSY_LEASED, 3: cw.BUSY_UNFIT}
    assert len([q for q in pool.queries if "tg_accounts" in q]) == 1, (
        "по запросу на аккаунт: на флоте в полсотни это полсотни round-trip "
        "подряд перед каждым ходом и каждой отрисовкой экрана"
    )


def test_readiness_judges_by_the_lease_not_the_stuck_flag():
    """`in_operation` остаётся висеть после упавшей операции."""
    src = cw._AVAIL_COLS
    assert "op_lease_until" in src, (
        "занятость читается не по аренде — залипший флаг in_operation выведет "
        "из разогрева свободный аккаунт"
    )
    assert "in_operation" not in src


def test_a_missing_account_is_unfit_not_free():
    pool = _Pool([_acc(1)])
    got = asyncio.run(cw.fleet_availability(pool, [1, 999]))
    assert got[999]["code"] == cw.BUSY_UNFIT
    assert got[999]["label"] == "#999"


def test_readiness_is_fail_open():
    assert asyncio.run(cw.fleet_availability(_Pool(raises=True), [1, 2])) == {}, (
        "сбой чтения останавливает разогрев вместо того, чтобы вернуть прежнее "
        "поведение"
    )


def test_a_different_row_shape_does_not_kill_the_turn():
    """Урезанная выборка не имеет права уронить ход — только порядок."""
    pool = _Pool([{"id": 1}])  # поля готовности отсутствуют
    assert asyncio.run(cw.fleet_availability(pool, [1])) == {}, (
        "выборка другого вида валит чтение готовности наружу — а это весь ход, "
        "а не только порядок кандидатов"
    )


def test_a_barred_account_is_seen_as_such():
    pool = _Pool([_acc(1), _acc(2)], barred=[2])
    got = asyncio.run(cw.fleet_availability(pool, [1, 2], session_id=7))
    assert got[2]["code"] == cw.BUSY_NO_WRITE
    assert "не даёт ему писать" in got[2]["reason"]


def test_quarantine_reaches_the_readiness(monkeypatch):
    async def _one(pool, ids, **kw):
        return {2}

    monkeypatch.setattr(infra_memory, "quarantined_accounts", _one)
    pool = _Pool([_acc(1), _acc(2)])
    got = asyncio.run(cw.fleet_availability(pool, [1, 2]))
    assert got[2]["code"] == cw.BUSY_QUARANTINE


def test_a_broken_health_pulse_does_not_hold_the_fleet(monkeypatch):
    async def _boom(pool, ids, **kw):
        raise RuntimeError("пульс недоступен")

    monkeypatch.setattr(infra_memory, "quarantined_accounts", _boom)
    pool = _Pool([_acc(1)])
    got = asyncio.run(cw.fleet_availability(pool, [1]))
    assert got[1]["code"] == cw.FREE, "недоступный пульс остановил разогрев"


# ── Отбор участников под потолок ─────────────────────────────────────────────


def test_the_owners_fleet_of_fiftyone():
    """21 свободный, 10 под арендой, 20 негодных, потолок 30."""
    accounts = ([_acc(i) for i in range(1, 22)]
                + [_acc(i, leased=True) for i in range(22, 32)]
                + [_acc(i, status="banned") for i in range(32, 52)])
    pool = _Pool(accounts)

    chosen, detail = asyncio.run(
        cw.pick_fleet(pool, [a["id"] for a in accounts], 30))

    assert len(chosen) == 30
    assert set(range(1, 22)) <= set(chosen), (
        "свободные аккаунты не вошли в сессию — ровно то, на что жаловался "
        "владелец"
    )
    assert not (set(chosen) & set(range(32, 52))), (
        "забаненные аккаунты заняли места в сессии: они не заговорят никогда, "
        "а мест всего 30"
    )
    assert sum(d["count"] for d in detail) == 21
    assert any("потолок" in d["reason"] for d in detail), (
        "владельцу не сказали, что часть аккаунтов просто не поместилась"
    )
    assert any("не годится" in d["reason"] or "негоден" in d["reason"]
               for d in detail)


def test_a_busy_account_still_gets_a_seat_when_there_is_room():
    """Занятость аренды проходит за минуты — исключать за неё нельзя."""
    accounts = [_acc(1), _acc(2, leased=True), _acc(3, cooling=True)]
    chosen, detail = asyncio.run(cw.pick_fleet(_Pool(accounts), [1, 2, 3], 30))
    assert chosen == [1, 2, 3]
    assert detail == []


def test_trust_breaks_the_tie_not_the_upload_date():
    accounts = [_acc(1, trust=10), _acc(2, trust=90), _acc(3, trust=50)]
    chosen, _ = asyncio.run(cw.pick_fleet(_Pool(accounts), [1, 2, 3], 2))
    assert chosen == [2, 3], (
        "при равной занятости выбор снова идёт по id, то есть по дате заливки"
    )


def test_the_cap_is_respected():
    accounts = [_acc(i) for i in range(1, 51)]
    chosen, _ = asyncio.run(
        cw.pick_fleet(_Pool(accounts), [a["id"] for a in accounts], 30))
    assert len(chosen) == 30, "потолок флота на чат перестал действовать"


def test_the_pick_is_fail_open():
    ids = list(range(1, 51))
    chosen, detail = asyncio.run(cw.pick_fleet(_Pool(raises=True), ids, 30))
    assert chosen == ids[:30], "нечитаемая готовность обнулила выбор"
    assert detail == []


def test_a_fleet_of_only_dead_accounts_picks_nobody():
    """Пустой выбор — чтобы ручка сказала правду, а не создала молчащую сессию."""
    accounts = [_acc(i, status="banned") for i in (1, 2, 3)]
    chosen, detail = asyncio.run(cw.pick_fleet(_Pool(accounts), [1, 2, 3], 30))
    assert chosen == []
    assert sum(d["count"] for d in detail) == 3


# ── Ход и экран пользуются тем же чтением ────────────────────────────────────


def test_the_turn_builds_its_queue_from_readiness():
    import inspect

    src = inspect.getsource(cw._process_session)
    assert "fleet_availability" in src, (
        "ход снова выбирает кандидатов вслепую"
    )
    i = src.index("_speaker_order")
    assert "_avail" in src[i:i + 200], (
        "готовность прочитана, но в очередь не передана"
    )


def test_the_turn_names_every_reason_when_nobody_can_speak():
    accounts = [_acc(1, status="banned"), _acc(2, has_session=False)]
    pool = _Pool(accounts)
    asyncio.run(cw._process_session(
        pool, {"id": 7, "owner_id": 42, "account_ids": [1, 2], "mode": "mixed"}))

    said = " ".join(str(a) for _sql, a in pool.executed)
    assert "не годится" in said and "перезалейте" in said, (
        f"сводная причина не называет, что именно мешает каждому: {said}"
    )
    assert any("UPDATE chat_warmup_sessions" in sql for sql, _a in pool.executed), (
        "сессия не отложена — попытка повторится через 20 секунд"
    )


# Этот тест заменяет прежний грep по исходнику diagnose: признак «выведен из
# разогрева» переехал в общее чтение готовности (fleet_availability +
# busy_reason_ru), поэтому проверяем не текст функции, а ПОВЕДЕНИЕ экрана — это
# строго сильнее: прежняя проверка прошла бы и на функции, которая строку
# упоминает, но на экран не выводит.
def test_the_screen_names_a_barred_account():
    pool = _Pool([_acc(1), _acc(2)], barred=[2])
    out = asyncio.run(cw.diagnose(pool, 42, {"id": 7, "account_ids": [1, 2]}))

    by_label = {a["label"]: a for a in out["accounts"]}
    assert not by_label["acc2"]["ok"]
    assert "не даёт ему писать" in by_label["acc2"]["reason"], (
        "выведенный из разогрева выглядит на экране здоровым и свободным"
    )
    assert out["ready"] == 1


def test_the_screen_does_not_claim_the_fleet_is_ready_when_it_cannot_read():
    out = asyncio.run(cw.diagnose(_Pool(raises=True), 42,
                                  {"id": 7, "account_ids": [1, 2, 3]}))
    assert out["ready"] == 0
    assert any("не удалось" in g.lower() for g in out["global"]), (
        "сбой чтения отрисовал весь флот готовым — «все готовы, а флот молчит» "
        "это ровно тот ответ, из-за которого экран и появился"
    )


def test_the_session_is_composed_by_readiness():
    import os

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "services", "mini_app_warmup.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "pick_fleet" in src, (
        "состав сессии снова срезается потолком вслепую"
    )
    assert "acc_ids[:_CW_FLEET_CAP]" not in src, (
        "остался слепой срез списка: в тридцать мест попадут аккаунты с самыми "
        "малыми id, то есть самые старые, а не свободные"
    )
