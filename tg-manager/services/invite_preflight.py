"""Пре-флайт готовности флота к инвайту в конкретную группу.

До запуска операции подключает выбранные аккаунты и по целевой группе выясняет,
кто реально готов инвайтить: подключился ли, в группе ли, админ ли. Оператор
видит проблему ЗАРАНЕЕ («3 не подключились, 5 не в группе, 0 админов»), а не
после впустую потраченного прогона.

Проверки идут с ограничением параллелизма (семафор) и таймаутом на аккаунт.
Сама per-account проверка — seam (по умолчанию движковая), поэтому агрегатор
тестируется без Telegram.
"""
from __future__ import annotations

import asyncio
import logging

import asyncpg

log = logging.getLogger(__name__)

_CONCURRENCY = 8          # столько аккаунтов проверяем одновременно
_PER_ACC_TIMEOUT = 25.0   # потолок на один аккаунт (быстрый пре-чек, без ретраев)


def classify_conn_error(err: str) -> str:
    """Сырую ошибку коннекта → человеко-понятную причину (для сводки пре-флайта).

    Чтобы «14 не подключились» перестало быть чёрным ящиком: оператор видит
    ИМЕННО почему (сессия отозвана / забанен / конфликт IP / таймаут / сеть) и
    понимает, что делать (перезалить сессию, сменить IP, подождать)."""
    # Сравниваем без разделителей: и «auth_key_duplicated», и класс
    # «AuthKeyDuplicatedError» дают одну форму.
    e = (err or "").lower()
    if not e:
        return "неизвестно"
    flat = e.replace("_", "").replace(" ", "")
    if "authkeyduplicated" in flat or "twodifferentip" in flat:
        return ("конфликт: сессия зашла с двух IP (AUTH_KEY_DUPLICATED) — "
                "если повторяется, сессию нужно перезалить")
    if "authkeyunregistered" in flat or "unregistered" in flat or "revoked" in flat:
        return "сессия отозвана/недействительна — нужно перезалить аккаунт"
    if "deactivated" in flat or "banned" in flat:
        return "аккаунт удалён/забанен Telegram"
    if "flood" in e:
        return "флуд-контроль Telegram — подождать"
    if "timeout" in e or "timed out" in e:
        return "таймаут подключения — сеть/прокси медленные"
    if ("proxy" in e or "socks" in e or "connect" in e or "network" in e
            or "unreachable" in e or "refused" in e or "reset" in e):
        return "сеть/транспорт — прокси мёртв или прямой выход заблокирован"
    if "phone" in e and "migrate" in e:
        return "миграция дата-центра — повторить"
    return err[:70]


async def _default_checker(session_string, acc, group):
    from services import mass_inviter_engine as inv
    return await inv.check_membership_and_admin(session_string, acc, group)


async def run_preflight(pool: asyncpg.Pool, owner_id: int, group: str,
                        account_ids: list[int] | None = None, limit: int = 200,
                        checker=None, claim: bool = True) -> dict:
    """Проверить готовность аккаунтов оператора к инвайту в `group`.

    account_ids=None → берём активные аккаунты владельца (до limit). Возвращает
    сводку по состояниям + короткий список деталей + вердикт-совет.

    claim=True: на время пре-чека помечаем аккаунты in_operation (mark_in_use),
    чтобы фоновые циклы (монитор/здоровье/прогрев) НЕ подключили ту же сессию
    параллельно. Иначе один auth-key коннектится из двух мест → Telegram видит
    вход с двух IP и УНИЧТОЖАЕТ ключ (AUTH_KEY_DUPLICATED, безвозвратно). Именно
    это сжигало свежий флот. Занятых реальной операцией не трогаем и НЕ
    отпускаем (release только своих). claim=False — для юнит-тестов без op_worker.
    """
    checker = checker or _default_checker
    if account_ids:
        rows = await pool.fetch(
            "SELECT id, phone, session_str FROM tg_accounts "
            "WHERE owner_id=$1 AND id=ANY($2::bigint[]) AND session_str IS NOT NULL",
            owner_id, [int(i) for i in account_ids])
    else:
        rows = await pool.fetch(
            "SELECT id, phone, session_str FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
            "ORDER BY trust_score DESC NULLS LAST LIMIT $2",
            owner_id, int(limit))
    accounts = [dict(r) for r in rows]

    # Захватываем СВОБОДНЫЕ аккаунты на время пре-чека (предотвращаем гонку с
    # фоновыми коннектами). Занятые реальной операцией — пропускаем из живой
    # проверки (их и так нельзя трогать), помечаем busy.
    _opw = None
    claimed_ids: list[int] = []
    busy_ids: set[int] = set()
    if claim:
        try:
            from services import op_worker as _opw_mod
            _opw = _opw_mod
            # Атомарный захват свободных (check-and-set под одним локом): устраняет
            # TOCTOU между проверкой is_account_in_use и mark. Что не захватилось —
            # занято реальной операцией; такие пропускаем из живой проверки.
            all_ids = [int(a["id"]) for a in accounts]
            claimed_ids = await _opw.try_claim_accounts(all_ids)
            busy_ids = set(all_ids) - set(claimed_ids)
        except Exception:
            _opw = None  # op_worker недоступен — работаем без захвата (best-effort)

    check_accounts = [a for a in accounts if int(a["id"]) not in busy_ids]
    counts = {"admin": 0, "member": 0, "not_member": 0,
              "no_connect": 0, "group_bad": 0, "error": 0, "busy": len(busy_ids)}
    reasons: dict[str, int] = {}   # причина неудачи → сколько аккаунтов
    details: list[dict] = []
    can_invite = 0
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(acc):
        async with sem:
            try:
                res = await asyncio.wait_for(
                    checker(acc["session_str"], acc, group), timeout=_PER_ACC_TIMEOUT)
            except asyncio.TimeoutError:
                res = {"state": "no_connect", "error": "timeout"}
            except Exception as e:
                res = {"state": "error", "error": str(e)[:120]}
            return acc, res

    try:
        results = await asyncio.gather(*[_one(a) for a in check_accounts])
    finally:
        if _opw and claimed_ids:
            try:
                await _opw.release_accounts(claimed_ids)
            except Exception:
                log.warning("run_preflight: release_accounts failed", exc_info=True)
    for acc, res in results:
        st = res.get("state", "error")
        counts[st] = counts.get(st, 0) + 1
        if res.get("can_invite") or st == "admin":
            can_invite += 1
        err = res.get("error")
        if st in ("no_connect", "error") and err:
            why = classify_conn_error(err)
            reasons[why] = reasons.get(why, 0) + 1
        details.append({"id": acc["id"], "phone": acc.get("phone"), "state": st,
                        "can_promote": bool(res.get("can_promote")),
                        "error": (err or "")[:120]})

    total = len(check_accounts)   # реально проверенные (без занятых операцией)
    ready = counts["admin"] + counts["member"]  # в группе (могут инвайтить/промоутиться)
    # Топ-причины неудач — оператор сразу видит корень (а не «просто не вышло»).
    top_reasons = [{"reason": k, "count": v}
                   for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])]
    verdict = _verdict(total, counts)
    return {"total": total, "counts": counts, "ready": ready,
            "can_invite": can_invite, "admins": counts["admin"], "busy": len(busy_ids),
            "details": details, "reasons": top_reasons, "verdict": verdict}


async def count_already_invited(pool: asyncpg.Pool, owner_id: int, group: str,
                                items: list, cap: int = 50000) -> int:
    """Сколько из `items` уже приглашались в `group` (по invite_target_log).

    Fail-open: нет группы/пусто/список > cap/ошибка → 0. group нормализуется тем
    же parse_group_ref, что и ключ дедупа в op_worker."""
    if not group or not items or len(items) > cap:
        return 0
    try:
        from services.mass_inviter_engine import parse_group_ref
        from services.contact_opt_out import compare_key

        gk = parse_group_ref(group) or group
        # Сравнение регистронезависимое — ровно то же, чем дедупит исполнитель.
        # Пока здесь стояло сырое `target = ANY(...)`, пре-флайт занижал число
        # «уже приглашено» для username'ов, чей регистр в списке отличался от
        # записанного в журнал, и обещал оператору больше новых целей, чем
        # операция потом реально брала. Цифра пре-флайта обязана совпадать с тем,
        # что сделает прогон, иначе она хуже, чем её отсутствие.
        keys = list({compare_key(x) for x in items})
        n = await pool.fetchval(
            "SELECT COUNT(*) FROM invite_target_log "
            "WHERE owner_id=$1 AND group_key=$2 AND lower(target) = ANY($3::text[])",
            owner_id, gk, keys)
        return int(n or 0)
    except Exception:
        return 0


async def _default_joiner(session_string, acc, group):
    from services import account_manager as am
    return await am.join_channel(session_string, group, _acc=acc)


async def join_all(pool: asyncpg.Pool, owner_id: int, group: str,
                   account_ids: list[int] | None = None, limit: int = 200,
                   joiner=None) -> dict:
    """Вступить всеми аккаунтами оператора в `group` (для прямого инвайта нужно
    членство). Идемпотентно: уже-участник считается 'already'. Возвращает
    {total, joined, already, failed}. joiner — seam (тестируется без Telegram).
    """
    joiner = joiner or _default_joiner
    if account_ids:
        rows = await pool.fetch(
            "SELECT id, phone, session_str FROM tg_accounts "
            "WHERE owner_id=$1 AND id=ANY($2::bigint[]) AND session_str IS NOT NULL",
            owner_id, [int(i) for i in account_ids])
    else:
        rows = await pool.fetch(
            "SELECT id, phone, session_str FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
            "ORDER BY trust_score DESC NULLS LAST LIMIT $2",
            owner_id, int(limit))
    accounts = [dict(r) for r in rows]
    # Вступление заметнее инвайта — меньше параллелизм.
    sem = asyncio.Semaphore(3)

    async def _one(acc):
        async with sem:
            try:
                r = await asyncio.wait_for(
                    joiner(acc["session_str"], acc, group), timeout=_PER_ACC_TIMEOUT)
            except Exception:
                return "failed"
            err = str((r or {}).get("error", "")).lower()
            if not err:
                return "joined"
            if "already" in err or "participant" in err:
                return "already"
            return "failed"

    outs = await asyncio.gather(*[_one(a) for a in accounts])
    joined = sum(1 for x in outs if x == "joined")
    already = sum(1 for x in outs if x == "already")
    failed = sum(1 for x in outs if x == "failed")
    return {"total": len(accounts), "joined": joined,
            "already": already, "failed": failed}


def _verdict(total: int, c: dict) -> str:
    """Короткий совет оператору по итогам пре-флайта."""
    if total == 0:
        return "Нет аккаунтов для проверки."
    parts: list[str] = []
    if c["no_connect"]:
        parts.append(f"{c['no_connect']} не подключились — проверьте сессии/сеть "
                     "(без прокси включается прямой выход с реального IP).")
    if c["group_bad"] == total and total:
        return "Группа недоступна ни одному аккаунту — проверьте ссылку/доступ."
    if c["not_member"]:
        parts.append(f"{c['not_member']} не в группе — они должны сперва вступить, "
                     "иначе инвайтить не смогут.")
    if c["admin"] == 0 and (c["member"] or c["not_member"]):
        parts.append("нет ни одного админа с правом «Назначать админов» — метод "
                     "«через админку»/автовыдача прав не сработают; сделайте один "
                     "аккаунт админом чата либо используйте обычный инвайт/ссылку.")
    elif c["admin"]:
        parts.append(f"есть {c['admin']} админ(а) — автовыдача прав остальным сработает.")
    if not parts:
        return "✅ Флот готов к инвайту."
    return " ".join(parts)
