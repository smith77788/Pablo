"""Один контракт на все движки инвайта и одна дорога к ним.

Почему храповик, а не тесты по случаям. Дыры инвайта этой осени были одной
породы: новая дверь или новый движок жили по своим правилам. Флуд терял цель
(её писали в «ошибки» и в дедуп), бот звал людей мимо журнала приглашений и
мимо суточного лимита, промоут-трюк оставлял постороннего админом. Каждую
закрывали поштучно — этот файл закрывает породу:

1. КОНТРАКТ (поведение, живыми вызовами с заглушкой Telegram). Каждый движок
   на долгой паузе Telegram и на PeerFlood: возвращает всех непробованных в
   `untried`, не пишет их в отказы и поднимает сигнал флуда наверх. Новый
   движок добавляется в ENGINES — иначе падает пункт 2.

2. ГДЕ ЗОВУТ TELEGRAM. Сырые InviteToChannel / EditAdmin — только в известных
   функциях. Новый вызов в другом месте — это новая дверь мимо контракта.

3. КТО ЗОВЁТ ДВИЖКИ. Только исполнитель массового инвайта и дверь бота «из
   контактов» (карточка канала ставит операцию `mass_invite`) — они сверяются с журналом приглашений, лимитом и пульсом. Новый вызывающий
   обязан пройти тот же путь и вписаться сюда осознанно.

4. ЖУРНАЛ ПРИГЛАШЁННЫХ пишется одной функцией (`_record_invited_targets`), а
   снаружи исполнителя — через `services/invite_dedup`.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telethon.errors import FloodWaitError, PeerFloodError  # стаб-классы из conftest

from services import account_manager
from services import mass_inviter_engine as mie

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ["@a", "@b", "@c"]
PHONES = ["+79000000001", "+79000000002"]


def _flood(seconds: int = 900) -> FloodWaitError:
    e = FloodWaitError("flood")
    e.seconds = seconds
    return e


class _Client:
    """Telegram, который отвечает заданной ошибкой на первое действие.

    `import_first` — первый вызов отдаёт результат импорта контактов (путь по
    номерам), ошибка приходит на первом приглашении.
    """

    def __init__(self, exc, import_first=False):
        self.exc = exc
        self.import_first = import_first
        self.calls = 0
        self.disconnect = AsyncMock()

    async def connect(self):
        return None

    def is_connected(self):
        return True

    async def get_entity(self, ref):
        return ref

    async def __call__(self, req):
        self.calls += 1
        if self.import_first and self.calls == 1:
            users = [SimpleNamespace(id=i + 1, username=None) for i in range(len(PHONES))]
            return SimpleNamespace(
                users=users,
                imported=[SimpleNamespace(client_id=i, user_id=u.id) for i, u in enumerate(users)])
        if self.exc is not None:
            exc, self.exc = self.exc, None
            raise exc
        return SimpleNamespace(missing_invitees=[])


def _engine_patches(client):
    async def _connect(*a, **k):
        return client

    async def _group(c, ref, acc_id=None):
        return "GRP"
    return [
        patch.object(account_manager, "connect_client", _connect),
        patch.object(account_manager, "_make_client", lambda *a, **k: client),
        patch.object(account_manager, "timeboxed", lambda c: c),
        patch.object(account_manager, "_resolve_channel_peer", AsyncMock(return_value="GRP")),
        patch.object(mie, "_resolve_group_entity", _group),
        patch.object(asyncio, "sleep", AsyncMock()),
        patch("services.flood_engine.record_flood", AsyncMock(return_value=0)),
        patch("services.flood_engine.record_peer_flood", AsyncMock(return_value=0)),
    ]


async def _call(name, exc):
    """Вызвать движок `name` с Telegram, отвечающим `exc`. Вернуть (итог, цели)."""
    if name == "invite_via_link_batch":
        dm = {"flood_wait": 900} if isinstance(exc, FloodWaitError) else {"peer_flood": True}
        with patch.object(account_manager, "send_dm", AsyncMock(return_value=dm)), \
                patch.object(asyncio, "sleep", AsyncMock()):
            return await mie.invite_via_link_batch("s", {"id": 1}, "https://t.me/+x",
                                                   TARGETS, "{link}"), TARGETS
    client = _Client(exc, import_first=(name == "invite_by_phones"))
    ps = _engine_patches(client)
    for p in ps:
        p.start()
    try:
        if name == "invite_batch":
            return await mie.invite_batch("s", {"id": 1}, "@g", TARGETS, bulk=False), TARGETS
        if name == "invite_batch[bulk]":
            return await mie.invite_batch("s", {"id": 1}, "@g", TARGETS, bulk=True), TARGETS
        if name == "add_via_promote":
            return await mie.add_via_promote("s", {"id": 1}, "@g", TARGETS), TARGETS
        if name == "invite_by_phones":
            return await mie.invite_by_phones("s", {"id": 1}, "@g", PHONES), PHONES
        if name == "invite_users_to_channel":
            return await account_manager.invite_users_to_channel(
                "s", 100, TARGETS, _acc={"id": 1}), TARGETS
        raise AssertionError(name)
    finally:
        for p in ps:
            p.stop()


# Все движки, которые сами зовут Telegram приглашать. Новый — сюда.
ENGINES = ["invite_batch", "invite_batch[bulk]", "add_via_promote", "invite_by_phones",
           "invite_via_link_batch", "invite_users_to_channel"]


def _failed_n(res) -> int:
    f = res.get("failed")
    return len(f) if isinstance(f, list) else int(f or 0)


@pytest.mark.parametrize("name", ENGINES)
@pytest.mark.parametrize("kind", ["long_flood", "peer_flood"])
def test_flood_returns_everyone_untried_and_signals(name, kind):
    exc = _flood(900) if kind == "long_flood" else PeerFloodError("peer")
    res, targets = asyncio.run(_call(name, exc))
    assert list(res.get("untried") or []) == list(targets), (
        f"{name}: на флуде непробованные должны вернуться в untried — иначе их "
        "никто не позовёт или они уйдут в дедуп как обработанные")
    assert _failed_n(res) == 0, f"{name}: флуд — отказ аккаунту, а не людям"
    if kind == "long_flood":
        assert int(res.get("flood_wait") or 0) == 900, f"{name}: пауза не поднята наверх"
    else:
        assert res.get("peer_flood"), f"{name}: PeerFlood не поднят наверх"


# ── статика: где зовут Telegram и кто зовёт движки ───────────────────────────

_RAW = {"InviteToChannelRequest", "EditAdminRequest"}
_ENGINE_FUNCS = {"invite_batch", "add_via_promote", "invite_by_phones",
                 "invite_via_link_batch", "invite_users_to_channel", "revoke_promoted"}

RAW_ALLOWED = {
    ("services/mass_inviter_engine.py", "_try_bulk_invite._send"),
    ("services/mass_inviter_engine.py", "invite_batch"),
    ("services/mass_inviter_engine.py", "add_via_promote"),
    ("services/mass_inviter_engine.py", "_revoke_admin"),
    ("services/mass_inviter_engine.py", "invite_by_phones"),
    ("services/account_manager.py", "invite_users_to_channel"),
    # выдача и снятие прав инвайтерам (не приглашение людей)
    ("services/account_manager.py", "promote_to_admin_ex"),
    ("services/account_manager.py", "demote_from_admin"),
    ("services/account_manager.py", "demote_from_admin_batch"),
    # добавление СВОЕГО бота админом в свой канал — не люди
    ("services/brand_injection.py", "add_botmother_as_channel_admin"),
}

CALLERS_ALLOWED = {
    ("services/op_worker.py", "_exec_mass_invite"),
    ("services/op_worker.py", "_exec_mass_invite._fire"),
    ("services/op_worker.py", "_exec_mass_invite._lf_send"),
    ("bot/handlers/channel_ops.py", "_cinv_bg_inner._invite_one"),
}

JOURNAL_WRITERS = {
    ("services/op_worker.py", "_exec_mass_invite"),
    ("services/op_worker.py", "_exec_mass_invite._remember_invited"),
    ("services/invite_dedup.py", "remember"),
}


def _calls(names: set) -> set:
    out: set = set()
    for f in sorted(ROOT.rglob("*.py")):
        rel = f.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "deploy/")) or "/." in rel:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue

        def visit(node, fn):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = node.name if fn is None else f"{fn}.{node.name}"
            if isinstance(node, ast.Call):
                c = node.func
                nm = c.attr if isinstance(c, ast.Attribute) else getattr(c, "id", None)
                if nm in names:
                    out.add((rel, fn or "<module>", nm))
            for ch in ast.iter_child_nodes(node):
                visit(ch, fn)
        visit(tree, None)
    return out


def test_raw_telegram_invites_only_in_known_engines():
    bad = sorted((f, fn, nm) for f, fn, nm in _calls(_RAW) if (f, fn) not in RAW_ALLOWED)
    assert not bad, (
        "Приглашение через Telegram мимо движков инвайта (контракт untried, флуд в "
        f"пульс, журнал приглашённых): {bad}. Зовите движок из mass_inviter_engine "
        "или, если это новый движок, добавьте его в ENGINES и RAW_ALLOWED.")


def test_engines_are_called_only_by_doors_that_dedup_and_limit():
    bad = sorted((f, fn, nm) for f, fn, nm in _calls(_ENGINE_FUNCS)
                 if (f, fn) not in CALLERS_ALLOWED
                 and not f.startswith("services/mass_inviter_engine.py"))
    assert not bad, (
        "Новая дверь инвайта: " + repr(bad) + ". Она обязана сверяться с журналом "
        "(services/invite_dedup.filter_new), брать лимит (account_allowance) и "
        "учитывать итог (settle) — как двери бота. Сделав это, впишите её в "
        "CALLERS_ALLOWED.")


def test_invite_journal_has_one_writer():
    bad = sorted((f, fn) for f, fn, _ in _calls({"_record_invited_targets"})
                 if (f, fn) not in JOURNAL_WRITERS)
    assert not bad, f"Журнал приглашённых пишут в обход invite_dedup.remember: {bad}"
    raw_sql = [f.relative_to(ROOT).as_posix() for f in ROOT.rglob("*.py")
               if not f.relative_to(ROOT).as_posix().startswith("tests/")
               and "INSERT INTO invite_target_log" in f.read_text(encoding="utf-8",
                                                                  errors="ignore")]
    assert raw_sql == ["services/op_worker.py"], raw_sql


def test_detectors_see_known_sites():
    """Самопроверка: детектор находит то, что точно есть (иначе он молчит зря)."""
    raw = {(f, fn) for f, fn, _ in _calls(_RAW)}
    assert ("services/mass_inviter_engine.py", "invite_batch") in raw
    callers = {(f, fn) for f, fn, _ in _calls(_ENGINE_FUNCS)}
    assert ("bot/handlers/channel_ops.py", "_cinv_bg_inner._invite_one") in callers
