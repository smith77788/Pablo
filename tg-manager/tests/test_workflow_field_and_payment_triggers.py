"""Сценарий запускается, когда человек заполнил поле или оплатил.

ЧТО БЫЛО. Запустить сценарий после оплаты («спасибо, вот доступ») или после
того, как человек оставил телефон, можно было только через «Событие» с
ручным вводом служебного имени (`payment.received`), а поле — никак: событие
не говорило, КАКОЕ поле заполнено. Сценарий, который сам пишет поле, ещё и
запускал бы сам себя по кругу.
"""
import pytest

from services.workflow_runtime import trigger_matches, validate_trigger


def _ev(etype, **data):
    return {"event_type": etype, "data": data}


def test_a_filled_field_starts_only_its_scenario():
    wf = {"id": 5, "trigger_type": "field_set", "trigger_value": "Phone"}
    assert trigger_matches(wf, _ev("contact.field_set", field="phone", value="+7"))
    assert not trigger_matches(wf, _ev("contact.field_set", field="email", value="a@b.c"))
    # Поле, записанное этим же сценарием, не перезапускает его.
    assert not trigger_matches(wf, _ev("contact.field_set", field="phone", workflow_id=5))
    assert trigger_matches(wf, _ev("contact.field_set", field="phone", workflow_id=9))


def test_a_payment_starts_the_after_sale_scenario_but_not_itself():
    wf = {"id": 7, "trigger_type": "paid", "trigger_value": None}
    assert trigger_matches(wf, _ev("payment.received", amount=100))
    assert trigger_matches(wf, _ev("payment.received", workflow_id=3))
    assert not trigger_matches(wf, _ev("payment.received", workflow_id=7))
    assert not trigger_matches(wf, _ev("order.created"))


def test_the_screen_values_are_checked():
    assert validate_trigger("paid", "") == ("paid", None)
    assert validate_trigger("field_set", "phone") == ("field_set", "phone")
    with pytest.raises(ValueError):
        validate_trigger("field_set", "")
