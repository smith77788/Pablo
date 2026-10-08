"""Из бота нельзя поставить операцию, которую исполнитель отказывается делать.

ЧТО БЫЛО. «Фабрика каналов → ✏️ Редактировать» вела владельца по четырём
экранам (поле → охват → значение → предпросмотр), на «✅ Применить» ставила
операцию `bulk_edit_channels` и писала «✅ Редактирование каналов запущено,
операция #N в очереди». А исполнитель этой операции к тому времени был
осознанно отключён и отвечал отказом: «Массовая смена названий отключена —
выберите один канал». То есть владелец проходил весь диалог, видел «запущено»
и получал операцию, падавшую сразу, ничего не изменив.

Полоса прогресса у неё тоже стояла на нуле — именно на это и указал храповик
`test_op_progress_is_reported`: операция объявляла total_items=len(accounts), а
done_items ей писать было нечем, потому что работы не происходило вовсе.

ПЕРВЫЙ ФИКС убрал ложь, но вместе с ней и само действие: название из бота
перестало меняться вовсе, владельца отправляли в мини-апп — там есть шаг
подтверждения состава, а в боте его не было.

ЧТО ТЕПЕРЬ. Шаг подтверждения есть и в боте. Экран ставит ту же операцию, что
мини-апп: `bulk_chan_exec` парами «канал + аккаунт». Переименование уходит
только с подтверждением (`channel_change_approval`), выданным на конкретный
список и значение: массовая смена названий необратима (старые названия нигде не
хранятся), поэтому исполнитель принимает её только подтверждённой. Предпросмотр
показывает РЕАЛЬНЫЙ список «старое → новое», а не одно лишь новое значение, и
подписывается именно он.
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_HANDLER = "bot/handlers/channel_factory.py"


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_bot_does_not_queue_the_disabled_operation():
    src = _read(_HANDLER)
    assert "bulk_edit_channels" not in src, (
        "бот снова ставит отключённую операцию — она падает сразу после "
        "«запущено»")
    assert '"bulk_chan_exec"' in src, (
        "редактирование каналов должно идти через живого исполнителя")


def test_the_disabled_executor_really_refuses():
    """Сторож самой проверки: если отказ убрали, тест выше нужно переписать."""
    ow = _read("services/op_worker.py")
    start = ow.index("async def _exec_bulk_edit_channels(")
    body = ow[start:start + 600]
    assert '"status": "failed"' in body, (
        "исполнитель bulk_edit_channels больше не отказывает — проверьте, "
        "не вернули ли ему работу")


def test_title_change_is_submitted_only_with_an_approval():
    src = _read(_HANDLER)
    confirm = src[src.index('F.action == "be_confirm"'):]
    confirm = confirm[:confirm.index("\n# ═")]
    assert "channel_change_approval.verify(" in confirm, (
        "массовое переименование уходит без подтверждения — исполнитель его "
        "отвергнет, а владелец увидит «запущено»")
    assert '"approval_token"' in confirm
    assert "channel_acc_pairs" in confirm, (
        "исполнитель работает парами «канал + аккаунт», а не списком аккаунтов")


def test_preview_shows_the_real_list_of_channels():
    src = _read(_HANDLER)
    preview = src[src.index("async def fsm_be_value("):]
    preview = preview[:preview.index('F.action == "be_confirm"')]
    assert "_be_targets(" in preview, (
        "предпросмотр считается по числу аккаунтов, а меняются каналы")
    assert "Каналов:" in preview and "→" in preview, (
        "владелец обязан видеть, что именно изменится (старое → новое)")
    assert "channel_change_approval.issue(" in preview, (
        "подтверждение выдаётся на показанный список, иначе оно ничего не "
        "подтверждает")


# ── Поведение подтверждения ──────────────────────────────────────────────────

class _Pool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, q, *a):
        assert "managed_channels" in q
        return list(self._rows)


class _State:
    def __init__(self, data):
        self._data = dict(data)
        self.cleared = False

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self.cleared = True


class _Msg:
    def __init__(self):
        self.text = ""

    async def edit_text(self, text, **kw):
        self.text = text


class _User:
    id = 4242


class _Callback:
    def __init__(self):
        self.message = _Msg()
        self.from_user = _User()

    async def answer(self, *a, **kw):
        return None


PAIRS = [
    {"channel_id": 101, "acc_id": 7, "title": "Старое A",
     "access_hash": 1, "username": None},
    {"channel_id": 102, "acc_id": 7, "title": "Старое B",
     "access_hash": 2, "username": None},
]


@pytest.fixture
def stand(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "x" * 40)
    from bot.handlers import channel_factory
    from services import operation_bus

    submitted: list[tuple] = []

    async def _submit(pool, owner_id, op_type, params, **kw):
        submitted.append((op_type, params, kw))
        return 555

    monkeypatch.setattr(operation_bus, "submit", _submit)
    return channel_factory, submitted


def test_confirm_submits_the_approved_rename(stand):
    channel_factory, submitted = stand
    from services import channel_change_approval

    token = channel_change_approval.issue(_User.id, "Новое имя", PAIRS)
    cb, state = _Callback(), _State(
        {"edit_field": "title", "edit_value": "Новое имя",
         "be_acc_ids": [7], "be_token": token, "be_scope": "account"})

    asyncio.run(channel_factory.cb_chanf_be_confirm(cb, state, _Pool(PAIRS)))

    assert submitted, f"операция не поставлена: {cb.message.text!r}"
    op_type, params, kw = submitted[0]
    assert op_type == "bulk_chan_exec"
    assert params["op"] == "chan_title"
    assert [p["channel_id"] for p in params["channel_acc_pairs"]] == [101, 102]
    assert params["approval_token"] == token
    assert kw.get("total_items") == 2, "полоса прогресса считается по каналам"
    assert "#555" in cb.message.text


def test_a_changed_list_is_not_renamed_on_an_old_approval(stand):
    """Подтверждение выдано на два канала — третий не должен уехать с ними."""
    channel_factory, submitted = stand
    from services import channel_change_approval

    token = channel_change_approval.issue(_User.id, "Новое имя", PAIRS)
    grown = PAIRS + [{"channel_id": 103, "acc_id": 7, "title": "Старое C",
                      "access_hash": 3, "username": None}]
    cb, state = _Callback(), _State(
        {"edit_field": "title", "edit_value": "Новое имя",
         "be_acc_ids": [7], "be_token": token, "be_scope": "account"})

    asyncio.run(channel_factory.cb_chanf_be_confirm(cb, state, _Pool(grown)))

    assert not submitted, "переименование ушло по устаревшему подтверждению"
    assert "подтвержд" in cb.message.text.lower()


def test_description_change_needs_no_approval(stand):
    """Описание переписывается без подтверждения: оно не теряет имя канала."""
    channel_factory, submitted = stand
    cb, state = _Callback(), _State(
        {"edit_field": "about", "edit_value": "Новое описание",
         "be_acc_ids": [7], "be_token": "", "be_scope": "account"})

    asyncio.run(channel_factory.cb_chanf_be_confirm(cb, state, _Pool(PAIRS)))

    assert submitted, f"операция не поставлена: {cb.message.text!r}"
    op_type, params, _kw = submitted[0]
    assert op_type == "bulk_chan_exec" and params["op"] == "chan_about"
    assert "approval_token" not in params


def test_nothing_is_queued_when_there_are_no_channels(stand):
    channel_factory, submitted = stand
    cb, state = _Callback(), _State(
        {"edit_field": "about", "edit_value": "Текст",
         "be_acc_ids": [7], "be_token": "", "be_scope": "account"})

    asyncio.run(channel_factory.cb_chanf_be_confirm(cb, state, _Pool([])))

    assert not submitted, "поставлена операция без единого канала"
    assert "нет каналов" in cb.message.text.lower()


def test_executor_accepts_what_the_bot_sends():
    """Ключи параметров обязаны совпадать с теми, что читает исполнитель."""
    ow = _read("services/op_worker.py")
    start = ow.index("async def _exec_bulk_chan_exec(")
    body = ow[start:start + 2500]
    for key in ("channel_acc_pairs", "approval_token", "value", "base_uname"):
        assert re.search(rf'params\.get\(\s*"{key}"', body), (
            f"исполнитель не читает {key} — бот отправляет его впустую")
    assert '"chan_title"' in body and '"chan_about"' in body
