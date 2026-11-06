"""Два тапа «отправить через N минут» — одна отложенная рассылка, не две.

ТОТ ЖЕ КЛАСС, ДРУГОЙ ВХОД. Дедуп постановки сравнивает не только params, но и
`scheduled_for`, и сравнивает его ТОЧНО. Отложенная рассылка считала время
запуска как «сейчас + N минут», то есть у двух тапов оно различалось
микросекундами — и дедуп не срабатывал ни разу. В очередь встают две рассылки,
подписчики получают сообщение дважды.

ПОЧЕМУ ЗАПРЕТ ЖИВОГО ДВОЙНИКА В ИСПОЛНИТЕЛЕ ЗДЕСЬ НЕ СПАСАЕТ. Он снимает
залп, у которого есть ЖИВОЙ двойник в статусе 'running'. Две отложенные
рассылки запускаются с разницей в секунду; на маленькой аудитории первая
успевает завершиться раньше, чем начнётся вторая, и живого двойника вторая уже
не находит. Поэтому лечить нужно постановку, а не только исполнение.

ЧТО ЗАКРЕПЛЕНО. Время отложенного запуска округляется вверх до целой минуты
(`operation_bus.quantized_schedule_iso` — помощник живёт рядом с самой
идентичностью дедупа, частью которой время и является), поэтому у двух тапов
подряд оно совпадает и дедуп срабатывает. Вверх, а не вниз: вниз сдвинуло бы
запуск раньше просьбы владельца.

ПЕРВАЯ ВЕРСИЯ ЭТОГО СТОРОЖА ПРОПУСТИЛА ДВА МЕСТА. Она искала в тексте
`datetime.now`, а мини-апп зовёт то же самое через псевдоним модуля
(`_dtm.now(_tzn.utc)`): отложенная рассылка и отложенный пост в свой канал из
мини-аппа — те же два тапа, те же две операции — сторожу были не видны. Теперь
ищется любой вызов `.now(`, и самопроверка ниже держит именно псевдоним.
"""
from __future__ import annotations

import asyncio
import ast
import os
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993618

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    yield


PARAMS = {"bot_id": 77, "text": "Скидка до вечера"}


def _submit(pool, when) -> int:
    from services import operation_bus

    return _run(operation_bus.submit(
        pool, OWNER, "run_broadcast", dict(PARAMS),
        total_items=5, scheduled_for=when, bypass_plan_check=True))


def test_two_taps_a_moment_apart_are_one_scheduled_broadcast(pool):
    from services import operation_bus

    first = _submit(pool, operation_bus.quantized_schedule_iso(10))
    second = _submit(pool, operation_bus.quantized_schedule_iso(10))
    assert second == first, (
        "второй тап поставил ВТОРУЮ отложенную рассылку — подписчики получат "
        "сообщение дважды"
    )
    assert int(_run(pool.fetchval(
        "SELECT count(*) FROM operation_queue WHERE owner_id=$1", OWNER))) == 1


def test_the_unrounded_time_is_what_used_to_slip_through(pool):
    """Контроль: именно микросекунды «сейчас» и выключали дедуп."""
    naive_1 = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    naive_2 = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    assert naive_1 != naive_2, "часы слишком грубые — контроль бессмысленен"
    assert _submit(pool, naive_1) != _submit(pool, naive_2), (
        "неокруглённое время дало одну операцию — проверка больше ничего "
        "не доказывает, перечитать"
    )


def test_rounding_never_moves_the_send_earlier_than_asked():
    from services import operation_bus

    now = datetime(2026, 11, 6, 12, 30, 17, 123456, tzinfo=timezone.utc)
    got = datetime.fromisoformat(operation_bus.quantized_schedule_iso(10, now=now))
    assert got >= now + timedelta(minutes=10), (
        "округление сдвинуло отложенный запуск раньше просьбы владельца"
    )
    assert (got.second, got.microsecond) == (0, 0), "время не округлено до минуты"
    assert got - (now + timedelta(minutes=10)) < timedelta(minutes=1), (
        "округление отодвинуло запуск больше чем на минуту"
    )
    exact = datetime(2026, 11, 6, 12, 30, 0, 0, tzinfo=timezone.utc)
    assert datetime.fromisoformat(
        operation_bus.quantized_schedule_iso(5, now=exact)) == exact + timedelta(minutes=5), (
        "ровное время сдвинуто без нужды"
    )


# ── Сторож: время запуска, посчитанное от «сейчас», в дедуп-ключ не идёт ─────

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Любой вызов «сейчас», а НЕ только `datetime.now`: модуль зовут и через
# псевдоним (`_dtm.now(...)`), и именно так сторож пропустил два места.
VOLATILE_NOW = re.compile(r"\.now\(|utcnow|\btime\.time\(|monotonic")

# Разрешённое исключение с причиной, а не молчаливый пропуск.
ALLOWED = {
    # Следующий круг расписания ставится через минуты (`_rmin`), а окно
    # идемпотентности — 20 секунд. Дубль, если он случится, придёт из
    # восстановления много позже окна, поэтому дедуп здесь не защита в
    # принципе: дверью служит отметка «цепочка продлена» на закрываемом круге.
    ("services/op_worker.py", "_reschedule_recurring"),
}


def _findings_in(src: str, rel: str) -> list[str]:
    out: list[str] = []
    if "submit(" in src:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return out
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigns: dict[str, str] = {}
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            assigns[t.id] = ast.get_source_segment(src, n) or ""
            for n in ast.walk(fn):
                if not isinstance(n, ast.Call):
                    continue
                f = n.func
                name = (f.attr if isinstance(f, ast.Attribute)
                        else f.id if isinstance(f, ast.Name) else "")
                if name != "submit":
                    continue
                sf = next((k.value for k in n.keywords
                           if k.arg == "scheduled_for"), None)
                if sf is None:
                    continue
                vsrc = ast.get_source_segment(src, sf) or ""
                blob = vsrc + "\n" + assigns.get(vsrc.strip(), "")
                if not VOLATILE_NOW.search(blob):
                    continue
                if (rel, fn.name) in ALLOWED:
                    continue
                out.append(f"{rel}:{n.lineno} в {fn.name}(): {vsrc.strip()[:70]}")
    return out


def _findings() -> list[str]:
    out: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("tests/") or "/.venv/" in rel:
            continue
        out += _findings_in(path.read_text(encoding="utf-8"), rel)
    return out


def test_no_submit_passes_an_unrounded_now_as_the_launch_time():
    bad = _findings()
    assert not bad, (
        "время запуска посчитано от «сейчас» и уходит в дедуп-ключ как есть — "
        "у двух тапов оно различается микросекундами, и окно идемпотентности "
        "не сработает ни разу:\n  " + "\n  ".join(bad)
    )


def test_the_measurer_sees_a_now_called_through_an_alias():
    """Ровно та слепота, из-за которой два места прожили лишний день.

    Мини-апп зовёт время через псевдоним модуля, и мерка, искавшая строку
    `datetime.now`, не видела ни отложенную рассылку, ни отложенный пост.
    """
    aliased = (
        "async def h(pool, uid, p):\n"
        "    from datetime import datetime as _dtm, timedelta as _tdl, timezone as _tzn\n"
        "    await _obus.submit(pool, uid, 'run_broadcast', p,\n"
        "        scheduled_for=_dtm.now(_tzn.utc) + _tdl(minutes=10))\n")
    rounded = (
        "async def h(pool, uid, p):\n"
        "    await _obus.submit(pool, uid, 'run_broadcast', p,\n"
        "        scheduled_for=_obus.quantized_schedule_iso(10))\n")
    assert _findings_in(aliased, "services/probe.py"), (
        "мерка снова не видит «сейчас», вызванное через псевдоним модуля")
    assert _findings_in(rounded, "services/probe.py") == [], (
        "мерка ругается на округлённое время")


def test_the_rounded_time_is_used_everywhere_it_is_needed():
    """Все места, считающие время запуска от «сейчас», зовут одного помощника."""
    users = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("tests/"):
            continue
        if "quantized_schedule_iso" in path.read_text(encoding="utf-8"):
            users.append(rel)
    assert "services/operation_bus.py" in users, "помощник пропал из шины"
    assert len(users) >= 4, (
        f"помощником пользуются только {users} — отложенный запуск где-то "
        "снова считается от «сейчас» напрямую"
    )


def test_the_measurer_knows_its_allowed_exception_still_exists():
    """Разрешение, под которым уже нет кода, — молчаливая дыра."""
    for rel, fn in ALLOWED:
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert f"def {fn}" in src, (
            f"разрешённое исключение {rel}:{fn} больше не существует — "
            "список исключений надо перечитать, а не держать про запас"
        )
