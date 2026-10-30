"""Синхронизация контактов «как с устройства» при подключении аккаунта.

Владелец (05.10 и 11.10.2026): хранилище контактов создано, чтобы пустые
аккаунты флота получали контакты в свою книгу Telegram, и это должно
происходить сразу при подключении, как с реального устройства. Раньше
хранилище только читало книги аккаунтов, обратно не выгружало ничего, и
аккаунт без контактов не мог работать с людьми из хранилища.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import account_manager
from services.contacts_hub import device_sync

ROOT = Path(__file__).resolve().parents[1]


class _Client:
    def __init__(self, book, flood_on=None):
        self.book = book
        self.flood_on = flood_on
        self.imports: list = []

    async def __call__(self, req):
        if isinstance(req, _Import):
            if self.flood_on is not None and len(self.imports) == self.flood_on:
                from telethon.errors import FloodWaitError
                e = FloodWaitError("flood")
                e.seconds = 300
                raise e
            self.imports.append([c.phone for c in req.contacts])
            return SimpleNamespace(imported=[object()] * len(req.contacts))
        return SimpleNamespace(users=[SimpleNamespace(phone=p) for p in self.book])

    async def disconnect(self):
        return None


class _Get:
    def __init__(self, hash=0):
        self.hash = hash


class _Import:
    def __init__(self, contacts):
        self.contacts = contacts


class _Phone:
    def __init__(self, client_id, phone, first_name, last_name):
        self.phone = phone
        self.first_name = first_name


@pytest.fixture
def client(monkeypatch):
    import telethon.tl.functions.contacts as tfc
    import telethon.tl.types as tt
    monkeypatch.setattr(tfc, "GetContactsRequest", _Get, raising=False)
    monkeypatch.setattr(tfc, "ImportContactsRequest", _Import, raising=False)
    monkeypatch.setattr(tt, "InputPhoneContact", _Phone, raising=False)

    def _install(c):
        async def _connect(*a, **k):
            return c
        monkeypatch.setattr(account_manager, "connect_client", _connect)

        async def _fast(*a, **k):
            return None
        monkeypatch.setattr(device_sync.asyncio, "sleep", _fast)
        return c
    return _install


def _contacts(n):
    return [{"phone": f"+7900{i:07d}", "first_name": f"К{i}", "last_name": ""}
            for i in range(n)]


def test_only_missing_contacts_are_sent_in_batches(client):
    c = client(_Client(book=["79000000000", "79000000001"]))
    res = asyncio.run(device_sync.push_contacts("s", {"id": 1}, _contacts(205)))
    assert res["already"] == 2
    assert res["sent"] == 203 and res["imported"] == 203
    assert [len(b) for b in c.imports] == [100, 100, 3], "порции как у клиента Telegram"
    assert "+79000000000" not in c.imports[0], "уже в книге — повторно не шлём"


def test_flood_stops_and_counts_the_rest(client):
    client(_Client(book=[], flood_on=1))
    res = asyncio.run(device_sync.push_contacts("s", {"id": 1}, _contacts(250)))
    assert res["sent"] == 100 and res["flood_wait"] == 300
    assert res["left"] == 150


def test_cap_per_pass(client, monkeypatch):
    monkeypatch.setenv("CONTACTS_PUSH_CAP", "100")
    client(_Client(book=[]))
    res = asyncio.run(device_sync.push_contacts("s", {"id": 1}, _contacts(130)))
    assert res["sent"] == 100 and res["left"] == 30


def test_hub_contacts_skip_excluded_and_dedup():
    class _Pool:
        async def fetch(self, q, *a):
            assert "excluded" in q, "личные контакты не должны уезжать в книги флота"
            return [{"first_name": "Аня", "last_name": "", "display_name": "",
                     "phones": json.dumps(["79001112233", "+7 900 111-22-33"])},
                    {"first_name": "", "last_name": "", "display_name": "",
                     "phones": ["79005556677"]}]
    out = asyncio.run(device_sync.hub_contacts(_Pool(), 1))
    assert [c["phone"] for c in out] == ["+79001112233", "+79005556677"]
    assert out[1]["first_name"] == "+79005556677", "имя обязательно — подставляем номер"


@pytest.mark.parametrize("path,needle", [
    ("database/db.py", "async def upsert_tg_account("),
    ("database/db.py", "async def add_tg_account("),
    ("services/session_importer.py", "async def import_sessions("),
    ("bot/handlers/auto_registrar.py", "async def _save_account("),
])
def test_every_connect_path_schedules_sync(path, needle):
    """Синхронизацию ставит общая запись аккаунта; каждый вход идёт через неё."""
    src = (ROOT / path).read_text(encoding="utf-8")
    i = src.index(needle)
    j = src.find("\nasync def ", i + 1)
    body = src[i:j if j > 0 else None]
    assert ("schedule_for_new_account(" in body
            or "upsert_tg_account(" in body), f"{path}: подключение без синхронизации"


def test_schedule_goes_through_operation_bus(monkeypatch):
    from services import operation_bus
    seen = []

    async def _submit(pool, owner, op_type, params, **kw):
        seen.append((owner, op_type, params))
        return 9
    monkeypatch.setattr(operation_bus, "submit", _submit)
    assert asyncio.run(device_sync.schedule_for_new_account(None, 5, 7)) == 9
    assert seen == [(5, "contacts_push", {"account_ids": [7]})]


def test_schedule_failure_never_breaks_connect(monkeypatch):
    from services import operation_bus

    async def _boom(*a, **k):
        raise RuntimeError("очередь недоступна")
    monkeypatch.setattr(operation_bus, "submit", _boom)
    assert asyncio.run(device_sync.schedule_for_new_account(None, 5, 7)) is None


def test_executor_pushes_hub_and_reads_book_back(monkeypatch):
    from database import db
    from services import op_worker
    from services.contacts_hub import sync_service

    pulled: list = []

    async def _acc(pool, acc_id, owner):
        return {"id": acc_id, "phone": "+7999", "session_str": "s"}

    async def _hub(pool, owner):
        return _contacts(3)

    async def _push(sess, acc, contacts):
        return {"already": 1, "sent": 2, "imported": 2, "left": 0, "flood_wait": 0}

    async def _pull(pool, owner, acc_id):
        pulled.append(acc_id)
        return {}

    async def _yes(*a, **k):
        return True

    async def _no(*a, **k):
        return False

    async def _none(*a, **k):
        return None
    monkeypatch.setattr(db, "get_account_for_telethon", _acc)
    monkeypatch.setattr(device_sync, "hub_contacts", _hub)
    monkeypatch.setattr(device_sync, "push_contacts", _push)
    monkeypatch.setattr(sync_service, "sync_account", _pull)
    monkeypatch.setattr(op_worker, "try_claim_account", _yes)
    monkeypatch.setattr(op_worker, "release_accounts", _none)
    monkeypatch.setattr(op_worker, "_safe_execute", _none)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _no)
    res = asyncio.run(op_worker._exec_contacts_push(None, 1, 5, {"account_ids": [7]}))
    assert res["status"] == "done" and res["ok"] == 2
    assert "добавлено 2" in res["summary"]
    assert pulled == [7], "хранилище должно узнать, что контакты теперь у аккаунта"
