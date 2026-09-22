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
