"""Двойной тап «Повторить» даёт одну операцию, а не две.

ЧТО ЛОМАЛОСЬ. Шина гасит двойной тап кнопки окном идемпотентности: внутри окна
идентичный сабмит переиспользует уже стоящую операцию. Ровно на кнопках повтора
это окно было ОТКЛЮЧЕНО — с обоснованием «повтор это намеренно та же операция с
теми же params, окно приняло бы его за двойной тап».

Обе половины обоснования не верны:

  * дедуп ищет совпадение только среди ЖИВЫХ операций ('pending'/'running'), а
    повторить можно лишь недоведённую, то есть завершённую — `can_retry` прямо
    отказывает операции в работе. Исходная операция под дедуп не попадает;
  * params повтора несут ссылку `retry_of_op`, которой у исходной нет, так что и
    по содержимому они различаются.

Зато отключённое окно снимало защиту именно там, где по кнопке бьют чаще всего:
владелец видит недоведённую работу и жмёт «Повторить» дважды. Получались ДВЕ
одинаковые операции — двойная рассылка тем же адресатам, двойной пост в те же
каналы, двойной расход лимитов аккаунтов. У кнопки «перезапустить все
недоведённые» цена выше всего: она берёт до 25 операций за нажатие, то есть два
тапа давали 50.

ЧТО ТЕПЕРЬ. Оба пути повтора идут с обычным окном идемпотентности.
"""
from __future__ import annotations

import ast
import inspect

import pytest


def _func_body(module, name: str) -> str:
    src = inspect.getsource(module)
    tree = ast.parse(src)
    lines = src.split("\n")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"функция {name} не найдена в {module.__name__}")


@pytest.mark.parametrize("name", ["retry_operation", "operations_retry_failed"])
def test_retry_keeps_the_double_tap_window(name):
    from services import mini_app_api

    body = _func_body(mini_app_api, name)

    assert "dedup_window_sec=0" not in body, (
        f"{name} отключает окно идемпотентности — двойной тап по кнопке повтора "
        f"даст две одинаковые операции: двойная рассылка тем же адресатам и "
        f"двойной расход лимитов аккаунтов"
    )


def test_no_caller_disables_the_window():
    """Окно отключает только сама шина — больше никто."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders = []
    for base in ("bot", "services"):
        for path in sorted((root / base).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel == "services/operation_bus.py":
                continue
            src = path.read_text(encoding="utf-8")
            for i, line in enumerate(src.split("\n"), 1):
                if "dedup_window_sec=0" in line and not line.strip().startswith("#"):
                    offenders.append(f"{rel}:{i}")
    assert not offenders, (
        "защита от двойного тапа отключена в обход шины:\n  " + "\n  ".join(offenders)
    )


def test_the_window_only_matches_live_operations():
    """Обоснование фикса: завершённая операция под дедуп не попадает."""
    from services import operation_bus

    src = inspect.getsource(operation_bus.submit)

    assert "status IN ('pending', 'running')" in src, (
        "дедуп начал смотреть и на завершённые операции — тогда повтор "
        "действительно слился бы с исходной, и его пришлось бы исключать"
    )


def test_retry_params_differ_from_the_source():
    """Вторая половина обоснования: ссылка на предка меняет params."""
    from services import operation_bus

    src = {"text": "пост"}
    out = operation_bus.params_for_retry(41, src)

    assert out != src, (
        "params повтора совпадают с исходными — дедуп принял бы повтор за "
        "двойной тап исходной операции"
    )
    assert out["retry_of_op"] == 41


def test_a_second_identical_retry_is_merged(monkeypatch):
    """Два одинаковых сабмита повтора дают один op_id."""
    import asyncio

    from services import operation_bus

    class _Conn:
        def __init__(self, store):
            self.store = store

        def transaction(self):
            conn = self

            class _Tx:
                async def __aenter__(self_inner):
                    return conn

                async def __aexit__(self_inner, *exc):
                    return False

            return _Tx()

        async def execute(self, query, *args):
            return "SELECT 1"

        async def fetchval(self, query, *args):
            # args: owner_id, op_type, params_json, sched, window
            return self.store.get((args[0], args[1], args[2]))

        async def fetchrow(self, query, *args):
            new_id = 100 + len(self.store)
            self.store[(args[0], args[1], args[2])] = new_id
            return {"id": new_id}

    class _Pool:
        def __init__(self):
            self.store: dict = {}

        def acquire(self):
            conn = _Conn(self.store)

            class _Acq:
                async def __aenter__(self_inner):
                    return conn

                async def __aexit__(self_inner, *exc):
                    return False

            return _Acq()

        async def execute(self, query, *args):
            return "UPDATE 1"

        async def fetchrow(self, query, *args):
            return None

        async def fetch(self, query, *args):
            return []

        async def fetchval(self, query, *args):
            return None

    pool = _Pool()
    params = operation_bus.params_for_retry(41, {"text": "пост"})
    # Тип без требований по тарифу: проверяем дедуп, а не гейт подписки.
    op_type = "contacts_sync"

    async def _both():
        first = await operation_bus.submit(pool, 5, op_type, dict(params), total_items=1)
        second = await operation_bus.submit(pool, 5, op_type, dict(params), total_items=1)
        return first, second

    first, second = asyncio.run(_both())

    assert first == second, (
        "второй сабмит того же повтора создал ВТОРУЮ операцию — двойной тап "
        "по кнопке повтора удваивает работу"
    )
