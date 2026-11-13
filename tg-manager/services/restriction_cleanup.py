"""Подтверждаемая очистка по завершённой проверке ограничений ресурсов."""

from __future__ import annotations

import json
from collections import defaultdict

import asyncpg
from database import db

from services import entity_restriction_check as check

_KINDS = frozenset({"bot", "channel", "group"})
_GROUP_TYPES = ("chat", "group", "megagroup", "supergroup")
_MAX_TARGETS = 1000
_STALE = "Состояние ресурсов изменилось. Откройте предпросмотр заново."


class CleanupError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _telegram_id(value):
    if type(value) not in (int, str):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number and -(2**63) <= number < 2**63 else None


def _target_keys(targets):
    if not isinstance(targets, list) or not 1 <= len(targets) <= _MAX_TARGETS:
        raise CleanupError(f"Выберите от 1 до {_MAX_TARGETS} ресурсов")
    keys = set()
    for target in targets:
        kind = target.get("kind") if isinstance(target, dict) else None
        item_id = _telegram_id(target.get("id")) if isinstance(target, dict) else None
        if not isinstance(kind, str) or kind not in _KINDS or item_id is None:
            raise CleanupError("Неверный тип или идентификатор ресурса")
        keys.add((kind, item_id))
    if len(keys) != len(targets):
        raise CleanupError("Ресурс указан несколько раз")
    return keys


async def _report(conn, owner_id, op_id, *, lock=False):
    query = """SELECT result FROM operation_queue
                WHERE owner_id=$1 AND id=$2
                  AND op_type='check_owned_restrictions' AND status IN ('done', 'partial')"""
    if lock:
        query += " FOR UPDATE"
    row = await conn.fetchrow(query, owner_id, op_id)
    if row is None:
        raise CleanupError("Завершённая проверка ограничений не найдена", 404)
    result = row["result"]
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = None
    if not isinstance(result, dict):
        result = {}
    entries = {}
    raw_findings = result.get("findings")
    for entry in raw_findings if isinstance(raw_findings, list) else []:
        if not isinstance(entry, dict):
            continue
        kind, item_id, status = entry.get("kind"), _telegram_id(entry.get("id")), entry.get("status")
        if not isinstance(kind, str) or kind not in _KINDS or item_id is None:
            continue
        if not isinstance(status, str) or not (check.is_problem(status) or status == check.STATUS_CHECK_FAILED):
            continue
        entries[kind, item_id] = entry
    return entries, result.get("findings_version") != 2


async def _snapshot(conn, owner_id, entries, *, lock=False):
    bots = sorted(item_id for kind, item_id in entries if kind == "bot")
    channels = sorted(item_id for kind, item_id in entries if kind != "bot")
    suffix = " FOR UPDATE" if lock else ""
    bot_rows = await conn.fetch(
        """SELECT bot_id, first_name, username, is_active, xmin::text AS revision
             FROM managed_bots
            WHERE added_by=$1 AND bot_id=ANY($2::bigint[]) AND deleted_at IS NULL
            ORDER BY bot_id""" + suffix, owner_id, bots)
    ch_rows = await conn.fetch(
        """SELECT id, channel_id, acc_id, title, username, type, xmin::text AS revision
             FROM managed_channels
            WHERE owner_id=$1 AND channel_id=ANY($2::bigint[])
            ORDER BY channel_id, id""" + suffix, owner_id, channels)
    assets = defaultdict(list)
    for row in bot_rows:
        assets["bot", row["bot_id"]].append(row)
    for row in ch_rows:
        kind = "group" if (row["type"] or "").lower() in _GROUP_TYPES else "channel"
        assets[kind, row["channel_id"]].append(row)

    projects = defaultdict(dict)
    nodes = defaultdict(list)
    for row in await conn.fetch(
        """SELECT n.id AS node_id, n.node_type, n.ref_id, p.id, p.name
             FROM network_nodes n JOIN network_instances p ON p.id=n.instance_id
            WHERE p.owner_id=$1
              AND ((n.node_type='bot' AND n.ref_id=ANY($2::bigint[]))
               OR (n.node_type IN ('channel', 'group', 'chat', 'megagroup', 'supergroup') AND n.ref_id=ANY($3::bigint[])))
            ORDER BY p.id, n.id""" + (" FOR UPDATE OF n" if lock else ""),
        owner_id, bots, channels,
    ):
        kind = "group" if row["node_type"] in _GROUP_TYPES else row["node_type"]
        key = kind, row["ref_id"]
        projects[key][row["id"]] = {"id": row["id"], "name": row["name"]}
        nodes[key].append({"id": row["node_id"], "project_id": row["id"]})

    busy_bots = {row["bot_id"] for row in await conn.fetch(
        """SELECT b.bot_id
             FROM unnest($2::bigint[]) AS b(bot_id)
            WHERE EXISTS (
                    SELECT 1 FROM workflow_definitions w
                     WHERE w.owner_id=$1 AND w.is_active
                       AND (w.bot_id=b.bot_id OR b.bot_id=ANY(w.bot_ids)))
               OR EXISTS (
                    SELECT 1 FROM workflow_runs r
                     WHERE r.owner_id=$1 AND r.bot_id=b.bot_id
                       AND r.status IN ('running', 'waiting'))""", owner_id, bots)}

    items = {}
    for key, entry in entries.items():
        owned = assets[key]
        if not owned:
            continue
        kind, item_id = key
        row = owned[0]
        title = row["first_name"] if kind == "bot" else row["title"]
        active = bool(row["is_active"]) if kind == "bot" else True
        reason = ""
        if projects[key]:
            reason = "Сначала отвяжите ресурс от проектов"
        elif not active:
            reason = "Бот уже на паузе"
        elif kind == "bot" and item_id in busy_bots:
            reason = "Сначала остановите активные сценарии и прогоны бота"
        elif kind != "bot" and (len(owned) != 1 or not row["acc_id"]):
            reason = "Нет однозначной привязки к аккаунту для восстановления"
        items[key] = {
            "kind": kind, "id": str(item_id),
            "title": str(title or row["username"] or entry.get("title") or item_id),
            "status": entry["status"], "reason": str(entry.get("reason") or ""),
            "projects": list(projects[key].values()), "active": active,
            "can_disable": not reason, "disable_reason": reason,
            "revisions": [value["revision"] for value in owned],
        }
        if kind != "bot":
            items[key]["bindings"] = [
                {"row_id": value["id"], "account_id": value["acc_id"]} for value in owned
            ]
    return items, nodes


async def findings(pool, owner_id, op_id):
    async with pool.acquire() as conn:
        entries, legacy = await _report(conn, owner_id, op_id)
        items, _ = await _snapshot(conn, owner_id, entries)
    return {"findings": list(items.values()), "legacy": legacy, "max_targets": _MAX_TARGETS}


async def _plan(conn, owner_id, op_id, targets, action, project_id, *, lock=False):
    keys = _target_keys(targets)
    if action not in ("detach", "disable"):
        raise CleanupError("Неизвестное действие")
    if action == "detach" and (
        type(project_id) is not int or not 1 <= project_id < 2**31
    ):
        raise CleanupError("Выберите проект для отвязки")
    if action == "disable" and project_id is not None:
        raise CleanupError("Отключение ресурса действует на весь аккаунт")

    if lock:
        await conn.execute("SET LOCAL lock_timeout = '3s'")
        if action == "detach" or any(kind != "bot" for kind, _ in keys):
            # Захват публикации не должен совпасть с проверкой привязок.
            await conn.execute("LOCK TABLE operation_queue IN SHARE ROW EXCLUSIVE MODE")
            if action == "disable":
                await db.lock_managed_channel_catalog(conn, owner_id)
    if lock and action == "disable" and any(kind == "bot" for kind, _ in keys):
        # Старт нового прогона или активация сценария между проверкой и
        # выключением бота должны дождаться завершения этой транзакции.
        await conn.execute("LOCK TABLE workflow_definitions, workflow_runs IN SHARE MODE")

    entries, _ = await _report(conn, owner_id, op_id, lock=lock)
    if not keys <= entries.keys():
        raise CleanupError("Выбранного ресурса нет в результатах проверки", 409)
    if action == "detach":
        project_sql = "SELECT id FROM network_instances WHERE id=$1 AND owner_id=$2"
        if not await conn.fetchval(project_sql + (" FOR UPDATE" if lock else ""), project_id, owner_id):
            raise CleanupError("Проект не найден", 404)

    deployment_running = action == "detach" and await conn.fetchval(
        """SELECT 1 FROM operation_queue WHERE owner_id=$1 AND status='running'
           AND op_type='deploy_network' AND params->>'instance_id'=$2 LIMIT 1""",
        owner_id, str(project_id),
    )
    items, nodes = await _snapshot(
        conn, owner_id, {key: entries[key] for key in sorted(keys)}, lock=lock)
    channel_ids = [str(item_id) for kind, item_id in sorted(keys) if kind != "bot"]
    running_publications = await conn.fetch(
        """SELECT params->'channel_ids' AS targets
             FROM operation_queue
            WHERE owner_id=$1 AND status='running'
              AND op_type IN ('mass_publish', 'quick_post', 'bulk_post_chans')
              AND (($2::text IS NOT NULL AND params->>'project_id'=$2)
                OR ($2::text IS NULL AND cardinality($3::text[])>0))""",
        owner_id, str(project_id) if project_id else None, channel_ids)
    busy_channels = set()
    for publication in running_publications:
        selected = publication["targets"]
        if isinstance(selected, str):
            selected = json.loads(selected)
        if not isinstance(selected, list) or not selected:
            selected = channel_ids
        busy_channels.update(str(value) for value in selected)

    for key in sorted(keys):
        entry = entries[key]
        item = items.setdefault(key, {
            "kind": key[0], "id": str(key[1]),
            "title": str(entry.get("title") or key[1]), "status": entry["status"],
        })
        if deployment_running:
            reason = "Дождитесь завершения развёртывания проекта"
        elif item["id"] in busy_channels and item["kind"] != "bot":
            reason = "Дождитесь завершения текущей публикации"
        elif "active" not in item:
            reason = "Ресурс больше не доступен владельцу"
        else:
            reason = ""
        item["outcome"] = "ready"
        if reason:
            item.update(outcome="skipped", reason=reason)
            continue
        if action == "detach":
            node_ids = [node["id"] for node in nodes[key] if node["project_id"] == project_id]
            if not node_ids:
                item.update(outcome="skipped", reason="Ресурс больше не связан с выбранным проектом")
            else:
                item["node_ids"] = node_ids
        elif not item["can_disable"]:
            item.update(outcome="skipped", reason=item["disable_reason"])
    if action == "detach":
        selected = [node_id for item in items.values() for node_id in item.get("node_ids", [])]
        edges = await conn.fetch(
            """SELECT id, instance_id, source_node_id, target_node_id, edge_type
                 FROM network_edges
                WHERE source_node_id=ANY($1::int[]) OR target_node_id=ANY($1::int[])
                ORDER BY id""" + (" FOR UPDATE" if lock else ""), selected)
        if any(edge["instance_id"] != project_id for edge in edges):
            raise CleanupError("Обнаружена связь с другим проектом. Исправьте топологию перед отвязкой.", 409)
        for item in items.values():
            if item["outcome"] == "skipped":
                continue
            item["edge_ids"] = [edge["id"] for edge in edges if (
                edge["source_node_id"] in item["node_ids"] or edge["target_node_id"] in item["node_ids"]
            )]
    return {"action": action, "op_id": op_id, "project_id": project_id,
            "items": list(items.values())}


async def plan(pool, owner_id, op_id, targets, action, *, project_id=None):
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        return await _plan(conn, owner_id, op_id, targets, action, project_id)


async def apply(pool, owner_id, op_id, targets, action, *, project_id=None, verify):
    try:
        async with pool.acquire() as conn, conn.transaction(isolation="serializable"):
            preview = await _plan(conn, owner_id, op_id, targets, action, project_id, lock=True)
            verify(preview)
            ready = [item for item in preview["items"] if item["outcome"] == "ready"]
            if action == "detach" and ready:
                edge_ids = sorted({edge for item in ready for edge in item["edge_ids"]})
                node_ids = sorted({node for item in ready for node in item["node_ids"]})
                await conn.execute(
                    "DELETE FROM network_edges WHERE instance_id=$1 AND id=ANY($2::int[])",
                    project_id, edge_ids)
                await conn.execute(
                    "DELETE FROM network_nodes WHERE instance_id=$1 AND id=ANY($2::int[])",
                    project_id, node_ids)
            if action == "disable":
                for item in ready:
                    await _disable(conn, owner_id, op_id, item)
            await conn.executemany(
                """INSERT INTO operation_audit(owner_id, operation_id, action, target, result)
                   VALUES($1, $2, $3, $4, 'success')""",
                [(owner_id, op_id, f"restriction_cleanup_{action}",
                  f"{item['kind']}:{item['id']}" + (f" project:{project_id}" if project_id else ""))
                 for item in ready],
            )
    except (asyncpg.SerializationError, asyncpg.DeadlockDetectedError,
            asyncpg.LockNotAvailableError) as exc:
        raise CleanupError(_STALE, 409) from exc
    items = [{**item, "outcome": "applied" if item["outcome"] == "ready" else "skipped"}
             for item in preview["items"]]
    changed = [{key: item[key] for key in ("kind", "id", "title")}
               for item in items if item["outcome"] == "applied"]
    return {"ok": True, "action": action, "op_id": op_id, "project_id": project_id,
            "items": items, "changed": changed, "count": len(changed)}


async def _disable(conn, owner_id, op_id, item):
    item_id = int(item["id"])
    if item["kind"] == "bot":
        if not await db.set_managed_bot_active(conn, item_id, owner_id, False, cleanup_pause=True):
            raise CleanupError(_STALE, 409)
    elif not await db.remove_managed_channel_on_connection(
        conn, owner_id, item_id, reason=db.REMOVED_BY_OWNER,
        detail=f"Отключён по проверке ограничений №{op_id}",
    ):
        raise CleanupError(_STALE, 409)
