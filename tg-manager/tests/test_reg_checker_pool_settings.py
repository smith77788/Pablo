"""Регресс: экран «⚙️ Настройки пула» в Reg-Checker всегда показывал только
«🌐 Все пулы», даже когда у владельца реально есть несколько именованных пулов.

Первопричина: `cb_reg_settings` звал `db.get_distinct_pools(pool)` без
`owner_id`, хотя функция требует оба аргумента (`database/db.py`). Каждый
вызов падал `TypeError`, пойманным широким `except Exception:` — молча
превращаясь в пустой список `pools = []`. Индивидуальные кнопки пулов никогда
не рендерились, при этом СОСЕДНИЙ запрос на той же функции (`pool_counts`)
корректно фильтровал по `owner_id` и доказывал, что пулы у аккаунтов реально
есть — баг был чисто в вызове, не в данных.

Реальный Postgres — нужно и связывание параметров, и то, что второй запрос
на этом же экране использует owner_id корректно (иначе баг был бы не виден).
"""
from __future__ import annotations

import asyncio
import importlib.util
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 991301

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _load_reg_checker():
    path = os.path.join(ROOT, "bot", "handlers", "reg_checker.py")
    spec = importlib.util.spec_from_file_location("_reg_checker_pool_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeUser:
    def __init__(self, uid: int):
        self.id = uid


class _FakeMessage:
    def __init__(self):
        self.last_text: str | None = None
        self.last_markup = None

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.last_text = text
        self.last_markup = reply_markup


class _FakeCallback:
    def __init__(self, uid: int):
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage()

    async def answer(self, *a, **k):
        pass


class _FakeState:
    async def get_data(self):
        return {}


@pytest.fixture(scope="module")
def pool():
    import glob
    import re

    import asyncpg

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        files = ["schema.sql"] + sorted(
            glob.glob(os.path.join(ROOT, "schema_v*.sql")),
            key=lambda p_: int(re.search(r"schema_v(\d+)", p_).group(1)))
        for f in files:
            path = f if os.path.isabs(f) else os.path.join(ROOT, f)
            if not os.path.exists(path):
                continue
            try:
                await p.execute(open(path, encoding="utf-8").read())
            except Exception:
                pass  # схемы идемпотентны; частичный сбой не должен рушить стенд
        return p

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))
    _run(p.close())


def _button_texts(markup) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]


def test_pool_buttons_render_for_owners_real_pools(pool):
    _run(pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))
    _run(pool.executemany(
        "INSERT INTO tg_accounts(owner_id, phone, session_str, acc_status, pool) "
        "VALUES($1,$2,'s','active',$3)",
        [(OWNER, f"+7900{i:07d}", pname) for i, pname in enumerate(["warm", "cold", "warm"])],
    ))

    reg_checker = _load_reg_checker()
    cb = _FakeCallback(OWNER)
    _run(reg_checker.cb_reg_settings(cb, _FakeState(), pool))

    texts = _button_texts(cb.message.last_markup)
    assert any("warm" in t for t in texts), f"кнопка пула 'warm' не отрендерилась: {texts}"
    assert any("cold" in t for t in texts), f"кнопка пула 'cold' не отрендерилась: {texts}"
    assert any("Все пулы" in t for t in texts)


def test_pool_button_shows_the_real_account_count(pool):
    _run(pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))
    _run(pool.executemany(
        "INSERT INTO tg_accounts(owner_id, phone, session_str, acc_status, pool) "
        "VALUES($1,$2,'s','active',$3)",
        [(OWNER, f"+7901{i:07d}", "warm") for i in range(3)],
    ))

    reg_checker = _load_reg_checker()
    cb = _FakeCallback(OWNER)
    _run(reg_checker.cb_reg_settings(cb, _FakeState(), pool))

    texts = _button_texts(cb.message.last_markup)
    warm_btn = next((t for t in texts if "warm" in t), None)
    assert warm_btn is not None and "3" in warm_btn, f"счётчик аккаунтов в пуле неверный: {warm_btn!r}"
