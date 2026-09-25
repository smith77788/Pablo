"""
Audience Parser Framework — извлечение аудитории из каналов/групп.

Поддерживает:
- participants: все участники канала/группы
- active: пользователи с недавними сообщениями
- commenters: авторы комментариев к постам
- reaction_givers: пользователи оставившие реакции

Результаты сохраняются в parsed_audiences (дедупликация по owner+source+user_id).
История запусков — в parser_runs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from services.logger import log_exc_swallow
from services import infra_memory

import asyncpg

log = logging.getLogger(__name__)

# Потолок на ОТДЕЛЬНЫЙ запрос к Telegram. Значение то же, что в
# account_manager._OP_TIMEOUT, но константа своя: account_manager здесь
# импортируется ЛОКАЛЬНО, по функциям (защита от кольца импортов), и в
# _collect_and_save_senders его в области видимости нет вовсе.
#
# Коннект ограничен своим таймаутом, а мёртвый прокси чаще отдаёт не отказ,
# а half-open сокет: TCP установлен, ответа нет и не будет. Без потолка
# разбор аудитории вешался навсегда, держа слот операции и аккаунт.
_OP_TIMEOUT = 45



# ── Чистые хелперы (тестируемые, без БД/Telethon) ─────────────────────────

def normalize_source_ref(raw: str) -> str:
    """Нормализует ссылку/username источника к «голому» username или id.

    Пользователи вставляют что угодно: `@name`, `https://t.me/name`,
    `t.me/name?after=123`, `telegram.me/name/456`. Раньше на входе парсера был
    только `.lstrip("@")` — ссылка `https://t.me/name` уходила как есть и не
    резолвилась. Здесь убираем протокол, хосты t.me/telegram.me, ведущий '@',
    query-хвост и путь после username. Приватные инвайты (`+HASH`, `joinchat/…`)
    сохраняем как есть (это не username).
    """
    s = (raw or "").strip()
    if not s:
        return ""
    for pref in ("https://", "http://"):
        if s.lower().startswith(pref):
            s = s[len(pref):]
    low = s.lower()
    for host in ("t.me/", "telegram.me/", "telegram.dog/"):
        idx = low.find(host)
        if idx != -1:
            s = s[idx + len(host):]
            break
    s = s.lstrip("@")
    # приватные инвайт-ссылки не трогаем (это хэш, не username)
    if s.startswith("+") or s.lower().startswith("joinchat/"):
        return s.split("?", 1)[0].strip()
    for sep in ("?", "/"):
        if sep in s:
            s = s.split(sep, 1)[0]
    return s.strip()


def status_to_days(status) -> int | None:
    """Telethon User.status → примерное число дней с последнего онлайна (Last Seen).

    Telegram отдаёт статус вёдрами (точное время скрыто privacy-настройкой):
    online→0, recently→~2, last_week→~7, last_month→~30, offline→точная дата.
    None/empty/скрыт → None (неизвестно). Используется для фильтра «активные».
    """
    if status is None:
        return None
    name = type(status).__name__
    if name == "UserStatusOnline":
        return 0
    if name == "UserStatusRecently":
        return 2
    if name == "UserStatusLastWeek":
        return 7
    if name == "UserStatusLastMonth":
        return 30
    if name == "UserStatusOffline":
        was = getattr(status, "was_online", None)
        if was is not None:
            try:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                delta = now - (was if was.tzinfo else was.replace(tzinfo=timezone.utc))
                return max(0, delta.days)
            except Exception:  # noqa: BLE001 — битая дата → неизвестно
                return None
    return None


# ── Чистые хелперы экспорта аудитории (тестируемые, без БД/Telethon) ───────
# Форматы для многоформатного экспорта parsed_audiences. CSV делает эндпоинт
# через _csv_resp; TXT/JSON — эти хелперы (паритет Telegram Expert по экспорту).

def audience_to_txt(users: list[dict]) -> str:
    """Экспорт в TXT: одна строка на пользователя (@username либо tg_user_id)."""
    lines: list[str] = []
    for u in users:
        un = (u.get("username") or "").strip()
        lines.append("@" + un if un else str(u.get("tg_user_id") or ""))
    return "\n".join(lines)


def audience_to_json(users: list[dict]) -> str:
    """Экспорт в JSON (список объектов)."""
    import json as _json

    return _json.dumps(list(users), ensure_ascii=False, default=str, indent=2)


# Сколько ждём флуд на месте. Дольше — обрываем разбор и честно говорим об
# этом: продолжать раньше срока значит получить следующий FloodWait длиннее
# предыдущего и подвести аккаунт под спамблок.
_FLOOD_INLINE_MAX_S = 120


def flood_seconds(exc: BaseException) -> int | None:
    """Сколько Telegram просит подождать, или None если это не пауза.

    Псевдоним общего разбора из flood_engine: второй источник правды о паузах
    Telegram продукту не нужен.
    """
    from services import flood_engine as _fe

    return _fe.flood_seconds(exc)


async def _record_parse_flood(pool, account_id, seconds: int) -> None:
    """Флуд обязан попасть в общий пульс здоровья аккаунта.

    По нему остальные подсистемы понижают темп и держат аккаунт в покое.
    Раньше разбор переживал флуд в одиночку, и для продукта аккаунт оставался
    «спокойным» — следующая операция шла к нему как ни в чём не бывало.
    """
    if not account_id:
        return
    try:
        from services import flood_engine as _fe

        await _fe.record_flood(pool, int(account_id), int(seconds), "parse")
    except Exception:
        log_exc_swallow(log, "parser: FloodWait не записан в пульс здоровья")


async def _get_best_account(pool: asyncpg.Pool, owner_id: int) -> dict | None:
    from services.flood_engine import get_best_account

    return await get_best_account(pool, owner_id, action_type="parse")


async def _create_run(
    pool: asyncpg.Pool,
    owner_id: int,
    source_type: str,
    source_ref: str,
    parse_type: str,
    account_id: int | None,
) -> int:
    row = await pool.fetchrow(
        """INSERT INTO parser_runs(owner_id, source_type, source_ref, parse_type, account_id)
           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
        owner_id,
        source_type,
        source_ref,
        parse_type,
        account_id,
    )
    return row["id"]


async def _update_run(
    pool: asyncpg.Pool,
    run_id: int,
    status: str,
    total_found: int = 0,
    total_saved: int = 0,
    error: str | None = None,
) -> None:
    await pool.execute(
        """UPDATE parser_runs
           SET status=$1, total_found=$2, total_saved=$3, error=$4, finished_at=NOW()
           WHERE id=$5""",
        status,
        total_found,
        total_saved,
        error,
        run_id,
    )


async def _save_users(
    pool: asyncpg.Pool,
    owner_id: int,
    run_id: int,
    source_type: str,
    source_id: int,
    source_title: str,
    source_username: str,
    users: list[dict],
) -> int:
    from database import db as _db
    saved = 0
    for u in users:
        uid = u["id"]
        entity_type = "bot" if u.get("bot") else "user"
        display_name = " ".join(
            p for p in [u.get("first_name"), u.get("last_name")] if p
        ).strip() or None
        # Last Seen: last_seen_days из статуса; активным считаем ≤7 дней (online/
        # recently/last_week) — база для фильтра «активная аудитория».
        last_seen_days = u.get("last_seen_days")
        is_active = None if last_seen_days is None else (last_seen_days <= 7)
        try:
            result = await pool.execute(
                """INSERT INTO parsed_audiences(
                       owner_id, source_type, source_id, source_title, source_username,
                       parse_run_id, tg_user_id, username, first_name, last_name,
                       is_premium, is_bot, last_seen_days, is_active, parsed_at
                   ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,NOW())
                   ON CONFLICT (owner_id, source_id, tg_user_id) DO UPDATE
                   SET parse_run_id=$6, username=$8, first_name=$9, last_name=$10,
                       is_premium=$11,
                       last_seen_days=COALESCE(EXCLUDED.last_seen_days, parsed_audiences.last_seen_days),
                       is_active=COALESCE(EXCLUDED.is_active, parsed_audiences.is_active),
                       parsed_at=NOW()""",
                owner_id,
                source_type,
                source_id,
                source_title,
                source_username,
                run_id,
                uid,
                u.get("username"),
                u.get("first_name"),
                u.get("last_name"),
                bool(u.get("premium")),
                bool(u.get("bot")),
                last_seen_days,
                is_active,
            )
            if "INSERT" in str(result):
                saved += 1
        except Exception as e:
            log.debug("parser save user %s: %s", uid, e)
        # Radar: record sighting with the source chat context
        await _db.record_entity_sighting(pool, uid, entity_type, chat_id=source_id)
        await _db.record_name_snapshot(pool, uid, entity_type, u.get("username"), display_name)
    return saved


async def parse_members(
    pool: asyncpg.Pool,
    owner_id: int,
    source_ref: str,
    limit: int = 5000,
    progress_cb: Optional[Callable[[int, int], Any]] = None,
) -> dict:
    """
    Парсинг участников канала/группы.
    progress_cb(current, total) вызывается каждые 200 пользователей.
    Возвращает {'run_id', 'total_found', 'total_saved', 'status'}.
    """
    from services import account_manager
    from telethon.tl.functions.channels import GetParticipantsRequest
    from telethon.tl.types import ChannelParticipantsSearch

    acc = await _get_best_account(pool, owner_id)
    if not acc:
        return {"status": "error", "error": "Нет доступных аккаунтов"}

    run_id = await _create_run(
        pool, owner_id, "channel", source_ref, "members", acc["id"]
    )

    client = account_manager._make_client(acc["session_str"], acc)
    t0_parse = time.monotonic()
    total_found = 0
    total_saved = 0
    flood_wait = 0
    source_id = 0
    source_title = source_ref
    source_username = source_ref.lstrip("@")

    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        try:
            entity = await asyncio.wait_for(client.get_entity(source_ref), timeout=_OP_TIMEOUT)
            source_id = entity.id
            source_title = getattr(entity, "title", source_ref)
            source_username = getattr(entity, "username", "") or source_ref.lstrip("@")
        except Exception as e:
            await _update_run(
                pool, run_id, "failed", error=f"Не удалось получить сущность: {e}"
            )
            infra_memory.record_account_op(
                acc["id"],
                "parse",
                False,
                str(e)[:100],
                duration_s=time.monotonic() - t0_parse,
            )
            return {"status": "error", "error": str(e), "run_id": run_id}

        offset = 0
        batch_size = 200
        while total_found < limit:
            try:
                result = await asyncio.wait_for(client(
                    GetParticipantsRequest(
                        entity,
                        ChannelParticipantsSearch(""),
                        offset=offset,
                        limit=min(batch_size, limit - total_found),
                        hash=0,
                    )
                ), timeout=_OP_TIMEOUT)
                if not result.users:
                    break

                users = [
                    {
                        "id": u.id,
                        "username": u.username,
                        "first_name": u.first_name,
                        "last_name": u.last_name,
                        "premium": getattr(u, "premium", False),
                        "bot": u.bot,
                        "last_seen_days": status_to_days(getattr(u, "status", None)),
                    }
                    for u in result.users
                    if not u.deleted
                ]

                batch_saved = await _save_users(
                    pool,
                    owner_id,
                    run_id,
                    "channel",
                    source_id,
                    source_title,
                    source_username,
                    users,
                )
                total_found += len(users)
                total_saved += batch_saved
                offset += len(result.users)

                if progress_cb:
                    try:
                        await progress_cb(
                            total_found, min(limit, getattr(result, "count", limit))
                        )
                    except Exception:
                        log_exc_swallow(log, "Сбой progress_cb в parser")

                if len(result.users) < batch_size:
                    break

                await asyncio.sleep(1.5)  # Anti-flood pause

            except Exception as e:
                _fw = flood_seconds(e)
                if _fw is not None:
                    await _record_parse_flood(pool, acc["id"], _fw)
                    if _fw > _FLOOD_INLINE_MAX_S:
                        log.info(
                            "parser: FloodWait %dс дольше порога — разбор оборван", _fw)
                        flood_wait = _fw
                        break
                    log.info("parser FloodWait %ds, sleeping", _fw)
                    await asyncio.sleep(_fw + 5)
                    continue
                log.warning("parser GetParticipants error: %s", e)
                break

    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой disconnect клиента в parser")

    status = "done" if total_found > 0 else "empty"
    # Оборванный флудом разбор — НЕ полный результат. Раньше он выглядел как
    # обычное завершение, и владелец считал, что в источнике ровно столько
    # людей, сколько успели собрать.
    _flood_note = (
        f"Разбор остановлен: Telegram просит подождать {flood_wait} с. "
        f"Собрано {total_found} — это не вся аудитория, повторите позже."
        if flood_wait else None
    )
    await _update_run(pool, run_id, status, total_found, total_saved,
                      error=_flood_note)
    _parse_dur = time.monotonic() - t0_parse
    if total_found > 0:
        infra_memory.record_account_op(acc["id"], "parse", True, duration_s=_parse_dur)
    else:
        infra_memory.record_account_op(
            acc["id"], "parse", False, "no_users_found", duration_s=_parse_dur
        )
    return {
        "run_id": run_id,
        "status": status,
        "total_found": total_found,
        "total_saved": total_saved,
        "source_title": source_title,
        "flood_wait": flood_wait,
        "partial": bool(flood_wait),
    }


async def _collect_and_save_senders(
    pool: asyncpg.Pool,
    owner_id: int,
    run_id: int,
    client: Any,
    entity: Any,
    *,
    source_type: str,
    source_id: int,
    source_title: str,
    source_username: str,
    days_back: int,
    limit: int,
    scan_cap: int = 5000,
    progress_cb: Optional[Callable[[int, int], Any]] = None,
    account_id: int | None = None,
) -> tuple[int, int, int]:
    """Итерирует сообщения ``entity`` и сохраняет уникальных отправителей.

    Общая логика для парсинга активных участников группы и комментаторов
    (сообщения в привязанной к каналу группе обсуждений). Возвращает
    (total_found, total_saved, flood_wait).

    `flood_wait` — сколько Telegram попросил подождать, если разбор пришлось
    оборвать. Раньше `get_entity` на каждого отправителя стоял под голым
    `except Exception`, то есть FloodWait просто проглатывался: цикл шёл
    дальше и продолжал долбить Telegram под действующим ограничением. Так
    аккаунт и зарабатывает спамблок вместо паузы.
    """
    from datetime import datetime, timedelta, timezone

    seen_ids: set[int] = set()
    total_found = 0
    total_saved = 0
    flood_wait = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    async for msg in client.iter_messages(entity, limit=scan_cap):
        if msg.date < cutoff:
            break
        if not msg.sender_id or msg.sender_id in seen_ids:
            continue
        seen_ids.add(msg.sender_id)

        user = None
        try:
            user = await asyncio.wait_for(client.get_entity(msg.sender_id), timeout=_OP_TIMEOUT)
        except Exception as exc:
            _fw = flood_seconds(exc)
            if _fw is not None:
                await _record_parse_flood(pool, account_id, _fw)
                if _fw > _FLOOD_INLINE_MAX_S:
                    log.info(
                        "parser: FloodWait %dс дольше порога — сбор оборван", _fw)
                    flood_wait = _fw
                    break
                log.info("parser: FloodWait %dс при сборе отправителей", _fw)
                await asyncio.sleep(_fw + 5)
                continue
            log_exc_swallow(log, "Сбой get_entity в parser", sender_id=msg.sender_id)

        if user:
            users_batch = [
                {
                    "id": user.id,
                    "username": getattr(user, "username", None),
                    "first_name": getattr(user, "first_name", None),
                    "last_name": getattr(user, "last_name", None),
                    "premium": getattr(user, "premium", False),
                    "bot": getattr(user, "bot", False),
                    "last_seen_days": status_to_days(getattr(user, "status", None)),
                }
            ]
            saved = await _save_users(
                pool,
                owner_id,
                run_id,
                source_type,
                source_id,
                source_title,
                source_username,
                users_batch,
            )
            total_found += 1
            total_saved += saved

        if total_found >= limit:
            break

        # Progress: first update at 1 user found, then every 50 after that.
        # Without this, parses returning <50 users show no progress at all.
        if total_found == 1 or total_found % 50 == 0:
            if progress_cb:
                try:
                    await progress_cb(total_found, limit)
                except Exception:
                    log_exc_swallow(log, "Сбой progress_cb в parser")
            await asyncio.sleep(0.5)

    return total_found, total_saved, flood_wait


async def parse_active_users(
    pool: asyncpg.Pool,
    owner_id: int,
    source_ref: str,
    days_back: int = 30,
    limit: int = 2000,
    progress_cb: Optional[Callable[[int, int], Any]] = None,
) -> dict:
    """
    Парсинг активных пользователей: те, кто писал в группе за последние N дней.
    Работает только для супергрупп (не каналов).
    """
    from services import account_manager

    acc = await _get_best_account(pool, owner_id)
    if not acc:
        return {"status": "error", "error": "Нет доступных аккаунтов"}

    run_id = await _create_run(pool, owner_id, "group", source_ref, "active", acc["id"])

    client = account_manager._make_client(acc["session_str"], acc)
    t0_parse = time.monotonic()
    total_found = 0
    total_saved = 0
    source_id = 0
    source_title = source_ref

    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        try:
            entity = await asyncio.wait_for(client.get_entity(source_ref), timeout=_OP_TIMEOUT)
            source_id = entity.id
            source_title = getattr(entity, "title", source_ref)
        except Exception as e:
            await _update_run(pool, run_id, "failed", error=str(e))
            infra_memory.record_account_op(
                acc["id"],
                "parse",
                False,
                str(e)[:100],
                duration_s=time.monotonic() - t0_parse,
            )
            return {"status": "error", "error": str(e), "run_id": run_id}

        total_found, total_saved, flood_wait = await _collect_and_save_senders(
            pool,
            owner_id,
            run_id,
            client,
            entity,
            source_type="group",
            source_id=source_id,
            source_title=source_title,
            source_username=source_ref.lstrip("@"),
            days_back=days_back,
            limit=limit,
            progress_cb=progress_cb,
            account_id=acc["id"],
        )

    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой disconnect клиента в parser")

    status = "done" if total_found > 0 else "empty"
    # Оборванный флудом разбор — НЕ полный результат. Раньше он выглядел как
    # обычное завершение, и владелец считал, что в источнике ровно столько
    # людей, сколько успели собрать.
    _flood_note = (
        f"Разбор остановлен: Telegram просит подождать {flood_wait} с. "
        f"Собрано {total_found} — это не вся аудитория, повторите позже."
        if flood_wait else None
    )
    await _update_run(pool, run_id, status, total_found, total_saved,
                      error=_flood_note)
    _parse_dur = time.monotonic() - t0_parse
    if total_found > 0:
        infra_memory.record_account_op(acc["id"], "parse", True, duration_s=_parse_dur)
    else:
        infra_memory.record_account_op(
            acc["id"], "parse", False, "no_users_found", duration_s=_parse_dur
        )
    return {
        "run_id": run_id,
        "status": status,
        "total_found": total_found,
        "total_saved": total_saved,
        "source_title": source_title,
        "flood_wait": flood_wait,
        "partial": bool(flood_wait),
    }


async def parse_commenters(
    pool: asyncpg.Pool,
    owner_id: int,
    source_ref: str,
    days_back: int = 90,
    limit: int = 2000,
    progress_cb: Optional[Callable[[int, int], Any]] = None,
) -> dict:
    """
    Парсинг комментаторов канала: авторы сообщений в привязанной к каналу
    группе обсуждений. Если источник сам является группой — парсит её
    сообщения напрямую (эквивалент «активных»).
    """
    from services import account_manager
    from telethon.tl.functions.channels import GetFullChannelRequest

    acc = await _get_best_account(pool, owner_id)
    if not acc:
        return {"status": "error", "error": "Нет доступных аккаунтов"}

    run_id = await _create_run(
        pool, owner_id, "comments", source_ref, "comments", acc["id"]
    )

    client = account_manager._make_client(acc["session_str"], acc)
    t0_parse = time.monotonic()
    total_found = 0
    total_saved = 0
    source_title = source_ref
    source_username = source_ref.lstrip("@")

    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        try:
            entity = await asyncio.wait_for(client.get_entity(source_ref), timeout=_OP_TIMEOUT)
            source_title = getattr(entity, "title", source_ref)
            source_username = getattr(entity, "username", "") or source_ref.lstrip("@")
        except Exception as e:
            await _update_run(pool, run_id, "failed", error=str(e))
            infra_memory.record_account_op(
                acc["id"], "parse", False, str(e)[:100],
                duration_s=time.monotonic() - t0_parse,
            )
            return {"status": "error", "error": str(e), "run_id": run_id}

        # Ищем привязанную группу обсуждений. Сообщения-комментарии живут в ней.
        target = entity
        try:
            full = await asyncio.wait_for(client(GetFullChannelRequest(entity)), timeout=_OP_TIMEOUT)
            linked_id = getattr(full.full_chat, "linked_chat_id", None)
            if linked_id:
                try:
                    target = await asyncio.wait_for(client.get_entity(linked_id), timeout=_OP_TIMEOUT)
                except Exception:
                    log_exc_swallow(
                        log, "Не удалось получить группу обсуждений", linked_id=linked_id
                    )
        except Exception:
            # Источник — обычная группа без обсуждений: парсим её напрямую.
            log_exc_swallow(log, "GetFullChannel недоступен, парсим источник напрямую")

        # source_id — id той сущности, из которой реально собираем (для дедупа),
        # но заголовок/username оставляем исходного канала для узнаваемости.
        source_id = getattr(target, "id", 0) or 0

        total_found, total_saved, flood_wait = await _collect_and_save_senders(
            pool,
            owner_id,
            run_id,
            client,
            target,
            source_type="comments",
            source_id=source_id,
            source_title=source_title,
            source_username=source_username,
            days_back=days_back,
            limit=limit,
            progress_cb=progress_cb,
            account_id=acc["id"],
        )

    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой disconnect клиента в parser")

    status = "done" if total_found > 0 else "empty"
    # Оборванный флудом разбор — НЕ полный результат. Раньше он выглядел как
    # обычное завершение, и владелец считал, что в источнике ровно столько
    # людей, сколько успели собрать.
    _flood_note = (
        f"Разбор остановлен: Telegram просит подождать {flood_wait} с. "
        f"Собрано {total_found} — это не вся аудитория, повторите позже."
        if flood_wait else None
    )
    await _update_run(pool, run_id, status, total_found, total_saved,
                      error=_flood_note)
    _parse_dur = time.monotonic() - t0_parse
    if total_found > 0:
        infra_memory.record_account_op(acc["id"], "parse", True, duration_s=_parse_dur)
    else:
        infra_memory.record_account_op(
            acc["id"], "parse", False, "no_users_found", duration_s=_parse_dur
        )
    return {
        "run_id": run_id,
        "status": status,
        "total_found": total_found,
        "total_saved": total_saved,
        "source_title": source_title,
        "flood_wait": flood_wait,
        "partial": bool(flood_wait),
    }


async def get_parsed_audience(
    pool: asyncpg.Pool,
    owner_id: int,
    source_id: int | None = None,
    run_id: int | None = None,
    offset: int = 0,
    limit: int = 200,
    active_only: bool = False,
    max_last_seen_days: int | None = None,
) -> list[dict]:
    """Получить сохранённую аудиторию с фильтрами.

    max_last_seen_days: оставить только тех, кто был в сети не позже N дней назад
    (Last Seen фильтр) — пользователи с неизвестным статусом отсекаются.
    """
    conditions = ["owner_id=$1"]
    params: list = [owner_id]
    p = 2

    if source_id:
        conditions.append(f"source_id=${p}")
        params.append(source_id)
        p += 1

    if run_id:
        conditions.append(f"parse_run_id=${p}")
        params.append(run_id)
        p += 1

    if active_only:
        conditions.append("is_active=TRUE")

    if max_last_seen_days is not None:
        conditions.append(f"last_seen_days IS NOT NULL AND last_seen_days <= ${p}")
        params.append(int(max_last_seen_days))
        p += 1

    where = " AND ".join(conditions)
    rows = await pool.fetch(
        f"SELECT * FROM parsed_audiences WHERE {where} "
        f"ORDER BY parsed_at DESC OFFSET ${p} LIMIT ${p + 1}",
        *params,
        offset,
        limit,
    )
    return [dict(r) for r in rows]


async def get_run_history(
    pool: asyncpg.Pool, owner_id: int, limit: int = 20
) -> list[dict]:
    rows = await pool.fetch(
        """SELECT id, source_type, source_ref, parse_type, status,
                  total_found, total_saved, started_at, finished_at
           FROM parser_runs WHERE owner_id=$1
           ORDER BY started_at DESC LIMIT $2""",
        owner_id,
        limit,
    )
    return [dict(r) for r in rows]


async def delete_audience(
    pool: asyncpg.Pool,
    owner_id: int,
    source_id: int | None = None,
    run_id: int | None = None,
) -> int:
    """Удалить спарсенную аудиторию. Возвращает число удалённых строк."""
    if run_id:
        result = await pool.execute(
            "DELETE FROM parsed_audiences WHERE owner_id=$1 AND parse_run_id=$2",
            owner_id,
            run_id,
        )
    elif source_id:
        result = await pool.execute(
            "DELETE FROM parsed_audiences WHERE owner_id=$1 AND source_id=$2",
            owner_id,
            source_id,
        )
    else:
        result = await pool.execute(
            "DELETE FROM parsed_audiences WHERE owner_id=$1", owner_id
        )
    try:
        return int(str(result).split()[-1])
    except Exception:
        log_exc_swallow(
            log, "Не удалось распарсить количество удалённых строк аудитории"
        )
        return 0
