"""Bot search-SEO auto-reoptimization (POWER_USER_ROADMAP P2).

Замыкает петлю анализ→рекомендация→применение для СТОРОНЫ БОТОВ. Ранжирование
ботов в Telegram-поиске идёт по токенам имени/краткого описания. Когда позиция
бота по ключу падает ниже порога (visibility alert в ranking_checker), система
генерирует рекомендацию по переоптимизации: имя/краткое описание, где всплывает
просевший ключ и топ-ключи владельца. Рекомендация сохраняется в
``bot_seo_suggestions`` и применяется оператором в один клик
(``/api/miniapp/seo/apply_bot``) — фонового авто-переименования НЕТ (renaming
бота — полудеструктивное действие, всегда под контролем оператора).

Переиспользует существующее:
  * ``database.db.fetch_bot`` — строка бота с расшифрованным токеном (owner-scope
    через ``managed_bots.added_by``);
  * ``search_memory`` — топ-ключи владельца (тот же источник, что у SEO каналов);
  * Bot API ``setMyName``/``setMyShortDescription`` — те же методы, что в
    ``op_worker._exec_bulk_bot_edit``.

``build_bot_seo_suggestion`` — чистая функция (без БД/сети), поэтому легко
тестируется и детерминирована.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import asyncpg

log = logging.getLogger(__name__)

# Лимиты Telegram Bot API.
BOT_NAME_MAX = 64
BOT_SHORT_DESC_MAX = 120

_API_TIMEOUT = 10


def _contains_kw(text: str | None, kw: str) -> bool:
    return bool(kw) and kw.lower() in (text or "").lower()


def build_bot_seo_suggestion(
    current_name: str | None,
    current_short_desc: str | None,
    dropped_keyword: str | None,
    top_keywords: list[str] | None = None,
) -> dict[str, Any]:
    """Построить рекомендацию по переоптимизации бота (чистая функция).

    Правило: просевший ключ ОБЯЗАН присутствовать в имени (главный сигнал
    ранжирования); краткое описание набивается ключом + топ-ключами владельца до
    лимита, без дублей. Возвращает предложение и флаги, что реально изменилось —
    если ничего (ключ уже в имени и в описании), вызывающий код может не сохранять
    пустую рекомендацию.
    """
    dropped_keyword = (dropped_keyword or "").strip()
    current_name = (current_name or "").strip()
    current_short_desc = (current_short_desc or "").strip()

    # Пул ключей: просевший первым, затем топ владельца; дедуп без учёта регистра.
    seen: set[str] = set()
    kw_pool: list[str] = []
    for k in [dropped_keyword, *(top_keywords or [])]:
        k = (k or "").strip()
        kl = k.lower()
        if not kl or kl in seen:
            continue
        seen.add(kl)
        kw_pool.append(k)

    # ── Имя: гарантируем присутствие просевшего ключа ──────────────────────────
    if not dropped_keyword or _contains_kw(current_name, dropped_keyword):
        new_name = current_name  # уже оптимизировано по имени (или нечего добавить)
    else:
        kw_label = dropped_keyword[:1].upper() + dropped_keyword[1:]
        candidate = f"{current_name} | {kw_label}" if current_name else kw_label
        new_name = candidate[:BOT_NAME_MAX].rstrip(" |")

    # ── Краткое описание: набиваем ключами до лимита ───────────────────────────
    desc_parts: list[str] = []
    if current_short_desc:
        desc_parts.append(current_short_desc.rstrip(". "))
    for k in kw_pool:
        if not _contains_kw(". ".join(desc_parts), k):
            desc_parts.append(k)
    new_short_desc = ". ".join(p for p in desc_parts if p).strip()
    if len(new_short_desc) > BOT_SHORT_DESC_MAX:
        new_short_desc = new_short_desc[:BOT_SHORT_DESC_MAX].rstrip(" ,.")

    changed_name = new_name != current_name
    changed_desc = new_short_desc != current_short_desc
    if changed_name and changed_desc:
        reason = f"Позиция по «{dropped_keyword}» просела — ключ добавлен в имя и описание"
    elif changed_name:
        reason = f"Позиция по «{dropped_keyword}» просела — ключ добавлен в имя бота"
    elif changed_desc:
        reason = f"Позиция по «{dropped_keyword}» просела — ключ усилен в описании"
    else:
        reason = "Имя и описание уже содержат ключ — переоптимизация не требуется"

    return {
        "name": new_name,
        "short_desc": new_short_desc,
        "keyword": dropped_keyword,
        "changed_name": changed_name,
        "changed_desc": changed_desc,
        "reason": reason,
    }


async def generate_and_store(
    pool: asyncpg.Pool,
    owner_id: int,
    bot_id: int,
    dropped_keyword: str,
) -> dict[str, Any] | None:
    """Сгенерировать и сохранить рекомендацию по переоптимизации бота.

    Возвращает предложение (или ``None``, если менять нечего — ключ уже в
    имени/описании). Ничего не применяет — только записывает в
    ``bot_seo_suggestions`` для одноклика оператора.
    """
    from database import db

    bot_row = await db.fetchrow_bot(
        pool,
        "SELECT bot_id, first_name, username FROM managed_bots "
        "WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
        bot_id,
        owner_id,
    )
    if not bot_row:
        return None

    try:
        kw_rows = await pool.fetch(
            "SELECT keyword FROM search_memory WHERE owner_id=$1 "
            "ORDER BY search_count DESC LIMIT 8",
            owner_id,
        )
    except Exception:
        kw_rows = []
    top_keywords = [r["keyword"] for r in kw_rows]

    sugg = build_bot_seo_suggestion(
        current_name=bot_row.get("first_name"),
        current_short_desc=None,  # не тянем getMyShortDescription в фоновом свипе
        dropped_keyword=dropped_keyword,
        top_keywords=top_keywords,
    )
    if not (sugg["changed_name"] or sugg["changed_desc"]):
        return None

    try:
        await pool.execute(
            """INSERT INTO bot_seo_suggestions(owner_id, bot_id, name, short_desc,
                                               reason, keyword, created_at, applied_at)
               VALUES($1,$2,$3,$4,$5,$6,now(),NULL)
               ON CONFLICT(owner_id, bot_id) DO UPDATE
               SET name=$3, short_desc=$4, reason=$5, keyword=$6,
                   created_at=now(), applied_at=NULL""",
            owner_id,
            bot_id,
            sugg["name"],
            sugg["short_desc"],
            sugg["reason"],
            sugg["keyword"],
        )
    except Exception as exc:
        log.warning("bot_reoptimizer.generate_and_store store failed bot=%s: %s", bot_id, exc)
        return None
    return sugg


async def _bot_api_call(token: str, method: str, payload: dict) -> dict:
    """Один вызов Bot API. Вынесено для тестируемости (мокается в тестах)."""
    async with aiohttp.ClientSession() as sess:
        resp = await sess.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=_API_TIMEOUT),
        )
        return await resp.json()


async def apply_bot_seo(
    pool: asyncpg.Pool,
    owner_id: int,
    bot_id: int,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Применить сохранённую рекомендацию к боту (инициирует оператор).

    Owner-scope через ``managed_bots.added_by``; токен расшифровывается
    ``db.fetch_bot``. ``fields`` — подмножество {"name","short_desc"} (по
    умолчанию оба, если непустые). Использует те же методы Bot API, что и
    ``_exec_bulk_bot_edit``.
    """
    from database import db

    sugg = await pool.fetchrow(
        "SELECT name, short_desc FROM bot_seo_suggestions "
        "WHERE owner_id=$1 AND bot_id=$2",
        owner_id,
        bot_id,
    )
    if not sugg:
        return {"ok": False, "error": "Нет сохранённой рекомендации для этого бота"}

    bot_row = await db.fetchrow_bot(
        pool,
        "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
        bot_id,
        owner_id,
    )
    if not bot_row or not bot_row.get("token"):
        return {"ok": False, "error": "Бот не найден или не принадлежит вам"}
    token = bot_row["token"]

    want = set(fields) if fields else {"name", "short_desc"}
    plan: list[tuple[str, str, dict]] = []
    if "name" in want and (sugg["name"] or "").strip():
        plan.append(("name", "setMyName", {"name": sugg["name"]}))
    if "short_desc" in want and (sugg["short_desc"] or "").strip():
        plan.append(
            ("short_desc", "setMyShortDescription", {"short_description": sugg["short_desc"]})
        )
    if not plan:
        return {"ok": False, "error": "Нечего применять (пустая рекомендация)"}

    applied: list[str] = []
    errors: list[str] = []
    for field, method, payload in plan:
        try:
            data = await _bot_api_call(token, method, payload)
            if data.get("ok"):
                applied.append(field)
            else:
                errors.append(f"{field}: {data.get('description', 'api error')}")
        except Exception as exc:
            log.warning("apply_bot_seo bot=%s field=%s error=%s", bot_id, field, exc)
            errors.append(f"{field}: {exc}")

    if applied:
        try:
            await pool.execute(
                "UPDATE bot_seo_suggestions SET applied_at=now() "
                "WHERE owner_id=$1 AND bot_id=$2",
                owner_id,
                bot_id,
            )
        except Exception:
            pass

    return {"ok": bool(applied), "applied": applied, "errors": errors}
