"""Замер позиций каналов/чатов в поиске Telegram по ключам.

Трекинг позиций был только для ботов. Каналы/чаты держатся в топе теми же
ключами (имя + @username), но их место в выдаче никто не мерил — а без замера
нельзя понять, работает ли SEO-массив и кого дожимать посевом. Здесь замер для
каналов/чатов: какие ключи отслеживаем и какая позиция во времени.

Ключи заводятся автоматически Фабрикой при создании (имя ресурса = его запрос) и
вручную. Замер — поиск глазами живого аккаунта (search_global_ranked, РЕАЛЬНАЯ
выдача единым списком) и поиск НАШЕГО канала в этой сквозной выдаче.

Здесь чистое ядро (сопоставление результата с нашим каналом — его и проверяют
тесты) и тонкие обёртки над БД/поиском. Важен ТРЕНД (поднялись/просели), а не
абсолют: глобальная выдача шумит, но движение по времени показательно.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# Сколько ключей проверяем за один проход (чтобы не жечь аккаунты поиском).
MAX_KEYWORDS_PER_RUN = 60
# Пользователю в поиске видны только верхние позиции (топ-10) — это и есть цель.
# Берём небольшой запас (чуть больше 10), чтобы различать «в топе» и «на подходе»
# для авто-усиления; всё за топ-10 в интерфейсе показываем как «вне топ-10».
TOP_N = 10
SEARCH_LIMIT = 15


def _norm_id(cid) -> int:
    """Нормализовать channel_id: снять знак и префикс -100…, вернуть чистый id."""
    try:
        raw = str(abs(int(cid)))
    except (TypeError, ValueError):
        return 0
    if raw.startswith("100") and len(raw) > 10:
        raw = raw[3:]
    return int(raw) if raw else 0


def _norm_uname(u) -> str:
    return str(u or "").lstrip("@").lower()


def normalize_keyword(text: str) -> str:
    """Ключ-запрос из имени ресурса: схлопнуть пробелы, нижний регистр, обрезать."""
    return " ".join(str(text or "").split()).lower()[:64]


def find_position(results: list[dict], *, channel_id=None, username: str = "") -> int | None:
    """Найти позицию НАШЕГО канала в выдаче поиска. Чистая функция.

    Сопоставляем по channel_id (нормализованный) или @username (без регистра).
    Возвращает position из результата или None, если нас нет в выдаче.
    """
    tgt_id = _norm_id(channel_id) if channel_id is not None else 0
    tgt_u = _norm_uname(username)
    for r in results or []:
        if tgt_id and _norm_id(r.get("channel_id")) == tgt_id:
            return r.get("position")
        if tgt_u and _norm_uname(r.get("username")) == tgt_u:
            return r.get("position")
    return None


# ── Обёртки над БД (тонкие; логика — выше) ─────────────────────────────────

async def add_keyword(pool, owner_id: int, channel_id: int, keyword: str) -> bool:
    kw = normalize_keyword(keyword)
    if not kw:
        return False
    try:
        await pool.execute(
            "INSERT INTO channel_tracked_keywords(owner_id, channel_id, keyword) "
            "VALUES($1,$2,$3) ON CONFLICT (channel_id, keyword) DO UPDATE SET is_active=TRUE",
            owner_id, int(channel_id), kw)
        return True
    except Exception:
        log.debug("channel_ranking.add_keyword failed", exc_info=True)
        return False


async def register_for_channel(pool, owner_id: int, channel_id: int, title: str) -> None:
    """Автозаведение ключа при создании ресурса: имя ресурса = его поисковый запрос."""
    kw = normalize_keyword(title)
    if kw:
        await add_keyword(pool, owner_id, channel_id, kw)


async def remove_keyword(pool, owner_id: int, keyword_id: int) -> bool:
    try:
        row = await pool.fetchrow(
            "DELETE FROM channel_tracked_keywords WHERE id=$1 AND owner_id=$2 RETURNING id",
            int(keyword_id), owner_id)
        return bool(row)
    except Exception:
        return False


async def record(pool, keyword_id: int, channel_id: int, position) -> None:
    try:
        await pool.execute(
            "INSERT INTO channel_search_rankings(keyword_id, channel_id, position) "
            "VALUES($1,$2,$3)", int(keyword_id), int(channel_id),
            int(position) if position is not None else None)
    except Exception:
        log.debug("channel_ranking.record failed", exc_info=True)


async def list_positions(pool, owner_id: int) -> list[dict]:
    """Ключи владельца с последней и предыдущей позицией (для тренда) + имя канала."""
    try:
        rows = await pool.fetch(
            """
            SELECT k.id, k.channel_id, k.keyword,
                   COALESCE(mc.title, mc.username, k.channel_id::text) AS title,
                   mc.username,
                   (SELECT position FROM channel_search_rankings r
                      WHERE r.keyword_id=k.id ORDER BY checked_at DESC LIMIT 1) AS pos,
                   (SELECT checked_at FROM channel_search_rankings r
                      WHERE r.keyword_id=k.id ORDER BY checked_at DESC LIMIT 1) AS checked_at,
                   (SELECT position FROM channel_search_rankings r
                      WHERE r.keyword_id=k.id ORDER BY checked_at DESC OFFSET 1 LIMIT 1) AS prev_pos
              FROM channel_tracked_keywords k
              LEFT JOIN managed_channels mc
                     ON mc.channel_id=k.channel_id AND mc.owner_id=k.owner_id
             WHERE k.owner_id=$1 AND k.is_active
             ORDER BY title, k.keyword
             LIMIT 500
            """, owner_id)
        return [dict(r) for r in (rows or [])]
    except Exception:
        log.debug("channel_ranking.list_positions failed", exc_info=True)
        return []


async def check_owner(pool, owner_id: int, *, bot=None) -> dict:
    """Проверить позиции всех активных ключей владельца. Возвращает
    {checked, found}. Fail-open. Один аккаунт на запрос, с паузами."""
    from services import resource_selector, account_manager
    try:
        rows = await pool.fetch(
            "SELECT id, channel_id, keyword FROM channel_tracked_keywords "
            "WHERE owner_id=$1 AND is_active ORDER BY id LIMIT $2",
            owner_id, MAX_KEYWORDS_PER_RUN)
    except Exception:
        return {"checked": 0, "found": 0}
    if not rows:
        return {"checked": 0, "found": 0}
    try:
        accs = await resource_selector.select_all_active(pool, owner_id, min_trust_score=0.0)
        accounts = [dict(a) for a in (accs or [])]
    except Exception:
        accounts = []
    if not accounts:
        return {"checked": 0, "found": 0, "error": "нет активных аккаунтов"}

    checked = found = 0
    # Достаём username каналов разом (для сопоставления, если id не совпал).
    unames: dict[int, str] = {}
    try:
        urows = await pool.fetch(
            "SELECT channel_id, username FROM managed_channels WHERE owner_id=$1", owner_id)
        unames = {int(r["channel_id"]): (r["username"] or "") for r in (urows or [])}
    except Exception:
        unames = {}

    for i, kw in enumerate(rows):
        acc = accounts[i % len(accounts)]
        try:
            # РЕАЛЬНАЯ выдача единым списком (каналы+боты+чаты) — сквозная позиция.
            results = await account_manager.search_global_ranked(
                acc["session_str"], kw["keyword"], limit=SEARCH_LIMIT, _acc=acc)
        except Exception:
            log.debug("channel_ranking: search failed kw=%s", kw["keyword"])
            continue
        pos = find_position(results, channel_id=kw["channel_id"],
                            username=unames.get(int(kw["channel_id"]), ""))
        await record(pool, kw["id"], kw["channel_id"], pos)
        checked += 1
        if pos is not None:
            found += 1
        await asyncio.sleep(2)          # мягкий пейсинг между поисками
    return {"checked": checked, "found": found}
