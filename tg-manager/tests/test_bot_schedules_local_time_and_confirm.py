"""Запланированные рассылки бота: время показано в поясе устройства без
ложной подписи «UTC», а отмена спрашивает подтверждение."""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/index.html").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def test_local_time_is_not_labelled_utc():
    body = _fn("loadSchedules")
    assert "toLocaleString('ru-RU'" in body
    assert "${dt} UTC" not in body


def test_cancel_asks_first():
    body = _fn("cancelSchedule")
    assert body.index("askConfirm(") < body.index("/cancel'")


def test_form_does_not_ask_for_utc():
    # submitSchedule читает поле как МЕСТНОЕ время (new Date(dateVal) без 'Z'),
    # значит и подпись поля обязана говорить о местном времени.
    i = SRC.index('id="schDate"')
    around = SRC[i - 300:i + 300]
    assert "UTC+0" not in around and "(UTC)" not in around
    assert "new Date(dateVal);  // datetime-local — ЛОКАЛЬНОЕ время" in SRC
