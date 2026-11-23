"""Рабочие часы автоответа сервер сверяет с часом ВЛАДЕЛЬЦА
(auto_responder._owner_hour по notification_settings.tz), а форма просила
вводить их «(UTC)» — владелец в Киеве честно сдвигал окно на два-три часа
и получал ответы не в те часы."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "mini_app/index.html").read_text(encoding="utf-8")
SRV = (ROOT / "services/auto_responder.py").read_text(encoding="utf-8")


def test_label_matches_server_clock():
    assert "_owner_hour_box[0]" in SRV
    i = SRC.index('id="arFromHour"')
    around = SRC[i - 300:i + 500]
    assert "(UTC)" not in around and "по вашему поясу" in around


def test_rule_card_says_hours_in_russian():
    assert "${r.active_to_hour}h`" not in SRC
    assert "${r.active_to_hour} ч`" in SRC
