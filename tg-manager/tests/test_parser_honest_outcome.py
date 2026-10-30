"""Парсер аудитории: честный итог запуска (D7, D10, D11, D20, D26).

Что ловит этот файл.

D7. Любая ошибка Telegram внутри разбора, кроме флуда (CHAT_ADMIN_REQUIRED у
    вещательного канала, сеть), была `break`: запуск закрывался «найдено 0»
    или неполным списком без причины. Владелец не мог отличить пустой
    источник от закрытого.

D10. Исключение вне внутреннего `try` (тайм-аут подключения, сбой посреди
    `iter_messages`) и отмена задачи оставляли запуск «в работе» навсегда.

D11. Бот разбирал сам, мимо шины операций: без тарифа, отмены, истории и
    передачи базы дальше.

D20/D26. Потолок выдачи Telegram не объяснялся; итог операции нёс сырой
    `{exc}`; выгрузка с телефонами отдавалась с CORS «*».
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest

from services import account_manager, infra_memory, op_status, parser

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ── Фальшивый Telegram ──────────────────────────────────────────────────────


class ChatAdminRequiredError(Exception):
    """Тот же класс и текст, что у Telethon."""

    def __init__(self):
        super().__init__(
            "Chat admin privileges are required to do that in the specified chat "
            "(caused by GetParticipantsRequest)")


class _U:
    def __init__(self, uid: int):
        self.id = uid
        self.username = f"u{uid}"
        self.first_name = "Имя"
        self.last_name = None
        self.premium = False
        self.bot = False
        self.deleted = False
        self.status = None


class _Page:
    def __init__(self, users, count):
        self.users = users
        self.count = count


class _Entity:
    def __init__(self, broadcast=False):
        self.id = 4242
        self.title = "Источник"
        self.username = "src"
        self.broadcast = broadcast


class _Client:
    """pages — что отдаёт каждый GetParticipants: список пользователей или
    исключение. connect_exc — чем падает подключение."""

    def __init__(self, pages=(), count=0, connect_exc=None, entity=None,
                 messages=(), entity_exc=None):
        self._pages = list(pages)
        self._count = count
        self._connect_exc = connect_exc
        self._entity = entity or _Entity()
        self._messages = list(messages)
        self._entity_exc = entity_exc
        self.disconnected = False

    async def connect(self):
        if self._connect_exc:
            raise self._connect_exc

    async def disconnect(self):
        self.disconnected = True

    async def get_entity(self, ref):
        if isinstance(ref, int) and ref != self._entity.id:
            if self._entity_exc:
                raise self._entity_exc
            return _U(ref)
        return self._entity

    async def __call__(self, request):
        if not self._pages:
            return _Page([], self._count)
        nxt = self._pages.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return _Page(nxt, self._count)

    def iter_messages(self, entity, limit=None):
        msgs = list(self._messages)

        async def _gen():
            for m in msgs:
                if isinstance(m, BaseException):
                    raise m
                yield m

        return _gen()


class _Msg:
    def __init__(self, sender_id):
        self.sender_id = sender_id
        self.date = datetime.now(timezone.utc)


@pytest.fixture
def harness(monkeypatch):
    """Разбор без БД и без Telegram: запись итога запуска перехватывается."""
    state = {"client": None, "updates": [], "released": [], "saved": []}

    async def _claim(pool, owner_id):
        return {"id": 9, "session_str": "s"}, 9

    async def _release(acc_id):
        state["released"].append(acc_id)

    async def _create(*a, **k):
        return 77

    async def _update(pool, run_id, status, total_found=0, total_saved=0,
                      error=None, source_id=None):
        state["updates"].append({"run_id": run_id, "status": status,
                                 "found": total_found, "error": error})

    async def _save(pool, owner_id, run_id, st, sid, title, uname, users):
        state["saved"].extend(users)
        return len(users)

    async def _nosleep(*_a, **_k):
        return None

    monkeypatch.setattr(parser, "_claim_best_account", _claim)
    monkeypatch.setattr(parser, "_release_account", _release)
    monkeypatch.setattr(parser, "_create_run", _create)
    monkeypatch.setattr(parser, "_update_run", _update)
    monkeypatch.setattr(parser, "_save_users", _save)
    monkeypatch.setattr(parser.asyncio, "sleep", _nosleep)
    monkeypatch.setattr(infra_memory, "record_account_op", lambda *a, **k: None)
    monkeypatch.setattr(account_manager, "_make_client",
                        lambda *a, **k: state["client"])
    return state


def _final(state):
    assert len(state["updates"]) == 1, (
        f"запуск должен закрыться ровно одной записью итога: {state['updates']!r}")
    return state["updates"][0]


# ── D7: ошибка — не «найдено 0» ─────────────────────────────────────────────


def test_admin_only_channel_is_a_failure_with_reason(harness):
    harness["client"] = _Client(pages=[ChatAdminRequiredError()])
    res = asyncio.run(parser.parse_members(None, 1, "@chan"))

    assert res["status"] == "error"
    assert "администратор" in res["error"]
    run = _final(harness)
    assert run["status"] == "failed", "закрытый список — это не пустой источник"
    assert "администратор" in run["error"]
    assert "caused by" not in run["error"], "сырой текст Telethon не для владельца"


def test_break_after_some_users_is_partial_with_reason(harness):
    page = [_U(i) for i in range(200)]
    harness["client"] = _Client(pages=[page, ConnectionError("proxy 1.2.3.4 refused")],
                                count=900)
    res = asyncio.run(parser.parse_members(None, 1, "@grp", limit=1000))

    assert res["status"] == "done" and res["partial"] is True
    assert res["total_found"] == 200
    run = _final(harness)
    assert run["status"] == "done"
    assert "не вся аудитория" in run["error"]
    assert "1.2.3.4" not in run["error"], "адрес прокси утёк владельцу"


def test_really_empty_source_is_empty_without_error(harness):
    harness["client"] = _Client(pages=[[]], count=0)
    res = asyncio.run(parser.parse_members(None, 1, "@grp"))

    assert res["status"] == "empty"
    run = _final(harness)
    assert run["status"] == "empty" and run["error"] is None


def test_unknown_source_is_named(harness):
    class UsernameNotOccupiedError(Exception):
        pass

    client = _Client()

    async def _bad_entity(ref):
        raise UsernameNotOccupiedError("The username is not in use by anyone else yet")

    client.get_entity = _bad_entity
    harness["client"] = client
    res = asyncio.run(parser.parse_members(None, 1, "@nope"))
    assert res["status"] == "error"
    assert "не найден" in res["error"]


# ── D20: потолок выдачи объяснён ────────────────────────────────────────────


def test_telegram_ceiling_is_explained(harness):
    # Telegram называет 25 000, а отдаёт одну неполную страницу.
    harness["client"] = _Client(pages=[[_U(i) for i in range(150)]], count=25000)
    res = asyncio.run(parser.parse_members(None, 1, "@big", limit=5000))

    assert res["status"] == "done"
    run = _final(harness)
    assert "150 из 25000" in run["error"]


# ── D10: запуск всегда доходит до терминального статуса ─────────────────────


def test_connect_timeout_closes_the_run(harness):
    harness["client"] = _Client(connect_exc=asyncio.TimeoutError())
    res = asyncio.run(parser.parse_members(None, 1, "@grp"))

    assert res["status"] == "error"
    run = _final(harness)
    assert run["status"] == "failed"
    assert "прокси" in run["error"]
    assert harness["released"] == [9], "аренда аккаунта не отпущена"


def test_task_cancellation_closes_the_run_and_propagates(harness):
    started = asyncio.Event()

    class _Hanging(_Client):
        async def __call__(self, request):
            started.set()
            await asyncio.Event().wait()

    harness["client"] = _Hanging()

    async def _go():
        t = asyncio.create_task(parser.parse_members(None, 1, "@grp"))
        await started.wait()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(_go())
    run = _final(harness)
    assert run["status"] == "cancelled"
    assert harness["released"] == [9]


def test_owner_stop_through_the_bus_is_cancelled(harness):
    harness["client"] = _Client(pages=[[_U(i) for i in range(200)]] * 3, count=600)
    calls = {"n": 0}

    async def _stop():
        calls["n"] += 1
        return calls["n"] > 1   # первая пачка проходит, вторая — уже отмена

    res = asyncio.run(parser.parse_members(None, 1, "@grp", should_stop=_stop))
    assert res["status"] == "cancelled" and res["total_found"] == 200
    assert _final(harness)["status"] == "cancelled"


def test_iter_messages_failure_closes_active_run(harness):
    harness["client"] = _Client(messages=[_Msg(1), _Msg(2), OSError("socket closed")])
    res = asyncio.run(parser.parse_active_users(None, 1, "@grp"))

    assert res["status"] == "done" and res["partial"] is True
    run = _final(harness)
    assert run["status"] == "done" and "оборван" in run["error"]


def test_active_on_broadcast_channel_says_why(harness):
    harness["client"] = _Client(entity=_Entity(broadcast=True), messages=[_Msg(-100123)])
    res = asyncio.run(parser.parse_active_users(None, 1, "@chan"))
    assert res["status"] == "error"
    assert "канал, а не группа" in res["error"]


def test_channel_posts_are_not_people(harness):
    harness["client"] = _Client(messages=[_Msg(-100123), _Msg(5), _Msg(-100123)])
    res = asyncio.run(parser.parse_active_users(None, 1, "@grp"))
    assert res["total_found"] == 1
    assert [u["id"] for u in harness["saved"]] == [5]


def test_dead_network_stops_sender_collection(harness):
    msgs = [_Msg(1000 + i) for i in range(50)]
    harness["client"] = _Client(messages=msgs,
                                entity_exc=asyncio.TimeoutError())
    res = asyncio.run(parser.parse_active_users(None, 1, "@grp"))
    assert res["status"] == "error"
    assert "прокси" in res["error"]


# ── Причины человеческим языком ────────────────────────────────────────────


@pytest.mark.parametrize("exc, needle", [
    (ChatAdminRequiredError(), "администратор"),
    (Exception("USER_NOT_PARTICIPANT"), "не состоит"),
    (Exception("CHANNEL_PRIVATE"), "закрыт"),
    (ValueError('Cannot find any entity corresponding to "zz"'), "не найден"),
    (asyncio.TimeoutError(), "прокси"),
    (ConnectionError("10.0.0.1:1080 general SOCKS server failure"), "прокси"),
])
def test_explain_parse_error(exc, needle):
    text = parser.explain_parse_error(exc)
    assert needle in text
    assert "10.0.0.1" not in text and "caused by" not in text


def test_unknown_error_shows_only_the_class_name():
    class WeirdError(Exception):
        pass

    text = parser.explain_parse_error(WeirdError("secret token abc123"))
    assert "WeirdError" in text and "abc123" not in text


# ── Исполнитель шины: честный финальный статус ──────────────────────────────


def _run_exec(monkeypatch, result):
    from services import op_worker as ow

    async def _fake(*a, **k):
        return dict(result)

    for name in ("parse_members", "parse_active_users", "parse_commenters"):
        monkeypatch.setattr(parser, name, _fake)

    async def _noop(*a, **k):
        return "UPDATE 1"

    monkeypatch.setattr(ow, "_safe_execute", _noop)
    return asyncio.run(ow._exec_parse_audience(
        None, None, 5, 1, {"source_ref": "<b>x</b>", "parse_type": "members"}))


def test_exec_partial_becomes_partial(monkeypatch):
    out = _run_exec(monkeypatch, {"status": "done", "partial": True, "run_id": 3,
                                  "total_found": 200, "total_saved": 150,
                                  "note": "Разбор оборван: связь. Собрано 200"})
    final = op_status.classify_final(out["status"], ok=out["ok"], failed=0,
                                     done_items=200, total_items=200)
    assert final == op_status.PARTIAL
    assert "оборван" in out["summary"]
    assert "<b>x</b>" not in out["summary"], "источник не экранирован в HTML-итоге"


def test_exec_failure_carries_reason_not_raw_exception(monkeypatch):
    out = _run_exec(monkeypatch, {"status": "error", "run_id": 3,
                                  "error": "Список подписчиков канала Telegram показывает "
                                           "только его администраторам."})
    assert out["status"] == "failed"
    assert "администратор" in out["summary"]


def test_exec_complete_parse_is_done_not_partial(monkeypatch):
    # 300 человек при лимите 500 — полный разбор, а не частичный.
    out = _run_exec(monkeypatch, {"status": "done", "run_id": 3,
                                  "total_found": 300, "total_saved": 0})
    final = op_status.classify_final(out["status"], ok=out["ok"], failed=0,
                                     done_items=300, total_items=300)
    assert final == op_status.DONE
    assert out["handoff"] == {"parse_run_id": 3}


def test_exec_empty_is_done_and_says_so(monkeypatch):
    out = _run_exec(monkeypatch, {"status": "empty", "run_id": 3,
                                  "total_found": 0, "total_saved": 0})
    assert out["status"] == "done" and out["ok"] == 0
    assert "не нашлось" in out["summary"]


def test_exec_exception_is_explained(monkeypatch):
    from services import op_worker as ow

    async def _boom(*a, **k):
        raise ConnectionError("proxy 9.9.9.9 refused")

    monkeypatch.setattr(parser, "parse_members", _boom)
    out = asyncio.run(ow._exec_parse_audience(
        None, None, 5, 1, {"source_ref": "x", "parse_type": "members"}))
    assert out["status"] == "failed" and "9.9.9.9" not in out["summary"]


# ── D11: бот идёт через шину ────────────────────────────────────────────────


def test_bot_does_not_parse_by_itself():
    src = _read("bot/handlers/audience_parser.py")
    for fn in ("parse_members(", "parse_active_users(", "parse_commenters("):
        assert fn not in src, f"бот снова разбирает сам: {fn}"
    assert '"parse_audience"' in src and "operation_bus.submit(" in src


def test_bot_submits_parse_to_the_bus(monkeypatch):
    from bot.handlers import audience_parser as ap
    from services import infra_orchestrator, operation_bus

    submitted = {}

    async def _submit(pool, owner_id, op_type, params, **kw):
        submitted.update(owner=owner_id, op=op_type, params=params, label=kw.get("label"))
        return 321

    async def _ready(pool, owner_id):
        return True, ""

    monkeypatch.setattr(operation_bus, "submit", _submit)
    monkeypatch.setattr(infra_orchestrator, "is_ready_for_op", _ready)

    class _State:
        async def get_data(self):
            return {"parse_source": "@grp", "parse_type": "comments"}

        async def clear(self):
            pass

    sent = []

    class _Target:
        async def answer(self, text, **kw):
            sent.append(text)

    asyncio.run(ap._start_parse(_Target(), _State(), None, 7, 800, days_back=7))

    assert submitted["op"] == "parse_audience" and submitted["owner"] == 7
    assert submitted["params"] == {"source_ref": "@grp", "parse_type": "comments",
                                   "limit": 800, "days_back": 7}
    assert "комментаторов" in submitted["label"]
    assert sent and "#321" in sent[-1]


def test_bot_plan_gate_is_the_bus_plan():
    from bot.handlers import audience_parser as ap
    from services.operation_bus import OP_REGISTRY

    assert ap._parse_plan() == OP_REGISTRY["parse_audience"]["min_plan"]


# ── D26: подписи и выгрузка ─────────────────────────────────────────────────


def test_miniapp_label_is_russian():
    src = _read("services/mini_app_parser.py")
    assert 'f"Парсинг {parse_type}' not in src
    assert "PARSE_TYPE_LABELS" in src


def test_audience_export_is_not_cors_open():
    for rel in ("services/mini_app_parser.py",):
        src = _read(rel)
        body = src[src.index("async def parsed_audience_export"):]
        body = body[:body.index("async def submit_parse_job")]
        assert '"Access-Control-Allow-Origin": "*"' not in body
    api = _read("services/mini_app_api.py")
    csv = api[api.index("def _csv_resp("):]
    csv = csv[:csv.index("\ndef ", 10)]
    assert "Access-Control-Allow-Origin" not in csv
