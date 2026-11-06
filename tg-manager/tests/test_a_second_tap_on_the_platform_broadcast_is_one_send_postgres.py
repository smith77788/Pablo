"""Запрет повтора админской рассылки живёт в SQL — значит, нужна живая база.

Заглушка пула проверяет ПОРЯДОК (заявка до отправки, повтор не шлёт), но не
проверяет сам запрос: `make_interval(secs => $4)`, слияние jsonb через `||`,
поиск по `details->>'ident'` и advisory-блокировку. Ровно эти места уже
обжигали: сторож, который сверял текст запроса, зеленел, а запрос был другой.

Личность — админ и отпечаток текста. Окно — 90 секунд. Проверяем: заявка
принята один раз, повтор в окне видит ИТОГ первой (а не нули заявки), другой
текст и другой админ не блокируются, запись вне окна не мешает.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
ADMIN = 993711
OTHER_ADMIN = 993712

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _wipe(pool):
    _run(pool.execute("DELETE FROM admin_audit_log WHERE admin_id = ANY($1::bigint[])",
                      [ADMIN, OTHER_ADMIN]))


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
    # Журнал ссылается на platform_users внешним ключом — админы должны быть.
    for uid in (ADMIN, OTHER_ADMIN):
        _run(p.execute(
            "INSERT INTO platform_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", uid))
    yield p
    _wipe(p)
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _wipe(pool)
    yield


def _claim(pool, admin, ident, details=None, window=90):
    from database.db import claim_admin_action

    return _run(claim_admin_action(
        pool, admin, "admin_broadcast", ident,
        details or {"recipients": 3, "sent": 0, "failed": 0, "started": True},
        window_sec=window, source="miniapp"))


def test_the_claim_is_taken_once_and_the_repeat_sees_it(pool):
    log_id, twin = _claim(pool, ADMIN, "aaa")
    assert log_id and twin is None, (log_id, twin)

    again_id, again_twin = _claim(pool, ADMIN, "aaa")
    assert again_id is None, "вторая заявка прошла — рассылка уйдёт дважды"
    assert again_twin is not None and again_twin["recipients"] == 3, again_twin


def test_the_repeat_sees_the_finished_numbers_not_the_zeros(pool):
    """Повтору отвечают итогом первой рассылки: «отправлено 0» — ложь."""
    from database.db import update_admin_action

    log_id, _ = _claim(pool, ADMIN, "bbb")
    _run(update_admin_action(pool, log_id, {"sent": 2, "failed": 1, "finished": True}))

    _id, twin = _claim(pool, ADMIN, "bbb")
    assert twin["sent"] == 2 and twin["failed"] == 1, twin
    assert twin["finished"] is True, twin
    assert twin["recipients"] == 3, "слияние jsonb потеряло поля заявки"


def test_another_text_and_another_admin_are_not_blocked(pool):
    assert _claim(pool, ADMIN, "ccc")[0]
    other_text_id, other_text_twin = _claim(pool, ADMIN, "ddd")
    assert other_text_id and other_text_twin is None, "другой текст заблокирован"
    other_admin_id, other_admin_twin = _claim(pool, OTHER_ADMIN, "ccc")
    assert other_admin_id and other_admin_twin is None, (
        "второй админ заблокирован текстом первого")


def test_a_record_older_than_the_window_does_not_block(pool):
    log_id, _ = _claim(pool, ADMIN, "eee")
    _run(pool.execute(
        "UPDATE admin_audit_log SET created_at = now() - interval '10 minutes' "
        "WHERE id = $1", log_id))
    fresh_id, twin = _claim(pool, ADMIN, "eee")
    assert fresh_id and twin is None, (
        "запись вне окна блокирует: вторая волна того же письма недоступна")


def test_simultaneous_claims_give_one(pool):
    """Два одновременных нажатия: без advisory-блокировки проходят оба."""
    from database.db import claim_admin_action

    async def _both():
        return await asyncio.gather(
            claim_admin_action(pool, ADMIN, "admin_broadcast", "fff",
                               {"recipients": 1}, window_sec=90),
            claim_admin_action(pool, ADMIN, "admin_broadcast", "fff",
                               {"recipients": 1}, window_sec=90),
        )

    res = _run(_both())
    taken = [r for r in res if r[0]]
    assert len(taken) == 1, f"заявку взяли дважды: {res}"


def test_the_journal_holds_one_row_for_two_taps(pool):
    _claim(pool, ADMIN, "ggg")
    _claim(pool, ADMIN, "ggg")
    rows = _run(pool.fetch(
        "SELECT details FROM admin_audit_log WHERE admin_id=$1 AND action='admin_broadcast'",
        ADMIN))
    assert len(rows) == 1, f"в журнале {len(rows)} записей на два нажатия"
    d = rows[0]["details"]
    d = json.loads(d) if isinstance(d, str) else dict(d)
    assert d["ident"] == "ggg", d
