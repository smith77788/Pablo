"""Proxy rotation — безопасная ротация назначений прокси по пулу.

Зачем: варьировать IP-отпечаток сетки аккаунтов (anti-detection), не ломая
главный инвариант продукта — ИЗОЛЯЦИЯ: один аккаунт ↔ один УНИКАЛЬНЫЙ прокси,
никогда два аккаунта на одном прокси одновременно.

⚠️ Класс багов «изоляция» (самый дорогой, CLAUDE.md). Границы безопасности,
которые обеспечиваются здесь (планировщик) и в apply (mini_app_api):
  1. Назначение ПОСЛЕ ротации — биекция: разные аккаунты → разные прокси (планировщик
     выдаёт перестановку пула → дубликатов нет).
  2. Ротацию применяет ТОЛЬКО к простаивающим аккаунтам (`in_operation=FALSE`,
     durable-флаг op_worker) внутри одной транзакции с `SELECT … FOR UPDATE` —
     нельзя переназначить прокси у аккаунта, который прямо сейчас коннектится.
     Это исключает одновременную работу одной сессии с двух IP (AUTH_KEY_DUPLICATED):
     смена proxy_id видна только СЛЕДУЮЩЕЙ операции, а параллельной операции на том
     же аккаунте не бывает (claim в op_worker).
  3. Меняем только `proxy_id` в БД. Резолвер (`account_manager._resolve_client_proxy`)
     НЕ трогаем — он и так читает актуальный proxy_id на старте каждой операции.

Здесь — только ЧИСТЫЙ планировщик (тестируется без БД). Эффект/транзакция — в apply.
"""
from __future__ import annotations


def plan_rotation(assignments: list[dict], pool_ids: list) -> dict:
    """Построить план ротации: каждому аккаунту — НОВЫЙ прокси из пула так, что
    итоговое назначение инъективно (никакие два аккаунта не делят прокси).

    assignments: [{"account_id": int, "proxy_id": int|None}, ...] — текущее состояние
                 (передавать только простаивающие аккаунты — busy отфильтровать до вызова).
    pool_ids:    список id прокси, доступных под ротацию (владелец, активные, НЕ занятые
                 аккаунтами вне группы — чтобы не сломать изоляцию с чужими аккаунтами).

    Возвращает {"plan": [{"account_id","old","new"}...], "rotated": n, "skipped_no_proxy": m}.
    Если прокси в пуле меньше, чем аккаунтов — ротируем сколько можем (первые по
    account_id), остальным ротации нет (skipped_no_proxy). Детерминировано.
    """
    pool = list(dict.fromkeys(p for p in (pool_ids or []) if p is not None))
    accs = sorted(assignments or [], key=lambda a: int(a["account_id"]))
    n = len(accs)

    # Сколько ведущих (по account_id) аккаунтов реально ротируем. Если прокси меньше,
    # чем аккаунтов, ОСТАЛЬНЫЕ оставляем на текущем прокси — и тогда их прокси НЕЛЬЗЯ
    # раздавать ротируемым (иначе два аккаунта окажутся на одном прокси). Фикспоинт:
    # выкидываем из пула прокси удерживаемых (skipped) аккаунтов; если пул сжался —
    # пересчитываем сколько можем ротировать. Сходится: пул только уменьшается.
    r = min(n, len(pool))
    while r < n:
        keep = {a.get("proxy_id") for a in accs[r:] if a.get("proxy_id") is not None}
        trimmed = [p for p in pool if p not in keep]
        if len(trimmed) == len(pool):
            break
        pool = trimmed
        r = min(n, len(pool))

    plan: list[dict] = []
    rotated = 0
    skipped = 0
    for i, a in enumerate(accs):
        if i >= r:
            # прокси не хватило — аккаунт остаётся на своём (без изменений)
            skipped += 1
            continue
        # циклический сдвиг на 1 по pool[:r]: индексы (i+1)%r для i=0..r-1 —
        # перестановка → все назначения РАЗНЫЕ; pool[:r] не содержит прокси
        # удерживаемых аккаунтов → нет коллизий с не-ротируемыми. Инвариант изоляции.
        new_pid = pool[(i + 1) % r]
        old_pid = a.get("proxy_id")
        plan.append({"account_id": int(a["account_id"]), "old": old_pid, "new": new_pid})
        if new_pid != old_pid:
            rotated += 1
    return {"plan": plan, "rotated": rotated, "skipped_no_proxy": skipped}


async def apply_rotation(
    pool,
    owner_id: int,
    account_ids: list[int] | None = None,
    proxy_ids: list[int] | None = None,
) -> dict:
    """Применить ротацию назначений прокси в БД (эффект + транзакция).

    Единая реализация для обоих фронтендов (mini-app И бот) — не дублируем
    деликатную логику изоляции. Все границы безопасности из докстринга модуля
    обеспечиваются здесь: FOR UPDATE на ротируемых аккаунтах, вычитание прокси
    занятых не-ротируемыми аккаунтами, guard `in_operation=FALSE` при UPDATE.

    account_ids/proxy_ids — необязательные фильтры (пусто = весь пул владельца).
    Возвращает {ok, rotated, skipped_busy, skipped_no_proxy, accounts, pool}.
    """
    acc_ids = [int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()]
    pool_ids_in = [int(x) for x in (proxy_ids or []) if str(x).strip().lstrip("-").isdigit()]

    async with pool.acquire() as conn:
        async with conn.transaction():
            # 1. Блокируем строки ротируемых аккаунтов (busy-safe против claim).
            if acc_ids:
                accs = await conn.fetch(
                    "SELECT id, proxy_id, COALESCE(in_operation,FALSE) AS busy "
                    "FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
                    "AND id = ANY($2::bigint[]) FOR UPDATE",
                    owner_id, acc_ids)
            else:
                accs = await conn.fetch(
                    "SELECT id, proxy_id, COALESCE(in_operation,FALSE) AS busy "
                    "FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE FOR UPDATE",
                    owner_id)
            group_ids = [int(r["id"]) for r in accs]
            free = [{"account_id": int(r["id"]), "proxy_id": r["proxy_id"]}
                    for r in accs if not r["busy"]]
            skipped_busy = len(accs) - len(free)
            free_ids = [a["account_id"] for a in free]

            # 2. Пул: владелец + активные; вычесть прокси, занятые ЛЮБЫМ
            # не-ротируемым аккаунтом (иначе потеря изоляции).
            if pool_ids_in:
                prows = await conn.fetch(
                    "SELECT id FROM user_proxies WHERE owner_id=$1 AND is_active=TRUE "
                    "AND id = ANY($2::int[])", owner_id, pool_ids_in)
            else:
                prows = await conn.fetch(
                    "SELECT id FROM user_proxies WHERE owner_id=$1 AND is_active=TRUE", owner_id)
            pool_available = [int(r["id"]) for r in prows]
            blocked_rows = await conn.fetch(
                "SELECT DISTINCT proxy_id FROM tg_accounts "
                "WHERE owner_id=$1 AND proxy_id = ANY($2::int[]) "
                "AND NOT (id = ANY($3::bigint[]))",
                owner_id, pool_available, free_ids or [0])
            blocked = {int(r["proxy_id"]) for r in blocked_rows if r["proxy_id"] is not None}
            pool_final = [p for p in pool_available if p not in blocked]

            # 3. План (чистый, инъективный) + 4. применение с гвардом busy.
            result = plan_rotation(free, pool_final)
            applied = 0
            for ch in result["plan"]:
                if ch["new"] == ch["old"]:
                    continue
                res = await conn.execute(
                    "UPDATE tg_accounts SET proxy_id=$1 WHERE id=$2 AND owner_id=$3 "
                    "AND COALESCE(in_operation,FALSE)=FALSE",
                    ch["new"], ch["account_id"], owner_id)
                if isinstance(res, str) and res.endswith(" 1"):
                    applied += 1
    return {
        "ok": True, "rotated": applied,
        "skipped_busy": skipped_busy,
        "skipped_no_proxy": result["skipped_no_proxy"],
        "accounts": len(group_ids), "pool": len(pool_final),
    }
