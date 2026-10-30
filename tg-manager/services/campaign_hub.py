"""Единый модуль рассылок (Кампании) — ядро-планировщик.

ЗАЧЕМ. Рассылки по каналам, чатам, ботам и в ДМ сейчас размазаны по разным типам
операций и разным экранам: пользователь прыгает между разделами, чтобы запустить
одну «кампанию по всем ресурсам». Этот модуль делает рассылку ОДНОЙ сущностью:
единый спек (контент + общие настройки + выбор ресурсов) → веер дочерних операций
по уже существующим, проверенным движкам, под общим `campaign_id`.

ЧТО ЗДЕСЬ. Чистое ядро без БД: `plan_campaign(spec)` валидирует единый спек и
раскладывает его на список дочерних операций (`PlannedOp`) для шины операций.
Никакого ввода-вывода — это контракт «что куда уйдёт», на котором строятся слой
постановки в очередь, API и UI. Движки НЕ переписываются: кампания переиспользует
`mass_publish` (каналы), `group_announce` (чаты), `network_broadcast` (боты) и
`dm_campaign` (ДМ) — со всеми их защитами (канарейка/откат/пульс/идемпотентность).

ПРИНЦИП. Один источник правды по тому, какой движок что исполняет, живёт в
op_worker.dispatch_table(); здесь — только маппинг общих настроек кампании в
параметры этих движков. Сменился параметр движка — меняем маппер, а не UI.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# Виды ресурсов, которые кампания умеет охватывать одним запуском.
KIND_CHANNELS = "channels"
KIND_CHATS = "chats"
KIND_BOTS = "bots"
KIND_DM = "dm"
ALL_KINDS = (KIND_CHANNELS, KIND_CHATS, KIND_BOTS, KIND_DM)

# Названия видов для владельца: причины отказа по ветке уезжают в тост
# мини-аппа («Не ушло: …»), а ключи вида `dm` и `channel_post` ему ничего не
# говорят — и по-английски он не читает (CLAUDE.md, первая строка).
KIND_LABEL = {
    KIND_CHANNELS: "каналы",
    KIND_CHATS: "чаты",
    KIND_BOTS: "боты",
    KIND_DM: "личные сообщения",
}

# Тип операции-исполнителя для каждого вида (ключи op_worker.dispatch_table()).
KIND_OP_TYPE = {
    KIND_CHANNELS: "mass_publish",
    KIND_CHATS: "group_announce",
    KIND_BOTS: "network_broadcast",
    KIND_DM: "dm_campaign",
}

# Сегменты рассылки по ботам — ровно те, что понимает _exec_network_broadcast.
ALLOWED_BOT_SEGMENTS = {
    "all", "all_each", "unique", "selected_bots", "cluster",
    "cold_all", "lost_all",
}

# Типы аудитории ДМ — ровно те, что принимает dm_campaign_create/_get_targets.
ALLOWED_DM_TARGETS = {
    "all_bots", "bot_users", "cohort", "crm", "contacts", "segment",
    "parsed_audience", "import_list",
}


class CampaignSpecError(ValueError):
    """Единый спек кампании невалиден. Сообщение — на русском, для показа владельцу."""


class PlannedOp(dict):
    """Одна дочерняя операция кампании: что и с какими параметрами уйдёт в шину.

    Это обычный dict со стабильными ключами — чтобы слой постановки мог просто
    развернуть его в operation_bus.submit(...):
      kind       — вид ресурса (channels/chats/bots/dm);
      op_type    — тип операции-исполнителя;
      params     — параметры для движка (без campaign_id — его проставит submit);
      total_items— оценка объёма для прогресс-бара (0 = движок посчитает сам);
      label      — человекочитаемая метка для очереди;
      dm_create  — только для ДМ: спек строки dm_campaigns, которую создаст submit.
    """


def _as_bool(v: Any) -> bool:
    return bool(v) and str(v).lower() not in ("false", "0", "", "none")


def _int_list(v: Any) -> list[int]:
    out: list[int] = []
    for x in v or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _content(spec: dict) -> dict:
    return spec.get("content") or {}


def _settings(spec: dict) -> dict:
    return spec.get("settings") or {}


def _text_of(spec: dict) -> str:
    return str(_content(spec).get("text") or "").strip()


def _common_send_params(spec: dict) -> dict:
    """Общие настройки отправки, понятные движкам (лишние ими игнорируются)."""
    s = _settings(spec)
    c = _content(spec)
    params: dict[str, Any] = {
        "text": _text_of(spec),
        "silent": _as_bool(s.get("silent")),
        "campaign": True,  # пометка «это часть кампании» — для аудита/аналитики
    }
    # Задержка между отправками (троттлинг). Разные движки читают разные ключи —
    # кладём оба, чтобы не зависеть от того, какой именно смотрит исполнитель.
    if s.get("delay_seconds") is not None or s.get("delay") is not None:
        d = int(s.get("delay_seconds") or s.get("delay") or 0)
        if d > 0:
            params["delay_seconds"] = d
            params["delay"] = d
    if c.get("buttons"):
        params["buttons"] = c.get("buttons")
    if c.get("media_file_id"):
        params["media_file_id"] = c.get("media_file_id")
        if c.get("media_type"):
            params["media_type"] = c.get("media_type")
    return params


def _plan_channels(spec: dict, tgt: dict) -> list[PlannedOp]:
    channel_ids = _int_list(tgt.get("channel_ids"))
    # Пустой список = «во все управляемые каналы» (поведение mass_publish).
    params = _common_send_params(spec)
    params["target"] = "channels"
    if channel_ids:
        params["channel_ids"] = channel_ids
    return [PlannedOp(
        kind=KIND_CHANNELS,
        op_type=KIND_OP_TYPE[KIND_CHANNELS],
        params=params,
        total_items=len(channel_ids),
        label="Кампания · каналы",
    )]


def _plan_chats(spec: dict, tgt: dict) -> list[PlannedOp]:
    # group_announce работает ОДНИМ аккаунтом за операцию (по его чатам), поэтому
    # на каждый выбранный аккаунт — своя дочерняя операция.
    acc_ids = _int_list(tgt.get("acc_ids"))
    if not acc_ids:
        raise CampaignSpecError("Для рассылки по чатам выберите хотя бы один аккаунт")
    ops: list[PlannedOp] = []
    for acc_id in acc_ids:
        params = _common_send_params(spec)
        params["acc_id"] = acc_id
        ops.append(PlannedOp(
            kind=KIND_CHATS,
            op_type=KIND_OP_TYPE[KIND_CHATS],
            params=params,
            total_items=0,
            label=f"Кампания · чаты (акк #{acc_id})",
        ))
    return ops


def _plan_bots(spec: dict, tgt: dict) -> list[PlannedOp]:
    segment = str(tgt.get("segment") or "all_each")
    if segment not in ALLOWED_BOT_SEGMENTS:
        raise CampaignSpecError(
            f"Неизвестный сегмент рассылки по ботам: {segment}")
    selected = _int_list(tgt.get("selected_bot_ids"))
    cluster = str(tgt.get("cluster_name") or "").strip()
    if segment == "selected_bots" and not selected:
        raise CampaignSpecError("Выберите ботов для рассылки по выбранным ботам")
    if segment == "cluster" and not cluster:
        raise CampaignSpecError("Укажите кластер для рассылки по кластеру ботов")
    params = _common_send_params(spec)
    params["segment"] = segment
    if selected:
        params["selected_bot_ids"] = selected
    if cluster:
        params["cluster_name"] = cluster
    return [PlannedOp(
        kind=KIND_BOTS,
        op_type=KIND_OP_TYPE[KIND_BOTS],
        params=params,
        total_items=len(selected),
        label="Кампания · боты",
    )]


def _plan_dm(spec: dict, tgt: dict) -> list[PlannedOp]:
    target_type = str(tgt.get("target_type") or "").strip()
    if target_type not in ALLOWED_DM_TARGETS:
        raise CampaignSpecError(
            "Тип аудитории ДМ должен быть одним из: "
            + ", ".join(sorted(ALLOWED_DM_TARGETS)))
    import_list = tgt.get("import_list")
    if target_type == "import_list" and not import_list:
        raise CampaignSpecError("Список получателей ДМ пуст")
    if target_type in ("cohort",) and not tgt.get("target_id"):
        raise CampaignSpecError("Для когорты ДМ выберите бота")
    # Строку dm_campaigns создаст слой постановки (нужен БД-доступ и проверки
    # владения). Здесь — только валидный спек этой строки.
    dm_create = {
        "name": str(spec.get("name") or "Кампания")[:120],
        "text": _text_of(spec),
        "target_type": target_type,
        "target_id": tgt.get("target_id"),
        "import_list": import_list,
        "cohort_type": tgt.get("cohort_type"),
    }
    return [PlannedOp(
        kind=KIND_DM,
        op_type=KIND_OP_TYPE[KIND_DM],
        params={},  # campaign_id (строки dm_campaigns) проставит submit
        total_items=0,
        label="Кампания · ДМ",
        dm_create=dm_create,
    )]


_PLANNERS = {
    KIND_CHANNELS: _plan_channels,
    KIND_CHATS: _plan_chats,
    KIND_BOTS: _plan_bots,
    KIND_DM: _plan_dm,
}


def _kind_ru(kind: str) -> str:
    """Название вида для владельца; незнакомый вид возвращаем как есть."""
    return KIND_LABEL.get(kind, kind)


def enabled_kinds(spec: dict) -> list[str]:
    """Виды ресурсов, включённые в спеке (в стабильном порядке)."""
    targets = spec.get("targets") or {}
    return [k for k in ALL_KINDS
            if _as_bool((targets.get(k) or {}).get("enabled"))]


def plan_campaign(spec: dict) -> list[PlannedOp]:
    """Разложить единый спек кампании на дочерние операции.

    Бросает CampaignSpecError (русское сообщение) на невалидном спеке. Возвращает
    список PlannedOp в порядке видов ресурсов; слой постановки разворачивает их в
    operation_bus.submit под общим campaign_id.
    """
    if not isinstance(spec, dict):
        raise CampaignSpecError("Пустой или неверный спек кампании")
    if not _text_of(spec) and not _content(spec).get("media_file_id"):
        raise CampaignSpecError("Добавьте текст или медиа для рассылки")
    kinds = enabled_kinds(spec)
    if not kinds:
        raise CampaignSpecError(
            "Выберите хотя бы один ресурс: каналы, чаты, боты или ДМ")
    targets = spec.get("targets") or {}
    ops: list[PlannedOp] = []
    for kind in kinds:
        ops.extend(_PLANNERS[kind](spec, targets.get(kind) or {}))
    return ops


# ── Слой постановки: один спек → веер операций под общим campaign_id ──────────
async def _create_dm_row(pool, owner_id: int, dm: dict) -> int:
    """Создать строку dm_campaigns для ДМ-ветки кампании и вернуть её id.

    Переиспользует тот же контур, что и обычная DM-кампания: target_type + params
    (cohort_type / import_list читает dm_engine). IDOR-защита: при таргете на бота
    сверяем владение — иначе кампания ушла бы в чужую аудиторию."""
    target_type = dm["target_type"]
    target_id = dm.get("target_id")
    if target_type in ("bot_users", "cohort") and target_id:
        owns = await pool.fetchval(
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
            int(target_id), owner_id)
        if not owns:
            raise CampaignSpecError("Бот для ДМ не найден среди ваших")
    params: dict[str, Any] = {}
    if target_type == "cohort" and dm.get("cohort_type"):
        params["cohort_type"] = str(dm["cohort_type"])
    if target_type == "import_list":
        from services.dm_engine import parse_import_list
        recipients = parse_import_list(dm.get("import_list"))
        if not recipients:
            raise CampaignSpecError("Список получателей ДМ пуст или невалиден")
        params["import_list"] = recipients
    return await pool.fetchval(
        """INSERT INTO dm_campaigns(owner_id, name, text_template, target_type,
                                    target_id, status, total_targets, params)
           VALUES($1,$2,$3,$4,$5,'draft',0,$6::jsonb) RETURNING id""",
        owner_id, dm.get("name") or "Кампания", dm["text"], target_type,
        int(target_id) if target_id else None, json.dumps(params))


async def launch_campaign(pool, owner_id: int, spec: dict) -> dict:
    """Запустить единую кампанию: создать группу и веером поставить дочерние операции.

    Возвращает {campaign_id, status, children:[{kind,op_id,op_type}], errors:[...]}.
    Отказ отдельной ветки (тариф/иммунитет/валидация ДМ) НЕ валит всю кампанию —
    остальные ресурсы запускаются, причина копится в errors (как в батч-импорте).
    Бросает CampaignSpecError только на негодном спеке (до создания группы).
    """
    ops = plan_campaign(spec)  # валидация до любых записей
    from services import operation_bus as obus

    name = str(spec.get("name") or "Кампания")[:120]
    scheduled_for = (spec.get("settings") or {}).get("scheduled_for")
    campaign_id = await pool.fetchval(
        """INSERT INTO broadcast_campaigns(owner_id, name, spec, status)
           VALUES($1,$2,$3::jsonb,'running') RETURNING id""",
        owner_id, name, json.dumps(spec))

    children: list[dict] = []
    errors: list[str] = []
    for op in ops:
        kind = op["kind"]
        try:
            params = dict(op["params"])
            params["broadcast_campaign_id"] = int(campaign_id)
            dm_row_id = None
            if kind == KIND_DM:
                dm_row_id = await _create_dm_row(pool, owner_id, op["dm_create"])
                params["campaign_id"] = int(dm_row_id)  # dm_campaign читает ЭТОТ id
            op_id = await obus.submit(
                pool, owner_id, op["op_type"], params,
                total_items=int(op.get("total_items") or 0),
                scheduled_for=scheduled_for, label=op.get("label"))
            await pool.execute(
                "INSERT INTO broadcast_campaign_ops(campaign_id, op_id, kind) "
                "VALUES($1,$2,$3)", campaign_id, op_id, kind)
            if dm_row_id is not None:
                await pool.execute(
                    "UPDATE dm_campaigns SET status='running', started_at=now() "
                    "WHERE id=$1 AND owner_id=$2", dm_row_id, owner_id)
            children.append({"kind": kind, "op_id": int(op_id), "op_type": op["op_type"]})
        except PermissionError as exc:
            # Тариф (PlanRequiredError) или предохранитель Ban Weather — не сбой.
            errors.append(f"{_kind_ru(kind)}: {str(exc) or 'требуется подписка'}")
        except CampaignSpecError as exc:
            errors.append(f"{_kind_ru(kind)}: {exc}")
        except Exception:
            # Текст встроенного исключения владельцу не показываем: он
            # по-английски, без имени поля и без того, что делать. Причина
            # известна ЗДЕСЬ — ветка не поставилась, — её и пишем, а подробности
            # уходят в журнал выше.
            log.exception("launch_campaign owner=%s kind=%s", owner_id, kind)
            errors.append(f"{_kind_ru(kind)}: не удалось поставить операцию, "
                          "причина записана в журнал сервера")

    status = "running" if children else "failed"
    if children and errors:
        status = "partial"
    await pool.execute(
        "UPDATE broadcast_campaigns SET status=$2 WHERE id=$1", campaign_id, status)
    return {"campaign_id": int(campaign_id), "status": status,
            "children": children, "errors": errors}


async def campaign_status(pool, owner_id: int, campaign_id: int) -> Optional[dict]:
    """Единый прогресс кампании: сводка по всем дочерним операциям сразу.

    None — кампания не найдена или чужая. Иначе {id, name, status, kinds:{...},
    totals:{...}} с разбивкой по ресурсам и агрегатом — чтобы один экран показывал
    всю кампанию, а не её куски по разделам."""
    camp = await pool.fetchrow(
        "SELECT id, name, status, created_at FROM broadcast_campaigns "
        "WHERE id=$1 AND owner_id=$2", campaign_id, owner_id)
    if not camp:
        return None
    rows = await pool.fetch(
        """SELECT o.kind, q.status, COALESCE(q.done_items,0) AS done,
                  COALESCE(q.total_items,0) AS total
             FROM broadcast_campaign_ops o
             JOIN operation_queue q ON q.id = o.op_id
            WHERE o.campaign_id=$1""", campaign_id)
    kinds: dict[str, dict] = {}
    agg = {"ops": 0, "done": 0, "total": 0,
           "running": 0, "finished": 0, "failed": 0}
    for r in rows:
        k = kinds.setdefault(r["kind"], {"ops": 0, "done": 0, "total": 0,
                                          "running": 0, "finished": 0, "failed": 0})
        for bucket in (k, agg):
            bucket["ops"] += 1
            bucket["done"] += int(r["done"] or 0)
            bucket["total"] += int(r["total"] or 0)
            st = r["status"]
            if st in ("pending", "running"):
                bucket["running"] += 1
            elif st in ("done", "partial"):
                bucket["finished"] += 1
            elif st in ("failed", "cancelled"):
                bucket["failed"] += 1
    created = camp["created_at"]
    return {"id": int(camp["id"]), "name": camp["name"],
            "status": camp["status"],
            "created_at": created.isoformat() if created else None,
            "kinds": kinds, "totals": agg}
