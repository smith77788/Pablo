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

_CONCURRENCY = 5          # столько аккаунтов проверяем одновременно
_PER_ACC_TIMEOUT = 60.0   # потолок на один аккаунт


async def _default_checker(session_string, acc, group):
    from services import mass_inviter_engine as inv
    return await inv.check_membership_and_admin(session_string, acc, group)


async def run_preflight(pool: asyncpg.Pool, owner_id: int, group: str,
                        account_ids: list[int] | None = None, limit: int = 200,
                        checker=None) -> dict:
    """Проверить готовность аккаунтов оператора к инвайту в `group`.

    account_ids=None → берём активные аккаунты владельца (до limit). Возвращает
    сводку по состояниям + короткий список деталей + вердикт-совет.
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
    counts = {"admin": 0, "member": 0, "not_member": 0,
              "no_connect": 0, "group_bad": 0, "error": 0}
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

    results = await asyncio.gather(*[_one(a) for a in accounts])
    for acc, res in results:
        st = res.get("state", "error")
        counts[st] = counts.get(st, 0) + 1
        if res.get("can_invite") or st == "admin":
            can_invite += 1
        details.append({"id": acc["id"], "phone": acc.get("phone"), "state": st,
                        "can_promote": bool(res.get("can_promote"))})

    total = len(accounts)
    ready = counts["admin"] + counts["member"]  # в группе (могут инвайтить/промоутиться)
    verdict = _verdict(total, counts)
    return {"total": total, "counts": counts, "ready": ready,
            "can_invite": can_invite, "admins": counts["admin"],
            "details": details, "verdict": verdict}


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
