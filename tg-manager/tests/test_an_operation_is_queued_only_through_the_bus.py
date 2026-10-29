"""Операция ставится в очередь только через `operation_bus.submit()`.

ПОЧЕМУ. На пути `submit()` висит всё, что делает постановку безопасной:
проверка тарифа (`min_plan`), окно идемпотентности против двойного тапа,
предохранитель Ban Weather, реестр типов операций (неизвестный тип не
поставится), запись параметров в проверенном виде. Прямой
`INSERT INTO operation_queue` обходит ВСЁ это разом: операция появляется в
очереди, воркер её берёт, и ни один гейт не сработал.

Снаружи это выглядит хуже всего у платных операций и у инвайта: free-владелец
получает платную работу, а приостановленный предохранителем тип операции
стартует как ни в чём не бывало.

СОСТОЯНИЕ: обходов НЕТ ни одного, и этот ноль проверен — ниже есть
самопроверка на заведомо больном примере. Тесты вставляют строки напрямую
намеренно (им нужна готовая очередь без гейтов), поэтому перепись смотрит
только на рабочий код.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Где живёт рабочий код. tests/ исключены намеренно: фикстуре нужна очередь, а
# не гейты.
_PROD_DIRS = ("services", "bot", "database")
# Единственное место, которому постановка разрешена.
_THE_BUS = os.path.join("services", "operation_bus.py")
# Обходы, разрешённые СОЗНАТЕЛЬНО, с причиной. Пусто — и это правильное
# состояние: пустой реестр значит «обходов нет», а не «про них забыли».
QUEUEING_BYPASS_ON_PURPOSE: dict[str, str] = {}

_QUEUES = re.compile(r"insert\s+into\s+operation_queue", re.I)
# Второй способ положить строки в таблицу мимо INSERT — массовая загрузка.
_BULK = re.compile(r"copy_records_to_table\s*\(", re.I)


def _prod_files() -> dict[str, str]:
    out = {}
    for d in _PROD_DIRS:
        for base, _dirs, files in os.walk(os.path.join(ROOT, d)):
            if "__pycache__" in base:
                continue
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(base, f)
                rel = os.path.relpath(path, ROOT)
                with open(path, encoding="utf-8") as fh:
                    out[rel] = fh.read()
    return out


def queueing_bypasses(files: dict[str, str]) -> list[str]:
    bad = []
    for rel, src in sorted(files.items()):
        if rel == _THE_BUS or rel in QUEUEING_BYPASS_ON_PURPOSE:
            continue
        if _QUEUES.search(src) or (_BULK.search(src) and "operation_queue" in src):
            bad.append(rel)
    return bad


def test_nothing_queues_an_operation_behind_the_bus():
    bad = queueing_bypasses(_prod_files())
    assert not bad, (
        f"операция ставится в очередь мимо operation_bus.submit(): {bad}. "
        "Этот путь обходит проверку тарифа, окно идемпотентности, "
        "предохранитель и реестр типов — операция появится в очереди, воркер "
        "её возьмёт, и ни один гейт не сработает")


def test_the_census_bites():
    """Измеритель, молчащий на заведомо больном примере, бесполезен."""
    sick = {"services/made_up.py":
            "async def f(pool):\n"
            "    await pool.execute(\n"
            "        \"INSERT INTO operation_queue(owner_id, op_type) \"\n"
            "        \"VALUES($1,$2)\", 1, 'mass_invite')\n"}
    assert queueing_bypasses(sick) == ["services/made_up.py"]


def test_the_census_sees_the_bus_itself():
    """Если постановка исчезла из шины, перепись стала бы вечно-зелёной."""
    files = _prod_files()
    assert _THE_BUS in files, "operation_bus.py не прочитан — перепись ослепла"
    assert _QUEUES.search(files[_THE_BUS]), (
        "в operation_bus.py больше нет постановки в очередь — значит она "
        "переехала, и перепись сторожит пустоту")


def test_the_census_ignores_case_and_bulk_load():
    """Выборка по способу: строчный INSERT и массовая загрузка — те же обходы."""
    lower = {"services/a.py": "q = 'insert into operation_queue(owner_id) values($1)'"}
    bulk = {"services/b.py":
            "await conn.copy_records_to_table('operation_queue', records=rows)"}
    assert queueing_bypasses(lower) == ["services/a.py"]
    assert queueing_bypasses(bulk) == ["services/b.py"]


def test_every_listed_bypass_carries_a_reason():
    for name, reason in sorted(QUEUEING_BYPASS_ON_PURPOSE.items()):
        assert reason and re.search(r"[а-яА-Я]", reason), (
            f"{name}: причина обязана быть по-русски — её читает владелец кода")
        assert len(reason) > 40, f"{name}: причина слишком коротка: {reason!r}"


def test_no_listed_bypass_is_dead():
    """Файл переписали — запись перестаёт что-либо значить."""
    files = _prod_files()
    gone = sorted(n for n in QUEUEING_BYPASS_ON_PURPOSE if n not in files)
    assert not gone, f"в реестре обходов остались несуществующие файлы: {gone}"
