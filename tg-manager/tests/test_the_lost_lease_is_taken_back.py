"""Пропавшая аренда занятого аккаунта берётся заново, а не только логируется.

Аренда (`op_lease_owner`/`op_lease_until`) — единственный межпроцессный арбитр
живых сессий. Heartbeat `renew_leases()` продлевает её тем аккаунтам, которые
процесс РЕАЛЬНО держит в памяти, и давно умел замечать обратное расхождение:
в памяти аккаунт есть, а строки с нашей арендой в БД уже нет. Причины бытовые —
кто-то снял `in_operation`, перебил `op_lease_owner`, или наши продления не
доходили до базы дольше TTL и владелец разморозил флот кнопкой в панели.

Но заметить было всё, что происходило: одна строка в логе и счётчик. Сессия
продолжала работать БЕЗ аренды, и через TTL аккаунт законно забирал другой
исполнитель — это AUTH_KEY_DUPLICATED, самая дорогая поломка продукта
(мёртвый auth-key, аккаунт не восстановить).

Поэтому на расхождении аренда берётся заново тем же арбитром `_db_claim`:
он fail-closed и живую аренду соседа не отбирает, значит лечение безопасно.
Не вернулось — вот это настоящий конфликт, его и записываем.
"""
from __future__ import annotations

import asyncio

import pytest

import services.op_worker as ow

RENEWED = 101        # аренда на месте — обычное продление
DRIFTED = 202        # в памяти держим, а аренды в БД нет


class _Pool:
    """Пул, который отвечает по состоянию аренды и записывает захваты.

    `leased` — аккаунты, чья строка в БД числится за ЭТИМ процессом;
    `claimable` — те, которых арбитр отдаст по запросу (свободные или наши).
    """

    def __init__(self, leased: set[int], claimable: set[int]):
        self.leased = set(leased)
        self.claimable = set(claimable)
        self.claims: list[list[int]] = []

    async def fetch(self, query: str, *args):
        # Список id — единственный аргумент-список: у продления он последний,
        # у захвата первый, и привязка к позиции врёт на одном из двух.
        ids = next((sorted(int(i) for i in a)
                    for a in args if isinstance(a, (list, tuple, set))), [])
        if "SET in_operation" in query:
            # Запрос захвата (единственный, кто ставит флаг): арбитр отдаёт
            # только то, что свободно.
            self.claims.append(ids)
            taken = sorted(set(ids) & self.claimable)
            self.leased.update(taken)
            return [{"id": i} for i in taken]
        # Запрос продления: продлевается только своя живая аренда.
        return [{"id": i} for i in sorted(set(ids) & self.leased)]


@pytest.fixture
def stand(monkeypatch):
    """Процесс держит в памяти два аккаунта; состояние БД задаёт тест."""
    saved_pool = ow._db_pool
    saved_in_use = set(ow._accounts_in_use)
    seen: list[tuple[str, float]] = []
    monkeypatch.setattr(
        ow, "_reliability_metric",
        lambda name, value=1.0, **lbl: seen.append((name, float(value))))
    ow._accounts_in_use.clear()
    ow._accounts_in_use.update({RENEWED, DRIFTED})
    try:
        yield seen
    finally:
        ow._db_pool = saved_pool
        ow._accounts_in_use.clear()
        ow._accounts_in_use.update(saved_in_use)


def _counted(seen, name: str) -> float:
    return sum(v for n, v in seen if n == name)


def test_a_vanished_lease_is_taken_back_in_the_same_beat(stand):
    """Аренда пропала, второго держателя нет — берём заново тем же тактом."""
    pool = _Pool(leased={RENEWED}, claimable={DRIFTED})
    ow._db_pool = pool

    confirmed = asyncio.run(ow.renew_leases())

    assert pool.claims == [[DRIFTED]], (
        "аренда занятого аккаунта пропала из БД, а heartbeat не попытался взять "
        f"её заново (запросы захвата: {pool.claims}) — сессия работает без "
        "арбитра, и через TTL аккаунт законно заберёт другой исполнитель: "
        "это AUTH_KEY_DUPLICATED")
    assert DRIFTED in pool.leased, "аренда так и не восстановлена в БД"
    assert confirmed == 2, (
        f"heartbeat отчитался о {confirmed} подтверждённых арендах из двух")
    assert _counted(stand, "infragram_account_lease_regained_total") == 1, (
        f"восстановление аренды не оставило следа: {stand}")
    assert _counted(stand, "infragram_account_lease_renew_drift_total") == 0, (
        "вылеченное расхождение записано как неустранимый конфликт — по этому "
        f"счётчику поднимают тревогу: {stand}")


def test_a_live_lease_of_a_neighbour_is_not_stolen_but_is_recorded(stand):
    """Аренду держит сосед — не отбираем, но молчать об этом нельзя."""
    pool = _Pool(leased={RENEWED}, claimable=set())
    ow._db_pool = pool

    confirmed = asyncio.run(ow.renew_leases())

    assert DRIFTED not in pool.leased, (
        "живая аренда другого исполнителя отобрана — именно это и есть "
        "AUTH_KEY_DUPLICATED, а не защита от него")
    assert confirmed == 1, f"подтверждённой считается чужая аренда: {confirmed}"
    assert _counted(stand, "infragram_account_lease_renew_drift_total") == 1, (
        f"настоящий конфликт аренды не записан: {stand}")
    assert _counted(stand, "infragram_account_lease_regained_total") == 0, (
        f"неудавшийся захват записан как восстановление: {stand}")


def test_the_whole_fleet_in_order_asks_nobody(stand):
    """Пробник: когда расхождения нет, захватов быть не должно вовсе."""
    pool = _Pool(leased={RENEWED, DRIFTED}, claimable={RENEWED, DRIFTED})
    ow._db_pool = pool

    assert asyncio.run(ow.renew_leases()) == 2
    assert pool.claims == [], (
        "heartbeat дёргает арбитра на здоровом флоте — тогда проверка выше "
        f"зелёная по случайности: {pool.claims}")
    assert _counted(stand, "infragram_account_lease_regained_total") == 0


def test_the_retake_goes_through_the_arbiter():
    """Храповик: заново аренда берётся ТОЛЬКО через `_db_claim`.

    Соблазн — дописать в heartbeat свой UPDATE с `in_operation = TRUE`. Такой
    запрос не умеет главного, что умеет арбитр: не трогать живую аренду
    соседа. Условие живёт в одном месте, и обойти его нельзя.
    """
    import ast
    import inspect

    src = inspect.getsource(ow.renew_leases)
    body = ast.parse(src.lstrip()).body[0]
    doc = ast.get_docstring(body, clean=False) or ""
    calls = {
        n.func.id for n in ast.walk(body)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_db_claim" in calls, (
        "heartbeat не обращается к арбитру аренды — значит либо расхождение не "
        "лечится, либо лечится в обход единственного места, где написано «чужую "
        "живую аренду не отбирать»")
    for node in ast.walk(body):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value != doc):
            continue
        # Смотрим ТОЛЬКО список присваиваний (между SET и WHERE): сам запрос
        # продления упоминает `in_operation = TRUE` в условии — это чтение
        # флага, а не выставление, и запрещать его нечего.
        head, _, rest = node.value.partition("SET")
        assigns = rest.partition("WHERE")[0] if rest else ""
        assert "in_operation" not in assigns, (
            "в heartbeat появился собственный запрос, выставляющий "
            f"in_operation — он не проверяет чужую живую аренду: {assigns[:80]}")
