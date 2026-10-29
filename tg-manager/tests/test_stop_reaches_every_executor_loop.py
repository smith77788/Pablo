"""«Остановить» слышит каждый исполнитель, у которого есть цикл по целям.

Отмена в продукте немедленная: владелец нажимает «Остановить», и операция
обязана прекратить РЕАЛЬНЫЕ действия на ближайшем чекпоинте, а не доиграть
список до конца. Дверь одна — `op_worker._is_cancelled` (кэш 5 секунд, читает
`op_status.stop_requested`, то есть замечает и удалённую операцию).

Так было у 72 исполнителей из 74. Выпадала выгрузка контактов на устройство
(`_exec_contacts_push`): она идёт по аккаунтам, у каждого выгрузка книги плюс
обратное чтение (до 600 секунд), и на флоте это часы. Владелец остановил
операцию, а она продолжала заливать контакты аккаунт за аккаунтом — заметно
это было только по тому, что книга менялась ПОСЛЕ стопа.

Здесь поведенческая проверка на этот исполнитель и храповик на всех: цикл по
целям без обращения к двери отмены запрещён.
"""
from __future__ import annotations

import ast
import asyncio
import os

import services.op_worker as ow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")

# Дверь отмены во всех её видах: прямая проверка, пауза, слушающая отмену, и
# общая функция решения (её читают исполнители, вынесенные в свои модули).
CANCEL_DOORS = ("_is_cancelled", "_pause_watching_for_cancel", "stop_requested")

# Исполнители, которые спрашивают дверь НЕ сами. Список намеренно короткий: он
# про вынесенные движки, где чекпоинт стоит внутри, а не про «тут неудобно».
DELEGATED = {
    # strike_engine._op_cancelled() читает op_status.stop_requested между
    # волнами — у исполнителя остаётся только разбор параметров.
    "_exec_strike": "services/strike_engine.py",
}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _executors():
    with open(SRC, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    for node in tree.body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("_exec_")):
            yield node.name, (ast.get_source_segment(src, node) or ""), node


def _is_retry_loop(node) -> bool:
    """Цикл повтора записи, а не цикл по целям.

    Идиома проекта: `for _attempt in (1, 2):` вокруг защитной записи — повтор
    на случай блипа базы, две итерации без единого реального действия.
    Детектор считал его циклом по целям, и исполнитель, у которого циклов по
    целям нет вообще, объявлялся глухим: ложная находка ровно на том коде,
    который добавляли ради надёжности. Признак узкий — перебор литерала из
    чисел; `for t in targets` так не выглядит никогда.
    """
    if not isinstance(node, (ast.For, ast.AsyncFor)):
        return False
    it = node.iter
    if isinstance(it, (ast.Tuple, ast.List)):
        return bool(it.elts) and all(
            isinstance(e, ast.Constant) and isinstance(e.value, int)
            for e in it.elts)
    return False


def _target_loops(node):
    """Циклы исполнителя, кроме повторов записи."""
    return [n for n in ast.walk(node)
            if isinstance(n, (ast.For, ast.AsyncFor, ast.While))
            and not _is_retry_loop(n)]


def _door_in_a_loop(node) -> bool:
    """Дверь отмены стоит ВНУТРИ цикла по целям."""
    return any(any(d in ast.unparse(lp) for d in CANCEL_DOORS)
               for lp in _target_loops(node))


def _door_handed_to_the_engine(node) -> bool:
    """Дверь передана наружу замыканием — цикл по целям живёт в движке.

    Так устроен поиск контакта: исполнитель объявляет `async def _cancelled()`
    с `_is_cancelled` внутри и отдаёт его в `contact_finder.hunt`, который и
    идёт по кандидатам. Это не слабее двери в цикле: отмену спрашивают на
    КАЖДОЙ итерации, просто итерации чужие.
    """
    closures = {f.name for f in ast.walk(node)
                if isinstance(f, (ast.AsyncFunctionDef, ast.FunctionDef))
                and f is not node
                and any(d in ast.unparse(f) for d in CANCEL_DOORS)}
    if not closures:
        return False
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        passed = [ast.unparse(a) for a in call.args]
        passed += [ast.unparse(k.value) for k in call.keywords]
        if any(p in closures for p in passed):
            return True
    return False


def test_no_executor_loop_ignores_the_stop_button():
    """Храповик: цикл по целям обязан спрашивать дверь отмены.

    Проверка была слабее, чем читалась: дверь искали в ТЕКСТЕ исполнителя, а не
    в его цикле. Исполнитель, спросивший отмену один раз ПЕРЕД циклом на триста
    целей, храповик проходил — а «Остановить» у него не работает до конца
    списка, то есть ровно то, от чего храповик и ставился. Теперь дверь обязана
    стоять внутри цикла (или быть отдана замыканием тому, кто по целям идёт).
    """
    deaf = []
    seen = 0
    for name, seg, node in _executors():
        seen += 1
        if name in DELEGATED:
            continue
        if not _target_loops(node):
            continue
        if _door_in_a_loop(node) or _door_handed_to_the_engine(node):
            continue
        deaf.append(name)
    assert seen > 50, f"пробник не нашёл исполнителей ({seen}) — он сломан"
    assert not deaf, (
        "эти исполнители идут по списку целей, не спрашивая про отмену внутри "
        f"цикла: «Остановить» для них не работает до конца списка — {deaf}. "
        "Поставьте проверку в голову цикла, передайте замыкание тому, кто идёт "
        "по целям, или внесите исполнителя в DELEGATED с объяснением")


_DOOR_BEFORE_THE_LOOP = (
    "async def _exec_x(pool, op_id):\n"
    "    if await _is_cancelled(pool, op_id):\n"
    "        return {}\n"
    "    for t in targets:\n"
    "        await send(t)\n")
_DOOR_INSIDE_THE_LOOP = (
    "async def _exec_x(pool, op_id):\n"
    "    for t in targets:\n"
    "        if await _is_cancelled(pool, op_id):\n"
    "            break\n"
    "        await send(t)\n")
_DOOR_HANDED_OVER = (
    "async def _exec_x(pool, op_id):\n"
    "    async def _cancelled():\n"
    "        return await _is_cancelled(pool, op_id)\n"
    "    for q in (1, 2):\n"
    "        pass\n"
    "    return await engine.hunt(targets, cancelled=_cancelled)\n")


def _one(src):
    return ast.parse(src).body[0]


def test_a_door_before_the_loop_is_not_enough():
    """Главная самопроверка усиления: раньше этот пример проходил."""
    node = _one(_DOOR_BEFORE_THE_LOOP)
    assert any(d in ast.unparse(node) for d in CANCEL_DOORS), "пример устарел"
    assert not _door_in_a_loop(node), (
        "проверка перед циклом принята за дверь в цикле — усиление не работает")
    assert not _door_handed_to_the_engine(node)


def test_a_door_inside_the_loop_passes():
    assert _door_in_a_loop(_one(_DOOR_INSIDE_THE_LOOP))


def test_a_door_handed_to_the_engine_passes():
    node = _one(_DOOR_HANDED_OVER)
    assert not _door_in_a_loop(node), "пример устарел: дверь оказалась в цикле"
    assert _door_handed_to_the_engine(node), (
        "замыкание с дверью, отданное движку, не признано — перепись потребует "
        "правок у исполнителя, который отмену как раз спрашивает")


def test_the_deaf_executor_detector_bites():
    """Самопроверка: на заведомо глухом исполнителе детектор обязан сработать."""
    sick = ("async def _exec_x(pool, op_id):\n"
            "    for t in targets:\n"
            "        await send(t)\n")
    node = ast.parse(sick).body[0]
    seg = ast.get_source_segment(sick, node) or ""
    has_loop = any(isinstance(n, (ast.For, ast.AsyncFor, ast.While))
                   for n in ast.walk(node))
    assert has_loop and not any(d in seg for d in CANCEL_DOORS)
    # А на здоровом — нет.
    healthy = sick.replace("        await send(t)",
                           "        if await _is_cancelled(pool, op_id):\n"
                           "            return {}\n        await send(t)")
    node2 = ast.parse(healthy).body[0]
    seg2 = ast.get_source_segment(healthy, node2) or ""
    assert any(d in seg2 for d in CANCEL_DOORS)


def test_a_retry_loop_is_not_a_target_loop():
    """Повтор защитной записи не делает исполнителя глухим.

    Без этого различия добавление `for _attempt in (1, 2):` вокруг записи
    превращало в находку исполнителя, у которого циклов по целям нет вовсе
    (так и случилось с `_exec_run_broadcast`). А настоящий цикл по целям
    детектор обязан видеть по-прежнему — вторая половина проверки про это.
    """
    retry = ("async def _exec_x(pool, op_id):\n"
             "    for _attempt in (1, 2):\n"
             "        if await _write(pool) != 'ERROR':\n"
             "            break\n")
    node = ast.parse(retry).body[0]
    assert _target_loops(node) == [], (
        "повтор записи принят за цикл по целям — детектор будет находить "
        "глухоту там, где её нет")

    real = ("async def _exec_x(pool, op_id):\n"
            "    for _attempt in (1, 2):\n"
            "        if await _write(pool) != 'ERROR':\n"
            "            break\n"
            "    for t in targets:\n"
            "        await send(t)\n")
    assert len(_target_loops(ast.parse(real).body[0])) == 1, (
        "цикл по целям рядом с повтором записи перестал быть виден")


def test_contacts_push_stops_before_touching_the_next_account(monkeypatch):
    """Поведение: после стопа следующий аккаунт книгу не получает."""
    from services.contacts_hub import device_sync

    touched: list[int] = []

    async def _hub(pool, owner_id):
        return [{"phone": "+79990000001", "first_name": "A"}]

    async def _push(session, acc, contacts):
        touched.append(int(acc["id"]))
        return {"sent": 1, "imported": 1, "already": 0, "left": 0}

    async def _acc(pool, acc_id, owner_id):
        return {"id": acc_id, "phone": f"+7999000{acc_id}", "session_str": "s"}

    monkeypatch.setattr(device_sync, "hub_contacts", _hub)
    monkeypatch.setattr(device_sync, "push_contacts", _push)
    from database import db as _dbmod
    monkeypatch.setattr(_dbmod, "get_account_for_telethon", _acc)

    async def _no_quarantine(pool, acc_id):
        return False
    monkeypatch.setattr(ow._infra_mem, "is_account_quarantined", _no_quarantine)

    async def _claim(acc_id):
        return True
    monkeypatch.setattr(ow, "try_claim_account", _claim)

    async def _release(ids):
        return None
    monkeypatch.setattr(ow, "release_accounts", _release)

    async def _noop_execute(*a, **k):
        return "UPDATE 1"
    monkeypatch.setattr(ow, "_safe_execute", _noop_execute)

    async def _sync(pool, owner_id, acc_id):
        return None
    from services.contacts_hub import sync_service
    monkeypatch.setattr(sync_service, "sync_account", _sync)

    # Отмена приходит ПОСЛЕ первого аккаунта — ровно как нажатие кнопки в
    # середине выгрузки по флоту.
    calls = {"n": 0}

    async def _cancelled(pool, op_id):
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(ow, "_is_cancelled", _cancelled)

    res = _run(ow._exec_contacts_push(
        None, 777, 42, {"account_ids": [11, 22, 33]}))

    assert touched == [11], (
        "после «Остановить» выгрузка продолжилась на следующие аккаунты: "
        f"книгу получили {touched}")
    assert res["status"] == "cancelled", (
        f"операция отчиталась статусом {res['status']!r} вместо отмены")
    assert res["ok"] == 1, f"потеряна уже сделанная работа: {res}"
