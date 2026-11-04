"""Уведомления владельцу и оператору: текст постороннего — не HTML.

Лид по ключевому слову (сообщение из чужого чата), вызов живого оператора и
новый заказ уходили с parse_mode=HTML с вставленными как есть именем, текстом
и названием чата. «<» ломал разметку — Telegram отклонял сообщение, и лид или
вызов оператора терялся; ссылка `<a href=…>` становилась живой в чате
владельца. Тесты падают без фикса.
"""
from __future__ import annotations

from services import auto_responder as ar
from services import keyword_watcher as kw

EVIL = '<a href="https://phish.example">жми</a> цена < 100'


async def test_keyword_lead_is_escaped():
    sent: list[str] = []

    class _Bot:
        async def send_message(self, chat_id, text, **k):
            sent.append(text)

    ok = await kw._deliver_hit(
        _Bot(), 1, {"chat_title": "<b>Чат</b>"},
        {"text": EVIL, "from_username": "x"}, "цена")
    assert ok and sent
    assert "<a href" not in sent[0] and "<b>Чат</b>" not in sent[0]
    assert "цена &lt; 100" in sent[0]


async def test_operator_handoff_and_order_are_escaped(monkeypatch):
    sent: list[str] = []

    async def _send(http, token, chat_id, text, **k):
        sent.append(text)
        return True, None

    monkeypatch.setattr(ar.bot_api, "send_message", _send)
    who = {"first_name": "<b>Иван</b>"}
    await ar._notify_operator(None, "t", {"chat_id": 5}, 7, who, EVIL)
    await ar._notify_operator_order(None, "t", {"chat_id": 5}, 7, who,
                                    "Товар ×1\nТелефон: <script>")
    assert len(sent) == 2
    for t in sent:
        assert "<b>Иван</b>" not in t and "<a href" not in t and "<script>" not in t
    assert "цена &lt; 100" in sent[0]
