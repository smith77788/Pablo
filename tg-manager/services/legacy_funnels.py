"""Воронки и автоворонки исполняет движок сценариев.

ЧТО БЫЛО. Три исполнителя цепочек сообщений жили каждый сам по себе:
воронки (`funnels`, services/funnel_runner.py), автоворонки (`auto_funnels`,
services/auto_funnel.py) и сценарии (`workflow_definitions`,
services/workflow_runtime.py). У двух старых не было цели с автовыходом,
журнала шагов, выручки, источника, «стопа» по прогону, и каждый чинился
отдельно.

КАК ТЕПЕРЬ. Старые таблицы остаются тем, что редактирует владелец: экраны
воронок и все места, где на воронку подписывают человека, не меняются.
Этот модуль — мост:
  * `sync_definitions` держит для каждой воронки сценарий-зеркало
    (`legacy_kind`/`legacy_id`) с шагами «задержка → сообщение»;
  * `adopt` передаёт каждую живую подписку прогону движка на тот же шаг и на
    то же время отправки (`workflow_run_id` в строке подписки);
  * `sync_back` пишет итог прогона обратно в подписку (пройдена / выбыла),
    чтобы старые экраны и счётчики показывали правду, и останавливает
    прогон, если подписку остановили в старом разделе;
  * `release` — откат: живые прогоны возвращаются в подписки на тот же шаг,
    и старый исполнитель продолжает с того же места.

Включено по умолчанию; FUNNELS_ENGINE=legacy возвращает старых
исполнителей (при следующем круге живые прогоны отдаются обратно).

Мост вызывают сами старые исполнители в начале своего круга
(`funnel_runner.run_once`, `auto_funnel._process_due_runs`): один цикл на
вид воронки, поэтому мост и старый путь никогда не идут одновременно.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

KINDS = ("funnel", "auto_funnel")
_ADOPT_BATCH = 500
_ADOPT_ROUNDS = 10
_LIVE = ("running", "waiting")
_FINAL = ("completed", "stopped", "failed", "cancelled")
_NAME_PREFIX = {"funnel": "Воронка", "auto_funnel": "Автоворонка"}
_DESCRIPTION = ("Создан из раздела «{section}». Шаги и включение меняются "
                "там; здесь — цель, статистика и журнал по каждому человеку.")
_SECTION = {"funnel": "Воронки", "auto_funnel": "Автоворонки"}
MAX_BUTTON_TEXT = 64


def engine_on() -> bool:
    return os.getenv("FUNNELS_ENGINE", "workflow").strip().lower() != "legacy"


# ── Сборка шагов (ЧИСТЫЕ) ───────────────────────────────────────────────────

def compile_funnel(steps) -> list[dict]:
    """Шаги воронки → шаги сценария. Задержка шага воронки отсчитывается от
    предыдущего сообщения (у первого — от подписки), поэтому каждый шаг —
    «задержка, затем сообщение». Нулевая задержка шага не даёт."""
    out: list[dict] = []
    for s in sorted(steps, key=lambda r: int(r["step_order"])):
        minutes = int(s["delay_minutes"] or 0)
        if minutes > 0:
            out.append({"type": "delay", "delay_minutes": minutes})
        out.append({"type": "message", "text": s["message_text"] or "",
                    "legacy_step": int(s["step_order"])})
    return out


def compile_auto_funnel(steps) -> list[dict]:
    """Шаги автоворонки → шаги сценария. Задержка в часах — от предыдущего
    сообщения (так её и исполнял services/auto_funnel.py); кнопка-ссылка
    переезжает кнопкой-ссылкой."""
    out: list[dict] = []
    for s in sorted(steps, key=lambda r: int(r["step_num"])):
        hours = int(s["delay_hours"] or 0)
        if hours > 0:
            out.append({"type": "delay", "delay_minutes": hours * 60})
        msg: dict = {"type": "message", "text": s["message_text"] or "",
                     "legacy_step": int(s["step_num"])}
        if s.get("button_text") and s.get("button_url"):
            msg["buttons"] = [{"text": str(s["button_text"])[:MAX_BUTTON_TEXT],
                               "url": s["button_url"]}]
        out.append(msg)
    return out


def message_index(steps: list[dict], legacy_step: int) -> int | None:
    """Индекс сообщения, с которого продолжать: первый шаг воронки с номером
    не меньше `legacy_step`. Задержку перед ним не повторяем — время его
    отправки уже записано в подписке. None — шагов дальше нет."""
    for i, st in enumerate(steps):
        if st.get("type") == "message" and int(st.get("legacy_step", -1)) >= legacy_step:
            return i
    return None


def legacy_position(steps: list[dict], idx: int,
                    next_run_at: datetime | None) -> tuple[int | None, datetime]:
    """Обратная сборка для отката: (номер шага воронки, когда его отправить).
    Если прогон стоит на задержке перед сообщением, время отправки — конец
    этой задержки."""
    now = datetime.now(timezone.utc)
    when = next_run_at or now
    for i in range(max(idx, 0), len(steps)):
        st = steps[i]
        if st.get("type") == "delay":
            when = when + timedelta(minutes=int(st.get("delay_minutes") or 0))
            continue
        if st.get("type") == "message" and "legacy_step" in st:
            return int(st["legacy_step"]), max(when, now)
    return None, now


def _as_list(raw) -> list:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    return raw if isinstance(raw, list) else []


# ── Зеркала определений ─────────────────────────────────────────────────────

async def _legacy_rows(pool, kind: str) -> list[dict]:
    if kind == "funnel":
        heads = await pool.fetch(
            """SELECT f.id, f.name, f.bot_id, mb.added_by AS owner_id,
                      (f.is_active AND COALESCE(mb.is_active, FALSE)) AS active
                 FROM funnels f
                 JOIN managed_bots mb ON mb.bot_id = f.bot_id
                WHERE mb.added_by IS NOT NULL""")
        steps = await pool.fetch(
            "SELECT funnel_id, step_order, message_text, delay_minutes FROM funnel_steps")
        compile_ = compile_funnel
    else:
        heads = await pool.fetch(
            """SELECT f.id, f.name, f.bot_id, f.owner_id,
                      (f.enabled AND COALESCE(mb.is_active, FALSE)) AS active
                 FROM auto_funnels f
                 LEFT JOIN managed_bots mb ON mb.bot_id = f.bot_id""")
        steps = await pool.fetch(
            """SELECT funnel_id, step_num, message_text, delay_hours,
                      button_text, button_url FROM auto_funnel_steps""")
        compile_ = compile_auto_funnel
    by_funnel: dict[int, list] = {}
    for s in steps:
        by_funnel.setdefault(int(s["funnel_id"]), []).append(dict(s))
    return [{**dict(h), "steps": compile_(by_funnel.get(int(h["id"]), []))}
            for h in heads]


async def sync_definitions(pool, kind: str) -> dict[int, dict]:
    """Привести зеркала к старым таблицам. {id воронки: {id, steps}}."""
    rows = await _legacy_rows(pool, kind)
    have = {int(r["legacy_id"]): dict(r) for r in await pool.fetch(
        """SELECT id, legacy_id, owner_id, name, bot_id, steps, is_active
             FROM workflow_definitions WHERE legacy_kind=$1""", kind)}
    out: dict[int, dict] = {}
    for r in rows:
        fid = int(r["id"])
        name = f"{_NAME_PREFIX[kind]}: {r['name']}"[:200]
        cur = have.get(fid)
        same = cur is not None and (
            _as_list(cur["steps"]) == r["steps"]
            and bool(cur["is_active"]) == bool(r["active"])
            and cur["name"] == name and cur["bot_id"] == r["bot_id"]
            and cur["owner_id"] == r["owner_id"])
        if same:
            out[fid] = {"id": cur["id"], "steps": r["steps"]}
            continue
        wid = await pool.fetchval(
            """INSERT INTO workflow_definitions
                   (owner_id, name, description, steps, is_active, bot_id,
                    legacy_kind, legacy_id, allow_reentry)
               VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, TRUE)
               ON CONFLICT (legacy_kind, legacy_id) WHERE legacy_kind IS NOT NULL
               DO UPDATE SET owner_id=EXCLUDED.owner_id, name=EXCLUDED.name,
                             steps=EXCLUDED.steps, is_active=EXCLUDED.is_active,
                             bot_id=EXCLUDED.bot_id, updated_at=now()
               RETURNING id""",
            r["owner_id"], name, _DESCRIPTION.format(section=_SECTION[kind]),
            json.dumps(r["steps"]), bool(r["active"]), r["bot_id"], kind, fid)
        out[fid] = {"id": wid, "steps": r["steps"]}
    gone = [cur["id"] for fid, cur in have.items() if fid not in out]
    if gone:
        await pool.execute(
            """UPDATE workflow_runs
                  SET status='cancelled', finished_at=now(), wait_kind=NULL,
                      error_message='Воронка удалена'
                WHERE workflow_id = ANY($1::int[]) AND status IN ('running','waiting')""",
            gone)
        await pool.execute(
            "DELETE FROM workflow_definitions WHERE id = ANY($1::int[]) "
            "AND legacy_kind IS NOT NULL", gone)
    return out


# ── Передача подписок движку ────────────────────────────────────────────────

async def _start_run(conn, wf: dict, owner_id: int, bot_id: int, chat_id: int,
                     idx: int, when, started_at, legacy: dict) -> int | None:
    run_id = await conn.fetchval(
        """INSERT INTO workflow_runs
               (owner_id, workflow_id, status, input_data, current_step,
                total_steps, started_at, bot_id, chat_id, steps_snapshot,
                vars, next_run_at, source)
           VALUES ($1, $2, 'running', '{}'::jsonb, $3, $4, $5, $6, $7,
                   $8::jsonb, $9::jsonb, $10,
                   (SELECT fields->>'source' FROM bot_users
                     WHERE bot_id=$6 AND user_id=$7))
           ON CONFLICT (workflow_id, bot_id, chat_id)
              WHERE status IN ('running', 'waiting') AND chat_id IS NOT NULL
           DO NOTHING
           RETURNING id""",
        owner_id, wf["id"], idx, len(wf["steps"]), started_at or datetime.now(timezone.utc),
        bot_id, chat_id, json.dumps(wf["steps"]),
        json.dumps({"_legacy": legacy}), when)
    if run_id:
        return run_id
    # Живой прогон этого человека по этому зеркалу уже есть — к нему и
    # привязываем: второй параллельной цепочки быть не должно.
    return await conn.fetchval(
        """SELECT id FROM workflow_runs
            WHERE workflow_id=$1 AND bot_id=$2 AND chat_id=$3
              AND status IN ('running','waiting') LIMIT 1""",
        wf["id"], bot_id, chat_id)


async def adopt(pool, kind: str, defs: dict[int, dict]) -> int:
    """Передать движку живые подписки, которых у него ещё нет. Число переданных."""
    moved = 0
    for _ in range(_ADOPT_ROUNDS):
        if kind == "funnel":
            rows = await pool.fetch(
                """SELECT fs.id, fs.funnel_id, fs.user_id, fs.current_step AS step,
                          fs.next_send_at, fs.created_at AS started_at,
                          f.bot_id, mb.added_by AS owner_id
                     FROM funnel_subscriptions fs
                     JOIN funnels f ON f.id = fs.funnel_id
                     JOIN managed_bots mb ON mb.bot_id = f.bot_id
                    WHERE fs.completed = FALSE AND COALESCE(fs.dropped, FALSE) = FALSE
                      AND fs.workflow_run_id IS NULL
                      AND fs.funnel_id = ANY($1::bigint[])
                    ORDER BY fs.id LIMIT $2""",
                list(defs), _ADOPT_BATCH)
        else:
            rows = await pool.fetch(
                """SELECT r.id, r.funnel_id, r.user_id,
                          GREATEST(r.next_step_num, COALESCE(r.last_sent_step, 0) + 1) AS step,
                          r.next_send_at, r.started_at, r.bot_id, f.owner_id
                     FROM auto_funnel_runs r
                     JOIN auto_funnels f ON f.id = r.funnel_id
                    WHERE r.status = 'active' AND r.workflow_run_id IS NULL
                      AND r.funnel_id = ANY($1::bigint[])
                    ORDER BY r.id LIMIT $2""",
                list(defs), _ADOPT_BATCH)
        for row in rows:
            wf = defs.get(int(row["funnel_id"]))
            if not wf:
                continue
            idx = message_index(wf["steps"], int(row["step"]))
            async with pool.acquire() as conn:
                async with conn.transaction():
                    if idx is None:
                        # Шагов дальше нет — подписка уже пройдена.
                        await _mark_done(conn, kind, row["id"], row["funnel_id"])
                        continue
                    run_id = await _start_run(
                        conn, wf, row["owner_id"], row["bot_id"], row["user_id"],
                        idx, row["next_send_at"], row["started_at"],
                        {"kind": kind, "id": int(row["id"])})
                    if not run_id:
                        continue
                    table = "funnel_subscriptions" if kind == "funnel" else "auto_funnel_runs"
                    await conn.execute(
                        f"UPDATE {table} SET workflow_run_id=$2 "
                        "WHERE id=$1 AND workflow_run_id IS NULL", row["id"], run_id)
                    moved += 1
        if len(rows) < _ADOPT_BATCH:
            break
    if moved:
        from services import workflow_runtime
        workflow_runtime.wake()
    return moved


async def _mark_done(conn, kind: str, row_id: int, funnel_id: int) -> None:
    if kind == "funnel":
        res = await conn.execute(
            """UPDATE funnel_subscriptions SET completed=TRUE, completed_at=now()
                WHERE id=$1 AND completed=FALSE""", row_id)
        if res.endswith(" 1"):
            await conn.execute(
                "UPDATE funnels SET completed_count = completed_count + 1 WHERE id=$1",
                funnel_id)
    else:
        await conn.execute(
            "UPDATE auto_funnel_runs SET status='completed' WHERE id=$1 AND status='active'",
            row_id)


# ── Итог прогона — обратно в подписку ───────────────────────────────────────

async def sync_back(pool, kind: str) -> int:
    """Закончившиеся прогоны → итог в подписке; остановленные в старом
    разделе подписки → отмена прогона. Число обновлённых подписок."""
    if kind == "funnel":
        rows = await pool.fetch(
            """UPDATE funnel_subscriptions fs
                  SET completed = (r.status = 'completed'),
                      dropped = (r.status <> 'completed'),
                      completed_at = COALESCE(r.finished_at, now())
                 FROM workflow_runs r
                WHERE fs.workflow_run_id = r.id
                  AND fs.completed = FALSE AND COALESCE(fs.dropped, FALSE) = FALSE
                  AND r.status = ANY($1::text[])
            RETURNING fs.id, fs.funnel_id, fs.user_id, r.status, r.bot_id""",
            list(_FINAL))
        for r in rows:
            col = "completed_count" if r["status"] == "completed" else "dropped_count"
            await pool.execute(
                f"UPDATE funnels SET {col} = {col} + 1 WHERE id=$1", r["funnel_id"])
            if r["status"] == "completed" and r["bot_id"]:
                from services.funnel_runner import _record_funnel_conversion
                await _record_funnel_conversion(
                    pool, r["bot_id"], r["user_id"], r["funnel_id"], r["id"])
        await pool.execute(
            """UPDATE workflow_runs r
                  SET status='cancelled', finished_at=now(), wait_kind=NULL,
                      error_message='Остановлено в разделе «Воронки»'
                 FROM funnel_subscriptions fs
                WHERE fs.workflow_run_id = r.id
                  AND (fs.completed OR COALESCE(fs.dropped, FALSE))
                  AND r.status IN ('running','waiting')""")
        return len(rows)
    rows = await pool.fetch(
        """UPDATE auto_funnel_runs ar
              SET status = CASE r.status WHEN 'completed' THEN 'completed'
                                         WHEN 'failed' THEN 'error'
                                         ELSE 'stopped' END
             FROM workflow_runs r
            WHERE ar.workflow_run_id = r.id AND ar.status = 'active'
              AND r.status = ANY($1::text[])
        RETURNING ar.id""", list(_FINAL))
    await pool.execute(
        """UPDATE workflow_runs r
              SET status='cancelled', finished_at=now(), wait_kind=NULL,
                  error_message='Остановлено в разделе «Автоворонки»'
             FROM auto_funnel_runs ar
            WHERE ar.workflow_run_id = r.id AND ar.status <> 'active'
              AND r.status IN ('running','waiting')""")
    return len(rows)


# ── Откат ───────────────────────────────────────────────────────────────────

async def release(pool, kind: str) -> int:
    """Вернуть живые прогоны старому исполнителю на тот же шаг и время."""
    table = "funnel_subscriptions" if kind == "funnel" else "auto_funnel_runs"
    rows = await pool.fetch(
        f"""SELECT t.id AS row_id, t.funnel_id, r.id AS run_id, r.current_step,
                   r.next_run_at, r.steps_snapshot, r.status
              FROM {table} t JOIN workflow_runs r ON r.id = t.workflow_run_id""")
    back = 0
    for row in rows:
        async with pool.acquire() as conn:
            async with conn.transaction():
                if row["status"] in _LIVE:
                    step, when = legacy_position(
                        _as_list(row["steps_snapshot"]), int(row["current_step"] or 0),
                        row["next_run_at"])
                    await conn.execute(
                        """UPDATE workflow_runs
                              SET status='cancelled', finished_at=now(), wait_kind=NULL,
                                  error_message='Возвращено старому исполнителю воронок'
                            WHERE id=$1 AND status IN ('running','waiting')""",
                        row["run_id"])
                    if step is None:
                        await _mark_done(conn, kind, row["row_id"], row["funnel_id"])
                    elif kind == "funnel":
                        await conn.execute(
                            """UPDATE funnel_subscriptions
                                  SET current_step=$2, next_send_at=$3
                                WHERE id=$1""", row["row_id"], step, when)
                    else:
                        await conn.execute(
                            """UPDATE auto_funnel_runs
                                  SET next_step_num=$2, next_send_at=$3,
                                      last_sent_step=LEAST(COALESCE(last_sent_step, 0), $2 - 1)
                                WHERE id=$1""", row["row_id"], step, when)
                await conn.execute(
                    f"UPDATE {table} SET workflow_run_id=NULL WHERE id=$1", row["row_id"])
                back += 1
    if back or rows:
        await pool.execute(
            "UPDATE workflow_definitions SET is_active=FALSE WHERE legacy_kind=$1", kind)
    return back


# ── Один круг моста ─────────────────────────────────────────────────────────

async def tick(pool, kind: str) -> bool:
    """True — воронки этого вида исполняет движок, старому исполнителю делать
    нечего. False — движок выключен, старый исполнитель работает как раньше."""
    if not engine_on():
        await sync_back(pool, kind)
        await release(pool, kind)
        return False
    # Итог — первым: подписка, пройденная до этого круга, не должна быть
    # принята за новую.
    await sync_back(pool, kind)
    defs = await sync_definitions(pool, kind)
    await adopt(pool, kind, defs)
    return True


def is_legacy(row) -> bool:
    return bool(row and row.get("legacy_kind"))


def guard_message(kind: str | None) -> str:
    return (f"Этот сценарий собран из раздела «{_SECTION.get(kind or '', 'Воронки')}»: "
            "шаги, включение и удаление меняются там. Здесь можно задать цель "
            "и смотреть статистику.")
