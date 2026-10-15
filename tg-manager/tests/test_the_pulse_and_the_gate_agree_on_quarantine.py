"""Экран говорит «Карантин» ровно тогда, когда двери отказываются брать аккаунт.

ЧТО ЛОМАЛОСЬ. Гейт (`quarantined_accounts`) считал карантином серьёзное
ограничение за последние трое суток, а экран здоровья (`get_account_health`)
выносил вердикт «Карантин» по собственному окну в семь суток. С четвёртых по
седьмые сутки после ограничения владелец читал на экране аккаунта «аккаунт на
паузе», подсказку «дайте отлежаться 3+ дней» и плашку в мини-аппе — а все двери
выбора этот аккаунт уже брали и работали им. Экран описывал не то поведение,
которое у системы есть; совет при этом ссылался на ТРИ дня, то есть был написан
под окно гейта, а вердикт стоял на семи.

Теперь «Карантин» означает ровно отказ дверей. Ограничение, чьё окно уже
прошло, остаётся «Под риском» — оно было, но паузы больше нет.
"""
from __future__ import annotations

import datetime
import os

import pytest

from services import infra_memory as im

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
UTC = datetime.timezone.utc


def test_the_two_windows_are_one_number():
    """Разъехаться им нельзя: это одно утверждение о системе."""
    assert im.QUARANTINE_DAYS == 3
    import inspect

    for fn in (im.quarantined_accounts, im.is_account_quarantined):
        default = inspect.signature(fn).parameters["days"].default
        assert default == im.QUARANTINE_DAYS, (
            f"{fn.__name__} считает карантин по своему окну — экран и гейт "
            f"снова разойдутся"
        )


def test_the_screen_verdict_stands_on_the_gate_window():
    """Храповик: вердикт не должен вернуться на широкое окно экрана."""
    import inspect

    src = inspect.getsource(im.get_account_health)
    assert "severe_gate" in src, "вердикт перестал считать окно гейта"
    assert 'if severe_gate or status_bad' in src, (
        "«Карантин» снова выносится по окну экрана, а не по окну гейта"
    )


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_screen_and_gate_agree_day_by_day_postgres():
    import asyncio
    import asyncpg

    OWNER = 825022

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)

        async def _cl():
            await pool.execute("DELETE FROM restriction_events WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)

        async def _verdict(acc):
            pulse = await im.get_account_health(pool, OWNER)
            row = next(a for a in pulse["accounts"] if int(a["account_id"]) == acc)
            gated = acc in await im.quarantined_accounts(pool, [acc])
            return row, gated

        try:
            await _cl()
            acc = int(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,"
                "acc_status,trust_score,added_at) VALUES($1,$2,'sess',TRUE,'active',"
                "1.0, now() - interval '30 days') RETURNING id",
                OWNER, "+79000009022"))

            # Ограничение сутки назад: и экран, и гейт держат аккаунт.
            await pool.execute(
                "INSERT INTO restriction_events(owner_id,account_id,event_type,"
                "severity,created_at) VALUES($1,$2,'account_restricted','critical',"
                "now() - interval '1 day')", OWNER, acc)
            row, gated = await _verdict(acc)
            assert gated is True
            assert row["status"] == "quarantine", (
                "свежее ограничение — экран обязан говорить «Карантин»")

            # То же ограничение пятидневной давности: окно гейта прошло.
            await pool.execute(
                "UPDATE restriction_events SET created_at = now() - interval '5 days' "
                "WHERE owner_id=$1", OWNER)
            row, gated = await _verdict(acc)
            assert gated is False, "гейт изменил окно — тест держится не за то"
            assert row["status"] != "quarantine", (
                "двери уже берут аккаунт, а экран всё ещё пишет «Карантин» — "
                "владелец видит паузу, которой нет"
            )
            assert row["status"] == "at_risk", (
                "ограничение было — аккаунт не «здоров», он «под риском»")
            assert "severe_past" in (row.get("reason_codes") or []), (
                "причина не названа: экран молча понизил статус")

            # Владелец снял риск — обе стороны обязаны отпустить сразу.
            await pool.execute(
                "UPDATE restriction_events SET created_at = now() - interval '1 day' "
                "WHERE owner_id=$1", OWNER)
            await pool.execute(
                "UPDATE tg_accounts SET risk_cleared_at = now() WHERE id=$1", acc)
            row, gated = await _verdict(acc)
            assert gated is False
            assert row["status"] != "quarantine", (
                "кнопка «Сбросить кулдаун» не снимает карантин на экране")

            await _cl()
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())
