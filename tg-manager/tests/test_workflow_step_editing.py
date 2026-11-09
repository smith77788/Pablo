"""Правка сценария не ломает переходы, и включить молчащий сценарий нельзя.

ЧТО БЫЛО.
1. Удаление шага не пересчитывало переходы «на шаг N»: в шаблоне
   «Квалификация заявки» после удаления первого шага кнопка «Хочу купить»
   (→ шаг 2) вела на вопрос о телефоне, а «Просто смотрю» (→ шаг 8) — на
   «Спасибо, менеджер свяжется».
2. Удаление любого шага выбрасывало все шаги «перейти к шагу»: сохранённый
   шаг хранит номер в `goto`, а повторная проверка искала его в `value`.
3. Шаг нельзя было исправить на месте или переставить.
4. Включить можно было сценарий без шагов или с удалённым ботом.

Чистые функции проверяются без базы; путь через БД — на живом Postgres
(INFRAGRAM_TEST_DSN), как остальные *_postgres тесты.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services import workflow_engine as wf


# ── Чистые функции ──────────────────────────────────────────────────────────

def _lead_steps():
    return wf.validate_steps(wf.workflow_template("lead")["steps"])


def test_after_delete_keeps_targets_on_their_steps():
    # 5 шагов, удалён шаг 2.
    assert [wf._after_delete(n, 2, 5) for n in (1, 2, 3, 4, 5, 9)] == [1, 2, 2, 3, 4, 9]


def test_remap_moves_every_kind_of_jump():
    steps = [
        {"type": "message", "text": "a", "buttons": [{"text": "x", "goto": 3},
                                                      {"text": "u", "url": "https://e.x"}]},
        {"type": "condition", "condition": "tag_has", "condition_value": "t", "else_goto": 4},
        {"type": "split", "branches": [{"label": "A", "weight": 50, "goto": 3},
                                       {"label": "B", "weight": 50, "goto": 4}]},
        {"type": "action", "action": "goto", "goto": 1},
        {"type": "wait", "event": "payment.received", "timeout_goto": 3},
    ]
    out = wf.remap_jumps(steps, lambda n: n + 10)
    assert out[0]["buttons"][0]["goto"] == 13
    assert "goto" not in out[0]["buttons"][1]
    assert out[1]["else_goto"] == 14
    assert [b["goto"] for b in out[2]["branches"]] == [13, 14]
    assert out[3]["goto"] == 11
    assert out[4]["timeout_goto"] == 13
    # Исходник не тронут.
    assert steps[0]["buttons"][0]["goto"] == 3


def test_saved_goto_action_survives_revalidation():
    step = wf.validate_step({"type": "action", "action": "goto", "value": "3"})
    assert step == {"type": "action", "action": "goto", "goto": 3}
    # Повторная проверка сохранённого шага (её делает удаление соседа).
    assert wf.validate_step(step)["goto"] == 3


def test_permutation_follows_moved_step():
    # Шаг 3 переставлен на первое место: [2, 0, 1] — старые индексы.
    f = wf._after_permutation([2, 0, 1], 3)
    assert [f(1), f(2), f(3), f(7)] == [2, 3, 1, 7]


def test_check_steps_flags_empty_template_text_and_dangling_jump():
    assert wf.check_steps([], "start")[0]["level"] == "block"
    probs = wf.check_steps(_lead_steps(), "start")
    texts = " ".join(p["text"] for p in probs)
    assert "текст из шаблона не изменён" in texts
    assert all(p["level"] == "warn" for p in probs)
    dangling = wf.check_steps([{"type": "message", "text": "Свой текст",
                                "buttons": [{"text": "x", "goto": 7}]}], "start")
    assert any("шаг 7, которого нет" in p["text"] for p in dangling)
    manual = wf.check_steps([{"type": "message", "text": "Свой текст"}], None)
    assert any("Только вручную" in p["text"] for p in manual)
    assert wf.blocking_text(manual) is None


# ── Через базу ──────────────────────────────────────────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993881
BOT_ID = 880088123
_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


async def _cleanup(p):
    await p.execute("DELETE FROM workflow_runs WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM workflow_definitions WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg
    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=3)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    _run(_cleanup(p))
    yield p
    _run(_cleanup(p))
    _run(p.close())


@pytest.fixture()
def lead(pool):
    async def _mk():
        await _cleanup(pool)
        await pool.execute(
            "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
            "VALUES($1,$2,'wf_edit_test_bot',TRUE,$3)",
            f"1234:wf-edit-{BOT_ID}", BOT_ID, OWNER)
        res = await wf.create_workflow(pool, OWNER, "Заявка", steps=_lead_steps(),
                                       is_active=False, bot_id=BOT_ID)
        await wf.set_trigger(pool, OWNER, res["id"], "start", None)
        return res["id"]
    return _run(_mk())


async def _steps(pool, wid):
    return wf._steps_list(await pool.fetchval(
        "SELECT steps FROM workflow_definitions WHERE id=$1", wid))


def test_delete_first_step_keeps_buttons_on_their_branches(pool, lead):
    before = _run(_steps(pool, lead))
    # Кнопки ведут на «Как к вам обращаться?» и на тег «смотрит».
    want = [before[b["goto"] - 1]["text"] if before[b["goto"] - 1].get("text")
            else before[b["goto"] - 1].get("value") for b in before[0]["buttons"]]
    # Вставим шаг перед «Квалификацией»: добавим и поднимем наверх.
    _run(wf.append_step(pool, OWNER, lead, {"type": "message", "text": "Вступление"}))
    _run(wf.move_step(pool, OWNER, lead, len(before), 0))
    moved = _run(_steps(pool, lead))
    assert moved[0]["text"] == "Вступление"
    btns = moved[1]["buttons"]
    got = [moved[b["goto"] - 1].get("text") or moved[b["goto"] - 1].get("value") for b in btns]
    assert got == want
    # И удалим вступление обратно.
    assert _run(wf.delete_step(pool, OWNER, lead, 0)) == len(before)
    after = _run(_steps(pool, lead))
    got = [after[b["goto"] - 1].get("text") or after[b["goto"] - 1].get("value")
           for b in after[0]["buttons"]]
    assert got == want


def test_delete_neighbour_keeps_goto_action(pool, lead):
    _run(wf.append_step(pool, OWNER, lead, {"type": "action", "action": "goto", "value": "1"}))
    n = len(_run(_steps(pool, lead)))
    _run(wf.delete_step(pool, OWNER, lead, 1))
    after = _run(_steps(pool, lead))
    assert len(after) == n - 1
    assert after[-1] == {"type": "action", "action": "goto", "goto": 1}


def test_update_step_in_place(pool, lead):
    res = _run(wf.update_step(pool, OWNER, lead, 0,
                              {"type": "message", "text": "Добрый день! Что подобрать?",
                               "buttons": [{"text": "Купить", "goto": 2}]}))
    assert res is not None
    steps = _run(_steps(pool, lead))
    assert steps[0]["text"] == "Добрый день! Что подобрать?"
    assert len(steps) == len(_lead_steps())
    with pytest.raises(ValueError):
        _run(wf.update_step(pool, OWNER, lead, 0, {"type": "nope"}))
    assert _run(wf.update_step(pool, OWNER, lead, 99, {"type": "message", "text": "x"})) is None


def test_activation_blocked_without_working_bot(pool, lead):
    probs = _run(wf.activation_problems(pool, OWNER, lead))
    assert wf.blocking_text(probs) is None          # только предупреждения
    _run(pool.execute("UPDATE managed_bots SET is_active=FALSE WHERE bot_id=$1", BOT_ID))
    probs = _run(wf.activation_problems(pool, OWNER, lead))
    assert "Бот сценария отключён" in (wf.blocking_text(probs) or "")
    assert _run(wf.activation_problems(pool, OWNER + 1, lead)) is None


def test_activation_blocked_for_empty_scenario(pool, lead):
    res = _run(wf.create_workflow(pool, OWNER, "Пустой", steps=[], bot_id=BOT_ID))
    assert "нет шагов" in wf.blocking_text(_run(wf.activation_problems(pool, OWNER, res["id"])))


# ── Один сценарий на много ботов ────────────────────────────────────────────

BOT2 = BOT_ID + 1
FOREIGN = BOT_ID + 2


def test_trigger_listens_only_to_listed_bots():
    from services import workflow_runtime as rt
    wfd = {"id": 1, "trigger_type": "start", "bot_id": 10, "bot_ids": [10, 11]}
    ev = {"event_type": "contact.started", "data": {}}
    assert rt.trigger_matches(wfd, {**ev, "bot_id": 11})
    assert rt.trigger_matches(wfd, {**ev, "bot_id": 10})
    assert not rt.trigger_matches(wfd, {**ev, "bot_id": 12})
    # Без списка — как раньше.
    assert not rt.trigger_matches({**wfd, "bot_ids": None}, {**ev, "bot_id": 11})


@pytest.fixture()
def fleet(pool, lead):
    async def _mk():
        await pool.execute("DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[])",
                           [BOT2, FOREIGN])
        await pool.execute(
            "INSERT INTO managed_bots(token, bot_id, username, is_active, added_by) "
            "VALUES($1,$2,'wf_edit_bot2',FALSE,$3), ($4,$5,'wf_edit_foreign',TRUE,$6)",
            f"1234:wf-edit-{BOT2}", BOT2, OWNER, f"1234:wf-edit-{FOREIGN}", FOREIGN, OWNER + 7)
    _run(_mk())
    yield lead
    _run(pool.execute("DELETE FROM bot_users WHERE bot_id = ANY($1::bigint[])",
                      [BOT_ID, BOT2]))
    _run(pool.execute("DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[])",
                      [BOT2, FOREIGN]))


def test_set_bots_reports_each_bot(pool, fleet):
    res = _run(wf.set_bots(pool, OWNER, fleet, [BOT_ID, BOT2, FOREIGN, BOT_ID]))
    assert res["bot_ids"] == [BOT_ID, BOT2]
    assert res["connected"] == 2 and res["skipped"] == 1
    by = {r["bot_id"]: r for r in res["results"]}
    assert by[FOREIGN]["ok"] is False
    assert "отключён" in by[BOT2]["reason"]
    row = _run(pool.fetchrow("SELECT bot_id, bot_ids FROM workflow_definitions WHERE id=$1",
                             fleet))
    assert row["bot_id"] == BOT_ID and list(row["bot_ids"]) == [BOT_ID, BOT2]
    # Один бот из двух не работает — предупреждение, не отказ.
    probs = _run(wf.activation_problems(pool, OWNER, fleet))
    assert any("1 из 2" in p["text"] for p in probs)
    assert wf.blocking_text(probs) is None
    # Вебхук на нескольких ботах — отказ.
    with pytest.raises(ValueError):
        _run(wf.set_trigger(pool, OWNER, fleet, "webhook", None))
    # Только чужие — отказ, прежний список на месте.
    with pytest.raises(ValueError):
        _run(wf.set_bots(pool, OWNER, fleet, [FOREIGN]))
    # Пусто — все мои боты.
    res = _run(wf.set_bots(pool, OWNER, fleet, []))
    assert res["bot_id"] is None and res["bot_ids"] == []


def test_scheduled_scenario_runs_on_every_bot_once(pool, fleet):
    from datetime import datetime, timedelta, timezone
    from services import workflow_runtime as rt

    async def go():
        await pool.execute("UPDATE managed_bots SET is_active=TRUE WHERE bot_id=$1", BOT2)
        for b in (BOT_ID, BOT2):
            await pool.execute(
                "INSERT INTO bot_users(bot_id, user_id, first_name, is_active) "
                "VALUES($1, 5551, 'Ира', TRUE) ON CONFLICT DO NOTHING", b)
        await wf.set_bots(pool, OWNER, fleet, [BOT_ID, BOT2])
        await wf.set_trigger(pool, OWNER, fleet, "schedule", "10:00", True)
        await pool.execute("UPDATE workflow_definitions SET is_active=TRUE WHERE id=$1", fleet)
        await pool.execute("DELETE FROM automation_cursors WHERE consumer LIKE $1",
                           f"workflow_schedule:{fleet}%")
        morning = datetime(2026, 11, 4, 7, 30, tzinfo=timezone.utc)
        assert await rt.scan_audience_triggers(pool, morning, force=True) == 2
        bots = sorted(r["bot_id"] for r in await pool.fetch(
            "SELECT bot_id FROM workflow_runs WHERE workflow_id=$1", fleet))
        assert bots == [BOT_ID, BOT2]
        assert await rt.scan_audience_triggers(pool, morning + timedelta(minutes=20),
                                               force=True) == 0
        await pool.execute("DELETE FROM workflow_runs WHERE workflow_id=$1", fleet)
        await pool.execute("UPDATE workflow_definitions SET is_active=FALSE WHERE id=$1", fleet)

    _run(go())
