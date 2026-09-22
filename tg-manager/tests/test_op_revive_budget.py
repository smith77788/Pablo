"""Живучесть операций ограничена: «ядовитая» операция не воскресает вечно.

Разрыв. Операции, застрявшие в 'running', возвращались в 'pending' двумя
механизмами — сбросом на старте воркера (после SIGTERM/рестарта Railway) и
сторожем зависших (running дольше 60 минут). Оба сбрасывали статус БЕЗ учёта
попыток. Поэтому операция, которая роняет или вешает САМ воркер, воскресала
бесконечно: упала → рестарт контейнера → снова подхвачена → снова уронила.
Каждый круг занимал слот параллельности и забирал аккаунты флота, а владелец
видел операцию, которая «всё время выполняется» и никогда не заканчивается.

retry_count для этого не подходит: он расходуется на повторы по ОШИБКЕ
исполнителя (FloodWait, сеть). Смешав счётчики, мы бы либо съедали живучесть
честным повтором после флуда, либо прятали ядовитую операцию за неизрасходованным
бюджетом флуда. Поэтому отдельная колонка revive_count (schema_v218).

op_worker импортирует telethon и в тестовой среде не поднимается — связку
проверяем по исходнику, как и соседние тесты очереди.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _fn(src: str, name: str) -> str:
    """Тело функции от её def до следующего def верхнего уровня."""
    start = src.index(f"async def {name}(")
    m = re.search(r"\nasync def |\ndef ", src[start + 10:])
    return src[start:start + 10 + m.start()] if m else src[start:]


def test_migration_adds_revive_count():
    sql = _read("schema_v218_op_revive_count.sql")
    assert "operation_queue" in sql
    assert "ADD COLUMN IF NOT EXISTS revive_count" in sql
    # Аддитивно: старые строки обязаны трактоваться как «ни разу не воскресали».
    assert "DEFAULT 0" in sql


def test_budget_is_bounded_and_configurable():
    ow = _read("services/op_worker.py")
    assert "_MAX_REVIVES = _int_env(" in ow, "бюджет живучести должен быть настраиваемым"
    m = re.search(r'_MAX_REVIVES = _int_env\("OP_MAX_REVIVES", (\d+), (\d+), (\d+)\)', ow)
    assert m, "бюджет живучести не найден"
    default, lo, hi = (int(g) for g in m.groups())
    assert 1 <= lo <= default <= hi, (default, lo, hi)
    assert hi < 1000, "верхняя граница обязана быть конечной — иначе предела нет"


def test_startup_reset_counts_revives():
    """Сброс на старте обязан ТРАТИТЬ бюджет, а не поднимать операцию даром."""
    body = _fn(_read("services/op_worker.py"), "_reset_stale_running")
    assert "revive_count = COALESCE(revive_count, 0) + 1" in body, (
        "сброс после рестарта не увеличивает счётчик воскрешений — "
        "ядовитая операция будет подниматься бесконечно"
    )


def test_startup_reset_fails_poisoned_ops():
    """Исчерпавшая бюджет операция получает терминальный статус, а не новый круг."""
    body = _fn(_read("services/op_worker.py"), "_reset_stale_running")
    assert "COALESCE(revive_count, 0) >= $1" in body
    assert "SET status = 'failed'" in body
    assert "_MAX_REVIVES" in body
    # Порядок важен: сначала гасим исчерпавших, потом поднимаем остальных —
    # иначе тот же UPDATE поднял бы их ещё раз.
    assert body.index("SET status = 'failed'") < body.index("SET status = 'pending'")


def test_watchdog_counts_revives_and_fails_poisoned():
    """Зависшая (а не упавшая) операция — тот же класс: сторож её тоже ограничивает."""
    body = _fn(_read("services/op_worker.py"), "_watchdog_stale")
    assert "revive_count = COALESCE(revive_count, 0) + 1" in body, (
        "сторож зависших не тратит бюджет — операция будет забирать флот раз в час вечно"
    )
    assert "COALESCE(revive_count, 0) >= $3" in body
    assert "SET status = 'failed'" in body
    assert body.index("SET status = 'failed'") < body.index("SET status = 'pending'")


def test_watchdog_poison_respects_active_ops():
    """Живую операцию этого процесса гасить нельзя — она не зависла, она работает."""
    body = _fn(_read("services/op_worker.py"), "_watchdog_stale")
    poison = body[body.index("SET status = 'failed'"):body.index("SET status = 'pending'")]
    assert "id != ALL($2::bigint[])" in poison, (
        "гашение по бюджету обязано исключать активные op, как и сброс"
    )
    assert "started_at < now() - make_interval(mins => $1)" in poison, (
        "гасить можно только то, что реально висит дольше таймаута"
    )


def test_poisoned_op_explains_itself_to_owner():
    """Владелец должен прочитать, ПОЧЕМУ операция остановлена, а не гадать."""
    ow = _read("services/op_worker.py")
    assert "_POISON_ERROR" in ow
    m = re.search(r'_POISON_ERROR = \(\n((?:\s+".*"\n)+)\)', ow)
    assert m, "текст причины не найден"
    text = m.group(1)
    assert "error_msg" in ow
    # Текст для владельца — на русском и с понятным действием (CLAUDE.md: владелец
    # не понимает английский).
    assert re.search(r"[а-яА-Я]", text), text
    assert "заново" in text, "причина должна подсказывать, что делать дальше"


def test_revive_column_self_heals_on_lag():
    """Деплой и миграции расходятся во времени.

    Без колонки ОБА сторожа падали бы на каждом тике — то есть живучесть
    исчезала бы целиком вместо того, чтобы ограничиться.
    """
    ow = _read("services/op_worker.py")
    assert "async def _ensure_revive_column" in ow
    assert "ADD COLUMN IF NOT EXISTS revive_count" in ow
    reset = _fn(ow, "_reset_stale_running")
    assert "_ensure_revive_column(pool)" in reset
    # Сброс на старте выполняется ДО цикла сторожа — значит колонка есть к первому тику.
    run = _fn(ow, "run")
    assert run.index("_reset_stale_running(pool)") < run.index("_watchdog_stale(pool)")


def test_error_retry_budget_stays_separate():
    """retry_count не должен расходоваться на воскрешения (и наоборот)."""
    ow = _read("services/op_worker.py")
    requeue = _fn(ow, "_maybe_requeue")
    assert "retry_count=$1" in requeue
    assert "revive_count" not in requeue, (
        "повтор по ошибке исполнителя не должен трогать бюджет живучести"
    )
    for name in ("_reset_stale_running", "_watchdog_stale"):
        body = _fn(ow, name)
        assert "retry_count" not in body, (
            f"{name}: воскрешение не должно тратить бюджет повторов по ошибке"
        )
