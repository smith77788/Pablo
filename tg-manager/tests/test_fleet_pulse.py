"""Пульс флота: живое состояние аккаунтов на экране (ось №1 аудита).

Проверяем классификацию (готов/пауза/выбыл), человеческую причину, время до
готовности, сортировку «сначала требующие внимания», и разводку (команда/меню).
"""
from __future__ import annotations

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from services import fleet_pulse as fp  # noqa: E402

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def test_human_left_formats():
    assert fp._human_left(0) == "сейчас"
    assert fp._human_left(30) == "30 с"
    assert fp._human_left(120) == "2 мин"
    assert fp._human_left(3700).startswith("1 ч")


def test_summarize_counts_states():
    states = [{"state": "ready"}, {"state": "ready"}, {"state": "cooling"},
              {"state": "dead"}]
    s = fp.summarize(states)
    assert s["ready"] == 2 and s["cooling"] == 1 and s["dead"] == 1
    assert s["total"] == 4


def test_wired_into_bot():
    m = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "fleet_pulse_handler.router" in m, "роутер пульса не подключён"
    assert 'command="pulse"' in m, "команды /pulse нет в меню бота"
    h = open(os.path.join(ROOT, "bot", "handlers", "fleet_pulse.py"), encoding="utf-8").read()
    assert 'Command("pulse")' in h
    k = open(os.path.join(ROOT, "bot", "keyboards.py"), encoding="utf-8").read()
    assert 'BotCb(action="pulse")' in k, "кнопки пульса нет в главном меню"


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
def test_states_classify_on_real_schema():
    import asyncio
    import asyncpg

    async def _run():
        conn = await asyncpg.connect(DSN)
        try:
            with open(os.path.join(ROOT, "schema.sql"), encoding="utf-8") as f:
                await conn.execute(f.read())
        except Exception:
            pass
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS account_rehab_state ("
                "acc_id BIGINT PRIMARY KEY, owner_id BIGINT NOT NULL, "
                "phase TEXT NOT NULL DEFAULT 'appeal', attempts INTEGER DEFAULT 0, "
                "next_action_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            await conn.execute("DELETE FROM account_rehab_state WHERE owner_id=$1", 7700)
            await conn.execute("DELETE FROM tg_accounts WHERE owner_id=$1", 7700)
            await conn.execute(
                "INSERT INTO tg_accounts(id, owner_id, phone, first_name, is_active, "
                "session_str, acc_status, cooldown_until, trust_score) VALUES "
                "(77001,$1,'+1','Ready',TRUE,'s','active',NULL,0.8),"
                "(77002,$1,'+2','Cooling',TRUE,'s','cooldown', now()+interval '5 minutes',0.6),"
                "(77003,$1,'+3','Banned',TRUE,'s','banned',NULL,0.1),"
                "(77004,$1,'+4','Rehab',TRUE,'s','spamblock',NULL,0.2)",
                7700)
            # аккаунт под спам-блоком в фазе тихого прогрева
            await conn.execute(
                "INSERT INTO account_rehab_state(acc_id, owner_id, phase) "
                "VALUES (77004,$1,'warming')", 7700)

            states = await fp.account_states(conn, 7700)
            by_id = {s["id"]: s for s in states}
            assert by_id[77001]["state"] == "ready"
            assert by_id[77002]["state"] == "cooling"
            assert by_id[77002]["ready_in_sec"] > 0, "пауза без времени до готовности"
            assert by_id[77003]["state"] == "dead"
            assert "перезал" in by_id[77003]["reason"] or "забанен" in by_id[77003]["reason"]
            # живая фаза реабилитации отражена в причине
            assert by_id[77004]["state"] == "dead"
            assert "прогрев" in by_id[77004]["reason"], "фаза реабилитации не показана"

            # сортировка: сначала «требующие внимания» (dead/cooling), ready в конце
            assert states[-1]["state"] == "ready"
        finally:
            await conn.execute("DELETE FROM account_rehab_state WHERE owner_id=$1", 7700)
            await conn.execute("DELETE FROM tg_accounts WHERE owner_id=$1", 7700)
            await conn.close()

    asyncio.run(_run())


from datetime import datetime, timedelta, timezone  # noqa: E402


def test_rehab_reason_covers_every_phase():
    now = datetime.now(timezone.utc)
    assert "снятие" in fp._rehab_reason({"phase": "appeal"}, now)
    assert "прогрев" in fp._rehab_reason({"phase": "warming"}, now)
    # recheck со сроком в будущем показывает «через …»
    r = fp._rehab_reason({"phase": "recheck",
                          "next_action_at": now + timedelta(hours=6)}, now)
    assert "перепроверка" in r and "через" in r
    # recheck без корректного срока — без «через»
    r2 = fp._rehab_reason({"phase": "recheck", "next_action_at": None}, now)
    assert "перепроверка" in r2
    assert "ручной разбор" in fp._rehab_reason({"phase": "stuck"}, now)
    # неизвестная фаза → общий текст спам-блока
    assert "спам-блок" in fp._rehab_reason({"phase": "???"}, now)


def test_dead_reason_maps_known_statuses():
    assert "забанен" in fp._dead_reason("banned")
    assert "деактив" in fp._dead_reason("deactivated")
    assert "перезал" in fp._dead_reason("session_expired")
    assert fp._dead_reason("нечто") == "нечто"
