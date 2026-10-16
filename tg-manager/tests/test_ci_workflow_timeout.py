"""Храповик: зависший полный прогон CI обязан завершаться ограниченной ошибкой."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "tests.yml"
MAX_REASONABLE_TIMEOUT_MINUTES = 30


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
    assert 1 <= timeout <= MAX_REASONABLE_TIMEOUT_MINUTES, (
        "таймаут полного pytest-job должен быть положительным и не больше "
        f"{MAX_REASONABLE_TIMEOUT_MINUTES} минут, сейчас: {timeout}"
    )
