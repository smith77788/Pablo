"""Регресс: рассылка админ-панели больше не уходит по факту получения текста.

Первопричина. `handle_admin_message` слал рассылку ВСЕМ пользователям
платформы (и через ботов, и в каналы) сразу по получении текста от админа —
без единого подтверждения. Хуже: `admin_state` (флаг «жду текст рассылки»)
ничем не отменялся при уходе из меню («◀️ Главное меню админки» не чистил
строку) и не имел срока годности. Поэтому если админ хоть раз открывал
«Рассылка» и не доводил её до конца — СЛЕДУЮЩЕЕ его сообщение боту, каким бы
оно ни было (например, обычная реплика ИИ-ассистенту), мгновенно уходило
всем пользователям платформы.

Фикс: текст только готовит предпросмотр (`admin_state` переходит в
`{state}:confirm`, реальный текст лежит в data), а сама отправка происходит
ТОЛЬКО по нажатию «✅ Отправить» (adm:bc_go:*) — ровно как уже был устроен
per-bot broadcast.py. Плюс защита класса целиком: возврат в главное меню
админки (adm:main) теперь чистит любой незавершённый admin_state.

Реальный Postgres (INFRAGRAM_TEST_DSN) — заглушка пула не проверяет типы
параметров, а тут реальные INSERT/UPDATE/DELETE по admin_state и SELECT по
platform_users.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_UID = 424242

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _load_admin():
    path = os.path.join(ROOT, "bot", "handlers", "admin.py")
    spec = importlib.util.spec_from_file_location("_adm_bc_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeUser:
    def __init__(self, uid: int):
        self.id = uid


class _FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, uid, text, parse_mode=None):
        self.sent.append((uid, text))


class _FakeMessage:
    def __init__(self, uid: int, text: str | None, bot: "_FakeBot | None" = None):
        self.from_user = _FakeUser(uid)
        self.text = text
        self.bot = bot or _FakeBot()
        self.answers: list["_FakeMessage"] = []
        self.last_edit_text: str | None = None
        self.last_edit_markup = None
        self.last_reply_markup = None

    async def answer(self, text, parse_mode=None, reply_markup=None):
        m = _FakeMessage(self.from_user.id, None, self.bot)
        m.last_edit_text = text
        m.last_reply_markup = reply_markup
        self.answers.append(m)
        return m

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.last_edit_text = text
        self.last_reply_markup = reply_markup

    async def edit_reply_markup(self, reply_markup=None):
        self.last_edit_markup = reply_markup


class _FakeCallback:
    def __init__(self, uid: int, data: str, message: "_FakeMessage"):
        self.from_user = _FakeUser(uid)
        self.data = data
        self.message = message
        self.answered: list[tuple] = []

    async def answer(self, *a, **k):
        self.answered.append((a, k))


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        for f in ("schema_v14.sql", "schema_v39.sql"):
            with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
                for chunk in fh.read().split(";"):
                    body = "\n".join(
                        ln for ln in chunk.split("\n")
                        if not ln.strip().startswith("--")
                    ).strip()
                    if body:
                        try:
                            await p.execute(body)
                        except Exception:
                            pass  # идемпотентно; частичный сбой не должен рушить стенд
        return p

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM admin_state WHERE admin_id=$1", ADMIN_UID))
    _run(p.execute("DELETE FROM platform_users WHERE user_id >= 900000"))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean_state(pool):
    _run(pool.execute("DELETE FROM admin_state WHERE admin_id=$1", ADMIN_UID))
    _run(pool.execute("DELETE FROM platform_users WHERE user_id >= 900000"))
    yield


def _seed_users(pool, n=3):
    for i in range(n):
        _run(pool.execute(
            "INSERT INTO platform_users(user_id) VALUES($1) ON CONFLICT DO NOTHING",
            900000 + i,
        ))


def test_stray_text_with_stuck_broadcast_state_does_not_send(pool):
    """Ядро бага: admin_state='broadcast' застрял (меню покинули не доводя до
    конца) — следующее произвольное сообщение НЕ обязано улетать всем."""
    admin = _load_admin()
    admin._session_admins.add(ADMIN_UID)
    _seed_users(pool, 3)
    _run(pool.execute(
        "INSERT INTO admin_state(admin_id, state, data) VALUES($1,'broadcast','all')",
        ADMIN_UID,
    ))

    msg = _FakeMessage(ADMIN_UID, "привет, это вообще не для рассылки")
    _run(admin.handle_admin_message(msg, pool, http=None))

    assert msg.bot.sent == [], "текст ушёл пользователям без единого подтверждения"
    assert len(msg.answers) == 1, "должен появиться ровно один предпросмотр"
    assert "Отправить?" in msg.answers[0].last_edit_text
    assert "adm:bc_go:broadcast" in msg.answers[0].last_edit_text or \
        msg.answers[0].last_reply_markup is not None

    row = _run(pool.fetchrow(
        "SELECT state, data FROM admin_state WHERE admin_id=$1", ADMIN_UID))
    assert row is not None and row["state"] == "broadcast:confirm", (
        "текст обязан переводить состояние в подтверждение, а не отправлять")
    payload = json.loads(row["data"])
    assert payload["text"] == "привет, это вообще не для рассылки"


def test_confirm_button_actually_sends(pool):
    """Явное нажатие «Отправить» — и только оно — запускает реальную рассылку."""
    admin = _load_admin()
    admin._session_admins.add(ADMIN_UID)
    _seed_users(pool, 3)
    _run(pool.execute(
        "INSERT INTO admin_state(admin_id, state, data) VALUES($1,$2,$3)",
        ADMIN_UID, "broadcast:confirm", json.dumps({"text": "Новость дня", "seg": "all"}),
    ))

    msg = _FakeMessage(ADMIN_UID, None)
    cb = _FakeCallback(ADMIN_UID, "adm:bc_go:broadcast", msg)

    class _FakeState:
        async def get_data(self):
            return {}

        async def clear(self):
            pass

    _run(admin.cb_admin(cb, pool, http=None, state=_FakeState()))

    assert len(msg.bot.sent) == 3, "подтверждённая рассылка обязана дойти до всех"
    assert all(text == "Новость дня" for _, text in msg.bot.sent)

    row = _run(pool.fetchrow(
        "SELECT 1 FROM admin_state WHERE admin_id=$1", ADMIN_UID))
    assert row is None, "состояние подтверждения должно быть снято после отправки"


def test_cancel_button_sends_nothing_and_clears_state(pool):
    admin = _load_admin()
    admin._session_admins.add(ADMIN_UID)
    _seed_users(pool, 3)
    _run(pool.execute(
        "INSERT INTO admin_state(admin_id, state, data) VALUES($1,$2,$3)",
        ADMIN_UID, "broadcast:confirm", json.dumps({"text": "не отправлять", "seg": "all"}),
    ))

    msg = _FakeMessage(ADMIN_UID, None)
    cb = _FakeCallback(ADMIN_UID, "adm:bc_cancel", msg)

    class _FakeState:
        pass

    _run(admin.cb_admin(cb, pool, http=None, state=_FakeState()))

    assert msg.bot.sent == [], "отмена не должна ничего отправлять"
    row = _run(pool.fetchrow(
        "SELECT 1 FROM admin_state WHERE admin_id=$1", ADMIN_UID))
    assert row is None, "отмена обязана снимать admin_state"


def test_stale_confirm_after_manual_state_wipe_is_rejected(pool):
    """Если admin_state пропал (например, кто-то уже нажал «Отправить» в другой
    вкладке) — повторное нажатие не обязано падать и не должно слать повторно."""
    admin = _load_admin()
    admin._session_admins.add(ADMIN_UID)
    _seed_users(pool, 3)
    # admin_state сознательно НЕ ставим — имитируем «запрос устарел».

    msg = _FakeMessage(ADMIN_UID, None)
    cb = _FakeCallback(ADMIN_UID, "adm:bc_go:broadcast", msg)

    class _FakeState:
        pass

    _run(admin.cb_admin(cb, pool, http=None, state=_FakeState()))
    assert msg.bot.sent == [], "устаревшее подтверждение не должно ничего слать"
    assert cb.answered, "админу обязаны сообщить, что запрос устарел"


def test_returning_to_main_menu_clears_any_stuck_flow(pool, monkeypatch):
    """Общая защита класса бага: «◀️ Главное меню админки» теперь отменяет
    ЛЮБОЙ незавершённый флоу (не только рассылку) — раньше не отменял ничего."""
    admin = _load_admin()
    admin._session_admins.add(ADMIN_UID)
    monkeypatch.setattr(admin, "_show_admin_main", lambda *a, **k: asyncio.sleep(0))
    _run(pool.execute(
        "INSERT INTO admin_state(admin_id, state, data) VALUES($1,'block','')",
        ADMIN_UID,
    ))

    msg = _FakeMessage(ADMIN_UID, None)
    cb = _FakeCallback(ADMIN_UID, "adm:main", msg)

    class _FakeState:
        pass

    _run(admin.cb_admin(cb, pool, http=None, state=_FakeState()))

    row = _run(pool.fetchrow(
        "SELECT 1 FROM admin_state WHERE admin_id=$1", ADMIN_UID))
    assert row is None, "возврат в главное меню обязан снимать зависший admin_state"


def test_empty_confirmed_text_is_refused_not_sent_blank(pool):
    admin = _load_admin()
    admin._session_admins.add(ADMIN_UID)
    _seed_users(pool, 3)
    _run(pool.execute(
        "INSERT INTO admin_state(admin_id, state, data) VALUES($1,$2,$3)",
        ADMIN_UID, "broadcast:confirm", json.dumps({"text": "", "seg": "all"}),
    ))

    msg = _FakeMessage(ADMIN_UID, None)
    cb = _FakeCallback(ADMIN_UID, "adm:bc_go:broadcast", msg)

    class _FakeState:
        pass

    _run(admin.cb_admin(cb, pool, http=None, state=_FakeState()))
    assert msg.bot.sent == [], "пустой текст не должен уходить пользователям"
