"""Связки модулей: итог одной операции — вход следующей (services/op_chain).

Что было. Парсинг сохранял базу, и чтобы пригласить ИМЕННО её, человек шёл в
инвайтер и выбирал базу из списка заново; авторегистрация создавала аккаунты,
а прогревать их приходилось, разыскивая каждый в списке; созданные каналы
вручную собирались в папку. Подсказка после операции была одной на тип
(«открыть раздел») и не знала, что именно операция произвела. В боте —
ни одной такой кнопки.

Тесты держат: контракт передачи (`handoff`) у производителей, шаги по нему,
запуск только с сервера и только по своим целям, и то, что ОБЕ двери берут
шаги из одного места.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import op_chain as oc  # noqa: E402
from services import op_quick_actions as qa  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _func_src(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"функция {name} не найдена")


# ── шаги по итогу ────────────────────────────────────────────────────────────


def test_parse_result_opens_invite_and_campaign_for_this_base():
    steps = oc.steps_for("parse_audience", "done", {},
                         {"total_saved": 120, "handoff": {"parse_run_id": 7}})
    by = {s["id"]: s for s in steps}
    assert by["inv_run"]["screen"] == "invite_run:7"
    assert by["cmp_run"]["screen"] == "campaign_run:7"
    assert by["inv_run"]["bot"] == {"cb": "inviter_from_run", "arg": 7}


def test_empty_or_failed_result_gives_no_next_module():
    assert oc.steps_for("parse_audience", "done", {},
                        {"total_saved": 0, "handoff": {"parse_run_id": 7}}) == []
    assert oc.steps_for("parse_audience", "failed", {},
                        {"total_saved": 9, "handoff": {"parse_run_id": 7}}) == []
    assert oc.steps_for("auto_register", "cancelled", {},
                        {"handoff": {"account_ids": [1]}}) == []


def test_registered_accounts_go_to_warmup_and_check():
    steps = oc.steps_for("auto_register", "partial", {},
                         {"handoff": {"account_ids": [5, "6", 5, "x"]}})
    by = {s["id"]: s for s in steps}
    assert by["warm_new"]["kind"] == "launch"
    assert by["warm_new"]["data"]["account_ids"] == [5, 6]
    assert by["check_new"]["launcher"] == "check_accounts"


def test_created_channels_link_to_folder_admin_and_invite():
    many = {s["id"]: s for s in oc.steps_for(
        "bulk_create_channels", "done", {},
        {"handoff": {"channel_ids": [11, 12], "chan_kind": "channel"}})}
    assert many["folder"]["data"]["channel_ids"] == [11, 12]
    assert many["va"]["screen"] == "va"
    one = {s["id"]: s for s in oc.steps_for(
        "bulk_create_channels", "done", {},
        {"handoff": {"channel_ids": [11], "chan_kind": "channel"}})}
    assert "folder" not in one, "папка из одного канала бессмысленна"
    assert one["inv_chan"]["screen"] == "invite_channel:11"
    assert one["va"]["screen"] == "va_channel:11"
    groups = {s["id"] for s in oc.steps_for(
        "bulk_create_channels", "done", {},
        {"handoff": {"channel_ids": [11, 12], "chan_kind": "group"}})}
    assert "va" not in groups, "администратор ведёт каналы, не группы"


def test_result_stored_as_json_text_is_understood():
    steps = oc.steps_for("parse_audience", "done", {},
                         '{"total_saved": 3, "handoff": {"parse_run_id": 2}}')
    assert steps and steps[0]["screen"] == "invite_run:2"


def test_every_launch_step_has_a_launcher():
    samples = [
        ("auto_register", {"handoff": {"account_ids": [1]}}),
        ("bulk_create_channels", {"handoff": {"channel_ids": [1, 2]}}),
        ("scan_owned_bots", {"new": 3}),
    ]
    for op_type, res in samples:
        for st in oc.steps_for(op_type, "done", {}, res):
            if st["kind"] == "launch":
                assert st["launcher"] in oc._LAUNCHERS, st
                assert st["confirm"].strip()
                assert "data" not in oc.public_step(st)


# ── производители отдают handoff ─────────────────────────────────────────────


def test_producers_put_handoff_into_result():
    worker = _src("services/op_worker.py")
    for fn in ("_exec_parse_audience", "_exec_auto_register",
               "_exec_bulk_create_channels"):
        assert '"handoff"' in _func_src(worker, fn), f"{fn} не отдаёт handoff"
    assert '"account_ids": ok_ids' in _src("bot/handlers/auto_registrar.py")


async def test_created_channels_read_from_journal_including_retries(monkeypatch):
    from services import op_worker as ow

    async def _journal(pool, op_id):
        return [op_id, 3]

    async def _fetch(pool, q, *a, **k):
        assert "operation_log" in q and a[0] == [9, 3]
        return [{"message": "channel_id=-1001 @a"}, {"message": "channel_id=55"},
                {"message": "channel_id=55"}, {"message": None}]

    monkeypatch.setattr(ow, "journal_op_ids", _journal)
    monkeypatch.setattr(ow, "_safe_fetch", _fetch)
    assert await ow._created_channel_ids(object(), 9) == [-1001, 55]


# ── запуск: только с сервера и только своё ───────────────────────────────────


class _Pool:
    def __init__(self, op_row, owned):
        self.op_row, self.owned = op_row, owned
        self.calls = []

    async def fetchrow(self, q, *a):
        self.calls.append((q, a))
        if "FROM operation_queue" in q:
            assert "owner_id=$2" in q
            return self.op_row if a[1] == 1 else None
        return None

    async def fetch(self, q, *a):
        self.calls.append((q, a))
        assert "owner_id=$1" in q
        return [{"id": i} for i in a[1] if i in self.owned]


async def test_launch_warms_only_own_accounts(monkeypatch):
    from services import operation_bus as ob

    sent = []

    async def _submit(pool, owner, op_type, params, **kw):
        sent.append((owner, op_type, params))
        return 100 + len(sent)

    monkeypatch.setattr(ob, "submit", _submit)
    pool = _Pool({"op_type": "auto_register", "status": "done", "params": "{}",
                  "result": {"handoff": {"account_ids": [1, 2, 3]}}}, owned={1, 3})
    r = await oc.launch(pool, 1, 50, "warm_new")
    assert r["ok"] and r["op_ids"] == [101, 102]
    assert [p["account_id"] for _o, _t, p in sent] == [1, 3]
    assert all(t == "account_warmup" and p["plan_type"] == "gentle" for _o, t, p in sent)


async def test_launch_refuses_foreign_op_and_open_steps():
    pool = _Pool({"op_type": "parse_audience", "status": "done", "params": {},
                  "result": {"total_saved": 5, "handoff": {"parse_run_id": 4}}}, owned=set())
    assert (await oc.launch(pool, 2, 50, "inv_run"))["reason"] == "Операция не найдена"
    r = await oc.launch(pool, 1, 50, "inv_run")
    assert not r["ok"], "шаг-экран не запускается как операция"
    assert not (await oc.launch(pool, 1, 50, "nope"))["ok"]


async def test_launch_turns_plan_refusal_into_reason(monkeypatch):
    from services import operation_bus as ob

    async def _submit(*a, **k):
        raise PermissionError("Нужен тариф Pro")

    monkeypatch.setattr(ob, "submit", _submit)
    pool = _Pool({"op_type": "auto_register", "status": "done", "params": {},
                  "result": {"handoff": {"account_ids": [1]}}}, owned={1})
    r = await oc.launch(pool, 1, 50, "check_new")
    assert r == {"ok": False, "reason": "Нужен тариф Pro"}


# ── обе двери из одного источника ────────────────────────────────────────────


def test_miniapp_quick_actions_carry_chain_steps():
    out = qa.suggest("parse_audience", "done",
                     {"total_saved": 4, "handoff": {"parse_run_id": 8}}, op_id=31)
    by = {a["id"]: a for a in out}
    assert by["chain_inv_run"]["fn"] == "openChainStep"
    assert by["chain_inv_run"]["arg"] == "invite_run:8"
    assert not [a for a in out if a["id"].startswith("next_")], (
        "общий «открыть раздел» не нужен, когда есть конкретная связка")
    out = qa.suggest("auto_register", "done", {"handoff": {"account_ids": [1]}}, op_id=31)
    warm = [a for a in out if a["id"] == "chain_warm_new"][0]
    assert warm["fn"] == "launchChainStep" and warm["arg"] == "31:warm_new"


def test_miniapp_functions_and_screens_exist():
    ui = _src("mini_app/index.html")
    for fn in ("openChainStep", "launchChainStep"):
        assert re.search(r"function\s+" + fn + r"\s*\(", ui), fn
    assert "/api/miniapp/op_chain/launch" in _func_src(_src("services/mini_app_api.py"),
                                                      "setup_routes")
    body = ui[ui.index("function runPulseAction(a)"):]
    body = body[:body.index("\n}\n")]
    samples = [
        ("parse_audience", {"total_saved": 1, "handoff": {"parse_run_id": 1}}),
        ("bulk_create_channels", {"handoff": {"channel_ids": [1]}}),
        ("bulk_create_channels", {"handoff": {"channel_ids": [1, 2]}}),
    ]
    for op_type, res in samples:
        for st in oc.steps_for(op_type, "done", {}, res):
            if st["kind"] == "open":
                kind = st["screen"].split(":")[0]
                assert f"k==='{kind}'" in body, f"раздел {kind} не разбирается мини-аппом"


def test_both_doors_launch_through_op_chain():
    api = _src("services/mini_app_api.py")
    assert "_oc.launch(" in _func_src(api, "op_chain_launch")
    assert "op_chain.launch(" in _func_src(_src("bot/handlers/op_chain.py"), "chain_go")
    worker = _src("services/op_worker.py")
    assert "add_chain_buttons(" in worker and "_op_chain.steps_for(" in worker
    assert "op_chain_handler.router" in _src("main.py")


def test_bot_buttons_for_steps():
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.handlers import op_chain as h

    kb = InlineKeyboardBuilder()
    steps = oc.steps_for("auto_register", "done", {}, {"handoff": {"account_ids": [1]}})
    steps += oc.steps_for("parse_audience", "done", {},
                          {"total_saved": 2, "handoff": {"parse_run_id": 6}})
    assert h.add_chain_buttons(kb, 77, steps) >= 3
    datas = [b.callback_data for row in kb.as_markup().inline_keyboard for b in row
             if b.callback_data]
    assert "chn:go:77:warm_new" in datas
    assert "inv:from_run:6" in datas


def test_bot_inviter_accepts_prefilled_base():
    src = _src("bot/handlers/mass_inviter.py")
    entry = _func_src(src, "cb_inviter_from_run")
    assert "owner_id=$1 AND parse_run_id=$2" in entry
    group = _func_src(src, "msg_inviter_group")
    assert "prefill_run" in group and 'action="pick_run"' in group
