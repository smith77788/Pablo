"""Модуль «Инфраструктура»: пулы аккаунтов и журнал аудита.

ЗАЧЕМ ОТДЕЛЬНО. Экран «Пулы и аудит» показывал десять последних строк
журнала с английскими словами («warmup», «day 8»), без причины сбоя и без
перехода к операции, а пустой список пулов отправлял владельца «назначить
пул на карточке аккаунта» — настройки, которой в мини-аппе не было нигде.
Назначить пул умел только бот. Здесь собрано всё, что нужно экрану, чтобы
пулами можно было ПОЛЬЗОВАТЬСЯ: сводка по каждому пулу, создание и
назначение, переименование, роспуск, автораспределение по здоровью и полный
журнал с фильтрами и русскими подписями.

Скоуп владельца держит каждый запрос (`owner_id=$1`), а не обработчик:
id аккаунтов приходят с экрана, то есть со стороны клиента.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from services import account_status as _acc_status

# ── Пулы ────────────────────────────────────────────────────────────────────

POOL_NAME_MAX = 32

# Пулы, которые раскладывает автораспределение (и бот, и мини-апп — одни и
# те же имена, иначе бот и экран разложили бы флот по разным спискам).
# В базе лежит ключ, на экране — русское название.
SYSTEM_POOLS: dict[str, str] = {
    "primary": "Основной",
    "monitoring": "Под наблюдением",
    "cooldown": "На остывании",
}
SYSTEM_POOL_HINTS: dict[str, str] = {
    "primary": "траст от 75%, без предупреждений — берите в рискованные операции",
    "monitoring": "траст ниже 30% или есть предупреждения — только лёгкая работа",
    "cooldown": "аккаунт на паузе после флуда — не трогать до конца паузы",
}

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_SPACES = re.compile(r"\s+")


class PoolNameError(ValueError):
    """Неверное название пула. Текст — для владельца, по-русски."""


def normalize_pool_name(raw: Any) -> str:
    """Название пула для записи в базу. Бросает PoolNameError с причиной.

    Русские названия системных пулов принимаются и превращаются в ключ:
    владелец видит «Основной» и, создавая пул, напишет именно так — без
    этого рядом появился бы второй пул с тем же смыслом.
    """
    s = _SPACES.sub(" ", _CTRL.sub("", str(raw or ""))).strip()
    if not s:
        raise PoolNameError("Введите название пула")
    if len(s) > POOL_NAME_MAX:
        raise PoolNameError(f"Название пула — не длиннее {POOL_NAME_MAX} символов")
    if s in ("—", "-", "(нет)"):
        raise PoolNameError("Такое название зарезервировано — выберите другое")
    low = s.lower()
    for key, ru in SYSTEM_POOLS.items():
        if low == ru.lower():
            return key
    return s


def pool_label(name: Optional[str]) -> str:
    """Подпись пула для экрана."""
    if not name:
        return "Без пула"
    return SYSTEM_POOLS.get(name, name)


def classify_account(acc: dict) -> Optional[str]:
    """Куда автораспределение кладёт аккаунт. None — не трогать.

    Правило то же, что было у бота (перенесено сюда, чтобы бот и мини-апп
    распределяли одинаково): пауза после флуда → «На остывании»; траст от
    0.75 без предупреждений → «Основной»; траст ниже 0.3 или предупреждения →
    «Под наблюдением»; середина остаётся, где была.
    """
    trust = acc.get("trust_score")
    trust = 0.5 if trust is None else float(trust)
    warnings = acc.get("warnings") or []
    if acc.get("on_cooldown"):
        return "cooldown"
    if trust >= 0.75 and not warnings:
        return "primary"
    if trust < 0.3 or len(warnings) > 0:
        return "monitoring"
    return None


def _cnt(res: Any) -> int:
    try:
        return int(str(res).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


async def pools_overview(pool, owner_id: int) -> dict:
    """Сводка по пулам: сколько аккаунтов, сколько живых, траст, пауза.

    «Живой» считается тем же списком мёртвых статусов, что и весь продукт
    (`account_status.sql_dead_list`), иначе экран пула расходился бы с
    флотом. Отключённые (is_active=FALSE) тоже показываются: раньше они
    выпадали из сводки, и пул «из 40 аккаунтов» на деле был из 55.
    """
    dead = _acc_status.sql_dead_list()
    rows = await pool.fetch(
        f"""SELECT NULLIF(pool, '') AS pool,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE is_active
                        AND COALESCE(acc_status, 'active') NOT IN ({dead})) AS alive,
                   COUNT(*) FILTER (WHERE COALESCE(acc_status, 'active') IN ({dead})) AS dead,
                   COUNT(*) FILTER (WHERE cooldown_until IS NOT NULL
                        AND cooldown_until > now()) AS cooldown,
                   AVG(trust_score) AS avg_trust
              FROM tg_accounts
             WHERE owner_id=$1
             GROUP BY NULLIF(pool, '')""",
        owner_id,
    )
    pools: list[dict] = []
    unpooled = {"total": 0, "alive": 0}
    for r in rows or []:
        item = {
            "pool": r["pool"],
            "label": pool_label(r["pool"]),
            "system": r["pool"] in SYSTEM_POOLS,
            "hint": SYSTEM_POOL_HINTS.get(r["pool"] or "", ""),
            "total": int(r["total"] or 0),
            "alive": int(r["alive"] or 0),
            "dead": int(r["dead"] or 0),
            "cooldown": int(r["cooldown"] or 0),
            "avg_trust": (round(float(r["avg_trust"]) * 100)
                          if r["avg_trust"] is not None else None),
        }
        if r["pool"] is None:
            unpooled = {"total": item["total"], "alive": item["alive"]}
        else:
            pools.append(item)
    pools.sort(key=lambda p: (-p["total"], p["label"].lower()))
    return {"pools": pools, "unpooled": unpooled,
            "system": [{"pool": k, "label": v, "hint": SYSTEM_POOL_HINTS[k]}
                       for k, v in SYSTEM_POOLS.items()]}


async def assign_pool(pool, owner_id: int, pool_name: Optional[str], *,
                      account_ids: Optional[Iterable[int]] = None,
                      only_unpooled: bool = False) -> int:
    """Назначить пул (None — снять). Возвращает число изменённых аккаунтов.

    account_ids=None — все аккаунты владельца (с only_unpooled — только без
    пула: «собрать новых в пул»). Пустой список — НИКОГО: «выбранных нет» не
    должно превращаться во «весь флот».
    """
    args: list[Any] = [owner_id, pool_name]
    where = ["owner_id=$1", "pool IS DISTINCT FROM $2"]
    if account_ids is not None:
        ids = [int(i) for i in account_ids]
        if not ids:
            return 0
        args.append(ids)
        where.append(f"id = ANY(${len(args)}::bigint[])")
    if only_unpooled:
        where.append("(pool IS NULL OR pool = '')")
    res = await pool.execute(
        f"UPDATE tg_accounts SET pool=$2 WHERE {' AND '.join(where)}", *args)
    return _cnt(res)


async def rename_pool(pool, owner_id: int, old: str, new: str) -> int:
    """Переименовать пул. Если пул с новым именем уже есть — пулы сливаются
    (так и задумано: это единственный способ объединить два пула)."""
    if old == new:
        return 0
    res = await pool.execute(
        "UPDATE tg_accounts SET pool=$3 WHERE owner_id=$1 AND pool=$2",
        owner_id, old, new)
    return _cnt(res)


async def dissolve_pool(pool, owner_id: int, name: str) -> int:
    """Распустить пул: аккаунты остаются, у них просто снимается пул."""
    res = await pool.execute(
        "UPDATE tg_accounts SET pool=NULL WHERE owner_id=$1 AND pool=$2",
        owner_id, name)
    return _cnt(res)


_REBALANCE_SQL = """
    SELECT id, first_name, username, phone, trust_score, pool, warnings,
           (cooldown_until IS NOT NULL AND cooldown_until > now()) AS on_cooldown
      FROM tg_accounts
     WHERE owner_id=$1 AND is_active=TRUE"""


def acc_label(r: Any) -> str:
    """Подпись аккаунта: имя, @ник или телефон."""
    get = r.get if hasattr(r, "get") else (lambda k: r[k])
    name = (get("first_name") or "").strip()
    if name:
        return name
    if get("username"):
        return "@" + str(get("username"))
    return str(get("phone") or f"#{get('id')}")


async def rebalance_plan(pool, owner_id: int) -> list[dict]:
    """Что сделало бы автораспределение — без записи."""
    rows = await pool.fetch(_REBALANCE_SQL, owner_id)
    out = []
    for r in rows or []:
        target = classify_account(dict(r))
        if target and r["pool"] != target:
            out.append({
                "id": int(r["id"]), "label": acc_label(r),
                "from_pool": r["pool"], "from_label": pool_label(r["pool"]),
                "to_pool": target, "to_label": pool_label(target),
            })
    return out


async def rebalance_apply(pool, owner_id: int) -> dict:
    """Применить автораспределение. Одним запросом на каждый целевой пул,
    а не запросом на аккаунт (у бота было по запросу на аккаунт)."""
    plan = await rebalance_plan(pool, owner_id)
    by_target: dict[str, list[int]] = {}
    for c in plan:
        by_target.setdefault(c["to_pool"], []).append(c["id"])
    moved = {}
    for target, ids in by_target.items():
        moved[target] = await assign_pool(pool, owner_id, target, account_ids=ids)
    return {"changed": sum(moved.values()),
            "by_pool": [{"pool": k, "label": pool_label(k), "count": v}
                        for k, v in moved.items()]}


# ── Журнал аудита ───────────────────────────────────────────────────────────

# Действия, которые пишутся в operation_audit не типом операции, а своим
# словом: воркер (join/publish/...), бюджет аккаунта (post/dm/...), прогрев
# (warmup) и журнал ручных действий (account_delete/...). Тип операции
# переводится по описанию из operation_bus.OP_REGISTRY.
ACTION_RU: dict[str, str] = {
    "warmup": "Прогрев аккаунта",
    "join": "Вступление в чат",
    "leave": "Выход из чата",
    "publish": "Публикация",
    "post": "Публикация поста",
    "report": "Жалоба",
    "boost": "Накрутка",
    "reaction": "Реакция",
    "view": "Просмотры",
    "story": "Сторис",
    "profile": "Изменение профиля",
    "dm": "Личное сообщение",
    "invite": "Приглашение",
    "create_channel": "Создание канала",
    "create_group": "Создание группы",
    "gp_create_bot": "Создан бот",
    "gp_create_group": "Создана группа",
    "gp_create_channel": "Создан канал",
    "peer_flood": "Спам-ограничение Telegram",
    "account_add": "Добавлен аккаунт",
    "account_delete": "Удалён аккаунт",
    "account_reimport": "Аккаунт загружен заново",
    "accounts_bulk_delete": "Массовое удаление аккаунтов",
    "appeal_sent": "Отправлена апелляция",
    "spamblock_appeal": "Апелляция по спам-блоку",
    "bot_remove": "Удалён бот",
    "bots_bulk_delete": "Массовое удаление ботов",
    "bots_cleanup_dead": "Очистка мёртвых ботов",
    "channel_remove": "Канал убран из управления",
    "channel_restore": "Канал возвращён в управление",
    "channels_bulk_delete": "Массовое удаление каналов",
    "ecosystem_delete": "Удалена экосистема",
    "ecosystem_foreign_link_refused": "Отказ: чужая ссылка в экосистеме",
    "ecosystem_foreign_object_refused": "Отказ: чужой объект в экосистеме",
    "fleet_sweep_cleanup": "Уборка флота",
    "foreign_accounts_refused": "Отказ: чужие аккаунты",
    "presence_pack_delete": "Удалён Presence Pack",
    "presence_pack_foreign_bot_refused": "Отказ: чужой бот в Presence Pack",
    "presence_pack_foreign_channel_refused": "Отказ: чужой канал в Presence Pack",
    "proxies_bulk_delete": "Массовое удаление прокси",
    "proxy_cleanup_dead": "Очистка мёртвых прокси",
    "proxy_delete": "Удалён прокси",
    "workspace_delete": "Удалено рабочее пространство",
    "workspace_invite_create": "Создано приглашение в команду",
    "workspace_member_join": "Участник вошёл в команду",
    "workspace_member_leave": "Участник вышел из команды",
    "workspace_member_remove": "Участник удалён из команды",
}

RESULT_RU: dict[str, str] = {
    "success": "Успешно", "ok": "Успешно", "done": "Успешно",
    "partial": "Частично", "error": "Ошибка", "failed": "Ошибка",
    "flood_wait": "Пауза Telegram (флуд)", "banned": "Аккаунт заблокирован",
    "peer_flood": "Спам-ограничение", "skipped": "Пропущено",
    "refused": "Отказано", "requested": "Запрошено",
}
OK_RESULTS = ("success", "ok", "done", "requested")
FAIL_RESULTS = ("error", "failed", "banned", "peer_flood", "refused")
WARN_RESULTS = ("partial", "flood_wait", "skipped")

AUDIT_PERIODS: dict[str, Optional[int]] = {"24h": 1, "7d": 7, "30d": 30, "all": None}
AUDIT_RESULT_FILTERS = ("all", "ok", "fail", "warn")


def action_ru(action: Optional[str]) -> str:
    a = str(action or "").strip()
    if not a:
        return "Действие"
    low = a.lower()
    if low in ACTION_RU:
        return ACTION_RU[low]
    if low.startswith("content_block:"):
        return "Проверка контента"
    try:
        from services.operation_bus import OP_REGISTRY
        desc = (OP_REGISTRY.get(low) or {}).get("description")
        if desc:
            return str(desc)
    except Exception:  # реестр недоступен — подпись не стоит падения экрана
        pass
    # Неизвестное слово не показываем латиницей как есть: владелец её не
    # читает. Подчёркивания → пробелы хотя бы делают его словами.
    return a.replace("_", " ")


def result_ru(result: Optional[str]) -> str:
    r = str(result or "").strip().lower()
    return RESULT_RU.get(r, r or "—")


def result_tone(result: Optional[str]) -> str:
    r = str(result or "").strip().lower()
    if r in OK_RESULTS:
        return "ok"
    if r in WARN_RESULTS:
        return "warn"
    return "fail"


_DAY = re.compile(r"^day\s+(\d+)$", re.I)
_ALL_FAILED = re.compile(r"^all\s+(\d+)\s+actions?\s+failed$", re.I)
_SOME_FAILED = re.compile(r"^(\d+)\s*/\s*(\d+)\s+failed$", re.I)
_WAIT = re.compile(r"(?:wait of|flood ?wait[^0-9]*)(\d+)\s*(?:seconds?|s)?", re.I)
_CYR = re.compile(r"[А-Яа-яЁё]")

# Частые тексты Telegram/Telethon → что это значит для владельца.
_TG_ERRORS: tuple[tuple[str, str], ...] = (
    ("peer_flood", "Telegram ограничил аккаунт за спам (PeerFlood) — дайте ему отдохнуть"),
    ("user_deactivated", "Аккаунт удалён или заблокирован Telegram"),
    ("auth_key_unregistered", "Сессия аккаунта больше не действует — нужен повторный вход"),
    ("session_revoked", "Сессию завершили с другого устройства — нужен повторный вход"),
    ("user_banned_in_channel", "Аккаунту запрещено писать в этом чате"),
    ("chat_write_forbidden", "В этом чате аккаунту нельзя писать"),
    ("channels_too_much", "Аккаунт состоит в предельном числе каналов (500)"),
    ("invite_hash_expired", "Ссылка-приглашение устарела"),
    ("username_not_occupied", "Такого @ника не существует"),
    ("username_invalid", "Неверный @ник"),
    ("user_privacy_restricted", "Человек запретил приглашать себя в настройках приватности"),
    ("user_not_mutual_contact", "Человек не в контактах аккаунта — Telegram не даёт пригласить"),
    ("timeout", "Telegram не ответил вовремя"),
    ("connection", "Не удалось подключиться к Telegram (сеть или прокси)"),
    ("proxy", "Сбой прокси аккаунта"),
)


def target_ru(target: Optional[str]) -> str:
    t = str(target or "").strip()
    m = _DAY.match(t)
    if m:
        return f"день {m.group(1)}"
    return t


def error_ru(msg: Optional[str]) -> str:
    """Причина сбоя по-русски. Русский текст отдаётся как есть; известный
    английский — переводится; неизвестный — общей фразой (сам текст экран
    показывает отдельно, как технические подробности)."""
    s = str(msg or "").strip()
    if not s:
        return ""
    if _CYR.search(s):
        return s
    m = _ALL_FAILED.match(s)
    if m:
        return f"Не удалось ни одно из {m.group(1)} действий"
    m = _SOME_FAILED.match(s)
    if m:
        return f"Не удалось {m.group(1)} из {m.group(2)} действий"
    low = s.lower()
    m = _WAIT.search(s)
    if m and ("wait" in low or "flood" in low):
        return f"Telegram попросил подождать {m.group(1)} с (защита от флуда)"
    for needle, ru in _TG_ERRORS:
        if needle in low:
            return ru
    return "Ошибка Telegram — подробности ниже"


def _audit_where(owner_id: int, *, result: str = "all", period: str = "7d",
                 action: str = "", account_id: Optional[int] = None,
                 pool_name: str = "", q: str = "") -> tuple[str, list]:
    args: list[Any] = [owner_id]
    where = ["oa.owner_id=$1"]
    days = AUDIT_PERIODS.get(period, 7)
    if days:
        args.append(days)
        where.append(f"oa.occurred_at > now() - make_interval(days => ${len(args)})")
    if result == "ok":
        args.append(list(OK_RESULTS))
        where.append(f"oa.result = ANY(${len(args)}::text[])")
    elif result == "warn":
        args.append(list(WARN_RESULTS))
        where.append(f"oa.result = ANY(${len(args)}::text[])")
    elif result == "fail":
        args.append(list(OK_RESULTS + WARN_RESULTS))
        where.append(f"NOT (oa.result = ANY(${len(args)}::text[]))")
    if action:
        args.append(action)
        where.append(f"oa.action = ${len(args)}")
    if account_id:
        args.append(int(account_id))
        where.append(f"oa.account_id = ${len(args)}")
    if pool_name:
        args.append(pool_name)
        where.append(f"a.pool = ${len(args)}")
    if q:
        args.append("%" + q.replace("%", r"\%").replace("_", r"\_") + "%")
        n = len(args)
        where.append(f"(oa.target ILIKE ${n} OR oa.error_msg ILIKE ${n}"
                     f" OR a.first_name ILIKE ${n} OR a.username ILIKE ${n})")
    return " AND ".join(where), args


def audit_row(r: Any) -> dict:
    occurred = r["occurred_at"]
    return {
        "id": int(r["id"]),
        "action": r["action"],
        "action_ru": action_ru(r["action"]),
        "target": target_ru(r["target"]),
        "result": r["result"],
        "result_ru": result_ru(r["result"]),
        "tone": result_tone(r["result"]),
        "error_ru": error_ru(r["error_msg"]),
        "error_raw": (str(r["error_msg"] or "")[:500] or None),
        "flood_wait_s": r["flood_wait_s"],
        "duration_ms": r["duration_ms"],
        "operation_id": r["operation_id"],
        "account_id": r["account_id"],
        "account_label": (acc_label(r) if r["account_id"] and (
            r["first_name"] or r["username"] or r["phone"]) else None),
        "account_pool": r["pool"],
        "occurred_at": occurred.isoformat() if occurred else None,
    }


async def audit_page(pool, owner_id: int, *, result: str = "all", period: str = "7d",
                     action: str = "", account_id: Optional[int] = None,
                     pool_name: str = "", q: str = "",
                     offset: int = 0, limit: int = 30) -> dict:
    where, args = _audit_where(owner_id, result=result, period=period, action=action,
                               account_id=account_id, pool_name=pool_name, q=q)
    base = ("FROM operation_audit oa "
            "LEFT JOIN tg_accounts a ON a.id = oa.account_id AND a.owner_id = oa.owner_id "
            f"WHERE {where}")
    n = len(args)
    rows = await pool.fetch(
        f"""SELECT oa.id, oa.action, oa.target, oa.result, oa.error_msg,
                   oa.flood_wait_s, oa.duration_ms, oa.operation_id, oa.account_id,
                   oa.occurred_at, a.first_name, a.username, a.phone, a.pool
              {base}
             ORDER BY oa.occurred_at DESC NULLS LAST, oa.id DESC
             LIMIT ${n + 1} OFFSET ${n + 2}""",
        *args, int(limit), int(offset))
    total = await pool.fetchval(f"SELECT COUNT(*) {base}", *args)
    return {"rows": [audit_row(r) for r in rows or []],
            "total": int(total or 0), "offset": int(offset), "limit": int(limit)}


async def audit_summary(pool, owner_id: int, period: str = "7d") -> dict:
    """Итог периода: успехи/предупреждения/сбои, частые действия и частые
    причины сбоев (причина очищена от частностей — как в сводке админки)."""
    from services.error_digest import normalize_error

    days = AUDIT_PERIODS.get(period, 7) or 3650
    ok, warn = list(OK_RESULTS), list(WARN_RESULTS)
    tot = await pool.fetchrow(
        """SELECT COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE result = ANY($3::text[])) AS ok,
                  COUNT(*) FILTER (WHERE result = ANY($4::text[])) AS warn
             FROM operation_audit
            WHERE owner_id=$1 AND occurred_at > now() - make_interval(days => $2)""",
        owner_id, days, ok, warn)
    acts = await pool.fetch(
        """SELECT action, COUNT(*) AS n,
                  COUNT(*) FILTER (WHERE NOT (result = ANY($3::text[]))) AS bad
             FROM operation_audit
            WHERE owner_id=$1 AND occurred_at > now() - make_interval(days => $2)
            GROUP BY action ORDER BY n DESC LIMIT 12""",
        owner_id, days, ok + warn)
    errs = await pool.fetch(
        """SELECT action, error_msg FROM operation_audit
            WHERE owner_id=$1 AND occurred_at > now() - make_interval(days => $2)
              AND NOT (result = ANY($3::text[])) AND COALESCE(error_msg, '') <> ''
            ORDER BY occurred_at DESC LIMIT 2000""",
        owner_id, days, ok + warn)
    groups: dict[str, dict] = {}
    for r in errs or []:
        key = normalize_error(r["error_msg"])
        g = groups.setdefault(key, {"reason": error_ru(r["error_msg"]),
                                    "example": str(r["error_msg"])[:200],
                                    "count": 0, "action_ru": action_ru(r["action"])})
        g["count"] += 1
    total = int(tot["total"] or 0) if tot else 0
    okn = int(tot["ok"] or 0) if tot else 0
    warnn = int(tot["warn"] or 0) if tot else 0
    return {
        "period": period,
        "total": total, "ok": okn, "warn": warnn, "fail": max(0, total - okn - warnn),
        "actions": [{"action": r["action"], "action_ru": action_ru(r["action"]),
                     "count": int(r["n"]), "fail": int(r["bad"] or 0)} for r in acts or []],
        "top_errors": sorted(groups.values(), key=lambda g: -g["count"])[:5],
    }
