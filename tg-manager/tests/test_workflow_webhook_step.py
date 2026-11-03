"""Шаг «Вебхук»: что повторять, сколько ждать, что сохранять из ответа.

ЧТО БЫЛО. Шаг делал ровно одну попытку: сервис лёг на минуту — заявка не
дошла, а прогон шёл дальше, как будто всё хорошо. Ответ сервиса (номер
заявки, имя менеджера) выбрасывался, подставить его в сообщение было нельзя.
Путь исполнения целиком — в tests/test_workflow_runtime_postgres.py.
"""
import pytest

from services import workflow_engine as wf
from services import workflow_runtime as rt


def test_only_temporary_failures_are_retried():
    assert rt.webhook_retryable(None, "network")
    assert rt.webhook_retryable(503, "")
    assert rt.webhook_retryable(429, "")
    assert not rt.webhook_retryable(400, "")
    assert not rt.webhook_retryable(404, "")
    assert not rt.webhook_retryable(None, "blocked")


def test_the_pause_grows_and_is_capped():
    waits = [rt.webhook_backoff(i) for i in range(8)]
    assert waits == sorted(waits) and waits[0] == 30 and waits[-1] == 3600


def test_the_answer_becomes_flat_safe_variables():
    out = rt.response_vars("crm", {"id": 7, "ok": True, "Deal-Url": "https://x",
                                   "manager": {"name": "Олег", "deep": {"x": 1}},
                                   "items": [1, 2], "9bad": 1})
    assert out == {"crm_id": "7", "crm_ok": "да", "crm_deal_url": "https://x",
                   "crm_manager_name": "Олег", "crm_9bad": "1"}
    assert rt.response_vars("crm", "принято") == {"crm": "принято"}
    assert rt.response_vars("crm", None) == {}
    many = rt.response_vars("x", {f"k{i}": i for i in range(100)})
    assert len(many) == 20


def test_the_step_keeps_its_retry_settings():
    step = wf.validate_step({"type": "webhook", "url": "https://a.example/h",
                             "save_as": "CRM", "retries": "2", "else_goto": "4"})
    assert step == {"type": "webhook", "url": "https://a.example/h", "retries": 2,
                    "save_as": "crm", "else_goto": 4}
    assert wf.validate_step({"type": "webhook", "url": "https://a.example/h"})["retries"] == 3
    with pytest.raises(ValueError):
        wf.validate_step({"type": "webhook", "url": "https://a.example/h", "retries": 9})
    with pytest.raises(ValueError):
        wf.validate_step({"type": "webhook", "url": "https://a.example/h", "save_as": "1x"})
