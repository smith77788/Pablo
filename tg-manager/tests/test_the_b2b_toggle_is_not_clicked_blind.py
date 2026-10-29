"""Нечитаемое состояние тумблера — не повод нажать и доложить об успехе.

ЧТО СТОИТ НА КОНУ. Режим bot-to-bot включается ТОЛЬКО кликом по пункту меню
@BotFather: API для этого нет, и пункт — это ТУМБЛЕР, а не кнопка «включить».
Нажатие по уже включённому режиму его ВЫКЛЮЧАЕТ. Разборщик меню честен и
возвращает `state='unknown'`, когда подпись не говорит о состоянии (подписи
BotFather локале-зависимы и на живой сессии не проверены — об этом сказано в
докстринге модуля). А вызывающий сравнивал только с `'on'`, то есть на
`unknown` нажимал наугад:

  * на боте, где режим уже включён, операция его ВЫКЛЮЧАЛА — результат, прямо
    обратный запросу владельца;
  * и записывала имя в `enabled`, откуда исполнитель ставит
    `managed_bots.b2b_enabled=TRUE`. Дальше `plan_targets(only_disabled=True)`
    такого бота не берёт НИКОГДА, то есть ложная запись прячет поломку
    навсегда, а bot_mesh спотыкается о бота без видимой причины.

Второй провал того же места — исход. Исполнитель возвращал `done`, даже когда
не включил ни одного бота: ошибки уходили в текст сводки, а статус операции
говорил «выполнено». Для недоведённой работы в продукте есть `partial`.

ЕЩЁ ОДНО: СПИСКИ ПРИЗНАКОВ БЫЛИ МЁРТВЫМИ. `_ON_MARKERS` и `_OFF_MARKERS`
объявлены в модуле, но `find_b2b_toggle` их не читал — у него были свои,
более узкие наборы прямо в теле. Поэтому подпись вида «Bot-to-Bot Mode:
enabled» (состояние словом, а не предложением действия) уходила в `unknown` —
прямо в тот самый слепой клик. Ратчет ниже проверяет каждый признак из обоих
списков: список, который никто не читает, со временем начинает врать.
"""
from __future__ import annotations

import asyncio
import ast
import pathlib

import pytest

from services import bot_b2b as B

ROOT = pathlib.Path(__file__).resolve().parents[1]
OPW = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


# ── Разборщик: состояние словом читается так же, как предложением действия ──

def test_state_words_are_read_not_only_action_words():
    assert B.find_b2b_toggle(["Bot-to-Bot Mode: enabled"])["state"] == "on"
    assert B.find_b2b_toggle(["Bot-to-Bot Mode: disabled"])["state"] == "off"


def test_the_marker_lists_are_not_dead():
    """Каждый признак из обоих списков обязан давать своё состояние."""
    bad = []
    for m in B._ON_MARKERS:
        got = B.find_b2b_toggle([f"Bot-to-Bot {m}"])["state"]
        if got != "on":
            bad.append(f"{m!r} -> {got}, ожидалось on")
    for m in B._OFF_MARKERS:
        got = B.find_b2b_toggle([f"Bot-to-Bot {m}"])["state"]
        if got != "off":
            bad.append(f"{m!r} -> {got}, ожидалось off")
    assert not bad, (
        "признаки объявлены, но разборщик их не читает: " + "; ".join(bad))


def test_turn_off_still_means_currently_on():
    """Порядок проверки важен: в «turn off» сидит «off» из списка выключенных."""
    assert B.find_b2b_toggle(["Turn off Bot-to-Bot Mode"])["state"] == "on"


def test_a_bare_label_is_still_unknown():
    """Честный unknown никуда не девается — на него теперь просто не нажимают."""
    assert B.find_b2b_toggle(["Bot-to-Bot"])["state"] == "unknown"


# ── Диалог: на unknown не нажимаем ──────────────────────────────────────────

class _Msg:
    def __init__(self, labels, clicks):
        self._labels = list(labels)
        self._clicks = clicks
        rows = []
        for t in self._labels:
            rows.append(type("R", (), {"buttons": [type("B", (), {"text": t})()]})())
        self.reply_markup = type("M", (), {"rows": rows})()
        self.text = ""

    async def click(self, text=None):
        if text not in self._labels:
            raise ValueError("нет такой кнопки")
        self._clicks.append(text)


class _Client:
    """Заглушка Telethon: меню ведёт себя как у BotFather, клики записываются."""

    def __init__(self, labels, clicks):
        self._labels = labels
        self.clicks = clicks

    async def connect(self):
        return True

    async def disconnect(self):
        return True

    def is_connected(self):
        return True

    async def get_entity(self, who):
        return object()

    async def get_messages(self, who, limit=1):
        return [_Msg(self._labels, self.clicks)]

    async def send_message(self, who, text):
        return _Msg(self._labels, self.clicks)


def _run_dialog(monkeypatch, labels):
    clicks: list[str] = []
    from services import account_manager
    monkeypatch.setattr(account_manager, "_make_client",
                        lambda *a, **k: _Client(["@x"] + labels, clicks))

    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    res = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        B.enable_b2b_via_botfather("s", ["x"]))
    return res, clicks


def test_unknown_state_is_not_clicked(monkeypatch):
    res, clicks = _run_dialog(monkeypatch, ["Bot-to-Bot"])
    assert res["enabled"] == [], (
        "нажали по тумблеру, состояние которого не прочитано: на включённом "
        "боте это ВЫКЛЮЧАЕТ режим")
    assert "Bot-to-Bot" not in clicks, "клик по тумблеру всё-таки был"
    assert "x" in res["errors"] and "вручную" in res["errors"]["x"], (
        "владельцу не сказано, что бота надо проверить руками")


def test_a_readable_off_state_is_still_clicked(monkeypatch):
    res, clicks = _run_dialog(monkeypatch, ["Turn on Bot-to-Bot Mode"])
    assert res["enabled"] == ["x"] and "Turn on Bot-to-Bot Mode" in clicks


def test_a_readable_on_state_goes_to_already(monkeypatch):
    res, clicks = _run_dialog(monkeypatch, ["Turn off Bot-to-Bot Mode"])
    # Клик по «@x» — это выбор бота в списке /mybots, он законен всегда;
    # нажатия по самому тумблеру быть не должно.
    assert res["already"] == ["x"]
    assert "Turn off Bot-to-Bot Mode" not in clicks, "режим выключили"


# ── Исход операции ──────────────────────────────────────────────────────────

def _fn(name):
    for n in ast.walk(ast.parse(OPW)):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return ast.get_source_segment(OPW, n) or ""
    raise AssertionError(f"{name} не найдена")


@pytest.mark.asyncio
async def test_an_operation_that_enabled_nothing_does_not_report_done(monkeypatch):
    """Ни одного включённого бота — это не «выполнено»."""
    from services import op_worker as W

    class _Pool:
        async def fetch(self, q, *a):
            if "managed_bots" in q:
                return [{"bot_id": 7, "username": "x", "acc_id": 3,
                         "b2b_enabled": False}]
            return []

        async def execute(self, q, *a):
            return "UPDATE 1"

        async def fetchrow(self, q, *a):
            return None

    monkeypatch.setattr(W, "_db_get_account_for_telethon",
                        lambda *a, **k: _ok({"session_str": "s", "id": 3}))
    monkeypatch.setattr(W, "try_claim_account", lambda *a, **k: _ok(True))
    monkeypatch.setattr(W, "release_accounts", lambda *a, **k: _ok(None))
    monkeypatch.setattr(W, "_is_cancelled", lambda *a, **k: _ok(False))
    monkeypatch.setattr(B, "enable_b2b_via_botfather",
                        lambda *a, **k: _ok({"enabled": [], "already": [],
                                             "errors": {"x": "состояние не читается"}}))
    res = await W._exec_enable_bot_to_bot(_Pool(), None, 1, 2, {})
    assert res["status"] != "done", (
        "не включено ни одного бота, а исход «выполнено»: ошибки видны только "
        "в тексте сводки, а число на экране владельца их не знает")


@pytest.mark.asyncio
async def test_a_partly_enabled_operation_is_partial(monkeypatch):
    from services import op_worker as W

    class _Pool:
        async def fetch(self, q, *a):
            if "managed_bots" in q:
                return [{"bot_id": 7, "username": "x", "acc_id": 3,
                         "b2b_enabled": False},
                        {"bot_id": 8, "username": "y", "acc_id": 3,
                         "b2b_enabled": False}]
            return []

        async def execute(self, q, *a):
            return "UPDATE 1"

        async def fetchrow(self, q, *a):
            return None

    monkeypatch.setattr(W, "_db_get_account_for_telethon",
                        lambda *a, **k: _ok({"session_str": "s", "id": 3}))
    monkeypatch.setattr(W, "try_claim_account", lambda *a, **k: _ok(True))
    monkeypatch.setattr(W, "release_accounts", lambda *a, **k: _ok(None))
    monkeypatch.setattr(W, "_is_cancelled", lambda *a, **k: _ok(False))
    monkeypatch.setattr(B, "enable_b2b_via_botfather",
                        lambda *a, **k: _ok({"enabled": ["x"], "already": [],
                                             "errors": {"y": "состояние не читается"}}))
    res = await W._exec_enable_bot_to_bot(_Pool(), None, 1, 2, {})
    assert res["status"] == "partial", (
        "часть ботов осталась непройденной, а исход говорит «выполнено»")


async def _ok(v):
    return v


def test_the_executor_does_not_mark_unreadable_bots_as_enabled():
    """Ложная запись b2b_enabled=TRUE прячет поломку навсегда."""
    body = _fn("_exec_enable_bot_to_bot")
    assert "b2b_enabled=TRUE" in body
    # Писать TRUE можно только по именам из enabled/already, не по ошибкам.
    head = body[:body.index("b2b_enabled=TRUE")]
    assert "res.get('errors')" not in head and 'res.get("errors")' not in head, (
        "имена из errors попадают в список, по которому ставится TRUE")
