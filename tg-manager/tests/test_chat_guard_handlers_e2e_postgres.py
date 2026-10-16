"""Интеграция хендлеров «Модератора чатов» по живому Postgres.

Проверяем, что бот РЕАЛЬНО делает работу (не только пишет в БД):
  • при выдаче админки — ставит чат под охрану (on_my_status);
  • системное «X присоединился» — удаляется (on_service_message);
  • /ban от админа — реально банит и снимает варны.
Telegram-объекты заменены лёгкими заглушками (SimpleNamespace + async-стабы).
"""
from __future__ import annotations

import asyncio
import glob
import os
import re
from types import SimpleNamespace

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 992501
CHAT = -1009876543210
_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        for f in ["schema.sql"] + sorted(
                glob.glob("schema_v*.sql"),
                key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


class FakeBot:
    def __init__(self, admin_ids=()):
        self._admins = set(admin_ids)
        self.sent = []
        self.banned = []
        self.restricted = []        # id пользователей, кого ограничивали
        self.restricted_calls = []  # (user_id, kwargs) — для проверки прав/мьюта
        self.unbanned = []
        self.deleted = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=999)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)
        return True

    async def get_chat_member(self, chat_id, user_id):
        st = "administrator" if user_id in self._admins else "member"
        return SimpleNamespace(status=st)

    async def get_chat_administrators(self, chat_id):
        return [SimpleNamespace(user=SimpleNamespace(id=i)) for i in self._admins]

    async def ban_chat_member(self, chat_id, user_id, **kw):
        self.banned.append(user_id)
        return True

    async def unban_chat_member(self, chat_id, user_id, **kw):
        self.unbanned.append(user_id)
        return True

    async def restrict_chat_member(self, chat_id, user_id, **kw):
        self.restricted.append(user_id)
        self.restricted_calls.append((user_id, kw))
        return True

    async def get_me(self):
        return SimpleNamespace(username="guardbot")


def _msg(content_type="text", *, from_id=None, new_members=None, reply=None,
         chat_id=CHAT, text="", entities=None, forward=False):
    deleted = {"v": False}
    replies = []

    async def _delete():
        deleted["v"] = True

    async def _reply(text, **kw):
        replies.append(text)

    m = SimpleNamespace(
        content_type=content_type,
        chat=SimpleNamespace(id=chat_id, type="supergroup", title="T", username="t"),
        from_user=(SimpleNamespace(id=from_id, full_name="U", username="u",
                                   is_bot=False) if from_id else None),
        new_chat_members=new_members,
        reply_to_message=reply,
        text=text, caption=None, entities=entities,
        forward_origin=(object() if forward else None),
        forward_from=None, forward_from_chat=None,
        delete=_delete, reply=_reply,
    )
    m._deleted = deleted
    m._replies = replies
    return m


async def _drive(handler, *args):
    """Вызвать групповой хендлер, проглотив SkipHandler (= «не моё, дальше»)."""
    from aiogram.dispatcher.event.bases import SkipHandler
    try:
        await handler(*args)
        return True    # обработано (удалено/наказано)
    except SkipHandler:
        return False   # пропущено дальше


def _clean(pool):
    _run(pool.execute("DELETE FROM guard_chats WHERE owner_id=$1", OWNER))
    _run(pool.execute("DELETE FROM guard_warnings WHERE chat_id=$1", CHAT))
    _run(pool.execute("DELETE FROM guard_captcha_pending WHERE chat_id=$1", CHAT))


def test_promotion_registers_guard(pool):
    from bot.handlers import chat_guard as h
    from services import chat_guard as cg
    _clean(pool)
    bot = FakeBot()
    event = SimpleNamespace(
        chat=SimpleNamespace(id=CHAT, type="supergroup", title="Мой чат", username="mych"),
        from_user=SimpleNamespace(id=OWNER, full_name="Owner", username="own"),
        new_chat_member=SimpleNamespace(status="administrator",
                                        can_delete_messages=True,
                                        can_restrict_members=True),
        old_chat_member=SimpleNamespace(status="left"),
    )
    _run(h.on_my_status(event, bot, pool))
    g = _run(cg.is_guarded(pool, CHAT))
    assert g and g["owner_id"] == OWNER
    # прислал подтверждение активации
    assert bot.sent and "актив" in bot.sent[0][1].lower()


def test_service_join_message_deleted(pool):
    from bot.handlers import chat_guard as h
    from services import chat_guard as cg
    _clean(pool)
    _run(cg.register_chat(pool, OWNER, CHAT, title="T"))
    bot = FakeBot()
    m = _msg("new_chat_members", from_id=42,
             new_members=[SimpleNamespace(full_name="New", username="new",
                                          is_bot=False, id=42)])
    _run(h.on_service_message(m, bot, pool))
    assert m._deleted["v"] is True   # «присоединился» удалено


def test_service_not_deleted_when_disabled(pool):
    from bot.handlers import chat_guard as h
    from services import chat_guard as cg
    _clean(pool)
    _run(cg.register_chat(pool, OWNER, CHAT))
    _run(cg.set_setting(pool, CHAT, "clean_join", False))
    bot = FakeBot()
    m = _msg("new_chat_members", from_id=42,
             new_members=[SimpleNamespace(full_name="N", username="n",
                                          is_bot=False, id=42)])
    _run(h.on_service_message(m, bot, pool))
    assert m._deleted["v"] is False  # тумблер выключен → не трогаем


def test_welcome_sent_on_join(pool):
    from bot.handlers import chat_guard as h
    from services import chat_guard as cg
    _clean(pool)
    _run(cg.register_chat(pool, OWNER, CHAT))
    _run(cg.set_setting(pool, CHAT, "welcome_text", "Привет, {name}!"))
    _run(cg.set_setting(pool, CHAT, "welcome_ttl", 0))  # без авто-удаления в тесте
    bot = FakeBot()
    m = _msg("new_chat_members", from_id=42,
             new_members=[SimpleNamespace(full_name="Вася", username="v",
                                          is_bot=False, id=42)])
    _run(h.on_service_message(m, bot, pool))
    assert any("Привет, Вася!" in t for _, t in bot.sent)


def test_ban_command_requires_admin(pool):
    from bot.handlers import chat_guard as h
    from services import chat_guard as cg
    _clean(pool)
    _run(cg.register_chat(pool, OWNER, CHAT))
    # НЕ админ пытается банить → отказ, бана нет
    bot = FakeBot(admin_ids=())
    target = SimpleNamespace(from_user=SimpleNamespace(id=42, full_name="Bad",
                                                       username="bad", is_bot=False))
    m = _msg("text", from_id=555, reply=target)
    _run(h.cmd_ban(m, SimpleNamespace(args=None), bot, pool))
    assert bot.banned == []
    assert any("админ" in r.lower() for r in m._replies)


def test_ban_command_bans_and_clears_warns(pool):
    from bot.handlers import chat_guard as h
    from services import chat_guard as cg
    _clean(pool)
    _run(cg.register_chat(pool, OWNER, CHAT))
    _run(cg.add_warning(pool, CHAT, 42))
    bot = FakeBot(admin_ids={555})   # 555 — админ
    target = SimpleNamespace(from_user=SimpleNamespace(id=42, full_name="Bad",
                                                       username="bad", is_bot=False))
    m = _msg("text", from_id=555, reply=target)
    _run(h.cmd_ban(m, SimpleNamespace(args=None), bot, pool))
    assert 42 in bot.banned
    assert _run(cg.get_warnings(pool, CHAT, 42)) == 0   # варны сброшены при бане


def test_warn_escalates_to_punishment(pool):
    from bot.handlers import chat_guard as h
    from services import chat_guard as cg
    _clean(pool)
    _run(cg.register_chat(pool, OWNER, CHAT))
    _run(cg.set_setting(pool, CHAT, "max_warns", 2))
    _run(cg.set_setting(pool, CHAT, "warn_action", "ban"))
    bot = FakeBot(admin_ids={555})
    target = SimpleNamespace(from_user=SimpleNamespace(id=42, full_name="Bad",
                                                       username="bad", is_bot=False))
    # 1-й варн — просто предупреждение
    _run(h.cmd_warn(_msg("text", from_id=555, reply=target),
                    SimpleNamespace(args=None), bot, pool))
    assert bot.banned == []
    # 2-й варн (== max_warns) → бан + сброс
    _run(h.cmd_warn(_msg("text", from_id=555, reply=target),
                    SimpleNamespace(args=None), bot, pool))
    assert 42 in bot.banned
    assert _run(cg.get_warnings(pool, CHAT, 42)) == 0


# ── Продвинутая модерация: капча / стоп-слова / антифлуд / ночь ────────────────

def test_captcha_mutes_newcomer_and_records(pool):
    from bot.handlers import chat_guard as h
    from services import chat_guard as cg
    _clean(pool)
    _run(cg.register_chat(pool, OWNER, CHAT))
    _run(cg.set_setting(pool, CHAT, "captcha", True))
    bot = FakeBot()
    newcomer = SimpleNamespace(id=42, full_name="New", username="n", is_bot=False)
    m = _msg("new_chat_members", from_id=42, new_members=[newcomer])
    _run(h.on_service_message(m, bot, pool))
    # новичок замьючен (can_send_messages=False) и есть кнопка-капча
    assert 42 in bot.restricted
    uid, kw = bot.restricted_calls[0]
    assert kw["permissions"].can_send_messages is False
    assert any("не бот" in t.lower() for _, t in bot.sent)
    # записан в ожидание капчи
    assert _run(cg.captcha_get(pool, CHAT, 42)) is not None


def test_captcha_solve_unmutes(pool):
    from bot.handlers import chat_guard as h
    from services import chat_guard as cg
    _clean(pool)
    _run(cg.register_chat(pool, OWNER, CHAT))
    _run(cg.set_setting(pool, CHAT, "captcha", True))
    _run(cg.captcha_add(pool, CHAT, 42, captcha_msg_id=321, timeout=120, action="kick"))
    bot = FakeBot()
    answered = {"v": None}

    async def _answer(text=None, **kw):
        answered["v"] = text

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=42, full_name="New", username="n"),
        message=SimpleNamespace(chat=SimpleNamespace(id=CHAT, title="T")),
        answer=_answer)
    _run(h.cb_captcha(cb, SimpleNamespace(action="captcha", chat_id=CHAT, key=""),
                      bot, pool))
    # размьючен полными правами, запись снята, сообщение-капча удалено
    uid, kw = bot.restricted_calls[-1]
    assert kw["permissions"].can_send_messages is True
    assert kw["permissions"].can_send_photos is True
    assert _run(cg.captcha_get(pool, CHAT, 42)) is None
    assert 321 in bot.deleted


def test_captcha_solve_by_wrong_user_rejected(pool):
    from bot.handlers import chat_guard as h
    from services import chat_guard as cg
    _clean(pool)
    _run(cg.register_chat(pool, OWNER, CHAT))
    _run(cg.captcha_add(pool, CHAT, 42, captcha_msg_id=1, timeout=120, action="kick"))
    bot = FakeBot()
    alerts = {"v": None}

    async def _answer(text=None, **kw):
        alerts["v"] = (text, kw.get("show_alert"))

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=999, full_name="X", username="x"),  # НЕ тот
        message=SimpleNamespace(chat=SimpleNamespace(id=CHAT, title="T")),
        answer=_answer)
    _run(h.cb_captcha(cb, SimpleNamespace(action="captcha", chat_id=CHAT, key=""),
                      bot, pool))
    assert alerts["v"] and alerts["v"][1] is True    # show_alert «не для вас»
    assert _run(cg.captcha_get(pool, CHAT, 42)) is not None  # запись 42 цела


def test_stopword_deletes_message(pool):
    from bot.handlers import chat_guard as h
    from services import chat_guard as cg
    _clean(pool)
    _run(cg.register_chat(pool, OWNER, CHAT))
    _run(cg.set_setting(pool, CHAT, "stopwords", ["казино"]))
    bot = FakeBot()   # автор не админ
    m = _msg("text", from_id=42, text="лучшее КАЗИНО тут")
    handled = _run(_drive(h.on_group_message, m, bot, pool))
    assert handled and m._deleted["v"] is True
    # чистый текст не трогаем
    m2 = _msg("text", from_id=42, text="обычное сообщение")
    handled2 = _run(_drive(h.on_group_message, m2, bot, pool))
    assert handled2 is False and m2._deleted["v"] is False


def test_stopword_skips_admins(pool):
    from bot.handlers import chat_guard as h
    from services import chat_guard as cg
    _clean(pool)
    h._invalidate_admins(CHAT)
    _run(cg.register_chat(pool, OWNER, CHAT))
    _run(cg.set_setting(pool, CHAT, "stopwords", ["казино"]))
    bot = FakeBot(admin_ids={42})   # автор — админ
    m = _msg("text", from_id=42, text="казино")
    handled = _run(_drive(h.on_group_message, m, bot, pool))
    assert handled is False and m._deleted["v"] is False
    h._invalidate_admins(CHAT)


def test_antiflood_deletes_on_burst(pool):
    from bot.handlers import chat_guard as h
    from services import chat_guard as cg
    _clean(pool)
    h._invalidate_admins(CHAT)
    _run(cg.register_chat(pool, OWNER, CHAT))
    _run(cg.set_setting(pool, CHAT, "antiflood", True))
    _run(cg.set_setting(pool, CHAT, "flood_count", 3))
    _run(cg.set_setting(pool, CHAT, "flood_window", 30))
    _run(cg.set_setting(pool, CHAT, "flood_action", "delete"))
    cg.flood_reset(CHAT, 42)
    bot = FakeBot()
    # первые 2 проходят, 3-е — флуд → удаление
    r1 = _run(_drive(h.on_group_message, _msg("text", from_id=42, text="a"), bot, pool))
    r2 = _run(_drive(h.on_group_message, _msg("text", from_id=42, text="b"), bot, pool))
    m3 = _msg("text", from_id=42, text="c")
    r3 = _run(_drive(h.on_group_message, m3, bot, pool))
    assert r1 is False and r2 is False
    assert r3 is True and m3._deleted["v"] is True
    cg.flood_reset(CHAT, 42)


def test_night_mode_closes_chat(pool):
    from bot.handlers import chat_guard as h
    from services import chat_guard as cg
    _clean(pool)
    h._invalidate_admins(CHAT)
    _run(cg.register_chat(pool, OWNER, CHAT))
    _run(cg.set_setting(pool, CHAT, "night_mode", True))
    # окно на весь день (0→0 было бы «выкл», поэтому 0→23:59 через большое окно):
    _run(cg.set_setting(pool, CHAT, "night_from", 0))
    _run(cg.set_setting(pool, CHAT, "night_to", 23))
    _run(cg.set_setting(pool, CHAT, "night_tz", 0))
    bot = FakeBot()
    m = _msg("text", from_id=42, text="привет ночью")
    # если сейчас (UTC) попадает в [0,23) — удалит; иначе тест не показателен,
    # поэтому проверяем детерминированно через прямую логику is_night ниже.
    from datetime import datetime, timezone
    s = _run(cg.is_guarded(pool, CHAT))["settings"]
    now = datetime.now(timezone.utc)
    if cg.is_night(s, now):
        handled = _run(_drive(h.on_group_message, m, bot, pool))
        assert handled and m._deleted["v"] is True
    else:
        # 23:xx UTC — вне окна: сообщение не трогаем
        handled = _run(_drive(h.on_group_message, m, bot, pool))
        assert handled is False and m._deleted["v"] is False
