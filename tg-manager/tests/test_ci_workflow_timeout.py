"""Храповик: прогон CI обязан и завершаться по времени, и успевать целиком.

Предел нужен с двух сторон, и это выяснилось дорогой ценой.

СВЕРХУ: зависший тест без предела держит runner до стандартного шестичасового
лимита GitHub и при частых push накапливает очередь прогонов вместо быстрого
красного сигнала. Отсюда верхняя граница.

СНИЗУ (23.10.2026): предел стоял 20 минут и был написан, когда полный набор
занимал около восьми. Набор дорос до двадцати — и прогоны начали обрываться.
GitHub помечает убитую по таймауту работу как CANCELLED, а не как failure,
поэтому в списке прогонов это выглядело как «отменили лишний прогон»:
`c650b3b` прошёл за 16м43с, `69fe7e1` — за 16м10с, а `ab76e05`, `83526a8` и
`7ba2711` оборвались ровно на 20м18с каждый. Ветка автодеплойная, то есть код
уезжал в прод вообще без прогона тестов при ~5500 написанных.

Прежняя версия этого файла держала только верхнюю границу, да ещё и 30 минут —
то есть сама запрещала поднять предел до рабочего. Мерка, написанная под
вчерашний размер, со временем начинает защищать не то.

Поэтому здесь теперь обе границы и требование печатать самые долгие тесты:
без `--durations` следующий рост снова будет не виден до немой «отмены».
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "tests.yml"

# Снизу: измеренное время полного набора на 23.10.2026 — около 20 минут, и оно
# растёт. Ниже сорока предел снова начнёт обрывать прогоны молча.
MIN_TIMEOUT_MINUTES = 40
# Сверху: зависший тест обязан умереть за обозримое время, а не держать runner.
MAX_TIMEOUT_MINUTES = 60


def _pytest_job_block(source: str) -> str:
    match = re.search(
        r"(?ms)^  pytest:\s*\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\s*$|\Z)",
        source,
    )
    assert match, "в workflow не найден обязательный job `pytest`"
    return match.group("body")


def test_full_ci_job_has_bounded_timeout():
    source = WORKFLOW.read_text(encoding="utf-8")
    job = _pytest_job_block(source)
    match = re.search(r"(?m)^    timeout-minutes:\s*(\d+)\s*$", job)

    assert match, (
        "у полного pytest-job нет `timeout-minutes`: зависший тест может занять "
        "runner на несколько часов"
    )
    timeout = int(match.group(1))
    assert timeout >= MIN_TIMEOUT_MINUTES, (
        f"предел {timeout} мин меньше {MIN_TIMEOUT_MINUTES}: полный набор в него "
        "не влезает, и GitHub оборвёт прогон, показав его как «cancelled» — "
        "красный код уедет в прод незамеченным"
    )
    assert timeout <= MAX_TIMEOUT_MINUTES, (
        f"предел {timeout} мин больше {MAX_TIMEOUT_MINUTES}: зависший тест "
        "будет держать runner слишком долго"
    )


def test_the_run_names_its_slowest_tests():
    """Рост времени обязан быть виден в каждом прогоне, а не после обрыва."""
    source = WORKFLOW.read_text(encoding="utf-8")
    job = _pytest_job_block(source)
    run = re.search(r"(?m)^\s+python -m pytest tests/.*$", job)
    assert run, "в job не найден запуск полного набора"
    assert "--durations" in run.group(0), (
        "прогон не печатает самые долгие тесты: следующий раз, когда набор "
        "перерастёт предел, это снова будет не видно до немой «отмены»"
    )
