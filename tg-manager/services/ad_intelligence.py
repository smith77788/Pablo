"""Ad Intelligence — Telegram advertising market analysis and placement recommendations."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

# ── CTA и рекламные паттерны ──────────────────────────────────────────────

_AD_KEYWORDS = [
    "реклама",
    "рекламодател",
    "партнёр",
    "партнер",
    "erid:",
    "erid ",
    "#реклама",
    "#партнёрство",
    "#ad",
    "#sponsored",
    "промокод",
    "promo",
    "скидка",
    "перейти по ссылке",
    "переходи по ссылке",
    "нажми на кнопку",
    "подробнее по ссылке",
    "купить сейчас",
    "заказать сейчас",
    "оформить подписку",
    "попробовать бесплатно",
    "регистрируйся",
    "зарегистрируйся",
    "переходите",
    "подписывайтесь",
    "by @",
    "размещение рекламы",
    "рекламное сотрудничество",
]

_URL_PATTERN = re.compile(
    r"https?://(?!t\.me|telegram\.me)[^\s<>\"']+",
    re.IGNORECASE,
)

_ERID_PATTERN = re.compile(r"\berid[:\s][A-Za-z0-9/+=]+", re.IGNORECASE)

_CTA_PATTERNS = re.compile(
    r"(перейти|нажми|кликни|купить|заказать|оформить|зарегистрир|подпис|попробуй|скачай|узнай больше)",
    re.IGNORECASE,
)

_LINK_MENTION_PATTERN = re.compile(r"t\.me/[A-Za-z0-9_]+", re.IGNORECASE)


# ── Dataclass ─────────────────────────────────────────────────────────────


@dataclass
class AdPlacement:
    channel_username: str
    channel_title: str = ""
    subscribers: int = 0
    views_avg: int = 0
    er_rate: float = 0.0
    ad_price_est: int = 0
    quality_score: float = 0.0
    last_seen_ad_at: Optional[datetime] = None
    niches: list[str] = field(default_factory=list)


# ── Определение рекламного поста ──────────────────────────────────────────


def detect_ad_post(text: str, views: int, channel_subscribers: int) -> bool:
    """
    Эвристика определения рекламного поста.

    Проверяет текст на наличие рекламных маркеров:
    - Ключевые слова (реклама, партнёр, erid и т.д.)
    - Внешние ссылки (не t.me)
    - CTA-паттерны (перейти, купить, зарегистрироваться...)
    - ERID-маркировка обязательная по закону

    Возвращает True если пост, скорее всего, рекламный.
    """
    if not text:
        return False

    text_lower = text.lower()

    # Прямая маркировка
    if _ERID_PATTERN.search(text):
        return True

    # Ключевые слова
    kw_hits = sum(1 for kw in _AD_KEYWORDS if kw in text_lower)
    if kw_hits >= 1:
        return True

    # Внешняя ссылка + CTA
    has_external_url = bool(_URL_PATTERN.search(text))
    has_cta = bool(_CTA_PATTERNS.search(text))
    if has_external_url and has_cta:
        return True

    # Внешняя ссылка + упоминание другого канала — кросс-промо
    has_link_mention = bool(_LINK_MENTION_PATTERN.search(text))
    if has_external_url and has_link_mention:
        return True

    # Очень высокий охват относительно базовой аудитории — вирусная реклама
    if channel_subscribers > 0 and views > 0:
        ratio = views / channel_subscribers
        if ratio > 3.0 and has_external_url:
            return True

    return False


# ── Оценка качества канала ────────────────────────────────────────────────


def score_channel_quality(
    subscribers: int,
    views_avg: int,
    er_rate: float,
) -> float:
    """
    Рассчитывает quality_score 0–100 для канала.

    Формула учитывает:
    - Engagement Rate (ER): высокий ER при большой аудитории = хорошо
    - Соотношение просмотров к подписчикам: накрутка = просмотры >> реальный ER
    - Абсолютный размер аудитории (логарифмическая нормализация)

    Признаки накрутки снижают оценку.
    """
    if subscribers <= 0:
        return 0.0

    # Базовый ER-балл (0–40 очков)
    # Отличный ER для Telegram: 20%+, хороший: 10–20%, средний: 5–10%
    er_pct = er_rate * 100 if er_rate <= 1.0 else er_rate
    if er_pct >= 20:
        er_score = 40.0
    elif er_pct >= 10:
        er_score = 20.0 + (er_pct - 10) * 2.0
    elif er_pct >= 5:
        er_score = 10.0 + (er_pct - 5) * 2.0
    elif er_pct >= 1:
        er_score = er_pct * 2.0
    else:
        er_score = 0.0

    # Балл за размер аудитории (0–20 очков)
    import math
    if subscribers >= 1_000_000:
        size_score = 20.0
    elif subscribers >= 10_000:
        size_score = min(20.0, math.log10(subscribers / 10_000 + 1) * 10.0 + 5.0)
    elif subscribers >= 1_000:
        size_score = min(10.0, math.log10(subscribers / 1_000 + 1) * 5.0)
    else:
        size_score = 0.0

    # Балл за просмотры (0–20 очков)
    if subscribers > 0 and views_avg > 0:
        view_ratio = views_avg / subscribers
        if view_ratio >= 0.5:
            view_score = 20.0
        elif view_ratio >= 0.2:
            view_score = 10.0 + (view_ratio - 0.2) / 0.3 * 10.0
        elif view_ratio >= 0.05:
            view_score = (view_ratio - 0.05) / 0.15 * 10.0
        else:
            view_score = 0.0
    else:
        view_score = 0.0

    # Штраф за накрутку (0–20 очков штрафа)
    cheat_penalty = 0.0
    if subscribers > 0 and views_avg > 0 and er_rate > 0:
        # Если views_avg высокий, но ER очень низкий — подозрение на накрутку просмотров
        implied_er = views_avg / subscribers
        declared_er = er_rate if er_rate <= 1.0 else er_rate / 100
        if declared_er > 0 and implied_er / declared_er > 5:
            cheat_penalty = min(20.0, (implied_er / declared_er - 5) * 2.0)

    # Если просмотры многократно превышают подписчиков без реального ER
    if subscribers > 1000 and views_avg > subscribers * 2 and er_pct < 1:
        cheat_penalty = max(cheat_penalty, 20.0)

    raw = er_score + size_score + view_score - cheat_penalty
    return round(max(0.0, min(100.0, raw)), 2)


# ── Оценка цены рекламы ───────────────────────────────────────────────────


def _estimate_ad_price(subscribers: int, er_rate: float) -> int:
    """
    Оценочная цена рекламы в Stars (Telegram).
    Ориентировочная формула: CPM * views_est / 1000.
    CPM варьируется от 50 до 500 Stars в зависимости от ER.
    """
    if subscribers <= 0:
        return 0

    er_pct = er_rate * 100 if er_rate <= 1.0 else er_rate
    views_est = int(subscribers * max(0.05, er_pct / 100))

    # CPM в Stars: чем выше ER, тем выше CPM
    if er_pct >= 20:
        cpm = 500
    elif er_pct >= 10:
        cpm = 300
    elif er_pct >= 5:
        cpm = 150
    else:
        cpm = 50

    price = int(views_est / 1000 * cpm)
    return max(50, price)


# ── Извлечение рекламодателя из текста ───────────────────────────────────


def _extract_advertiser(text: str) -> Optional[str]:
    """Пытается извлечь @username рекламодателя из текста поста."""
    if not text:
        return None

    # Явные @mention
    mentions = re.findall(r"@([A-Za-z0-9_]{4,32})", text)
    if mentions:
        return mentions[0].lower()

    # t.me/username
    tme = re.findall(r"t\.me/([A-Za-z0-9_]{4,32})", text, re.IGNORECASE)
    if tme:
        return tme[0].lower()

    return None


# ── Определение ниш канала по ключевым словам ─────────────────────────────
# Ключ — метка ниши, значение — подстроки-маркеры (lowercase, RU+EN).
_NICHE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "крипта": ("крипт", "bitcoin", "btc", "эфир", "ethereum", "токен", "nft",
               "блокчейн", "trading", "трейд", "биржа", "memecoin", "web3", "defi"),
    "гемблинг": ("казино", "casino", "ставк", "беттинг", "betting", "слот",
                 "покер", "poker", "1win", "букмекер"),
    "финансы": ("инвест", "invest", "финанс", "займ", "кредит", "forex",
                "форекс", "трейдинг", "дивиденд"),
    "заработок": ("заработок", "пассивн", "доход", "схем", "млм", "referral",
                  "реферал", "arbitrage", "арбитраж трафика"),
    "обучение": ("курс", "обучени", "вебинар", "webinar", "школа", "урок", "менторств"),
    "здоровье": ("здоров", "похуден", "фитнес", "fitness", "диет", "бад", "нутрициол"),
    "недвижимость": ("недвижим", "квартир", "ипотек", "новостройк", "аренд жил"),
    "авто": ("автомобил", "автосалон", "автоподбор", "запчаст"),
    "бьюти": ("космет", "макияж", "маникюр", "визаж", "бьюти", "beauty"),
    "IT": ("программир", "разработ", "нейросет", "chatbot", "чат-бот", "python",
           "верстк", "backend", "frontend"),
    "товарка": ("скидк", "распродаж", "маркетплейс", "wildberries", "ozon",
                "доставк", "дропшип"),
    "adult": ("эскорт", "интим", "18+", "adult", "onlyfans"),
}


def detect_niches(title: str, texts: list[str], max_niches: int = 5) -> list[str]:
    """Определить ниши канала по заголовку и текстам постов.

    Считает попадания маркеров; возвращает ниши, отсортированные по числу
    совпадений (самые релевантные первыми). Возвращает [] если ничего не найдено.
    """
    haystack = (title + " " + " ".join(texts)).lower()
    if not haystack.strip():
        return []
    scored: list[tuple[int, str]] = []
    for niche, markers in _NICHE_KEYWORDS.items():
        hits = sum(haystack.count(m) for m in markers)
        if hits:
            scored.append((hits, niche))
    scored.sort(reverse=True)
    return [niche for _, niche in scored[:max_niches]]


# ── Сканирование рекламных постов ─────────────────────────────────────────


async def scan_channel_ads(
    pool: asyncpg.Pool,
    channel_username: str,
    account_id: int,
    owner_id: int,
) -> dict:
    """
    Читает последние 100 постов канала через Telethon, находит рекламные,
    сохраняет в ad_placements и ad_posts_log.

    Возвращает dict с ключами: status, ad_posts_found, placement_id, error.
    """
    from services import account_manager

    channel_username = channel_username.lstrip("@").strip()
    if not channel_username:
        return {"status": "error", "error": "Пустой username канала"}

    # Получаем аккаунт из пула
    async with pool.acquire() as conn:
        acc = await conn.fetchrow(
            "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts "
            "WHERE id = $1 AND owner_id = $2 AND is_active = TRUE",
            account_id,
            owner_id,
        )

    if not acc:
        # Пробуем любой активный аккаунт владельца
        async with pool.acquire() as conn:
            acc = await conn.fetchrow(
                "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts "
                "WHERE owner_id = $1 AND is_active = TRUE "
                "ORDER BY RANDOM() LIMIT 1",
                owner_id,
            )

    if not acc:
        return {"status": "error", "error": "Нет доступных аккаунтов Telegram"}

    client = None
    try:
        client = account_manager._make_client(acc["session_str"], acc)
        await asyncio.wait_for(client.connect(), timeout=15)

        # Получаем метаданные канала
        try:
            entity = await client.get_entity(f"@{channel_username}")
        except Exception as exc:
            return {"status": "error", "error": f"Не удалось получить канал: {exc}"}

        channel_title = getattr(entity, "title", channel_username)
        subscribers = getattr(entity, "participants_count", 0) or 0

        # Читаем последние 100 сообщений
        messages = []
        try:
            async for msg in client.iter_messages(entity, limit=100):
                if msg and msg.text:
                    messages.append(msg)
        except Exception as exc:
            log.warning("scan_channel_ads: iter_messages error %s", exc)

        if not messages:
            # Сохраняем канал даже без постов
            placement_id = await _upsert_placement(
                pool, owner_id, channel_username, channel_title, subscribers,
                0, 0.0, 0, 0.0, [], 0, None,
            )
            return {"status": "ok", "ad_posts_found": 0, "placement_id": placement_id}

        # Считаем средние просмотры
        view_counts = [
            getattr(m, "views", 0) or 0
            for m in messages
        ]
        views_avg = int(sum(view_counts) / len(view_counts)) if view_counts else 0
        er_rate = views_avg / subscribers if subscribers > 0 else 0.0

        # Фильтруем рекламные посты
        ad_posts = []
        for msg in messages:
            if detect_ad_post(msg.text or "", getattr(msg, "views", 0) or 0, subscribers):
                ad_posts.append(msg)

        quality_score = score_channel_quality(subscribers, views_avg, er_rate)
        ad_price_est = _estimate_ad_price(subscribers, er_rate)

        # Определяем ниши канала по заголовку + текстам постов (было: всегда [])
        niches = detect_niches(
            channel_title or channel_username,
            [m.text or "" for m in messages],
        )

        placement_id = await _upsert_placement(
            pool, owner_id, channel_username, channel_title, subscribers,
            views_avg, er_rate, ad_price_est, quality_score, niches,
            len(ad_posts),
            ad_posts[0].date if ad_posts else None,
        )

        # Сохраняем рекламные посты
        primary_niche = niches[0] if niches else ""
        for msg in ad_posts:
            advertiser = _extract_advertiser(msg.text or "")
            await _save_ad_post(
                pool,
                placement_id,
                owner_id,
                advertiser,
                (msg.text or "")[:2000],
                getattr(msg, "views", 0) or 0,
            )
            if advertiser:
                # Рекламодатель наследует нишу канала-размещения
                await _upsert_advertiser(pool, owner_id, advertiser, niche=primary_niche)

        return {
            "status": "ok",
            "ad_posts_found": len(ad_posts),
            "placement_id": placement_id,
            "channel_title": channel_title,
            "subscribers": subscribers,
            "quality_score": quality_score,
        }

    except asyncio.TimeoutError:
        return {"status": "error", "error": "Таймаут подключения к Telegram"}
    except Exception as exc:
        log.exception("scan_channel_ads unexpected error")
        return {"status": "error", "error": str(exc)[:200]}
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


# ── DB-хелперы ───────────────────────────────────────────────────────────


async def _upsert_placement(
    pool: asyncpg.Pool,
    owner_id: int,
    channel_username: str,
    channel_title: str,
    subscribers: int,
    views_avg: int,
    er_rate: float,
    ad_price_est: int,
    quality_score: float,
    niches: list[str],
    ad_posts_count: int,
    last_ad_seen_at: Optional[datetime],
) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO ad_placements
                (owner_id, channel_username, channel_title, subscribers,
                 views_avg, er_rate, ad_price_est, quality_score, niches,
                 ad_posts_count, last_ad_seen_at, last_scanned_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW())
            ON CONFLICT (owner_id, channel_username) DO UPDATE SET
                channel_title   = EXCLUDED.channel_title,
                subscribers     = EXCLUDED.subscribers,
                views_avg       = EXCLUDED.views_avg,
                er_rate         = EXCLUDED.er_rate,
                ad_price_est    = EXCLUDED.ad_price_est,
                quality_score   = EXCLUDED.quality_score,
                niches          = CASE WHEN array_length(EXCLUDED.niches,1) > 0
                                       THEN EXCLUDED.niches
                                       ELSE ad_placements.niches END,
                ad_posts_count  = EXCLUDED.ad_posts_count,
                last_ad_seen_at = COALESCE(EXCLUDED.last_ad_seen_at, ad_placements.last_ad_seen_at),
                last_scanned_at = NOW()
            RETURNING id
            """,
            owner_id, channel_username, channel_title, subscribers,
            views_avg, er_rate, ad_price_est, quality_score, niches,
            ad_posts_count, last_ad_seen_at,
        )
        return row["id"]


async def _save_ad_post(
    pool: asyncpg.Pool,
    placement_id: int,
    owner_id: int,
    advertiser_username: Optional[str],
    post_text: str,
    post_views: int,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ad_posts_log
                (placement_id, owner_id, advertiser_username, post_text, post_views)
            VALUES ($1,$2,$3,$4,$5)
            """,
            placement_id, owner_id, advertiser_username, post_text, post_views,
        )


async def _upsert_advertiser(
    pool: asyncpg.Pool,
    owner_id: int,
    advertiser_username: str,
    niche: str = "",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ad_advertisers
                (owner_id, advertiser_username, niche, placements_count, last_seen_at)
            VALUES ($1,$2,$3,1,NOW())
            ON CONFLICT (owner_id, advertiser_username) DO UPDATE SET
                placements_count = ad_advertisers.placements_count + 1,
                last_seen_at     = NOW(),
                niche            = CASE WHEN EXCLUDED.niche != ''
                                        THEN EXCLUDED.niche
                                        ELSE ad_advertisers.niche END
            """,
            owner_id, advertiser_username, niche,
        )


# ── Агрегированные отчёты ─────────────────────────────────────────────────


async def get_market_report(
    pool: asyncpg.Pool,
    owner_id: int,
    niche: str = "",
) -> dict:
    """
    Агрегированный отчёт по рекламному рынку:
    - Средняя цена рекламы в нише
    - Топ-10 каналов по quality_score
    - Активные рекламодатели за 30 дней
    """
    async with pool.acquire() as conn:
        # Базовые агрегаты
        if niche:
            agg = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total_channels,
                       AVG(quality_score) AS avg_quality,
                       AVG(ad_price_est)  AS avg_price,
                       SUM(ad_posts_count) AS total_ad_posts
                FROM ad_placements
                WHERE owner_id = $1 AND $2 = ANY(niches)
                """,
                owner_id, niche,
            )
        else:
            agg = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total_channels,
                       AVG(quality_score) AS avg_quality,
                       AVG(ad_price_est)  AS avg_price,
                       SUM(ad_posts_count) AS total_ad_posts
                FROM ad_placements
                WHERE owner_id = $1
                """,
                owner_id,
            )

        # Топ-10 каналов
        if niche:
            top_channels = await conn.fetch(
                """
                SELECT channel_username, channel_title, subscribers,
                       views_avg, er_rate, ad_price_est, quality_score, niches
                FROM ad_placements
                WHERE owner_id = $1 AND $2 = ANY(niches)
                ORDER BY quality_score DESC
                LIMIT 10
                """,
                owner_id, niche,
            )
        else:
            top_channels = await conn.fetch(
                """
                SELECT channel_username, channel_title, subscribers,
                       views_avg, er_rate, ad_price_est, quality_score, niches
                FROM ad_placements
                WHERE owner_id = $1
                ORDER BY quality_score DESC
                LIMIT 10
                """,
                owner_id,
            )

        # Активные рекламодатели за 30 дней
        active_advertisers = await conn.fetch(
            """
            SELECT advertiser_username, niche, placements_count, last_seen_at
            FROM ad_advertisers
            WHERE owner_id = $1
              AND last_seen_at >= NOW() - INTERVAL '30 days'
            ORDER BY placements_count DESC
            LIMIT 20
            """,
            owner_id,
        )

    return {
        "total_channels": agg["total_channels"] or 0,
        "avg_quality": round(float(agg["avg_quality"] or 0), 1),
        "avg_price": int(agg["avg_price"] or 0),
        "total_ad_posts": agg["total_ad_posts"] or 0,
        "top_channels": [dict(r) for r in top_channels],
        "active_advertisers": [dict(r) for r in active_advertisers],
        "niche": niche,
    }


async def get_recommendations(
    pool: asyncpg.Pool,
    owner_id: int,
    budget_stars: int = 0,
    niche: str = "",
) -> list[dict]:
    """
    Рекомендует лучшие каналы для размещения с оценкой ROI.
    Фильтрует по бюджету если задан, иначе топ по качеству.
    """
    async with pool.acquire() as conn:
        conditions = ["owner_id = $1", "quality_score > 20"]
        params: list = [owner_id]

        if niche:
            params.append(niche)
            conditions.append(f"${ len(params)} = ANY(niches)")

        if budget_stars > 0:
            params.append(budget_stars)
            conditions.append(f"ad_price_est <= ${ len(params)}")

        where = " AND ".join(conditions)
        rows = await conn.fetch(
            f"""
            SELECT channel_username, channel_title, subscribers,
                   views_avg, er_rate, ad_price_est, quality_score, niches,
                   ad_posts_count, last_ad_seen_at
            FROM ad_placements
            WHERE {where}
            ORDER BY quality_score DESC
            LIMIT 15
            """,
            *params,
        )

    results = []
    for r in rows:
        rec = dict(r)
        # Оценка ROI: quality_score / (цена / 1000) — больше = выгоднее
        if rec["ad_price_est"] > 0:
            rec["roi_score"] = round(
                rec["quality_score"] / (rec["ad_price_est"] / 1000), 2
            )
        else:
            rec["roi_score"] = 0.0
        results.append(rec)

    # Сортируем по ROI
    results.sort(key=lambda x: x["roi_score"], reverse=True)
    return results


async def get_dashboard_stats(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Статистика для главного дашборда Ad Intelligence."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total_channels,
                AVG(quality_score) AS avg_quality,
                COUNT(CASE WHEN last_scanned_at >= NOW() - INTERVAL '7 days' THEN 1 END)
                    AS recently_scanned
            FROM ad_placements
            WHERE owner_id = $1
            """,
            owner_id,
        )
        advertisers_30d = await conn.fetchval(
            """
            SELECT COUNT(DISTINCT advertiser_username)
            FROM ad_advertisers
            WHERE owner_id = $1
              AND last_seen_at >= NOW() - INTERVAL '30 days'
            """,
            owner_id,
        )
        total_ad_posts = await conn.fetchval(
            "SELECT COALESCE(SUM(ad_posts_count),0) FROM ad_placements WHERE owner_id=$1",
            owner_id,
        )

    return {
        "total_channels": row["total_channels"] or 0,
        "avg_quality": round(float(row["avg_quality"] or 0), 1),
        "recently_scanned": row["recently_scanned"] or 0,
        "advertisers_30d": advertisers_30d or 0,
        "total_ad_posts": total_ad_posts or 0,
    }
