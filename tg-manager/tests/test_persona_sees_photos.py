"""Персона видит картинки в чате.

Фото без подписи для разогрева было пустым сообщением: на «гляньте, что
пришло» флот отвечал мимо или не отвечал вовсе. Новое фото от человека
описывается моделью со зрением и входит в разговор репликой «[фото: …]».
"""
from __future__ import annotations

import asyncio

from services import ai_gateway as ag
from services import chat_warmup as cw


class MessageMediaPhoto:       # имя класса — как у Telethon
    pass


class _Msg:
    def __init__(self, mid, text="", photo=False):
        self.id, self.message = mid, text
        self.media = MessageMediaPhoto() if photo else None


class _Client:
    def __init__(self):
        self.downloads = 0

    async def download_media(self, msg, file=None):
        self.downloads += 1
        return b"\xff\xd8\xff" + b"0" * 64


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def test_media_type_and_vision_models_are_recognised():
    assert ag.image_media_type(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert ag.image_media_type(b"\x89PNG\r\n\x1a\n...") == "image/png"
    assert ag.image_media_type(b"hello") == ""
    assert ag.vision_capable("gemini-2.0-flash") and ag.vision_capable("gpt-4o-mini")
    assert not ag.vision_capable("llama-3.1-8b-instant")


def test_photo_line_reads_like_a_chat_message():
    assert cw.photo_line("график биткоина растёт", "гляньте") == "[фото: график биткоина растёт] гляньте"
    assert cw.photo_line("", "просто подпись") == "просто подпись"


def test_new_photo_from_a_person_is_described_once_and_becomes_a_target(monkeypatch):
    seen = []

    async def _describe(data, *, owner_id=None, hint="", timeout=30.0):
        seen.append((owner_id, hint))
        return ag.AiReply(text="скрин графика, линия вверх")
    monkeypatch.setattr(ag, "describe_image", _describe)
    monkeypatch.setattr(cw, "_SEEN_PHOTOS", {})
    raw = [_Msg(10, "привет"), _Msg(11, "", photo=True)]
    recent = [{"id": 10, "sender_id": 5, "text": "привет", "is_fleet": False},
              {"id": 11, "sender_id": 5, "text": "", "is_fleet": False}]
    client = _Client()
    assert _run(cw.see_photo(client, raw, recent, last_seen=9, chat_key="@c", owner_id=7))
    assert recent[1]["text"] == "[фото: скрин графика, линия вверх]"
    plan = cw.plan_turn(recent, [1, 2], "engage", None, 9)
    assert plan["reply_to_id"] == 11 and "скрин графика" in plan["reply_to_text"]
    # второй ход — из кэша, без новой загрузки и без нового вызова модели
    recent2 = [{"id": 11, "sender_id": 5, "text": "", "is_fleet": False}]
    assert _run(cw.see_photo(client, raw, recent2, last_seen=9, chat_key="@c", owner_id=7))
    assert client.downloads == 1 and len(seen) == 1 and seen[0] == (7, "")


def test_old_or_fleet_photos_are_not_looked_at(monkeypatch):
    async def _describe(*a, **kw):
        raise AssertionError("не должно звать модель")
    monkeypatch.setattr(ag, "describe_image", _describe)
    monkeypatch.setattr(cw, "_SEEN_PHOTOS", {})
    raw = [_Msg(5, photo=True), _Msg(6, photo=True)]
    recent = [{"id": 5, "text": "", "is_fleet": False},
              {"id": 6, "text": "", "is_fleet": True}]
    assert not _run(cw.see_photo(_Client(), raw, recent, last_seen=5, chat_key="@c", owner_id=1))


def test_vision_failure_does_not_break_the_turn(monkeypatch):
    async def _describe(*a, **kw):
        return ag.AiReply()
    monkeypatch.setattr(ag, "describe_image", _describe)
    monkeypatch.setattr(cw, "_SEEN_PHOTOS", {})
    recent = [{"id": 3, "text": "", "is_fleet": False}]
    assert not _run(cw.see_photo(_Client(), [_Msg(3, photo=True)], recent, last_seen=0,
                                 chat_key="@c", owner_id=1))
    assert recent[0]["text"] == ""


def test_describe_image_refuses_non_images_without_calling_anyone():
    r = _run(ag.describe_image(b"not an image", owner_id=None))
    assert r.text is None and r.errors


def test_the_turn_looks_at_photos_before_planning():
    import inspect
    src = inspect.getsource(cw._process_session)
    assert src.index("await see_photo(") < src.index("plan = plan_turn(")


def test_claude_gets_the_image_as_an_image_block(monkeypatch):
    from services import ai_claude
    sent = {}

    class _M:
        stop_reason = "end_turn"
        usage = None

    class _Cl:
        class messages:
            @staticmethod
            async def create(**kw):
                sent.update(kw)
                return _M()
    monkeypatch.setattr(ai_claude, "enabled", lambda: True)
    monkeypatch.setattr(ai_claude, "_make_client", lambda t: _Cl())
    monkeypatch.setattr(ai_claude, "_extract_text", lambda m: "кот на клавиатуре")
    text, usage = _run(ai_claude.describe_image("sys", "QUJD", "image/png", "Опиши"))
    assert text == "кот на клавиатуре"
    block = sent["messages"][0]["content"][0]
    assert block["type"] == "image" and block["source"]["media_type"] == "image/png"
