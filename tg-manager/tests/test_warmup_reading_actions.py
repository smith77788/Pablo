"""Читающие действия прогрева: делают ли они то, что обещают, и ничего сверх.

Задача этих действий — дать флоту больше обычных человеческих действий, не
добавив ни одного ban-сигнала. Значит проверять надо две вещи, а не одну:

* действие реально сходило в Telegram (пустой успех — главный баг этого
  модуля, из-за него прогрев уже рапортовал о несделанном);
* действие ничего не ОТПРАВИЛО: ни сообщения, ни реакции, ни вступления.
  Второе важнее первого: репертуар расширяется именно для тех дней, когда
  писать ещё нельзя.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import account_warmer as aw  # noqa: E402
from services import warmup_actions as wa  # noqa: E402


@pytest.fixture(autouse=True)
def _named_tl_requests(monkeypatch):
    """Заглушка telethon из conftest отдаёт на ЛЮБОЕ имя один и тот же объект
    `_Any`, поэтому отличить `GetMessagesViewsRequest` от `SendReactionRequest`
    в ней невозможно — а здесь проверяется именно это: что действие отправило
    один запрос и не отправило другой. Подменяем нужные подмодули на такие же
    лёгкие, но с ИМЕНОВАННЫМИ классами.
    """
    import types

    cache: dict[str, type] = {}

    class _TL(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            cls = cache.get(name)
            if cls is None:
                def _init(self, *a, **kw):
                    self.args = a
                    self.kwargs = kw

                cls = type(name, (), {"__init__": _init})
                cache[name] = cls
            return cls

    for mod in (
        "telethon.tl.functions.messages",
        "telethon.tl.functions.channels",
        "telethon.tl.functions.contacts",
        "telethon.tl.functions.account",
        "telethon.tl.functions.stories",
        "telethon.tl.functions.updates",
        "telethon.tl.types",
    ):
        monkeypatch.setitem(sys.modules, mod, _TL(mod))


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    """Человеческие паузы внутри действий — настоящие (секунды и десятки секунд).

    Это правильно в проде и недопустимо в тестах: один `deep_scroll` спит до
    минуты. Убираем сон, оставляя всё остальное нетронутым.
    """

    async def _instant(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


class _Msg:
    def __init__(self, mid, text="", photo=None, replies=0):
        self.id = mid
        self.text = text
        self.photo = photo
        self.media = None
        self.replies = type("R", (), {"replies": replies})() if replies else None


class _FakeClient:
    """Клиент, который запоминает ВСЁ, что через него прошло.

    Ключевое: отдельно считает отправки (send_message / forward / реакции) —
    по ним и проверяется, что читающее действие ничего не пишет.
    """

    def __init__(self, messages=None, requests_result=None):
        self._messages = messages if messages is not None else [_Msg(i) for i in range(1, 26)]
        self.requests: list = []
        self.sent: list = []
        self.downloaded = 0
        self.get_messages_calls: list[dict] = []
        self._requests_result = requests_result or {}

    async def get_entity(self, ref):
        return type("E", (), {"id": 1, "username": str(ref).lstrip("@"), "broadcast": True})()

    async def get_messages(self, entity, limit=10, **kw):
        self.get_messages_calls.append({"limit": limit, **kw})
        # add_offset уважается по-настоящему: иначе «пролистывание вглубь»
        # нечем проверить — фейк бесконечно отдавал бы первый экран.
        off = int(kw.get("add_offset") or 0)
        return self._messages[off : off + limit]

    async def iter_messages(self, entity, limit=10, **kw):
        for m in self._messages[:limit]:
            yield m

    async def download_media(self, msg, file=None):
        self.downloaded += 1
        return b"jpeg"

    async def send_message(self, *a, **kw):
        self.sent.append(("send_message", a, kw))
        return None

    async def forward_messages(self, *a, **kw):
        self.sent.append(("forward", a, kw))
        return None

    async def __call__(self, request):
        name = type(request).__name__
        self.requests.append(name)
        return self._requests_result.get(name, type("R", (), {"chats": [], "stories": None})())


def _run(coro):
    return asyncio.run(coro)


# ── действие реально работает ────────────────────────────────────────────────


def test_deep_scroll_goes_past_the_first_screen():
    """Смысл действия ровно в этом: `read_channel` берёт последние 10–15, а
    человек уходит в историю. Без add_offset это был бы тот же первый экран."""
    c = _FakeClient()
    assert _run(wa.deep_scroll(c, "@ch")) is True
    offsets = [call.get("add_offset") for call in c.get_messages_calls]
    assert len(c.get_messages_calls) >= 2, "пролистана одна страница — это не «вглубь»"
    assert max(o for o in offsets if o is not None) > 0, "все страницы с нулевым смещением"


def test_deep_scroll_stops_on_a_short_channel():
    """Канал кончился — не крутить цикл вхолостую."""
    c = _FakeClient(messages=[_Msg(1)])
    assert _run(wa.deep_scroll(c, "@ch")) is True
    assert len(c.get_messages_calls) <= 3


def test_view_posts_actually_counts_views():
    c = _FakeClient()
    assert _run(wa.view_posts(c, "@ch")) is True
    assert "GetMessagesViewsRequest" in c.requests


def test_view_posts_on_empty_channel_is_an_honest_failure():
    """Пустой канал — не успех. Засчитывать его успехом означало бы снова
    рапортовать о просмотре, которого не было."""
    c = _FakeClient(messages=[])
    assert _run(wa.view_posts(c, "@ch")) is False


def test_read_comments_reads_a_post_with_a_discussion():
    c = _FakeClient(messages=[_Msg(i, replies=3) for i in range(1, 10)])
    assert _run(wa.read_comments(c, "@ch")) is True


def test_read_comments_without_discussions_is_not_a_success():
    c = _FakeClient(messages=[_Msg(i) for i in range(1, 10)])
    assert _run(wa.read_comments(c, "@ch")) is False


def test_open_media_downloads_a_photo():
    c = _FakeClient(messages=[_Msg(1), _Msg(2, photo=object())])
    assert _run(wa.open_media(c, "@ch")) is True
    assert c.downloaded == 1


def test_open_media_skips_channels_without_photos():
    c = _FakeClient(messages=[_Msg(1), _Msg(2)])
    assert _run(wa.open_media(c, "@ch")) is False
    assert c.downloaded == 0


def test_open_link_requests_the_page():
    c = _FakeClient(messages=[_Msg(1, text="смотри https://example.com/a")])
    assert _run(wa.open_link(c, "@ch")) is True
    assert "GetWebPageRequest" in c.requests


def test_open_link_without_links_is_not_a_success():
    c = _FakeClient(messages=[_Msg(1, text="без ссылок")])
    assert _run(wa.open_link(c, "@ch")) is False


def test_client_chores_hit_telegram():
    for fn, req in (
        (wa.check_stickers, "GetAllStickersRequest"),
        (wa.top_peers, "GetTopPeersRequest"),
        (wa.open_folders, "GetDialogFiltersRequest"),
        (wa.settings_peek, "GetPrivacyRequest"),
    ):
        c = _FakeClient()
        assert _run(fn(c)) is True, f"{fn.__name__} не выполнилось"
        assert req in c.requests, f"{fn.__name__} не отправило {req}"


def test_read_saved_opens_saved_messages():
    c = _FakeClient()
    assert _run(wa.read_saved(c)) is True
    assert c.get_messages_calls, "Избранное не открывалось"


# ── и ничего не пишет ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "call",
    [
        lambda c: wa.deep_scroll(c, "@ch"),
        lambda c: wa.view_posts(c, "@ch"),
        lambda c: wa.read_comments(c, "@ch"),
        lambda c: wa.open_media(c, "@ch"),
        lambda c: wa.open_link(c, "@ch"),
        lambda c: wa.search_in_channel(c, "@ch"),
        lambda c: wa.explore_similar(c, "@ch"),
        lambda c: wa.read_saved(c),
        lambda c: wa.check_stickers(c),
        lambda c: wa.top_peers(c),
        lambda c: wa.open_folders(c),
        lambda c: wa.settings_peek(c),
    ],
)
def test_reading_action_sends_nothing(call):
    """Ни одного send/forward — иначе действие нельзя держать в наборе для
    свежего аккаунта, а весь смысл именно в этом."""
    c = _FakeClient(messages=[_Msg(1, text="ссылка https://t.me/some_channel", photo=object(), replies=2)])
    _run(call(c))
    assert not c.sent, f"читающее действие что-то отправило: {c.sent}"
    forbidden = {"SendReactionRequest", "JoinChannelRequest", "SendVoteRequest"}
    assert not (forbidden & set(c.requests)), f"запрещённые запросы: {c.requests}"


def test_fatal_error_is_not_swallowed():
    """Мёртвая сессия обязана всплыть наверх: иначе прогрев будет долбить
    отозванный аккаунт до конца дня."""

    class _Dead(_FakeClient):
        async def get_entity(self, ref):
            raise type("AuthKeyUnregisteredError", (Exception,), {})()

    with pytest.raises(Exception) as ei:
        _run(wa.deep_scroll(_Dead(), "@ch"))
    assert type(ei.value).__name__ == "AuthKeyUnregisteredError"


def test_ordinary_error_is_swallowed():
    class _Broken(_FakeClient):
        async def get_entity(self, ref):
            raise ValueError("нет такого канала")

    assert _run(wa.deep_scroll(_Broken(), "@ch")) is False


# ── находки каналов ──────────────────────────────────────────────────────────


def test_explore_similar_brings_new_channels():
    res = type("R", (), {"chats": [type("C", (), {"username": "found_one"})()]})()
    c = _FakeClient(requests_result={"GetChannelRecommendationsRequest": res})
    found: list[str] = []
    assert _run(wa.explore_similar(c, "@ch", found)) is True
    assert found == ["@found_one"]


def test_links_in_posts_are_a_source_of_channels():
    c = _FakeClient(messages=[_Msg(1, text="подпишись https://t.me/some_channel и всё")])
    found: list[str] = []
    _run(wa.open_link(c, "@ch", found))
    assert "@some_channel" in found


def test_collect_filters_garbage_and_duplicates():
    found: list[str] = []
    wa._collect(found, ["", None, "ok_channel", "@ok_channel", "ab", "x" * 99])
    assert found == ["@ok_channel"]


def test_collect_tolerates_no_output_list():
    wa._collect(None, ["whatever"])  # не должно падать


# ── проводка в реестр ────────────────────────────────────────────────────────


def test_reading_actions_are_wired_into_the_registry():
    for name in (
        "deep_scroll", "view_posts", "read_comments", "open_media", "open_link",
        "search_in_channel", "explore_similar", "channel_stories", "read_saved",
        "check_stickers", "top_peers", "open_folders", "settings_peek",
    ):
        assert name in aw.registered_actions(), f"{name} не доедет до исполнителя"
        assert aw._ACTION_SPECS[name].writes is False, f"{name} помечено пишущим"


def test_dispatcher_runs_a_reading_action_end_to_end():
    c = _FakeClient()
    ok, target = _run(aw.perform_action("view_posts", c, target="@ch"))
    assert ok is True and target == "@ch"
    assert "GetMessagesViewsRequest" in c.requests
