"""ЛС и профиль контакта, у которого есть ТОЛЬКО Telegram ID.

Жалоба владельца (23.11.2026): контакт без @username и телефона, известен
лишь ID 5422366449; «Написать» → операция «ЛС контакту @5422366449» падает
«✅ 0 ❌ 1». Причина не в том, что ID принимали за юзернейм (send_dm цифры
в int переводил), а в том, что голый int уходил прямо в
`client.send_message`. Telegram адресует пользователя только парой
(id, access_hash); StringSession кэш сущностей между подключениями НЕ хранит,
поэтому на свежем клиенте Telethon падал «Could not find the input entity for
PeerUser(...)» даже у аккаунта, который с человеком переписывался. Ошибка
обрезалась до 80 символов и считалась сбоем отправки; второй выбранный
аккаунт (который мог человека знать) не пробовался вовсе.

Теперь: числовой ID резолвится (кэш → проверенный access_hash → диалоги →
адресная книга), неразрешимый ID — отдельный исход `unresolved` с понятной
причиной, и исполнитель пробует следующий аккаунт.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from services import account_manager, op_worker
from services.dm_engine import classify_send_result

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TG_ID = 5422366449


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeClient:
    """Поведение Telethon на свежей StringSession: кэша сущностей нет."""

    def __init__(self, dialogs=(), contacts=(), valid_hash=None):
        self.cache: dict[int, tuple] = {}
        self._dialogs = list(dialogs)
        self._contacts = list(contacts)
        self._valid_hash = valid_hash
        self.sent_to: list = []
        self.requests: list[str] = []

    async def connect(self):
        return None

    async def get_input_entity(self, peer):
        if isinstance(peer, int):
            if peer in self.cache:
                return self.cache[peer]
            raise ValueError(f"Could not find the input entity for PeerUser(user_id={peer})")
        return ("username", peer)

    async def get_dialogs(self, limit=None):
        self.requests.append("dialogs")
        for uid in self._dialogs:
            self.cache[uid] = ("peer", uid)
        return []

    async def __call__(self, request):
        name = type(request).__name__
        self.requests.append(name)
        if "Contacts" in name or not self._valid_hash:
            for uid in self._contacts:
                self.cache[uid] = ("peer", uid)
            if not self._valid_hash:
                return []
        # GetUsersRequest с подходящим хэшем
        return [type("User", (), {"id": _TG_ID})()]

    async def send_message(self, target, text):
        if isinstance(target, int) and target not in self.cache:
            raise ValueError(
                f"Could not find the input entity for PeerUser(user_id={target}) (PeerUser)")
        self.sent_to.append(target)


@pytest.fixture
def _client(monkeypatch):
    holder: dict = {}

    def _make(session, acc=None):
        return holder["client"]

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(account_manager, "_make_client", _make)
    monkeypatch.setattr(account_manager, "mark_online", _noop)
    monkeypatch.setattr(account_manager, "safe_disconnect", _noop)
    return holder


def test_send_dm_by_bare_id_resolves_through_dialogs(_client):
    """Аккаунт переписывался с человеком: ID находится в диалогах."""
    cl = _client["client"] = _FakeClient(dialogs=[_TG_ID])
    res = _run(account_manager.send_dm("s", str(_TG_ID), "привет"))
    assert res == {"ok": True}, res
    assert cl.sent_to == [("peer", _TG_ID)], (
        "голый ID ушёл в send_message без резолва — на свежей сессии это "
        "«Could not find the input entity»")


def test_send_dm_by_bare_id_resolves_through_address_book(_client):
    cl = _client["client"] = _FakeClient(contacts=[_TG_ID])
    res = _run(account_manager.send_dm("s", str(_TG_ID), "привет"))
    assert res == {"ok": True}, res
    assert cl.sent_to == [("peer", _TG_ID)]


def test_send_dm_uses_known_access_hash(_client):
    """Сохранённый синком access_hash проверяется и используется без прогрева."""
    cl = _client["client"] = _FakeClient(valid_hash=777)
    res = _run(account_manager.send_dm("s", str(_TG_ID), "привет", access_hash=777))
    assert res == {"ok": True}, res
    assert len(cl.sent_to) == 1 and cl.sent_to[0] != _TG_ID
    assert "dialogs" not in cl.requests, "хэш подошёл — прогрев диалогов лишний"


def test_unknown_bare_id_is_a_clear_unresolved_outcome(_client):
    _client["client"] = _FakeClient()
    res = _run(account_manager.send_dm("s", str(_TG_ID), "привет"))
    assert res.get("peer_unresolved") is True, res
    assert "Could not find" not in res["error"]
    assert "по ID" in res["error"]
    assert classify_send_result(res) == "unresolved"


def test_username_target_is_untouched(_client):
    cl = _client["client"] = _FakeClient()
    res = _run(account_manager.send_dm("s", "@vasya", "привет"))
    assert res == {"ok": True}
    assert cl.sent_to == ["vasya"]


# ── Исполнитель: следующий аккаунт, честная причина ────────────────────────

class _FakePool:
    def __init__(self):
        self.logged: list[tuple] = []
        self.budget: list = []

    async def fetch(self, query, *args):
        return []

    async def fetchrow(self, query, *args):
        if "SELECT status FROM operation_queue" in query:
            return {"status": "running"}
        return None

    async def fetchval(self, query, *args):
        return None

    async def execute(self, query, *args):
        if "INSERT INTO operation_log" in query:
            self.logged.append((args[2], args[3], args[4]))
        return "UPDATE 1"

    async def executemany(self, query, rows):
        return None


@pytest.fixture
def _dm(monkeypatch):
    calls: list[tuple] = []
    deeps: list = []
    knows: set[int] = set()

    async def _send(session, username, text, _acc=None, access_hash=None, deep=None):
        calls.append((int(_acc["id"]), username, access_hash))
        deeps.append(deep)
        if int(_acc["id"]) in knows:
            return {"ok": True}
        return {"error": account_manager.PEER_UNRESOLVED_TEXT, "peer_unresolved": True}

    async def _select(pool, owner_id, include_ids=None, min_trust_score=0.0):
        return [{"id": 11, "session_str": "s"}, {"id": 12, "session_str": "s"}]

    async def _claim(ids):
        return list(ids)

    async def _noop(*_a, **_k):
        return None

    async def _quar(pool, ids):
        return set()

    async def _opted(pool, owner_id):
        return set()

    budget: list = []

    async def _budget(pool, owner_id, acc_id, kind, result=None, operation_id=None):
        budget.append((acc_id, result))

    _real_sleep = asyncio.sleep

    async def _no_sleep(*_a, **_k):
        await _real_sleep(0)

    monkeypatch.setattr(account_manager, "send_dm", _send)
    monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _select)
    monkeypatch.setattr(op_worker, "try_claim_accounts", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _noop)
    monkeypatch.setattr(op_worker._infra_mem, "quarantined_accounts", _quar)
    monkeypatch.setattr(op_worker, "_record_budget_actions", _budget)
    monkeypatch.setattr(op_worker.asyncio, "sleep", _no_sleep)
    from services import contact_opt_out
    monkeypatch.setattr(contact_opt_out, "load_opted_out", _opted)
    op_worker._cancel_cache.clear()
    return {"calls": calls, "knows": knows, "budget": budget, "deeps": deeps}


_PARAMS = {"account_ids": [11, 12], "usernames": [str(_TG_ID)], "text": "привет"}


def test_unresolved_on_one_account_tries_the_next(_dm):
    _dm["knows"].add(12)
    res = _run(op_worker._exec_bulk_dm_adhoc(_FakePool(), None, 918, 777, dict(_PARAMS)))
    assert [c[0] for c in _dm["calls"]] == [11, 12], (
        "аккаунт 11 не знает человека — нужно было попробовать аккаунт 12")
    assert res["ok"] == 1 and res["fail"] == 0, res
    assert _dm["budget"] == [(12, "sent")], "неотправленная попытка не тратит бюджет ЛС"


def test_source_accounts_go_first_and_get_the_hint(_dm):
    _dm["knows"].add(12)
    params = dict(_PARAMS, preferred_account_ids=[12], peer_hints={str(_TG_ID): 555})
    res = _run(op_worker._exec_bulk_dm_adhoc(_FakePool(), None, 918, 777, params))
    assert _dm["calls"] == [(12, str(_TG_ID), 555)], _dm["calls"]
    assert res["ok"] == 1


def test_nobody_knows_the_id_gives_an_honest_reason(_dm):
    pool = _FakePool()
    res = _run(op_worker._exec_bulk_dm_adhoc(pool, None, 918, 777, dict(_PARAMS)))
    assert [c[0] for c in _dm["calls"]] == [11, 12]
    assert res["ok"] == 0 and res["fail"] == 1
    assert "без доступа по ID" in res["summary"], res["summary"]
    assert any("нет доступа по ID" in msg for _t, _st, msg in pool.logged), pool.logged
    assert _dm["budget"] == []


def test_deep_hint_reaches_send_for_a_single_contact(_dm):
    _dm["knows"].add(11)
    deep = {"chats": ["somegroup"], "name": "Hang"}
    params = dict(_PARAMS, peer_deep={str(_TG_ID): deep})
    _run(op_worker._exec_bulk_dm_adhoc(_FakePool(), None, 918, 777, params))
    assert _dm["deeps"] == [deep]


def test_deep_search_is_off_for_a_mass_blast(_dm):
    """Поиск по группам на сотни получателей — сотни запросов и флуд."""
    _dm["knows"].update({11, 12})
    many = [str(_TG_ID + i) for i in range(5)]
    params = dict(_PARAMS, usernames=many,
                  peer_deep={r: {"chats": ["g"], "name": "x"} for r in many})
    _run(op_worker._exec_bulk_dm_adhoc(_FakePool(), None, 918, 777, params))
    assert _dm["deeps"] and all(d is None for d in _dm["deeps"])


# ── Без переписки: поиск в группах и адресная книга ─────────────────────────

class _GroupClient(_FakeClient):
    """Человек есть только среди участников группы — переписки не было."""

    def __init__(self, member_in: str, name: str):
        super().__init__()
        self._member_in = member_in
        self._name = name
        self.searches: list[tuple] = []

    async def get_entity(self, ref):
        return type("Chan", (), {"id": 100, "ref": ref, "participants_count": 50000})()

    async def get_participants(self, entity, search="", limit=None):
        self.searches.append((getattr(entity, "ref", None), search))
        if getattr(entity, "ref", None) == self._member_in and search == self._name:
            self.cache[_TG_ID] = ("peer", _TG_ID)
        return []


def test_send_dm_without_any_chat_finds_him_in_the_source_group(_client):
    cl = _client["client"] = _GroupClient("hang_group", "Hang")
    res = _run(account_manager.send_dm(
        "s", str(_TG_ID), "привет",
        deep={"chats": ["@other", "hang_group"], "name": "Hang"}))
    assert res == {"ok": True}, res
    assert cl.sent_to == [("peer", _TG_ID)]
    assert ("hang_group", "Hang") in cl.searches


def test_big_group_is_not_scanned_without_a_name(_client):
    cl = _client["client"] = _GroupClient("hang_group", "Hang")
    res = _run(account_manager.send_dm(
        "s", str(_TG_ID), "привет", deep={"chats": ["hang_group"], "name": ""}))
    assert res.get("peer_unresolved") is True
    assert cl.searches == [], "перебор группы на 50 000 участников — путь к флуду"


def test_add_to_contacts_resolves_and_adds(_client):
    cl = _client["client"] = _GroupClient("hang_group", "Hang")
    peer = _run(account_manager.add_user_to_contacts(
        cl, _TG_ID, "Hang", deep={"chats": ["hang_group"], "name": "Hang"}))
    assert peer == ("peer", _TG_ID)
    calls = [r for r in cl.requests if r != "dialogs"]
    assert len(calls) == 3, (
        "ожидались users.getUsers без ключа, contacts.getContacts (резолв) "
        f"и contacts.addContact: {calls}")


def test_add_to_contacts_unknown_person_is_unresolved(_client):
    cl = _client["client"] = _FakeClient()
    with pytest.raises(account_manager.PeerUnresolvedError):
        _run(account_manager.add_user_to_contacts(cl, _TG_ID, "Hang"))


class _HistoryClient(_FakeClient):
    """Список участников скрыт, но в истории чата есть след человека."""

    def __init__(self, how: str):
        super().__init__()
        self.how = how
        self.history_reads: list = []

    async def get_entity(self, ref):
        return type("Chan", (), {"id": 100, "ref": ref, "participants_count": 90000})()

    async def get_participants(self, entity, search="", limit=None):
        return []                     # скрытые участники

    async def get_input_entity(self, peer):
        if not isinstance(peer, int):
            return ("chat", getattr(peer, "ref", peer))
        return await super().get_input_entity(peer)

    async def get_messages(self, entity, limit=None):
        self.history_reads.append((entity.ref, limit))
        other = type("M", (), {"id": 1, "sender_id": 42, "fwd_from": None, "entities": []})()
        if self.how == "author":
            m = type("M", (), {"id": 7, "sender_id": _TG_ID, "fwd_from": None, "entities": []})()
        elif self.how == "forward":
            fwd = type("F", (), {"from_id": type("P", (), {"user_id": _TG_ID})()})()
            m = type("M", (), {"id": 8, "sender_id": 42, "fwd_from": fwd, "entities": []})()
        else:
            ent = type("E", (), {"user_id": _TG_ID})()
            m = type("M", (), {"id": 9, "sender_id": 42, "fwd_from": None, "entities": [ent]})()
        return [other, m]

    async def __call__(self, request):
        self.requests.append(type(request).__name__)
        # users.getUsers по inputUserFromMessage отдаёт полного пользователя.
        if len(self.history_reads) > 0:
            self.cache[_TG_ID] = ("peer", _TG_ID)
        return []


@pytest.mark.parametrize("how", ["author", "forward", "mention"])
def test_key_from_a_message_when_members_are_hidden(_client, how):
    """Скрытые участники: человека находит его сообщение в истории чата."""
    cl = _client["client"] = _HistoryClient(how)
    res = _run(account_manager.send_dm(
        "s", str(_TG_ID), "привет", deep={"chats": ["hang_group"], "name": "Hang",
                                         "scan_groups": False}))
    assert res == {"ok": True}, res
    assert cl.sent_to == [("peer", _TG_ID)]
    assert cl.history_reads == [("hang_group", account_manager._DEEP_SOURCE_HISTORY)]


def test_zero_hash_answer_is_used(_client):
    """Telegram отдал пользователя на запрос без ключа — берём его ключ."""
    class _Zero(_FakeClient):
        async def __call__(self, request):
            self.requests.append("getUsers0")
            return [type("U", (), {"id": _TG_ID, "access_hash": 99, "min": False})()]

    cl = _client["client"] = _Zero()
    res = _run(account_manager.send_dm("s", str(_TG_ID), "привет"))
    assert res == {"ok": True}, res
    assert "dialogs" not in cl.requests, "ключ уже есть — прогрев диалогов лишний"


# ── Мини-апп ────────────────────────────────────────────────────────────────

def test_profile_button_is_not_a_dead_tg_link():
    html = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert 'href="tg://user?id=' not in html, (
        "ссылка tg://user?id= из мини-аппа не открывается — профиль по ID "
        "должен идти через /tg_profile")
    assert "openContactProfile()" in html
    src = (_ROOT / "services" / "mini_app_uch.py").read_text(encoding="utf-8")
    assert "/uch/contacts/{contact_id}/tg_profile" in src
    assert "/uch/contacts/{contact_id}/tg_add" in src
    assert "openContactTgAdd()" in html
    assert 'label=f"ЛС контакту @{ref}' not in src, (
        "голый ID в подписи с собачкой выглядит как юзернейм")
