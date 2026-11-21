"""Очистка аккаунтов: отбор целей и защита своего.

Регрессии, которые здесь стерегутся:
* исполнитель очереди (мини-апп «Очистка» и массовое «выйти из всех» в списке
  аккаунтов) выходил из ВСЕХ групп и каналов, включая каналы, которые аккаунт
  ведёт в системе: защита каталога была только у бота;
* удаление личных диалогов сносило «Избранное» (диалог с самим собой) и
  служебный чат Telegram с кодами входа (777000);
* экран умел один аккаунт и две операции без настроек — теперь несколько
  аккаунтов, отбор, пробный прогон.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from services import account_cleaner as ac
from services import op_worker

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)


def _chan(id, *, group=False, creator=False, admin=None, username=None, days_ago=1, name="c"):
    ent = NS(id=id, creator=creator, admin_rights=admin, username=username)
    return NS(is_group=group, is_channel=True, is_user=False, entity=ent,
              date=NOW - timedelta(days=days_ago), name=name)


def _pm(id, *, is_self=False, bot=False, days_ago=1):
    ent = NS(id=id, is_self=is_self, bot=bot)
    return NS(is_group=False, is_channel=False, is_user=True, entity=ent,
              date=NOW - timedelta(days=days_ago), name=str(id))


def test_norm_chat_ref_forms():
    assert ac.norm_chat_ref("-1001234567890") == "1234567890"
    assert ac.norm_chat_ref("1234567890") == "1234567890"
    assert ac.norm_chat_ref("@MyChan") == "mychan"
    assert ac.norm_chat_ref("https://t.me/MyChan/") == "mychan"
    assert ac.norm_chat_ref("") == ""
    assert ac.parse_whitelist("@a, t.me/b\n-1001234567890 @a") == ["a", "b", "1234567890"]


def test_own_channels_are_kept_by_default():
    dialogs = [
        _chan(1),                                  # чужой — выходим
        _chan(2, creator=True),                    # создатель
        _chan(3, admin=object()),                  # админ
        _chan(1234567890),                         # из каталога (-100 форма)
    ]
    picked, sk = ac.pick_chats_to_leave(dialogs, own_ids=[-1001234567890], now=NOW)
    assert [d.entity.id for d in picked] == [1]
    assert sk["own"] == 3
    # Явно разрешили трогать своё — уходят все.
    picked, _ = ac.pick_chats_to_leave(dialogs, keep_own=False, own_ids=[-1001234567890], now=NOW)
    assert len(picked) == 4


def test_kinds_whitelist_and_inactivity():
    dialogs = [
        _chan(1, group=False, username="news"),     # канал из белого списка
        _chan(2, group=False, days_ago=200),        # старый канал
        _chan(3, group=True, days_ago=200),         # старая группа
        _chan(4, group=True, days_ago=2),           # свежая группа
    ]
    picked, sk = ac.pick_chats_to_leave(dialogs, whitelist=["@News"], now=NOW)
    assert [d.entity.id for d in picked] == [2, 3, 4] and sk["whitelist"] == 1
    picked, _ = ac.pick_chats_to_leave(dialogs, kinds="channels", whitelist=["news"], now=NOW)
    assert [d.entity.id for d in picked] == [2]
    picked, sk = ac.pick_chats_to_leave(dialogs, kinds="groups", inactive_days=90, now=NOW)
    assert [d.entity.id for d in picked] == [3] and sk["active"] == 1


def test_private_dialogs_never_touch_saved_messages_and_codes():
    dialogs = [_pm(10, is_self=True), _pm(777000), _pm(11), _pm(12, bot=True)]
    picked, sk = ac.pick_private_dialogs(dialogs, now=NOW)
    assert [d.entity.id for d in picked] == [11, 12] and sk["protected"] == 2
    picked, _ = ac.pick_private_dialogs(dialogs, keep_bots=True, now=NOW)
    assert [d.entity.id for d in picked] == [11]


def test_contacts_non_mutual():
    us = [NS(id=1, mutual_contact=True), NS(id=2, mutual_contact=False), NS(id=3)]
    assert [u.id for u in ac.pick_contacts(us, "non_mutual")] == [2, 3]
    assert len(ac.pick_contacts(us, "all")) == 3


class _Pool:
    def __init__(self, own=()):
        self.acc = {"id": 1, "session_str": "s", "proxy_url": ""}
        self.own = own
        self.executed = []

    async def fetchrow(self, q, *a):
        return self.acc

    async def fetch(self, q, *a):
        if "managed_channels" in q:
            return [{"channel_id": c} for c in self.own]
        return []

    async def fetchval(self, q, *a):
        return None

    async def execute(self, q, *a):
        self.executed.append(q)
        return "OK"


def _client(dialogs):
    c = AsyncMock()
    c.get_dialogs = AsyncMock(return_value=dialogs)
    return c


def _run_exec(fn, pool, client, params):
    with patch("services.account_manager._make_client", return_value=client), \
         patch.object(op_worker, "_is_cancelled", AsyncMock(return_value=False)), \
         patch.object(op_worker, "cancellable_pause", AsyncMock(return_value=0)), \
         patch.object(op_worker, "release_accounts", AsyncMock()):
        return asyncio.run(fn(pool, None, 5, 99, params))


def test_executor_keeps_catalog_channel_of_account():
    mine, other = _chan(1234567890), _chan(55)
    client = _client([mine, other])
    res = _run_exec(op_worker._exec_leave_all_chats, _Pool(own=[-1001234567890]), client,
                    {"account_id": 1})
    left = [c.args[0] for c in client.delete_dialog.call_args_list]
    assert left == [other.entity], "свой канал из каталога покинут"
    assert res["ok"] == 1 and "свои 1" in res["summary"]


def test_executor_dry_run_deletes_nothing():
    client = _client([_chan(1), _chan(2)])
    res = _run_exec(op_worker._exec_leave_all_chats, _Pool(), client,
                    {"account_id": 1, "dry_run": True})
    client.delete_dialog.assert_not_called()
    assert res["would_leave"] == 2 and "Пробный прогон" in res["summary"]


def test_executor_private_keeps_saved_messages():
    me, codes, friend = _pm(10, is_self=True), _pm(777000), _pm(11)
    client = _client([me, codes, friend])
    res = _run_exec(op_worker._exec_delete_private_dialogs, _Pool(), client, {"account_id": 1})
    deleted = [c.args[0] for c in client.delete_dialog.call_args_list]
    assert deleted == [friend.entity]
    assert res["deleted"] == 1


def test_executor_contacts_non_mutual_and_counts():
    us = [NS(id=1, mutual_contact=True), NS(id=2, mutual_contact=False)]
    client = _client([])
    client.side_effect = None
    client.return_value = NS(users=us)
    res = _run_exec(op_worker._exec_delete_contacts, _Pool(), client,
                    {"account_id": 1, "contacts_mode": "non_mutual"})
    assert res["ok"] == 1 and res["deleted"] == 1
