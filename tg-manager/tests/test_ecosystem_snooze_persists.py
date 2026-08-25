"""Заглушка уведомлений экосистем обязана РАБОТАТЬ, а не только отвечать.

ЧТО БЫЛО СЛОМАНО. Кнопка «😴 24ч» под уведомлением Ecosystem Copilot писала срок
в словарь уровня модуля и больше никуда. Читает этот словарь фоновый цикл — но
кнопку нажимают в процессе, который обрабатывает апдейты бота, а цикл живёт в
процессе-воркере (INFRAGRAM_ROLE=worker), где словарь всегда пуст. Бот отвечал
«Уведомления отложены на 24ч», и уведомления продолжали приходить. Даже в одном
процессе заглушка не переживала перезапуск.

Ровно этой жалобой всё и началось: «я устал от постоянных уведомлений и хочу
заглушить их на период». Поэтому здесь проверяется не «функция что-то вернула»,
а сквозной сценарий: заглушил в одном процессе → второй молчит.
"""
from __future__ import annotations

import asyncio
import time

import pytest


class _FakePool:
    """Минимальный platform_settings в памяти — ровно то, что трогает снуз."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.fail = False

    async def execute(self, sql, *args):
        if self.fail:
            raise RuntimeError("БД недоступна")
        if "platform_settings" in sql and args:
            self.kv[str(args[0])] = str(args[1])

    async def fetch(self, sql, *args):
        if self.fail:
            raise RuntimeError("БД недоступна")
        prefix = str(args[0]).rstrip("%") if args else ""
        return [{"key": k, "value": v} for k, v in self.kv.items()
                if k.startswith(prefix)]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def eco():
    from services import ecosystem_copilot as ec
    ec._snooze_until.clear()
    yield ec
    ec._snooze_until.clear()


def test_snooze_set_in_one_process_silences_another(eco):
    """Главное: заглушил в боте — молчит воркер.

    Второй процесс моделируется честно: его память пуста, он знает только БД.
    """
    pool = _FakePool()
    _run(eco.snooze_ecosystem_alerts_db(pool, 777, 24))

    eco._snooze_until.clear()          # ← другой процесс: своя пустая память
    assert not eco.is_snoozed(777), "предусловие: у второго процесса память пуста"

    _run(eco.reload_snoozes_from_db(pool))
    assert eco.is_snoozed(777), (
        "заглушка не доехала до процесса, который шлёт уведомления — кнопка "
        "«отложить» отвечает, но ничего не откладывает")


def test_snooze_survives_restart(eco):
    """Перезапуск не должен молча включать уведомления обратно."""
    pool = _FakePool()
    _run(eco.snooze_ecosystem_alerts_db(pool, 778, 6))
    eco._snooze_until.clear()          # ← рестарт процесса
    _run(eco.reload_snoozes_from_db(pool))
    assert eco.is_snoozed(778)


def test_clearing_snooze_reaches_other_processes(eco):
    """«Возобновить» тоже обязано доехать: иначе тишина затянется на весь срок."""
    pool = _FakePool()
    _run(eco.snooze_ecosystem_alerts_db(pool, 779, 24))
    _run(eco.clear_snooze_db(pool, 779))

    eco._snooze_until[779] = time.time() + 3600   # у соседа ещё старое значение
    _run(eco.reload_snoozes_from_db(pool))
    assert not eco.is_snoozed(779), "снятие заглушки не доехало до соседнего процесса"


def test_expired_snooze_is_dropped_on_reload(eco):
    """Истёкший срок не должен оживать при загрузке из БД."""
    pool = _FakePool()
    pool.kv[f"{eco._SNOOZE_PREFIX}780"] = str(time.time() - 10)
    _run(eco.reload_snoozes_from_db(pool))
    assert not eco.is_snoozed(780)


def test_db_outage_does_not_unmute(eco):
    """Молчащая БД не должна оборачиваться шквалом уведомлений.

    Если при сбое чтения затирать известные заглушки, единственный сбой БД
    разбудит все отложенные уведомления сразу.
    """
    pool = _FakePool()
    _run(eco.snooze_ecosystem_alerts_db(pool, 781, 24))
    pool.fail = True
    _run(eco.reload_snoozes_from_db(pool))
    assert eco.is_snoozed(781), "сбой чтения снял заглушку — уведомления вернутся"


def test_garbage_rows_do_not_break_reload(eco):
    """Мусор в настройках не должен ронять загрузку остальных заглушек."""
    pool = _FakePool()
    pool.kv[f"{eco._SNOOZE_PREFIX}не-число"] = "123"
    pool.kv[f"{eco._SNOOZE_PREFIX}782"] = "не-время"
    pool.kv[f"{eco._SNOOZE_PREFIX}783"] = str(time.time() + 3600)
    _run(eco.reload_snoozes_from_db(pool))
    assert eco.is_snoozed(783)


def test_loop_reloads_snoozes_every_cycle():
    """Перечитывание — внутри цикла, а не только на старте.

    Заглушку ставят между кругами; прочитанная один раз на старте, она не
    подействует до следующего перезапуска процесса.
    """
    import ast
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "services", "ecosystem_copilot.py"),
               encoding="utf-8").read()
    body = None
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "run_ecosystem_copilot_loop":
            body = "\n".join(src.split("\n")[node.lineno - 1:node.end_lineno])
    assert body, "цикл уведомлений не найден"
    i_while = body.index("while True")
    assert "reload_snoozes_from_db" in body[i_while:], (
        "заглушки перечитываются вне цикла — поставленная между кругами не сработает")


def test_handlers_persist_snooze_not_only_memory():
    """Храповик: обработчики кнопок обязаны писать в БД.

    Именно возврат к «только в памяти» и делает кнопку бутафорией — эту ошибку
    легко внести обратно, поэтому она зафиксирована тестом.
    """
    import ast
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "bot", "handlers", "ecosystems.py"),
               encoding="utf-8").read()
    lines = src.split("\n")
    found = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name in ("cb_eco_snooze", "cb_eco_snooze_clear"):
            found[node.name] = "\n".join(lines[node.lineno - 1:node.end_lineno])
    assert set(found) == {"cb_eco_snooze", "cb_eco_snooze_clear"}, found.keys()

    assert "snooze_ecosystem_alerts_db" in found["cb_eco_snooze"], (
        "кнопка «отложить» снова пишет только в память процесса")
    assert "clear_snooze_db" in found["cb_eco_snooze_clear"], (
        "кнопка «возобновить» снова снимает заглушку только в этом процессе")
