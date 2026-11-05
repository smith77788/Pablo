"""Очередь обновлений бота подтверждает ровно один опрашивающий.

Telegram держит для бота ОДНУ очередь обновлений. Просьба `getUpdates` с
положительным offset — это подтверждение: всё, что раньше этого номера,
Telegram забывает и больше не отдаёт НИКОМУ. Поэтому подтверждать вправе
только владелец очереди — цикл автоответчика, который эти обновления
разбирает и хранит оффсет в `bot_update_offsets`.

Нарушали это правило уже дважды, и оба раза тихо:

  * сбор аудитории (`bot_api.scan_all_users`) листал обновления пачками, то
    есть подтверждал их, и забирал у автоответчика сообщения, которых тот ещё
    не видел. Подписчик писал боту, попадал в базу — и не получал ни
    авто-ответа, ни воронки, ни ответа оператора;
  * `services/relay.py` держал ВТОРОЙ опрашивающий цикл на тех же токенах со
    своим оффсетом в памяти. Его отключили, но код остался на месте и ждал,
    пока кто-нибудь включит его обратно; теперь он удалён.

Читать обновления, НЕ подтверждая их, можно откуда угодно: `offset=0` (отдать
ожидающие) и отрицательный offset (заглянуть в хвост) очередь не трогают.

Третий случай тот же по сути, но проверки на него не было: ОДНО место в коде
ещё не значит один опрашивающий. Фоновые циклы идут в ролях `all` и `worker`
(`main.py: _resilient` гасит только `web`), и два таких процесса оба прочитают
один оффсет из `bot_update_offsets`, оба возьмут getUpdates с ним и оба
ОТВЕТЯТ КЛИЕНТУ. `_bot_busy` — память одного процесса и от этого не спасает,
а монотонный оффсет в базе не спасает тем более: второй ответ уже отправлен.
Бывает это не только при ошибке настройки, но и на деплое, пока старый процесс
ещё работает. Поэтому право на опрос — у держателя аренды исполнителя (той же,
по которой разбирается очередь операций), и это проверяется ниже.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
_SKIP = {".git", "node_modules", "site-packages", "venv", ".venv", "__pycache__", "tests"}

# Владелец очереди: разбирает обновления и хранит оффсет в базе.
QUEUE_OWNER = "services/auto_responder.py"


def _files():
    for path in sorted(ROOT.rglob("*.py")):
        if _SKIP & set(path.parts):
            continue
        yield path


def _offset_arg(call: ast.Call):
    for kw in call.keywords:
        if kw.arg == "offset":
            return kw.value
    return None


def _confirms(offset) -> bool:
    """Подтверждает ли такой offset прочитанное."""
    if offset is None:
        return False  # offset не задан — Telegram ничего не забывает
    if isinstance(offset, ast.Constant) and isinstance(offset.value, int):
        return offset.value > 0
    # -1, -100 и т.п. приходят как UnaryOp(USub)
    if isinstance(offset, ast.UnaryOp) and isinstance(offset.op, ast.USub):
        return False
    return True  # выражение вроде offset + 1 — подтверждает


def _is_get_updates(call: ast.Call) -> bool:
    return any(
        isinstance(a, ast.Constant) and a.value == "getUpdates" for a in call.args
    )


def _collect():
    total, confirming = 0, []
    for path in _files():
        text = path.read_text(encoding="utf-8")
        if "getUpdates" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rel = str(path.relative_to(ROOT))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_get_updates(node):
                continue
            total += 1
            if _confirms(_offset_arg(node)):
                confirming.append((rel, node.lineno))
    return total, confirming


def test_only_the_queue_owner_confirms_updates():
    total, confirming = _collect()
    strangers = [f"{f}:{ln}" for f, ln in confirming if f != QUEUE_OWNER]
    assert not strangers, (
        "getUpdates с подтверждающим offset вне владельца очереди:\n  "
        + "\n  ".join(strangers)
        + f"\n\nПодтверждать вправе только {QUEUE_OWNER}: подтверждённое "
        "Telegram забывает, и разбирать эти сообщения будет уже некому. "
        "Чтобы просто прочитать — offset=0 или отрицательный."
    )


def test_the_owner_still_confirms():
    """Иначе автоответчик разбирает один и тот же пакет по кругу."""
    _, confirming = _collect()
    assert any(f == QUEUE_OWNER for f, _ in confirming), (
        f"{QUEUE_OWNER} перестал подтверждать обновления — он будет получать "
        "тот же пакет каждые 10 секунд"
    )


def test_the_detector_looks_at_every_call():
    total, _ = _collect()
    assert total >= 4, f"найдено всего {total} вызовов getUpdates — сканер сломан"


def test_the_detector_catches_a_confirming_call():
    bad = ast.parse('_call(http, token, "getUpdates", offset=offset + 1, limit=100)')
    call = next(n for n in ast.walk(bad) if isinstance(n, ast.Call) and _is_get_updates(n))
    assert _confirms(_offset_arg(call))


def test_the_detector_lets_reading_through():
    for src in ('_call(s, t, "getUpdates", offset=0, limit=100)',
                '_call(s, t, "getUpdates", offset=-1, limit=1)',
                '_call(s, t, "getUpdates", limit=100)'):
        tree = ast.parse(src)
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_get_updates(n))
        assert not _confirms(_offset_arg(call)), src


# ── Один опрашивающий НА ВСЮ УСТАНОВКУ, а не на процесс ────────────────────
import asyncio  # noqa: E402

import pytest  # noqa: E402

from services import auto_responder as ar  # noqa: E402
from services import replica_guard as rg  # noqa: E402


@pytest.fixture
def lease(monkeypatch):
    """Управляем «держим ли мы аренду» так, как это видит процесс."""
    state = {"held": False}
    monkeypatch.setattr(rg, "holds_executor_lease", lambda: state["held"])
    monkeypatch.setattr(ar, "_poll_lease_log_at", 0.0, raising=False)
    return state


_BOTS = [{"bot_id": 555, "token": "123456789:AAHrealtokenlooking-value_0123456789ab"}]


def test_a_process_without_the_lease_does_not_poll(lease, monkeypatch):
    """Без аренды опрос не запускается вовсе — ни одной задачи."""
    started = []
    monkeypatch.setattr(ar, "_poll_one", lambda *a, **k: started.append(a))

    async def _go():
        return ar._launch_polls(object(), object(), _BOTS)

    assert asyncio.run(_go()) == 0, "опрос пошёл в процессе без аренды"
    assert not started, started


def test_the_lease_holder_polls(lease, monkeypatch):
    """С арендой — опрашивает, иначе гейт превратился бы в немоту."""
    started = []

    async def _fake(pool, http, bot_id, token, main_bot=None):
        started.append(bot_id)

    monkeypatch.setattr(ar, "_poll_one", _fake)
    lease["held"] = True

    async def _go():
        n = ar._launch_polls(object(), object(), _BOTS)
        await asyncio.sleep(0)
        return n

    assert asyncio.run(_go()) == 1
    assert started == [555], started


def test_the_webhook_path_is_not_gated(lease, monkeypatch):
    """Вебхук разбирается и БЕЗ аренды: апдейт приходит один раз.

    Ждать аренды тут означало бы потерять обновление: Telegram отдал его тому
    процессу, который принял запрос, и повторять не обязан. Поэтому зовём
    НАСТОЯЩИЙ `_process_bot` с готовым апдейтом (так его зовёт
    services/managed_bot_webhooks.py) и смотрим, что оффсет сдвинулся.
    """
    from unittest.mock import AsyncMock

    saved: list[int] = []

    async def _set_offset(pool, bot_id, value):
        saved.append(value)

    class _Pool:
        async def fetchrow(self, query, *args):
            if "FROM managed_bots" in query:
                return {"bot_role": None, "swarm_enabled": False, "cluster": None,
                        "added_by": 1, "username": "b", "first_name": "b",
                        "relay_enabled": False}
            return None

        async def fetch(self, query, *args):
            return []

        async def fetchval(self, query, *args):
            return None

        async def execute(self, query, *args):
            return "UPDATE 1"

    for target, repl in (
        ("database.db.get_update_offset", AsyncMock(return_value=100)),
        ("database.db.get_active_auto_replies", AsyncMock(return_value=[])),
        ("database.db.get_active_funnels", AsyncMock(return_value=[])),
        ("database.db.get_active_automation_rules", AsyncMock(return_value=[])),
        ("database.db.get_active_experiment", AsyncMock(return_value=None)),
        ("database.db.upsert_users", AsyncMock(return_value=[])),
        ("services.brand_injection.is_free_tier", AsyncMock(return_value=False)),
    ):
        mod, name = target.rsplit(".", 1)
        monkeypatch.setattr(__import__(mod, fromlist=[name]), name, repl)
    monkeypatch.setattr("database.db.set_update_offset", _set_offset)
    monkeypatch.setattr(ar, "_record_bot_ok", AsyncMock())
    monkeypatch.setattr(ar, "_record_bot_error", AsyncMock())

    upd = {"update_id": 101, "message": {
        "message_id": 1, "from": {"id": 7001, "is_bot": False, "first_name": "Артур"},
        "chat": {"id": 7001, "type": "private"}, "text": "привет"}}
    assert lease["held"] is False
    asyncio.run(ar._process_bot(
        _Pool(), object(), 555,
        "123456789:AAHrealtokenlooking-value_0123456789ab", None, updates=[upd]))
    assert saved == [101], (
        f"вебхук без аренды не разобрал обновление (оффсет {saved}) — "
        f"обновление потеряно, Telegram его не повторит")


def test_the_gate_stands_in_the_single_place_polls_start():
    """Гейт — в `_launch_polls`: это единственная точка запуска опроса.

    И полный проход, и быстрый опрос «горячих» ботов идут через неё; гейт,
    поставленный в одном из двух, пропускал бы второй.
    """
    src = (ROOT / "services" / "auto_responder.py").read_text(encoding="utf-8")
    body = src[src.index("def _launch_polls"):src.index("def _launch_polls") + 1800]
    assert "_poll_allowed()" in body, "опрос снова запускается без спроса аренды"
    launches = src.count("_launch_polls(")
    assert launches >= 3, f"точек запуска опроса стало {launches} — проверьте гейт"


def test_a_stale_lease_is_not_a_lease(monkeypatch):
    """Память процесса о своей аренде истекает вместе со сроком в базе.

    Иначе процесс, у которого аренду законно забрали, продолжал бы считать
    себя держателем и опрашивал ботов вторым — ровно то, от чего гейт.
    """
    import time as _t

    monkeypatch.setattr(rg, "_lease_held_by", "мы")
    monkeypatch.setattr(rg, "_lease_confirmed_at", _t.monotonic())
    assert rg.holds_executor_lease() is True
    monkeypatch.setattr(
        rg, "_lease_confirmed_at",
        _t.monotonic() - (rg._EXEC_LEASE_TTL_S - rg._EXEC_LEASE_MARGIN_S) - 1)
    assert rg.holds_executor_lease() is False, "просроченная память считается арендой"
    monkeypatch.setattr(rg, "_lease_held_by", None)
    monkeypatch.setattr(rg, "_lease_confirmed_at", _t.monotonic())
    assert rg.holds_executor_lease() is False, "чужая аренда считается своей"


def test_one_worker_name_per_process():
    """Имя процесса для аренд — одно.

    Два разных имени в одном процессе отбирали бы аренду друг у друга по
    кругу: опрос ботов и разбор очереди спрашивают одну и ту же аренду.
    """
    from services import op_worker as ow

    assert ow._WORKER_ID == rg.WORKER_ID, (
        "у разбора очереди и у опроса ботов разные имена процесса — аренда "
        "будет переходить туда-сюда, и опрашивать станут оба")
