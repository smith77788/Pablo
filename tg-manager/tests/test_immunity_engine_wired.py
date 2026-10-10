"""Ban Weather (Фаза 1) построен, покрыт тестами — и никогда не запускался.

ЧТО БЫЛО СЛОМАНО. `schema_v146_ban_weather.sql` ставит на `tg_accounts` триггер
`trg_immunity_capture_status`, который пишет КАЖДУЮ смену `acc_status` в
`account_status_events`. Триггер работает с момента применения схемы. Обработчик —
`services/immunity_engine.py`, 312 строк с юнит-тестами — не был подключён ни к
одному фоновому циклу: в `main.py` стартует 39 сервисов, и его среди них не было.

Что это значило на практике: таблица событий росла, `processed_at` у всех строк
оставался NULL, автопсии смертей аккаунтов не появлялись НИКОГДА. Пользователь
терял аккаунты и не получал ни одного ответа на вопрос «чем этот отличался от
выживших» — при том что весь механизм ответа уже был написан и оплачен.

Это тест на ПРОВОДКУ, а не на логику (логика — в test_immunity_engine.py):
готовый движок без строки запуска неотличим от отсутствующего.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")


def test_engine_is_started_as_background_service():
    assert "immunity_engine" in MAIN, (
        "движок обработки событий не запускается — триггер копит события в никуда"
    )
    assert re.search(r'_resilient\(\s*"immunity_engine"', MAIN), (
        "запуск обязан идти через _resilient: цикл, упавший без авто-рестарта, "
        "снова оставит события необработанными, но уже молча"
    )


def test_started_with_pool_only():
    """`start(pool, interval_sec=120)` не принимает bot — лишний аргумент уронил
    бы задачу на первом же тике, и вся проводка была бы декоративной."""
    m = re.search(r'_resilient\(\s*"immunity_engine",\s*([^)]+)\)', MAIN)
    assert m, "вызов не найден"
    args = [a.strip() for a in m.group(1).split(",")]
    assert args[1] == "pool", f"ожидался pool, получено {args[1:]}"
    assert len(args) == 2, f"start(pool) принимает только пул, передано: {args[1:]}"


def test_start_signature_matches_call():
    import inspect
    from services import immunity_engine
    sig = inspect.signature(immunity_engine.start)
    params = list(sig.parameters)
    assert params[0] == "pool"
    assert all(sig.parameters[p].default is not inspect.Parameter.empty
               for p in params[1:]), (
        "у start() появился обязательный параметр — проводка в main.py сломана"
    )


def test_trigger_exists_so_events_really_arrive():
    """Если триггера нет, запуск движка бессмыслен: обрабатывать нечего."""
    sql = (ROOT / "schema_v146_ban_weather.sql").read_text(encoding="utf-8")
    assert "trg_immunity_capture_status" in sql
    assert "account_status_events" in sql


def test_loop_is_fail_soft():
    """Иммунитет — надстройка, а не критический путь: его сбой не имеет права
    влиять на исполнение операций."""
    import inspect
    from services import immunity_engine
    src = inspect.getsource(immunity_engine.start)
    assert "except Exception" in src and "while True" in src, (
        "цикл обязан переживать сбой одного прохода"
    )
