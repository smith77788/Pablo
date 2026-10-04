"""Пул-заглушка воркера не должен утекать в предохранитель на весь прогон.

`op_worker.init_op_worker_pool(pool)` прокидывает пул ещё и в
`op_circuit_breaker.set_pool(pool)` — это правильно в проде и ловушка в тестах:
тест с пулом-заглушкой убирал за собой только `op_worker._db_pool`, а
предохранитель оставался с заглушкой до конца прогона. Заглушка отвечает одной
и той же строкой на любой запрос, поэтому следующий тест с НАСТОЯЩЕЙ базой
получал от предохранителя чужую строку:

    row = {'retry_count': 0, 'max_retries': 3}
    KeyError: 'failures'

Так падал `tests/test_contacts_sync_e2e_postgres.py` — зелёный по отдельности,
красный в полном прогоне. Держит это автофикстура `_reset_worker_pools` в
`tests/conftest.py`; тест следит, чтобы её не убрали.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFTEST = open(os.path.join(ROOT, "tests", "conftest.py"), encoding="utf-8").read()


def test_worker_pool_also_arms_the_circuit_breaker():
    """Связь, из-за которой фикстура нужна: один вызов задаёт два пула."""
    src = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    body = src[src.index("def init_op_worker_pool"):]
    body = body[:body.index("async def _db_claim")]
    assert "_ocb.set_pool(pool)" in body, (
        "воркер больше не прокидывает пул предохранителю — проверьте, "
        "нужна ли ещё фикстура _reset_worker_pools"
    )


def test_conftest_resets_both_pools_between_tests():
    m = re.search(r"def _reset_worker_pools\(\):(.*?)\n    _clear\(\)\n    yield",
                  CONFTEST, re.S)
    assert m, "автофикстура _reset_worker_pools пропала из tests/conftest.py"
    body = m.group(1)
    assert '"services.op_worker"' in body
    assert '"services.op_circuit_breaker"' in body
    assert '"_db_pool"' in body
    # Чистим и до, и после теста: иначе заглушка переживёт свой тест.
    assert CONFTEST.count("_clear()", CONFTEST.index("def _reset_worker_pools")) >= 2


def test_fixture_is_autouse():
    i = CONFTEST.find("def _reset_worker_pools")
    assert i > 0, "автофикстура _reset_worker_pools пропала из tests/conftest.py"
    head = CONFTEST[max(0, i - 120):i]
    assert "autouse=True" in head, (
        "фикстура перестала быть автоматической — загрязнение вернётся молча"
    )


def test_circuit_breaker_row_reader_needs_its_own_columns():
    """Почему чужая строка падает именно KeyError: читатель ждёт свои колонки."""
    src = open(os.path.join(ROOT, "services", "op_circuit_breaker.py"),
               encoding="utf-8").read()
    body = src[src.index("def _cb_from_row"):]
    body = body[:body.index("async def _cb_db_record")]
    for col in ("failures", "tripped_at", "cooldown_until"):
        assert f'row["{col}"]' in body, col
