"""Массовая отмена и повтор операций и сбой сводки показывали сырой текст
исключения (английский «Failed to fetch» и подобное). Теперь через errRu."""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/index.html").read_text(encoding="utf-8")


def test_no_raw_error_text_left():
    assert "lastErr.message" not in SRC
    assert "esc((e && e.message) ||" not in SRC
    assert SRC.count("${lastErr ? ': ' + errRu(lastErr, 'ошибка') : ''}") == 2
