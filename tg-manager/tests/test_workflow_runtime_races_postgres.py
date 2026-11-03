# ruff: noqa: F811  — фикстуры pool/tg импортированы из соседнего модуля
"""Исполнитель сценариев не путает состояния прогона на гонках — живая база.

Каждый тест ниже падал до исправления в `services/workflow_runtime.py`:

* неудачная отправка воскрешала прогон, который за время отправки отменил
  «стоп» человека: UPDATE в `_send_failed` не проверял статус и ставил
  'running' поверх 'cancelled', и сценарий продолжал писать отписавшемуся;
* `_finish` писал журнал и событие `workflow.completed` безусловно, то есть и
  для прогона, который уже закрыла цель, — вторая строка журнала и второе
  событие, а на `workflow.completed` запускаются другие сценарии;
* сметатель тайм-аутов закрывал «не ответил в срок» прогон, который человек
  уже успел пройти между чтением и закрытием;
* предел неудач — «подряд», но счётчик никогда не сбрасывался, и сценарий,
  у которого каждое сообщение в итоге дошло, падал на пятом сбое за жизнь;
* счёт без платёжного провайдера закрывал прогон, а журнал писал «отправлено».

Фикстуры и помощники — из соседнего `test_workflow_runtime_postgres.py`.
"""
from __future__ import annotations

from tests.test_workflow_runtime_postgres import (  # noqa: F401  (фикстуры)
    BOT_ID, OWNER, USER, _emit_old, _run, _run_row, _tick, _workflow, pool, tg,
)


async def _manual_run(pool, steps):
    from services import workflow_runtime as rt
    wf_id = await _workflow(pool, steps, trigger="manual")
    wf = dict(await pool.fetchrow("SELECT * FROM workflow_definitions WHERE id=$1",
                                  wf_id))
    await rt.start_run(pool, wf, BOT_ID, USER, force=True)
    return wf_id


def test_failed_send_does_not_revive_a_run_stopped_meanwhile(pool, tg, monkeypatch):
    from services import bot_api
    from services import workflow_runtime as rt

    async def _send_while_user_stops(http, token, chat_id, text, buttons=None,
                                     reply_markup=None, disable_notification=False):
        # Пока запрос в Telegram висел, человек написал «стоп».
        await rt.cancel_for_contact(pool, BOT_ID, USER)
        return False, None, "network"

    monkeypatch.setattr(bot_api, "send_message_classified", _send_while_user_stops)

    async def go():
        wf_id = await _manual_run(pool, [{"type": "message", "text": "Раз"},
                                         {"type": "message", "text": "Два"}])
        await _tick(pool)
        run = await _run_row(pool, wf_id)
        assert run["status"] == "cancelled", (
            "неудачная отправка вернула отменённый прогон в работу: "
            f"{run['status']}")

    _run(go())


def test_finishing_an_already_closed_run_emits_nothing(pool, tg):
    from services import workflow_runtime as rt

    async def go():
        wf_id = await _manual_run(pool, [{"type": "delay", "delay_minutes": 60}])
        run = dict(await _run_row(pool, wf_id))
        assert await rt._finish(pool, run, "completed") is True
        # Второй закрывающий (цель, соседний исполнитель) опоздал.
        assert await rt._finish(pool, run, "completed") is False
        n_events = await pool.fetchval(
            "SELECT COUNT(*) FROM automation_events WHERE owner_id=$1 "
            "AND event_type='workflow.completed' AND (data->>'run_id')::bigint=$2",
            OWNER, run["id"])
        assert n_events == 1, f"событий завершения: {n_events}"
        n_log = await pool.fetchval(
            "SELECT COUNT(*) FROM workflow_step_log WHERE run_id=$1 "
            "AND outcome='completed'", run["id"])
        assert n_log == 1, f"строк журнала о завершении: {n_log}"

    _run(go())


def test_timeout_sweep_does_not_expire_a_run_answered_meanwhile(pool, tg, monkeypatch):
    from services import workflow_runtime as rt

    async def go():
        wf_id = await _manual_run(pool, [
            {"type": "ask", "text": "Имя?", "save_as": "name"},
            {"type": "delay", "delay_minutes": 60},
        ])
        await _tick(pool)
        await pool.execute("UPDATE workflow_runs SET wait_until=now()-interval '1 minute' "
                           "WHERE workflow_id=$1", wf_id)

        real_fetch = pool.fetch
        answered = {"done": False}

        class _Pool:
            """Пул, у которого между чтением просроченных и закрытием человек
            успевает ответить."""

            def __getattr__(self, name):
                return getattr(pool, name)

            async def fetch(self, query, *args):
                rows = await real_fetch(query, *args)
                if "wait_until < now()" in query and not answered["done"]:
                    answered["done"] = True
                    r = await rt.on_reply(pool, BOT_ID, USER, "Аня")
                    assert r == {"consumed": True, "reply": None}
                return rows

        n = await rt.sweep_timeouts(_Pool())
        run = await _run_row(pool, wf_id)
        assert run["status"] == "running", (
            f"ответивший человек получил «не ответил в срок»: {run['status']}")
        assert n == 0

    _run(go())


def test_failure_limit_counts_consecutive_failures_only(pool, tg, monkeypatch):
    from services import bot_api
    from services import workflow_runtime as rt

    attempts: dict[str, int] = {}

    async def _flaky(http, token, chat_id, text, buttons=None, reply_markup=None,
                     disable_notification=False):
        attempts[text] = attempts.get(text, 0) + 1
        if attempts[text] == 1:
            return False, None, "network"
        tg.append({"method": "sendMessage", "chat_id": chat_id, "text": text})
        return True, None, ""

    monkeypatch.setattr(bot_api, "send_message_classified", _flaky)

    async def go():
        texts = [f"Сообщение {i}" for i in range(1, rt._FAIL_LIMIT + 2)]
        wf_id = await _manual_run(pool, [{"type": "message", "text": t} for t in texts])
        for _ in range(len(texts) * 2 + 2):
            await pool.execute("UPDATE workflow_runs SET next_run_at=now() "
                               "WHERE workflow_id=$1 AND status='running'", wf_id)
            await _tick(pool)
        run = await _run_row(pool, wf_id)
        assert run["status"] == "completed", (
            f"каждое сообщение дошло со второй попытки, а прогон: {run['status']} "
            f"({run['error_message']})")
        assert [m["text"] for m in tg if m["method"] == "sendMessage"] == texts

    _run(go())


def test_invoice_without_provider_is_not_logged_as_sent(pool, tg):
    async def go():
        wf_id = await _manual_run(pool, [
            {"type": "invoice", "title": "Курс", "amount": 1000, "currency": "RUB"},
        ])
        await _tick(pool)
        run = await _run_row(pool, wf_id)
        assert run["status"] == "failed"
        outcomes = [r["outcome"] for r in await pool.fetch(
            "SELECT outcome FROM workflow_step_log WHERE run_id=$1 ORDER BY id",
            run["id"])]
        assert "sent" not in outcomes, outcomes
        assert not any(m["method"] == "sendInvoice" for m in tg)

    _run(go())
