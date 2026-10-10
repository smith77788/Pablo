"""Регрессия: GPT AI-ответ был мёртвым бэкендом (раздел 7 паритета TE)."""
from __future__ import annotations
import inspect
from bot import keyboards
from bot.handlers import auto_reply
from services import auto_responder

def test_action_menu_has_ai_reply_button():
    assert 'action="act_ai_reply"' in inspect.getsource(keyboards.automation_action_menu)

def test_handler_saves_ai_reply_action():
    src = inspect.getsource(auto_reply)
    assert 'F.action == "act_ai_reply"' in src and 'action_type="send_ai_reply"' in src

def test_runtime_executes_ai_reply():
    assert 'send_ai_reply' in inspect.getsource(auto_responder)
