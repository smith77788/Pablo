"""Сверка карточек каналов должна НАХОДИТЬ канал и ЧИТАТЬ из ответа имя.

Почему прошлая починка не помогла
---------------------------------
В прошлый раз чинилось расписание сверки: мёртвый аккаунт больше не морозит
очередь, темп поднят, кнопка «Обновить данные» досчитывает участников. Всё это
верно — и всё это было бесполезно, потому что ниже лежали ещё два отказа, и
каждый сам по себе обнулял результат.

1. **Канал не находился.** `get_full_channel_info` искал его голым
   `client.get_entity(channel_id)`. У StringSession нет кэша сущностей, а
   `InputChannel` с нулевым access_hash Telegram отвергает, — для свежего
   клиента это почти гарантированный провал. Функция возвращала None по
   каждому каналу, вызывающий ставил отметку осмотра и шёл дальше. При этом
   и access_hash, и @username лежали в той же строке выборки и не передавались.

2. **Имя читалось не оттуда.** Даже когда канал находился через
   `InputPeerChannel(id, access_hash)` — основной способ добраться до
   приватного канала, — у такого пира НЕТ ни title, ни username. Читали их
   именно с него, получали пустые строки, COALESCE их гасил, и название
   оставалось прежним навсегда. Настоящая карточка лежит в `full.chats`.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import account_manager as am  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _func_body(src: str, name: str) -> str:
    i = src.index(f"async def {name}")
    rest = src[i + 1:]
    m = re.search(r"\n(?:async )?def ", rest)
    return rest[: m.start()] if m else rest


# ── имя берётся из настоящей карточки, а не из пира ──────────────────────────


class _Chat:
    def __init__(self, cid, title, username=""):
        self.id = cid
        self.title = title
        self.username = username


class _Full:
    def __init__(self, chats, members=0, about=""):
        self.chats = chats
        self.full_chat = type(
            "FC", (), {"participants_count": members, "about": about}
        )()


def test_title_comes_from_the_response_not_from_the_peer():
    """Ровно этот дефект: у InputPeerChannel нет ни title, ни username."""
    full = _Full([_Chat(1234567890, "Новое имя", "new_link")])
    chat = am._chat_from_full(full, 1234567890)
    assert chat.title == "Новое имя"
    assert chat.username == "new_link"


def test_right_chat_is_picked_when_the_answer_carries_several():
    """GetFullChannel возвращает и связанные чаты — взять первый попавшийся
    значит переименовать канал в имя его чата обсуждений."""
    full = _Full([_Chat(111, "Чат обсуждений"), _Chat(222, "Сам канал")])
    assert am._chat_from_full(full, 222).title == "Сам канал"


def test_minus_100_prefix_is_not_a_different_channel():
    """В базе id лежит голым, в Telegram встречается с префиксом -100."""
    full = _Full([_Chat(1234567890, "Канал")])
    assert am._chat_from_full(full, -1001234567890).title == "Канал"


def test_empty_answer_does_not_crash():
    assert am._chat_from_full(_Full([]), 1) is None


# ── канал ищется тем, что лежит в базе ───────────────────────────────────────


def test_lookup_uses_access_hash_and_username():
    """Обе колонки уже есть в выборке сверки и раньше просто выбрасывались."""
    body = _func_body(_read("services/account_manager.py"), "get_full_channel_info")
    assert "access_hash: int = 0" in body and 'username: str = ""' in body, (
        "функция по-прежнему не принимает то, чем канал только и найти"
    )
    assert "_resolve_channel_peer(" in body, "резолв снова голым get_entity"


def test_drift_check_passes_what_it_already_selected():
    src = _read("services/drift_detector.py")
    assert "mc.access_hash" in src, "access_hash не выбирается"
    assert "get_channels_full_info(" in src, (
        "сверка снова ходит по одному каналу на подключение"
    )


def test_batch_lookup_resolves_through_dialogs_once():
    body = _func_body(_read("services/account_manager.py"), "get_channels_full_info")
    assert body.count("iter_dialogs(") == 1, (
        "обход диалогов должен быть один на пачку, а не на канал"
    )
    assert "InputPeerChannel(" in body
    assert "_chat_from_full(" in body, "имя снова читается не из ответа"


def test_total_failure_is_loud():
    """Молчаливый ноль — это то, из-за чего дефект жил месяцами: сверка
    «отрабатывала» и ничего не меняла."""
    body = _func_body(_read("services/account_manager.py"), "get_channels_full_info")
    assert "ни один из %d каналов не прочитан" in body


# ── поведение целиком, на поддельном клиенте ─────────────────────────────────


class _FakeClient:
    """Клиент, который ведёт себя как настоящий в главном: get_entity по голому
    id не работает, а InputPeerChannel из диалогов — работает."""

    def __init__(self, dialogs, fulls):
        self._dialogs = dialogs
        self._fulls = fulls
        self.entity_calls = 0
        self.requests = []

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    def iter_dialogs(self, limit=None):
        async def _gen():
            for d in self._dialogs:
                yield type("D", (), {"entity": d})()

        return _gen()

    async def get_entity(self, ref):
        self.entity_calls += 1
        raise ValueError("Could not find the input entity")

    async def __call__(self, request):
        self.requests.append(request)
        peer = getattr(request, "channel", None)
        if peer is None:
            peer = (getattr(request, "args", None) or [None])[0]
        cid = getattr(peer, "channel_id", None)
        if cid is None:
            raise ValueError("пир без канала")
        return self._fulls[int(cid)]


@pytest.fixture
def _patched(monkeypatch):
    import types

    cache: dict[str, type] = {}

    class _TL(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            if name not in cache:
                def _init(self, *a, **kw):
                    # GetFullChannelRequest зовут позиционно, InputPeerChannel —
                    # по имени: стаб должен принимать оба вида.
                    self.args = a
                    for k, v in kw.items():
                        setattr(self, k, v)

                cache[name] = type(name, (), {"__init__": _init})
            return cache[name]

    monkeypatch.setitem(sys.modules, "telethon.tl.functions.channels", _TL("ch"))
    monkeypatch.setitem(sys.modules, "telethon.tl.types", _TL("ty"))

    async def _instant(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)
    return cache


def test_private_channel_is_read_through_its_dialog_hash(_patched, monkeypatch):
    """Главный сценарий: приватный канал, которого get_entity не находит.

    До починки здесь возвращался пустой словарь — и карточка не менялась.
    """
    ent = _Chat(555, "Старое из диалога", "")
    ent.access_hash = 999
    client = _FakeClient(
        dialogs=[ent],
        fulls={555: _Full([_Chat(555, "Новое имя", "new_link")], members=1234,
                          about="про канал")},
    )
    monkeypatch.setattr(am, "_make_client", lambda *a, **k: client)

    out = asyncio.run(am.get_channels_full_info("sess", [555]))
    assert out, "канал снова не прочитан — значит карточка не обновится"
    assert out[555]["members_count"] == 1234
    assert out[555]["title"] == "Новое имя"
    assert out[555]["username"] == "new_link"
    assert client.entity_calls == 0, "get_entity вызывался там, где он не нужен"


def test_channel_missing_from_dialogs_does_not_break_the_rest(_patched, monkeypatch):
    ok = _Chat(555, "Есть", "")
    ok.access_hash = 999
    client = _FakeClient(
        dialogs=[ok],
        fulls={555: _Full([_Chat(555, "Канал", "link")], members=7)},
    )
    monkeypatch.setattr(am, "_make_client", lambda *a, **k: client)

    out = asyncio.run(am.get_channels_full_info("sess", [555, 777]))
    assert set(out) == {555}, "недоступный канал не должен ронять пачку"


def test_one_connection_for_the_whole_batch(_patched, monkeypatch):
    made = []
    ent = _Chat(555, "К", "")
    ent.access_hash = 9
    client = _FakeClient([ent], {555: _Full([_Chat(555, "К", "")], members=1)})

    def _mk(*a, **k):
        made.append(1)
        return client

    monkeypatch.setattr(am, "_make_client", _mk)
    asyncio.run(am.get_channels_full_info("sess", [555, 555, 555]))
    assert len(made) == 1, "подключение на каждый канал — то, от чего уходили"


def test_empty_input_touches_nothing(_patched, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("подключение без единого канала")

    monkeypatch.setattr(am, "_make_client", _boom)
    assert asyncio.run(am.get_channels_full_info("sess", [])) == {}
