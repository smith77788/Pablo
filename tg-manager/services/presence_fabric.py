"""Ткань присутствия (presence-fabric) — декларативная самовосстанавливающаяся
контрольная плоскость для телеграм-присутствия.

Модель (три плоскости, как в K8s, но для телеграм-графа):
  • Desired State  — presence_identities + presence_targets: чего мы ХОТИМ
                     (какие логические личности и в каких чатах должны быть).
  • Observed State — реальность: жив ли физический аккаунт (tg_accounts /
                     is_account_quarantined), выполнилась ли операция вступления.
  • Reconciler     — reconcile_once(): diff(desired, observed) → актуация через
                     operation_bus.submit("bulk_join") → наблюдение → event.

Ключевой примитив: РАЗДЕЛЕНИЕ логической личности и физического аккаунта.
Личность — durable-состояние; аккаунт — расходное «тело». Тело умерло → личность
МАТЕРИАЛИЗУЕТСЯ на свежее тело (rematerialize), присутствие восстанавливается.

Актуация идёт ТОЛЬКО через шину операций (тариф-гейт, Ban-Weather, дедуп, флуд-
лимиты, захват аккаунта — всё наследуется). Прямых INSERT в operation_queue нет.
Реконсайлер — сабмиттер, а не второй исполнитель.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

# Состояния цели присутствия.
T_DESIRED = "desired"        # хотим быть, ещё не начали
T_CONVERGING = "converging"  # операция вступления в полёте
T_PRESENT = "present"        # подтверждённо на месте
T_LOST = "lost"              # были, но потеряли тело/членство (реконсайлер восстановит)


# ── Выбор «тела»: здоровый, свободный, де-коррелированный аккаунт ─────────────
async def _account_alive(pool, acc_id: int, quarantine_fn=None) -> bool:
    """Живо ли тело: аккаунт активен и не в карантине. Fail-open по карантину."""
    if not acc_id:
        return False
    r = await pool.fetchrow(
        "SELECT is_active FROM tg_accounts WHERE id=$1", int(acc_id))
    if not r or not r["is_active"]:
        return False
    qf = quarantine_fn
    if qf is None:
        from services import infra_memory
        qf = infra_memory.is_account_quarantined
    try:
        return not await qf(pool, int(acc_id))
    except Exception:
        return True  # fail-open: нет сигнала — считаем живым


async def _pick_body(pool, owner_id: int, *, exclude: set[int],
                     quarantine_fn=None) -> int | None:
    """Свежее тело для личности: активный аккаунт владельца, не в карантине и НЕ
    занятый другой личностью (де-корреляция — одно физическое тело на личность)."""
    rows = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE ORDER BY last_used NULLS FIRST, id",
        owner_id)
    for r in rows:
        aid = int(r["id"])
        if aid in exclude:
            continue
        if await _account_alive(pool, aid, quarantine_fn):
            return aid
    return None


async def _bound_bodies(pool, owner_id: int, *, except_identity: int | None = None) -> set[int]:
    """Тела, уже занятые активными личностями владельца (для де-корреляции)."""
    rows = await pool.fetch(
        "SELECT id, acc_id FROM presence_identities "
        "WHERE owner_id=$1 AND acc_id IS NOT NULL AND status='active'", owner_id)
    return {int(r["acc_id"]) for r in rows
            if except_identity is None or int(r["id"]) != int(except_identity)}


# ── Event-sourcing ────────────────────────────────────────────────────────────
async def append_event(pool, owner_id: int, identity_id: int | None, kind: str,
                       detail: dict | None = None) -> None:
    try:
        await pool.execute(
            "INSERT INTO presence_events(owner_id, identity_id, kind, detail) "
            "VALUES($1,$2,$3,$4::jsonb)",
            owner_id, identity_id, kind, json.dumps(detail or {}))
    except Exception:
        log.debug("presence_fabric.append_event failed kind=%s", kind)


async def list_events(pool, owner_id: int, *, identity_id: int | None = None,
                      limit: int = 100) -> list[dict]:
    if identity_id is not None:
        rows = await pool.fetch(
            "SELECT * FROM presence_events WHERE owner_id=$1 AND identity_id=$2 "
            "ORDER BY id DESC LIMIT $3", owner_id, identity_id, max(1, min(500, limit)))
    else:
        rows = await pool.fetch(
            "SELECT * FROM presence_events WHERE owner_id=$1 ORDER BY id DESC LIMIT $2",
            owner_id, max(1, min(500, limit)))
    return [dict(r) for r in rows]


# ── CRUD: логические личности ─────────────────────────────────────────────────
async def create_identity(pool, owner_id: int, name: str, *,
                          avatar_emoji: str = "🧑", persona_id: int | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("имя личности обязательно")
    r = await pool.fetchrow(
        """INSERT INTO presence_identities(owner_id, name, avatar_emoji, persona_id)
           VALUES($1,$2,$3,$4) RETURNING *""",
        owner_id, name, (avatar_emoji or "🧑").strip()[:8] or "🧑", persona_id)
    return dict(r)


async def get_identity(pool, identity_id: int) -> dict | None:
    r = await pool.fetchrow("SELECT * FROM presence_identities WHERE id=$1", identity_id)
    return dict(r) if r else None


async def list_identities(pool, owner_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT * FROM presence_identities WHERE owner_id=$1 ORDER BY id", owner_id)
    return [dict(r) for r in rows]


_IDENTITY_TEXT = {"name", "avatar_emoji", "status"}


async def update_identity(pool, identity_id: int, owner_id: int, **fields) -> dict | None:
    sets, args = [], []
    for k, v in fields.items():
        if v is None:
            continue
        if k in _IDENTITY_TEXT:
            args.append(str(v).strip()); sets.append(f"{k}=${len(args)}")
        elif k == "persona_id":
            args.append(int(v)); sets.append(f"persona_id=${len(args)}")
        elif k == "memory":
            args.append(json.dumps(v or {})); sets.append(f"memory=${len(args)}::jsonb")
    if not sets:
        return await get_identity(pool, identity_id)
    args.extend([identity_id, owner_id])
    r = await pool.fetchrow(
        f"UPDATE presence_identities SET {', '.join(sets)}, updated_at=now() "
        f"WHERE id=${len(args)-1} AND owner_id=${len(args)} RETURNING *", *args)
    return dict(r) if r else None


async def delete_identity(pool, identity_id: int, owner_id: int) -> bool:
    res = await pool.execute(
        "DELETE FROM presence_identities WHERE id=$1 AND owner_id=$2",
        identity_id, owner_id)
    return res.endswith("1")


# ── CRUD: желаемое членство ───────────────────────────────────────────────────
async def add_target(pool, identity_id: int, owner_id: int, chat_ref: str) -> dict:
    chat_ref = (chat_ref or "").strip()
    if not chat_ref:
        raise ValueError("нужна ссылка/@username чата")
    r = await pool.fetchrow(
        """INSERT INTO presence_targets(identity_id, owner_id, chat_ref)
           VALUES($1,$2,$3)
           ON CONFLICT (identity_id, chat_ref) DO UPDATE SET chat_ref=EXCLUDED.chat_ref
           RETURNING *""",
        identity_id, owner_id, chat_ref)
    return dict(r)


async def list_targets(pool, identity_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT * FROM presence_targets WHERE identity_id=$1 ORDER BY id", identity_id)
    return [dict(r) for r in rows]


async def delete_target(pool, target_id: int, owner_id: int) -> bool:
    res = await pool.execute(
        "DELETE FROM presence_targets WHERE id=$1 AND owner_id=$2", target_id, owner_id)
    return res.endswith("1")


# ── Материализация личности на теле ───────────────────────────────────────────
async def materialize(pool, identity: dict, *, quarantine_fn=None) -> int | None:
    """Гарантирует, что у личности есть ЖИВОЕ тело. Возвращает acc_id или None.

    Инвариант непрерывности: если текущее тело живо — не трогаем. Если тела нет
    или оно умерло — берём свежее (де-коррелированное) и переносим личность,
    помечая презентные цели на восстановление (их надо перевступить новым телом)."""
    owner_id = int(identity["owner_id"])
    iid = int(identity["id"])
    cur = identity.get("acc_id")
    if cur and await _account_alive(pool, int(cur), quarantine_fn):
        return int(cur)  # тело живо — ничего не делаем

    was_materialized = bool(cur)
    exclude = await _bound_bodies(pool, owner_id, except_identity=iid)
    body = await _pick_body(pool, owner_id, exclude=exclude, quarantine_fn=quarantine_fn)
    if body is None:
        await append_event(pool, owner_id, iid, "no_body",
                           {"reason": "нет свободного здорового аккаунта"})
        return None
    await pool.execute(
        "UPDATE presence_identities SET acc_id=$1, materialized_at=now(), updated_at=now() "
        "WHERE id=$2", body, iid)
    if was_materialized:
        # тело сменилось — членства надо восстановить на новом теле
        await pool.execute(
            "UPDATE presence_targets SET state=$1 WHERE identity_id=$2 AND state=$3",
            T_DESIRED, iid, T_PRESENT)
        await append_event(pool, owner_id, iid, "rematerialized",
                           {"old_acc_id": int(cur), "new_acc_id": body})
    else:
        await append_event(pool, owner_id, iid, "materialized", {"acc_id": body})
    return body


# ── Реконсиляция ──────────────────────────────────────────────────────────────
async def _op_status(pool, op_id: int | None) -> str | None:
    if not op_id:
        return None
    try:
        return await pool.fetchval(
            "SELECT status FROM operation_queue WHERE id=$1", int(op_id))
    except Exception:
        return None


async def _default_submit(pool, owner_id: int, chat_ref: str, acc_id: int) -> int | None:
    """Актуация через шину операций (bulk_join на конкретное тело). Реальный путь."""
    from services import operation_bus
    try:
        return await operation_bus.submit(
            pool, owner_id, "bulk_join",
            {"links": [chat_ref], "account_ids": [int(acc_id)], "delay_mode": "smart"},
            total_items=1, label=f"presence:{chat_ref}")
    except Exception as e:
        log.warning("presence_fabric: submit bulk_join failed: %s", str(e)[:160])
        return None


async def reconcile_identity(pool, identity: dict, *, submit_fn=None,
                             quarantine_fn=None) -> dict:
    """Приводит РЕАЛЬНОСТЬ одной личности к желаемому состоянию. Идемпотентно.
    Fail-open: сбой одной личности не роняет цикл."""
    owner_id = int(identity["owner_id"])
    iid = int(identity["id"])
    out = {"identity_id": iid, "body": None, "converging": 0, "present": 0,
           "submitted": 0, "no_body": False}
    if identity.get("status") != "active":
        return out

    # 1) Гарантируем живое тело (материализация/перенос при дрейфе).
    prev_acc = identity.get("acc_id")
    body = await materialize(pool, identity, quarantine_fn=quarantine_fn)
    if body is None:
        out["no_body"] = True
        return out
    out["body"] = body
    if prev_acc and int(prev_acc) != int(body):
        await append_event(pool, owner_id, iid, "drift",
                           {"detail": "тело сменилось, восстанавливаю присутствие"})

    submit = submit_fn or _default_submit

    # 2) Гоним каждую цель по машине состояний.
    targets = await list_targets(pool, iid)
    for t in targets:
        tid = int(t["id"])
        state = t["state"]
        if state == T_PRESENT:
            out["present"] += 1
            continue
        if state == T_CONVERGING:
            st = await _op_status(pool, t.get("last_op_id"))
            if st == "done":
                await pool.execute(
                    "UPDATE presence_targets SET state=$1, last_reconciled_at=now() WHERE id=$2",
                    T_PRESENT, tid)
                await append_event(pool, owner_id, iid, "target_present",
                                   {"chat_ref": t["chat_ref"]})
                out["present"] += 1
            elif st in ("failed", "cancelled"):
                # не вышло — вернём в desired, следующий цикл попробует снова
                await pool.execute(
                    "UPDATE presence_targets SET state=$1 WHERE id=$2", T_DESIRED, tid)
                await append_event(pool, owner_id, iid, "target_lost",
                                   {"chat_ref": t["chat_ref"], "op_status": st})
            else:
                out["converging"] += 1  # ещё в полёте — ждём
            continue
        # desired | lost → запускаем конвергенцию (актуация через шину)
        op_id = await submit(pool, owner_id, t["chat_ref"], body)
        if op_id:
            await pool.execute(
                "UPDATE presence_targets SET state=$1, last_op_id=$2, last_reconciled_at=now() "
                "WHERE id=$3", T_CONVERGING, int(op_id), tid)
            await append_event(pool, owner_id, iid, "target_converging",
                               {"chat_ref": t["chat_ref"], "op_id": int(op_id), "acc_id": body})
            out["submitted"] += 1
            out["converging"] += 1
    return out


async def reconcile_once(pool, *, owner_id: int | None = None, submit_fn=None,
                         quarantine_fn=None, limit: int = 200) -> dict:
    """Один проход реконсайлера по личностям (владельца или всем). Возвращает сводку."""
    if owner_id is not None:
        rows = await pool.fetch(
            "SELECT * FROM presence_identities WHERE owner_id=$1 AND status='active' "
            "ORDER BY id LIMIT $2", owner_id, max(1, min(1000, limit)))
    else:
        rows = await pool.fetch(
            "SELECT * FROM presence_identities WHERE status='active' ORDER BY id LIMIT $1",
            max(1, min(1000, limit)))
    summary = {"identities": 0, "submitted": 0, "present": 0, "no_body": 0}
    for r in rows:
        try:
            res = await reconcile_identity(pool, dict(r), submit_fn=submit_fn,
                                           quarantine_fn=quarantine_fn)
        except Exception:
            log.warning("presence_fabric: reconcile identity=%s failed", r["id"])
            continue
        summary["identities"] += 1
        summary["submitted"] += res["submitted"]
        summary["present"] += res["present"]
        summary["no_body"] += 1 if res["no_body"] else 0
    return summary


# ── Проекция состояния (read-model для UI) ────────────────────────────────────
async def project_identity(pool, identity_id: int) -> dict | None:
    ident = await get_identity(pool, identity_id)
    if not ident:
        return None
    targets = await list_targets(pool, identity_id)
    breakdown = {T_DESIRED: 0, T_CONVERGING: 0, T_PRESENT: 0, T_LOST: 0}
    for t in targets:
        breakdown[t["state"]] = breakdown.get(t["state"], 0) + 1
    body_alive = await _account_alive(pool, ident.get("acc_id"))
    return {
        "identity": ident,
        "targets": targets,
        "breakdown": breakdown,
        "body_alive": body_alive,
        "events": await list_events(pool, int(ident["owner_id"]),
                                    identity_id=identity_id, limit=50),
    }
