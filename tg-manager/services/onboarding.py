"""Онбординг-чеклист активации: путь новичка от нуля до первого результата.

Проблема конверсии: новый пользователь открывает мини-апп и видит стену из
десятков модулей — не понимает, с чего начать, и уходит, не активировавшись
(а значит и не покупает подписку). Чеклист сводит старт к нескольким шагам с
действием в один тап и исчезает, когда пользователь получил ПЕРВЫЙ РЕЗУЛЬТАТ.

ЧТО СЧИТАЕТСЯ РЕЗУЛЬТАТОМ. Раньше «активирован» значило «в operation_queue есть
хоть одна строка». Но подключение аккаунта само ставит `contacts_push`
(device_sync.schedule_for_new_account), поэтому чеклист гас сразу после первого
шага и показывал «активирован» человеку, который ничего не запускал (D8). И даже
запущенная им операция — ещё не результат: она могла упасть или ничего не
сделать. Теперь:

  * «первое действие» — операция, которую запустил сам владелец: без
    системных (`SYSTEM_OP_TYPES`, их ставит платформа) и служебных проверок
    (`SERVICE_OP_TYPES`, они ничего не делают для владельца);
  * «первый результат» — такая операция, закрытая продуктивно
    (op_status.PRODUCTIVE: done/partial) и взявшая хотя бы одну цель, либо
    пройденный день прогрева (прогрев идёт планом, а не одной операцией).

Активация липкая: первый результат записывается в `onboarding_progress`
(когда, какой операцией). Ветерану чеклист не возвращается, даже если потом
все его аккаунты умрут, а у владельца есть след, когда и чем он активировался.

build_checklist — чистая функция (тестируется без БД): по снимку состояния
собирает шаги. load_state читает снимок из базы, load_checklist — то и другое
вместе; им пользуются и мини-апп, и бот.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from services.account_status import DEAD_STATUSES

log = logging.getLogger(__name__)

# Ставит сама платформа, без участия владельца (выгрузка контактов «как с
# устройства» сразу после подключения аккаунта и её периодический повтор).
SYSTEM_OP_TYPES = frozenset({"contacts_push", "contacts_sync"})

# Служебные проверки и импорты: полезны, но для владельца ничего не делают —
# «проверил аккаунты» не то, ради чего он пришёл. Не считаются ни первым
# действием, ни результатом.
SERVICE_OP_TYPES = frozenset({
    "check_accounts_health",
    "check_owned_restrictions",
    "check_bot_geoban",
    "scan_owned_resources",
    "scan_owned_bots",
    "channel_import_all",
    "group_import_all",
    "reclassify_channels",
})

NOT_A_RESULT = tuple(sorted(SYSTEM_OP_TYPES | SERVICE_OP_TYPES))

# Мёртвые статусы — единый словарь account_status (не пересказываем).
_DEAD = tuple(sorted(DEAD_STATUSES))


def _n(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _op_title(op: Optional[dict]) -> str:
    if not op:
        return ""
    label = (op.get("label") or "").strip()
    if label:
        return label[:80]
    try:
        from services.operation_bus import OP_REGISTRY
        desc = (OP_REGISTRY.get(op.get("op_type") or "") or {}).get("description")
    except Exception:
        desc = None
    return (desc or "операция")[:80]


def build_checklist(
    *,
    accounts: int,
    accounts_alive: Optional[int] = None,
    accounts_with_proxy: int = 0,
    proxies: int = 0,
    plan_paid: bool = True,
    user_ops: int = 0,
    inflight_op: Optional[dict] = None,
    last_failed_op: Optional[dict] = None,
    first_result: Optional[dict] = None,
    warmup_days: int = 0,
    activated_at: Any = None,
    trial: Optional[dict] = None,
    trial_available: bool = False,
) -> dict:
    """Собрать чеклист из снимка состояния владельца.

    Обязательные шаги: живой аккаунт, подписка (операции платные), первое
    действие, первый результат. Прокси — рекомендуемый (без него аккаунт идёт
    с IP сервера), активацию не блокирует.

    activated=True только с первым результатом — тогда поверхность прячет
    чеклист и показывает итог. Аргументы *_op — словари {id, op_type, label,
    status, done_items, total_items, reason}.
    """
    accounts = _n(accounts)
    alive = accounts if accounts_alive is None else min(_n(accounts_alive), accounts)
    has_proxy = _n(proxies) > 0 or _n(accounts_with_proxy) > 0
    plan_paid = bool(plan_paid)
    has_op = _n(user_ops) > 0
    warmup_days = _n(warmup_days)
    has_result = bool(first_result) or warmup_days > 0 or activated_at is not None

    acc_step = {
        "id": "accounts",
        "title": "Подключить аккаунт",
        "hint": "Импортируйте сессию или войдите по номеру/QR — без аккаунта "
                "ничего не работает.",
        "done": alive > 0,
        "optional": False,
        "cta": "Подключить",
        "action": {"kind": "import_accounts"},
    }
    if alive > 0:
        acc_step["title"] = f"Аккаунты подключены: {alive}"
    elif accounts > 0:
        # Аккаунты есть, но ни один не может действовать: «✓ подключено» тут
        # было бы той же ложью, только на первом шаге.
        acc_step["hint"] = (f"Подключено {accounts}, но ни один не рабочий: бан, "
                            "спамблок или сессия отозвана. Подключите рабочий "
                            "аккаунт или переподключите эти.")

    steps = [
        acc_step,
        {
            "id": "proxy",
            "title": "Задать прокси",
            "hint": "Рекомендуется: свой прокси = стабильный IP и меньше рисков. "
                    "Без прокси аккаунт идёт напрямую с IP сервера.",
            "done": has_proxy,
            "optional": True,
            "cta": "Добавить прокси",
            "action": {"kind": "proxy"},
        },
        {
            "id": "plan",
            "title": "Оформить подписку",
            "hint": "Запуск действий — прогрев, сбор аудитории, инвайт, рассылки — "
                    "доступен в подписке. Без неё аккаунт подключён, но работать "
                    "не может.",
            "done": plan_paid,
            "optional": False,
            "cta": "Оформить",
            "action": {"kind": "billing"},
        },
    ]
    plan_step = steps[-1]
    if plan_paid and trial:
        # Идёт пробный период: шаг закрыт, но человек должен видеть, сколько
        # осталось, — иначе конец триала станет для него «всё сломалось».
        days_left = _n(trial.get("days_left")) or 1
        plan_step["title"] = f"Пробный период: осталось {days_left} дн."
        plan_step["hint"] = "Все функции открыты. Оформите подписку до конца срока — дни триала не сгорят."
        plan_step["cta"] = "Оформить"
    elif not plan_paid and trial_available:
        plan_step["title"] = "Пробный период"
        plan_step["hint"] = ("Подключите рабочий аккаунт — и все функции откроются "
                             "бесплатно на пробный период.")
        plan_step["cta"] = None
        plan_step["action"] = None

    op_step = {
        "id": "first_op",
        "title": "Запустить первое действие",
        "hint": "Прогрев, сбор аудитории, инвайт или рассылка — выберите, с чего "
                "начать.",
        "done": has_op,
        "optional": False,
        "cta": "Выбрать",
        "action": {"kind": "first_action"},
    }
    res_step = {
        "id": "first_result",
        "title": "Получить первый результат",
        "hint": "Результат — действие, которое что-то сделало: пригласило, "
                "отправило, собрало или прогрело хотя бы одну цель.",
        "done": has_result,
        "optional": False,
        "cta": None,
        "action": None,
    }
    if has_result:
        if first_result:
            ok = _n(first_result.get("ok"))
            res_step["title"] = (f"Первый результат: {_op_title(first_result)}"
                                 + (f" — {ok}" if ok else ""))
        elif warmup_days:
            res_step["title"] = "Первый результат: пройден день прогрева"
    elif inflight_op:
        done_i, total_i = _n(inflight_op.get("done_items")), _n(inflight_op.get("total_items"))
        prog = f" · {done_i} из {total_i}" if total_i else ""
        res_step["hint"] = f"Идёт «{_op_title(inflight_op)}»{prog}. Итог появится здесь."
        res_step["cta"] = "Открыть"
        res_step["action"] = {"kind": "operation", "param": int(inflight_op["id"])}
    elif last_failed_op:
        try:
            from services.secret_masking import redact_secrets
            reason = redact_secrets(last_failed_op.get("reason") or "", 200)
        except Exception:
            reason = ""
        reason = (reason or "").strip() or "ни одна цель не взята"
        res_step["hint"] = (f"«{_op_title(last_failed_op)}» не дала результата: "
                            f"{reason[:160]}. Откройте операцию — там причина по "
                            "каждой цели и повтор.")
        res_step["cta"] = "Разобрать"
        res_step["action"] = {"kind": "operation", "param": int(last_failed_op["id"])}
    elif has_op:
        res_step["hint"] = "Операция ещё не дала итога. Запустите действие ещё раз."
        res_step["cta"] = "Выбрать"
        res_step["action"] = {"kind": "first_action"}
    steps += [op_step, res_step]

    done_count = sum(1 for s in steps if s["done"])
    # Следующий незакрытый обязательный шаг — на нём фокус (подсветка).
    next_step = next((s["id"] for s in steps if not s["done"] and not s["optional"]), None)

    return {
        "activated": has_result,
        "steps": steps,
        "done": done_count,
        "total": len(steps),
        "next_step": next_step if not has_result else None,
        "first_result": (
            {"op_id": first_result.get("id"), "title": _op_title(first_result),
             "ok": _n(first_result.get("ok"))}
            if first_result else
            ({"op_id": None, "title": "Пройден день прогрева", "ok": warmup_days}
             if warmup_days else None)
        ),
        "activated_at": activated_at.isoformat() if hasattr(activated_at, "isoformat") else activated_at,
        "trial": (
            {"days_left": _n(trial.get("days_left")),
             "expires_at": (trial["expires_at"].isoformat()
                            if hasattr(trial.get("expires_at"), "isoformat") else trial.get("expires_at"))}
            if trial else None
        ),
    }


# ── Чтение снимка из базы ────────────────────────────────────────────────────

_OK_SQL = ("CASE WHEN q.result ? 'ok' THEN "
           "(CASE WHEN (q.result->>'ok') ~ '^[0-9]+$' THEN (q.result->>'ok')::int ELSE 0 END) "
           "ELSE COALESCE(q.done_items, 0) END")


async def _plan_paid(pool, uid: int) -> bool:
    try:
        from bot.utils.subscription import require_plan
        return bool(await require_plan(pool, uid, "paid"))
    except Exception:
        # Не смогли узнать тариф — не рисуем ложный шаг «оформите подписку».
        log.warning("onboarding: plan check failed uid=%s", uid, exc_info=True)
        return True


async def _plan_state(pool, uid: int, *, alive: bool) -> dict:
    """Тариф + пробный период. Новичку с живым аккаунтом триал выдаётся здесь:
    чеклист — первое место, где он его видит (services/trial.py)."""
    from services import trial as _trial
    paid = await _plan_paid(pool, uid)
    granted = None
    if not paid and alive:
        granted = await _trial.maybe_grant(pool, uid)
        if granted:
            paid = await _plan_paid(pool, uid)
    info = available = None
    try:
        info = await _trial.active_trial(pool, uid) if paid else None
        available = (not paid) and await _trial.is_eligible(pool, uid, need_account=False)
    except Exception:
        log.warning("onboarding: trial state failed uid=%s", uid, exc_info=True)
    return {"plan_paid": paid, "trial": info, "trial_available": bool(available)}


async def load_state(pool, uid: int) -> dict:
    """Снимок состояния владельца для build_checklist. Только чтение, кроме
    фиксации активации (см. record_activation)."""
    from services import op_status

    stored = await pool.fetchrow(
        "SELECT activated_at, first_result_op_id, first_result_label, first_result_ok "
        "FROM onboarding_progress WHERE owner_id=$1", uid)
    if stored and stored["activated_at"] is not None:
        # Активирован раньше — остальное не читаем: чеклист ему не покажут.
        fr = None
        if stored["first_result_op_id"] is not None or stored["first_result_label"]:
            fr = {"id": stored["first_result_op_id"],
                  "label": stored["first_result_label"],
                  "ok": stored["first_result_ok"]}
        return {"accounts": 1, "first_result": fr,
                "activated_at": stored["activated_at"]}

    acc = await pool.fetchrow(
        "SELECT COUNT(*) AS total, "
        "COUNT(*) FILTER (WHERE is_active IS NOT FALSE "
        "  AND COALESCE(acc_status, 'active') <> ALL($2::text[])) AS alive, "
        "COUNT(*) FILTER (WHERE proxy_id IS NOT NULL) AS with_proxy "
        "FROM tg_accounts WHERE owner_id=$1 "
        "AND session_str IS NOT NULL AND session_str <> ''",
        uid, list(_DEAD))
    proxies = await pool.fetchval(
        "SELECT COUNT(*) FROM user_proxies WHERE owner_id=$1", uid) or 0
    excl = list(NOT_A_RESULT)
    user_ops = await pool.fetchval(
        "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 "
        "AND op_type <> ALL($2::text[])", uid, excl) or 0

    first_result = inflight = last_failed = None
    if user_ops:
        row = await pool.fetchrow(
            f"SELECT q.id, q.op_type, q.label, {_OK_SQL} AS ok "
            "FROM operation_queue q WHERE q.owner_id=$1 "
            "AND q.op_type <> ALL($2::text[]) AND q.status = ANY($3::text[]) "
            f"AND {_OK_SQL} > 0 "
            "ORDER BY q.finished_at NULLS LAST, q.id LIMIT 1",
            uid, excl, sorted(op_status.PRODUCTIVE))
        first_result = dict(row) if row else None
        if not first_result:
            row = await pool.fetchrow(
                "SELECT q.id, q.op_type, q.label, q.status, q.done_items, q.total_items "
                "FROM operation_queue q WHERE q.owner_id=$1 "
                "AND q.op_type <> ALL($2::text[]) AND q.status = ANY($3::text[]) "
                "ORDER BY q.id DESC LIMIT 1",
                uid, excl, sorted(op_status.IN_FLIGHT))
            inflight = dict(row) if row else None
            row = await pool.fetchrow(
                "SELECT q.id, q.op_type, q.label, q.status, "
                f"{op_status.sql_error_reason('q.')} AS reason "
                "FROM operation_queue q WHERE q.owner_id=$1 "
                "AND q.op_type <> ALL($2::text[]) AND q.status = ANY($3::text[]) "
                "ORDER BY q.id DESC LIMIT 1",
                uid, excl, sorted(op_status.TERMINAL))
            last_failed = dict(row) if row else None
            if last_failed and last_failed.get("status") == op_status.CANCELLED:
                last_failed["reason"] = last_failed.get("reason") or "отменена"

    warmup_days = 0
    if not first_result:
        warmup_days = await pool.fetchval(
            "SELECT COALESCE(MAX(current_day), 0) FROM account_warmup_plans "
            "WHERE owner_id=$1", uid) or 0

    return {
        "accounts": acc["total"] if acc else 0,
        "accounts_alive": acc["alive"] if acc else 0,
        "accounts_with_proxy": acc["with_proxy"] if acc else 0,
        "proxies": proxies,
        **(await _plan_state(pool, uid, alive=bool(acc and acc["alive"]))),
        "user_ops": user_ops,
        "inflight_op": inflight,
        "last_failed_op": last_failed,
        "first_result": first_result,
        "warmup_days": warmup_days,
        "activated_at": None,
    }


async def record_activation(pool, uid: int, state: dict) -> None:
    """Зафиксировать первый результат: один раз на владельца (аудит + липкость)."""
    fr = state.get("first_result") or {}
    label = _op_title(fr) if fr else ("Пройден день прогрева" if state.get("warmup_days") else None)
    inserted = await pool.fetchval(
        "INSERT INTO onboarding_progress(owner_id, activated_at, first_result_op_id, "
        "first_result_op_type, first_result_label, first_result_ok) "
        "VALUES ($1, now(), $2, $3, $4, $5) "
        "ON CONFLICT (owner_id) DO NOTHING RETURNING activated_at",
        uid, fr.get("id"), fr.get("op_type") or ("account_warmup" if not fr else None),
        label, _n(fr.get("ok")) if fr else _n(state.get("warmup_days")))
    if inserted is not None:
        log.info("onboarding: owner=%s activated by op=%s (%s)", uid, fr.get("id"), label)
        state["activated_at"] = inserted


async def load_checklist(pool, uid: int) -> dict:
    """Снимок + чеклист; при первом результате фиксирует активацию."""
    state = await load_state(pool, uid)
    if state.get("activated_at") is None and (state.get("first_result") or state.get("warmup_days")):
        try:
            await record_activation(pool, uid, state)
        except Exception:
            # Фиксация — аудит и кеш; чеклист и без неё честный.
            log.warning("onboarding: record_activation failed uid=%s", uid, exc_info=True)
    return build_checklist(**state)
