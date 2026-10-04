"""Назначенная Telegram пауза обязана дожить до следующей операции.

FloodWait — это пауза, которую Telegram назначает АККАУНТУ на метод, а не
цели и не прогону. Пока пауза известна только текущему исполнителю (в виде
сна и строки «⏳ пропущен» в отчёте), для всего остального продукта аккаунт
выглядит отдохнувшим: `resource_selector` и `fleet_pulse` берут его первым,
следующая операция сразу идёт тем же методом и получает новый штраф — уже
длиннее. Так аккаунты уходят в бан не от объёма работы, а от того, что
правая рука не знала про паузу, назначенную левой.

Единая точка записи — `flood_engine` (`record_flood` / `record_peer_flood`),
в воркере поверх неё обёртка `_note_flood_penalty` (fail-open: сбой записи не
валит операцию). Обёртка это и обещает в своём докстринге: «штраф аккаунту
уже записан в flood_engine». До 04.10.2026 обещание было неправдой для
восьми мест: смена username и остальных полей профиля пачкой, два пути
создания каналов, фабрика каналов, назначение @username каналу, фабрика
ботов (два места) и общая папка (chatlist). Там пауза жила только в логе.

Поэтому здесь храповик, а не точечный тест: класс возвращается при каждом
новом исполнителе, и ловить его нужно на уровне правила, а не отдельного
случая. Оба пробника проверяются на заведомо плохом и заведомо хорошем
примере — детектор, который не проверен сам, в этом репозитории уже четыре
раза оказывался виноват вместо кода.

Поведение публикаторов (короткая пауза досиживается, длинная уходит в очередь,
флуд одного аккаунта виден всему флоту) проверяет
tests/test_flood_pause_outlives_the_run.py — там точечные тесты, здесь правило.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "services"

# Чтение паузы из ответа транспорта: result.get("flood_wait") / res["flood_wait"].
READS_FLOOD = re.compile(r'''\.get\(\s*["']flood_wait["']|\[["']flood_wait["']\]''')

# Любой способ ЗАПИСАТЬ паузу аккаунту.
#
# `cooldown_until` в списке намеренно: bulk_dm_adhoc пишет паузу своим UPDATE
# по tg_accounts, а не через flood_engine. Это второй способ записи того же
# факта, и сводить их в один стоит отдельной задачи — но пауза там НЕ теряется,
# а храповик защищает именно от потери.
RECORDERS = (
    "_note_flood_penalty",
    "record_flood",
    "record_peer_flood",
    "note_flood",
    "_rest_invite_account",
    "cooldown_until",
)

# Передача паузы наверх вместо записи: транспортный слой (account_manager,
# mass_inviter_engine) правильно не пишет в БД сам — он возвращает flood_wait
# исполнителю, который и записывает. Поэтому `raise` и возврат flood_wait
# считаются законным исходом для обработчика исключения.
PROPAGATORS = ("flood_wait", "raise")


def _functions(src: str):
    """Все функции файла, вместе с вложенными: исполнители воркера — это
    вложенные замыкания, и запись часто живёт в соседнем помощнике внутри
    того же исполнителя (так устроен инвайт: `_rest_invite_account`)."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _worker_offenders(src: str) -> list[str]:
    out = []
    for node in _functions(src):
        seg = ast.get_source_segment(src, node) or ""
        if not READS_FLOOD.search(seg):
            continue
        if any(m in seg for m in RECORDERS):
            continue
        out.append(f"{node.name} (строка {node.lineno})")
    return out


def _handler_offenders(src: str) -> list[str]:
    out = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        if "FloodWait" not in ast.unparse(node.type):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if any(m in seg for m in RECORDERS) or any(m in seg for m in PROPAGATORS):
            continue
        out.append(f"строка {node.lineno}")
    return out


# ── Проверка самих пробников ────────────────────────────────────────────────

_BAD_WORKER = '''
async def _exec_something(pool, op_id, acc):
    result = await transport.do(acc)
    if result.get("flood_wait"):
        log.info("пропущен")
'''

_GOOD_WORKER = '''
async def _exec_something(pool, op_id, acc):
    result = await transport.do(acc)
    if result.get("flood_wait"):
        await _note_flood_penalty(pool, acc["id"], result.get("flood_wait"), "x", op_id)
'''

_BAD_HANDLER = '''
async def read(client):
    try:
        return await client.get_messages(1)
    except FloodWaitError as e:
        log.warning("флуд %ds", e.seconds)
        return []
'''

_GOOD_HANDLER = '''
async def read(pool, acc, client):
    try:
        return await client.get_messages(1)
    except FloodWaitError as e:
        await record_flood(pool, acc["id"], e.seconds, "read")
        return []
'''


def test_worker_probe_sees_a_known_bad_example():
    assert _worker_offenders(_BAD_WORKER), "пробник не видит потерянную паузу"
    assert not _worker_offenders(_GOOD_WORKER), "пробник ругается на здоровый код"


def test_handler_probe_sees_a_known_bad_example():
    assert _handler_offenders(_BAD_HANDLER), "пробник не видит потерянную паузу"
    assert not _handler_offenders(_GOOD_HANDLER), "пробник ругается на здоровый код"


# ── Собственно храповики ────────────────────────────────────────────────────

def test_worker_records_every_flood_pause_it_reads():
    src = (SERVICES / "op_worker.py").read_text(encoding="utf-8")
    bad = _worker_offenders(src)
    assert not bad, (
        "исполнители читают назначенную Telegram паузу и НЕ записывают её "
        f"аккаунту: {bad}.\n"
        "Пауза, известная только этому прогону, для флота не существует: "
        "следующая операция возьмёт аккаунт как отдохнувший и получит новый "
        "штраф. Запись — await _note_flood_penalty(pool, acc_id, секунды, "
        "действие, op_id), она fail-open и операцию не валит."
    )


def test_flood_handlers_either_record_or_pass_the_pause_up():
    bad = {}
    for path in sorted(SERVICES.rglob("*.py")):
        src = path.read_text(encoding="utf-8", errors="ignore")
        if "FloodWaitError" not in src:
            continue
        offenders = _handler_offenders(src)
        if offenders:
            bad[path.relative_to(ROOT).as_posix()] = offenders
    assert not bad, (
        f"обработчики FloodWait, которые паузу теряют: {bad}.\n"
        "Законных исходов два: записать её (flood_engine) или вернуть "
        "flood_wait наверх тому, кто запишет."
    )


def test_wrapper_still_promises_what_it_does():
    """Обёртка обещает запись в докстринге — обещание должно остаться правдой."""
    src = (SERVICES / "op_worker.py").read_text(encoding="utf-8")
    i = src.index("async def _note_flood_penalty")
    seg = src[i:src.index("\ndef ", i)]
    assert "record_flood" in seg, "обёртка больше не пишет в flood_engine"
    assert "fail-open" in seg, "запись обязана не валить операцию"
