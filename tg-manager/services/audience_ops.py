"""Операции с базами аудитории: объединение / исключение / дедуп.

База = запуск парсера (`parser_runs.id`), участники — `parsed_audiences` с этим
`parse_run_id`. Операции НЕ трогают Telegram/аккаунты (чистые SQL set-операции),
поэтому это НЕ рисковая массовая операция: без канарейки/FloodWait, выполняется
синхронно.

Все операции НЕДЕСТРУКТИВНЫ: создают НОВУЮ производную базу (новый `parser_runs`),
исходные не меняют — результат можно отменить простым удалением новой базы.

Инвариант уникальности `parsed_audiences (owner_id, source_id, tg_user_id)`:
производные строки пишутся с `source_id = <новый run_id>`, поэтому каждый
`tg_user_id` в производной базе уникален и не конфликтует с исходными базами.
Дедуп по `tg_user_id` делается через `DISTINCT ON (tg_user_id)`.
"""

from __future__ import annotations

import logging

import asyncpg

log = logging.getLogger(__name__)

# Колонки-носители данных пользователя, переносимые в производную базу as-is.
# source_id/parse_run_id проставляются новым run_id (см. инвариант в docstring).
_COPY_COLS = (
    "username", "first_name", "last_name", "phone",
    "is_premium", "is_bot", "last_seen_days", "is_active",
    "geo_country", "geo_city",
)


class AudienceOpError(ValueError):
    """Некорректные входные данные операции (пустой список баз, чужая база и т.п.)."""


async def _owned_run_ids(pool: asyncpg.Pool, owner_id: int, run_ids: list[int]) -> list[int]:
    """Отфильтровать run_ids, реально принадлежащие owner_id (скоуп-гард против IDOR)."""
    if not run_ids:
        return []
    rows = await pool.fetch(
        "SELECT id FROM parser_runs WHERE owner_id=$1 AND id = ANY($2::bigint[])",
        owner_id, [int(r) for r in run_ids],
    )
    return [r["id"] for r in rows]


async def _create_derived_run(
    pool: asyncpg.Pool, owner_id: int, op: str, label: str
) -> int:
    """Создать пустой производный запуск-базу (source_type='op')."""
    row = await pool.fetchrow(
        """INSERT INTO parser_runs
               (owner_id, source_type, source_ref, parse_type, status, started_at)
           VALUES ($1, 'op', $2, $3, 'running', now())
           RETURNING id""",
        owner_id, label[:200], op,
    )
    return row["id"]


async def _finalize_run(pool: asyncpg.Pool, run_id: int, count: int) -> None:
    await pool.execute(
        "UPDATE parser_runs SET status='done', total_found=$2, total_saved=$2, "
        "finished_at=now() WHERE id=$1",
        run_id, count,
    )


def _insert_select_sql(where_clause: str) -> str:
    """INSERT ... SELECT DISTINCT ON (tg_user_id) с переносом _COPY_COLS.

    where_clause подставляется в WHERE (использует $1=owner_id и доп. плейсхолдеры).
    $LAST-плейсхолдер нового run_id передаётся вызывающим как последний аргумент.
    """
    copy = ", ".join(_COPY_COLS)
    return f"""
        INSERT INTO parsed_audiences
            (owner_id, source_type, source_id, source_title, parse_run_id,
             tg_user_id, {copy}, parsed_at)
        SELECT DISTINCT ON (tg_user_id)
            owner_id, 'op', $RUNID, $LABEL, $RUNID,
            tg_user_id, {copy}, now()
        FROM parsed_audiences
        WHERE {where_clause}
        ORDER BY tg_user_id, parsed_at DESC
    """


async def merge_runs(
    pool: asyncpg.Pool, owner_id: int, run_ids: list[int], label: str | None = None
) -> dict:
    """Объединить несколько баз в новую (UNION уникальных tg_user_id)."""
    owned = await _owned_run_ids(pool, owner_id, run_ids)
    if len(owned) < 2:
        raise AudienceOpError("для объединения нужны минимум 2 ваши базы")
    label = label or f"Объединение {len(owned)} баз"
    new_run = await _create_derived_run(pool, owner_id, "merge", label)
    sql = _insert_select_sql(
        "owner_id=$1 AND parse_run_id = ANY($2::bigint[])"
    ).replace("$RUNID", "$3").replace("$LABEL", "$4")
    res = await pool.execute(sql, owner_id, owned, new_run, label[:200])
    count = int(str(res).split()[-1]) if "INSERT" in str(res) else 0
    await _finalize_run(pool, new_run, count)
    log.info("audience_ops.merge owner=%s runs=%s → run=%s count=%d",
             owner_id, owned, new_run, count)
    return {"run_id": new_run, "count": count, "sources": owned}


async def exclude_runs(
    pool: asyncpg.Pool,
    owner_id: int,
    base_run_id: int,
    minus_run_ids: list[int],
    label: str | None = None,
) -> dict:
    """Новая база = участники base_run_id, которых НЕТ ни в одной из minus-баз.

    «Минус-база»: исключить уже-приглашённых/уже-охваченных перед новой рассылкой.
    """
    owned_base = await _owned_run_ids(pool, owner_id, [base_run_id])
    if not owned_base:
        raise AudienceOpError("основная база не найдена или не ваша")
    owned_minus = await _owned_run_ids(pool, owner_id, minus_run_ids)
    if not owned_minus:
        raise AudienceOpError("не выбрано ни одной корректной минус-базы")
    label = label or "Результат исключения"
    new_run = await _create_derived_run(pool, owner_id, "exclude", label)
    sql = _insert_select_sql(
        "owner_id=$1 AND parse_run_id=$2 "
        "AND tg_user_id NOT IN ("
        "  SELECT tg_user_id FROM parsed_audiences "
        "  WHERE owner_id=$1 AND parse_run_id = ANY($3::bigint[])"
        ")"
    ).replace("$RUNID", "$4").replace("$LABEL", "$5")
    res = await pool.execute(
        sql, owner_id, owned_base[0], owned_minus, new_run, label[:200])
    count = int(str(res).split()[-1]) if "INSERT" in str(res) else 0
    await _finalize_run(pool, new_run, count)
    log.info("audience_ops.exclude owner=%s base=%s minus=%s → run=%s count=%d",
             owner_id, owned_base[0], owned_minus, new_run, count)
    return {"run_id": new_run, "count": count,
            "base": owned_base[0], "minus": owned_minus}


async def dedup_run(
    pool: asyncpg.Pool, owner_id: int, run_id: int, label: str | None = None
) -> dict:
    """Новая база = уникальные по tg_user_id участники одной базы (снятие дублей)."""
    owned = await _owned_run_ids(pool, owner_id, [run_id])
    if not owned:
        raise AudienceOpError("база не найдена или не ваша")
    label = label or "Дедуп базы"
    new_run = await _create_derived_run(pool, owner_id, "dedup", label)
    sql = _insert_select_sql(
        "owner_id=$1 AND parse_run_id=$2"
    ).replace("$RUNID", "$3").replace("$LABEL", "$4")
    res = await pool.execute(sql, owner_id, owned[0], new_run, label[:200])
    count = int(str(res).split()[-1]) if "INSERT" in str(res) else 0
    await _finalize_run(pool, new_run, count)
    log.info("audience_ops.dedup owner=%s run=%s → run=%s count=%d",
             owner_id, owned[0], new_run, count)
    return {"run_id": new_run, "count": count, "source": owned[0]}
