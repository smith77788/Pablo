"""Запуск сценария входящим вебхуком: разбор тела (чужие данные)."""
from services import workflow_engine as wf
from services import workflow_runtime as rt


def test_user_id_and_vars_are_taken_from_the_body():
    uid, v = rt.inbound_request({"user_id": "42", "vars": {"Order": "A-17", "sum": 990}})
    assert uid == 42 and v == {"order": "A-17", "sum": "990"}


def test_flat_body_becomes_vars_when_no_vars_key():
    uid, v = rt.inbound_request({"chat_id": 7, "city": "Москва", "ok": True})
    assert uid == 7 and v == {"city": "Москва", "ok": "true"}


def test_junk_is_dropped():
    uid, v = rt.inbound_request({"user_id": True, "vars": {
        "nested": {"a": 1}, "list": [1], "_x": "1", "bad key": "1", "paid": "1",
        "last_button": "x", "none": None, "long": "я" * 900}})
    assert uid is None
    assert v == {"long": "я" * rt.INBOUND_MAX_VALUE}
    assert rt.inbound_request("text") == (None, {})
    assert rt.inbound_request({"user_id": -5})[0] is None


def test_vars_are_capped():
    _, v = rt.inbound_request({"user_id": 1, "vars": {f"k{i}": i for i in range(50)}})
    assert len(v) == rt.INBOUND_MAX_VARS


def test_the_trigger_is_offered_and_needs_no_value():
    assert "webhook" in rt.TRIGGER_TYPES
    assert rt.validate_trigger("webhook", "") == ("webhook", None)
    assert wf.inbound_url("https://x.io/", "abc") == "https://x.io/webhook/scenario/abc"
    assert wf.inbound_url("https://x.io", None) == ""
