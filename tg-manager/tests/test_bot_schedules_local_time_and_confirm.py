"""Запланированные рассылки бота: время показано в поясе устройства без
ложной подписи «UTC», а отмена спрашивает подтверждение."""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/index.html").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def _sched_screen() -> str:
    """Экран расписания целиком: загрузка страницы и отрисовка.

    Список стал листаться, и разметка уехала в `schRender`. Мерка, читающая
    только `loadSchedules`, видит запрос без разметки и объявляет пропавшим
    то, что стоит рядом.
    """
    return _fn("loadSchedules") + "\n" + _fn("schRender")


def test_local_time_is_not_labelled_utc():
    body = _sched_screen()
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


def test_repeat_is_shown_in_list():
    import json
    import subprocess
    js = _fn("schRepeatLabel") + "console.log(JSON.stringify([0,null,1440,10080,2880,120,45].map(schRepeatLabel)))"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["", "", "ежедневно", "еженедельно", "каждые 2 дн.", "каждые 2 ч", "каждые 45 мин"]
    assert "schRepeatLabel(s.repeat_interval_min)" in _sched_screen()
    srv = (Path(__file__).resolve().parents[1] / "services/mini_app_bots.py").read_text(encoding="utf-8")
    assert "COALESCE(repeat_interval_min, 0) AS repeat_interval_min" in srv
