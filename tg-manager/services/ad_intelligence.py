"""Разведка рекламы: чужие каналы, их рекламодатели и размещения.

Что считается и почему так (раньше было иначе, и экран вводил в заблуждение):

* Охват (`reach_rate`) — средние просмотры поста ÷ подписчики. Раньше его
  писали в `er_rate` и показывали как «ER», да ещё без умножения на 100.
* Вовлечённость (`engagement_rate`, настоящий ER) — (реакции + репосты +
  комментарии) ÷ просмотры, среднее по постам.
* Рекламный пост определяется по набору признаков с весами
  (`analyze_ad_post`), а не по одному слову: «подписывайтесь» или «скидка»
  сами по себе рекламой не делают.
* Рекламодатель — тот, на кого ведёт реклама: кнопка, скрытая ссылка, ссылка
  на канал/бота, внешний сайт. Сам сканируемый канал рекламодателем не
  бывает.
* Повторный скан не дублирует посты: у поста есть msg_id, а счётчики
  рекламодателя пересчитываются по журналу, а не прибавляются.
* Цена размещения — оценка в рублях: средние просмотры × CPM ниши ÷ 1000.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

import asyncpg
from services.logger import log_exc_swallow
from services import account_status as _acc_status

log = logging.getLogger(__name__)

# Сколько последних постов читает один скан.
SCAN_POSTS_LIMIT = 100
# Порог уверенности, с которого пост считается рекламным.
AD_CONFIDENCE_THRESHOLD = 50
# Как часто автообновление пересканирует канал.
AUTO_RESCAN_HOURS = 24

# ── Признаки рекламы ──────────────────────────────────────────────────────

_ERID_PATTERN = re.compile(r"\berid\s*[:=]?\s*[A-Za-z0-9/+=]{6,}", re.IGNORECASE)

# Явная маркировка: такой пост — реклама почти наверняка.
_MARKED_AD = re.compile(
    r"(#реклама|#ad\b|#sponsored|#партн[её]рство|#партн[её]р\b|"
    r"\bреклама\b|на правах рекламы|рекламный пост|партн[её]рский (?:пост|материал)|"
    r"\bрекламодател|\bинформация о рекламодателе)",
    re.IGNORECASE,
)

_PROMO_CODE = re.compile(r"\bпромокод|\bpromo ?code\b|\bкупон\b", re.IGNORECASE)
_DISCOUNT = re.compile(r"\bскидк[аиуе]\b|\b\d{1,2}\s?%\s?(?:скидк|off)\b", re.IGNORECASE)

_CTA_PATTERNS = re.compile(
    r"\b(переходи(?:те)?|перейти|жми(?:те)?|нажми(?:те)?|кликай|купи(?:ть|те)?|"
    r"закажи(?:те)?|заказать|оформи(?:ть|те)?|зарегистрир\w*|регистрируй\w*|"
    r"попробуй(?:те)?|скачай(?:те)?|узнай(?:те)? больше|забирай(?:те)?|"
    r"успей(?:те)?|подпишись|подписывайся)\b",
    re.IGNORECASE,
)

_URL_ANY = re.compile(r"(?:https?://)?(?:www\.)?([a-z0-9\-]+(?:\.[a-z0-9\-]+)+)(/[^\s<>\"')\]]*)?",
                      re.IGNORECASE)
_URL_HTTP = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_TME_LINK = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/(\+?[A-Za-z0-9_\-]{1,64}(?:/[A-Za-z0-9_\-]+)?)",
    re.IGNORECASE)
_MENTION = re.compile(r"(?<![\w.])@([A-Za-z][A-Za-z0-9_]{3,31})\b")

# Домены, ссылка на которые рекламодателя не называет (свои сервисы Telegram,
# сокращатели без хозяина в имени).
_NEUTRAL_DOMAINS = frozenset({
    "t.me", "telegram.me", "telegram.dog", "telegram.org", "telegra.ph",
})


@dataclass
class AdSignal:
    """Итог разбора одного поста."""
    is_ad: bool
    confidence: int = 0
    reasons: list[str] = field(default_factory=list)
    advertiser: Optional[str] = None
    advertiser_kind: Optional[str] = None   # channel | bot | site | invite
    link: Optional[str] = None
    has_erid: bool = False


def _domain_of(url: str) -> str:
    m = _URL_ANY.match(url.strip())
    if not m:
        return ""
    d = m.group(1).lower()
    return d[4:] if d.startswith("www.") else d


def _classify_tme(path: str) -> tuple[Optional[str], Optional[str]]:
    """t.me/<path> → (имя рекламодателя, тип)."""
    p = path.strip()
    low = p.lower()
    if p.startswith("+"):
        return p[:64], "invite"
    if low.startswith("joinchat/"):
        return "+" + p.split("/", 1)[1][:63], "invite"
    if low.startswith("s/"):
        p = p[2:]
    from_name = channel_username_from(p)
    if not from_name:
        return None, None
    kind = "bot" if from_name.lower().endswith("bot") else "channel"
    return from_name.lower(), kind


def _candidates(text: str, links: Iterable[str], buttons: Iterable[str],
                own: str) -> list[tuple[str, str, str, str]]:
    """Кандидаты в рекламодатели по убыванию веса: (имя, тип, ссылка, откуда).

    Кнопка весомее скрытой ссылки, скрытая — весомее текста, канал/бот —
    весомее сайта. Свой канал отбрасывается всегда."""
    own = (own or "").lower()
    out: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()

    def add_url(url: str, source: str) -> None:
        u = (url or "").strip()
        if not u:
            return
        m = _TME_LINK.match(u)
        if m:
            name, kind = _classify_tme(m.group(1))
            if not name or name == own or name in seen:
                return
            seen.add(name)
            href = u if u.lower().startswith("http") else "https://" + u
            out.append((name, kind, href, source))
            return
        dom = _domain_of(u)
        if not dom or dom in _NEUTRAL_DOMAINS or dom in seen:
            return
        seen.add(dom)
        href = u if u.lower().startswith("http") else "https://" + u
        out.append((dom, "site", href, source))

    for u in buttons or ():
        add_url(u, "кнопка")
    for u in links or ():
        add_url(u, "скрытая ссылка")
    for m in _TME_LINK.finditer(text or ""):
        add_url(m.group(0), "ссылка")
    for m in _MENTION.finditer(text or ""):
        name = m.group(1).lower()
        if name == own or name in seen:
            continue
        seen.add(name)
        kind = "bot" if name.endswith("bot") else "channel"
        out.append((name, kind, f"https://t.me/{name}", "упоминание"))
    for m in _URL_HTTP.finditer(text or ""):
        add_url(m.group(0), "ссылка")

    rank = {"кнопка": 0, "скрытая ссылка": 1, "ссылка": 2, "упоминание": 3}
    kind_rank = {"bot": 0, "channel": 0, "invite": 1, "site": 2}
    out.sort(key=lambda c: (rank.get(c[3], 9), kind_rank.get(c[1], 9)))
    return out


def analyze_ad_post(
    text: str,
    *,
    own_username: str = "",
    links: Iterable[str] = (),
    buttons: Iterable[str] = (),
    forwarded_from_other: bool = False,
) -> AdSignal:
    """Разобрать пост: реклама ли это, насколько уверенно, кто рекламодатель.

    `links` — адреса скрытых ссылок (текст со ссылкой), `buttons` — адреса
    кнопок под постом: в рекламе ссылка чаще всего именно там, а в тексте её
    не видно. Внешний текст — данные, в разбор он попадает только как
    строка для регулярных выражений."""
    text = text or ""
    buttons = [b for b in (buttons or ()) if b]
    links = [u for u in (links or ()) if u]
    if not text.strip() and not buttons:
        return AdSignal(is_ad=False)

    score = 0
    reasons: list[str] = []
    has_erid = bool(_ERID_PATTERN.search(text)) or any(
        "erid=" in u.lower() for u in (*links, *buttons))
    if has_erid:
        score += 100
        reasons.append("маркировка erid")
    if _MARKED_AD.search(text):
        score += 60
        reasons.append("помечен как реклама")

    cands = _candidates(text, links, buttons, own_username)
    if any(c[3] == "кнопка" for c in cands):
        score += 35
        reasons.append("кнопка ведёт наружу")
    if any(c[1] in ("channel", "bot", "invite") for c in cands):
        score += 20
        reasons.append("ссылка на чужой канал или бота")
    if any(c[1] == "site" for c in cands):
        score += 20
        reasons.append("ссылка на сайт")
    if _PROMO_CODE.search(text):
        score += 25
        reasons.append("промокод")
    elif _DISCOUNT.search(text):
        score += 10
        reasons.append("скидка")
    if _CTA_PATTERNS.search(text):
        score += 15
        reasons.append("призыв к действию")
    if forwarded_from_other:
        score += 25
        reasons.append("репост из другого канала")
    # Призыв без единой ссылки наружу — это обычный пост канала о себе.
    if not cands and not has_erid and not forwarded_from_other:
        score = min(score, 40)

    confidence = max(0, min(100, score))
    best = cands[0] if cands else None
    return AdSignal(
        is_ad=confidence >= AD_CONFIDENCE_THRESHOLD,
        confidence=confidence,
        reasons=reasons,
        advertiser=best[0] if best else None,
        advertiser_kind=best[1] if best else None,
        link=best[2] if best else None,
        has_erid=has_erid,
    )


def detect_ad_post(text: str, views: int = 0, channel_subscribers: int = 0) -> bool:
    """Совместимость со старыми вызовами: только «реклама или нет»."""
    return analyze_ad_post(text).is_ad


def _extract_advertiser(text: str, own_username: str = "") -> Optional[str]:
    """Совместимость: имя рекламодателя из текста поста (без своего канала)."""
    return analyze_ad_post(text, own_username=own_username).advertiser


# ── Метрики канала ────────────────────────────────────────────────────────


@dataclass
class PostStat:
    """Одна запись для подсчёта метрик (из сообщения Telethon или теста)."""
    views: int = 0
    reactions: int = 0
    forwards: int = 0
    replies: int = 0
    date: Optional[datetime] = None
    is_ad: bool = False


def channel_metrics(posts: list[PostStat], subscribers: int) -> dict:
    """Охват, вовлечённость, частота постов и рекламы, признаки накрутки."""
    views = [p.views for p in posts if p.views > 0]
    views_avg = int(sum(views) / len(views)) if views else 0
    reach = views_avg / subscribers if subscribers > 0 else 0.0
    ers = [(p.reactions + p.forwards + p.replies) / p.views for p in posts if p.views > 0]
    engagement = sum(ers) / len(ers) if ers else 0.0

    dates = sorted(p.date for p in posts if p.date)
    span_days = 0.0
    if len(dates) >= 2:
        span_days = max((dates[-1] - dates[0]).total_seconds() / 86400, 1 / 24)
    n_ads = sum(1 for p in posts if p.is_ad)
    posts_per_day = len(posts) / span_days if span_days else 0.0
    ads_per_week = n_ads / span_days * 7 if span_days else float(n_ads)
    ad_share = n_ads / len(posts) if posts else 0.0

    flags: list[str] = []
    if subscribers >= 500 and reach > 1.2:
        flags.append("просмотров больше, чем подписчиков")
    if len(views) >= 10 and views_avg > 0 and reach > 0.15:
        mean = sum(views) / len(views)
        sd = math.sqrt(sum((v - mean) ** 2 for v in views) / len(views))
        if sd / mean < 0.08:
            flags.append("просмотры почти одинаковые у всех постов")
    if views_avg >= 1000 and ers and engagement < 0.002:
        flags.append("реакций и репостов почти нет")
    if subscribers >= 5000 and 0 < reach < 0.03:
        flags.append("охват очень низкий — похоже на накрученных подписчиков")
    if ad_share > 0.5 and len(posts) >= 10:
        flags.append("больше половины постов — реклама")

    return {
        "views_avg": views_avg,
        "reach_rate": round(reach, 4),
        "engagement_rate": round(engagement, 4),
        "posts_per_day": round(posts_per_day, 2),
        "ads_per_week": round(ads_per_week, 2),
        "ad_share": round(ad_share, 3),
        "fraud_flags": flags,
    }


def score_quality(subscribers: int, reach_rate: float, engagement_rate: float,
                  fraud_flags: list[str] | None = None, ad_share: float = 0.0) -> float:
    """Качество канала для размещения, 0–100.

    Охват — до 40, вовлечённость — до 30, размер — до 20, умеренность
    рекламы — до 10; каждый признак накрутки снимает 15."""
    if subscribers <= 0:
        return 0.0
    reach_pts = min(40.0, max(0.0, reach_rate) / 0.4 * 40.0)
    eng_pts = min(30.0, max(0.0, engagement_rate) / 0.05 * 30.0)
    size_pts = min(20.0, max(0.0, math.log10(max(subscribers, 1)) - 2) * 6.67)
    ad_pts = 10.0 * (1.0 - min(1.0, max(0.0, ad_share) / 0.5))
    raw = reach_pts + eng_pts + size_pts + ad_pts - 15.0 * len(fraud_flags or [])
    return round(max(0.0, min(100.0, raw)), 1)


def score_channel_quality(subscribers: int, views_avg: int, er_rate: float) -> float:
    """Совместимость: оценка по подписчикам и средним просмотрам."""
    reach = views_avg / subscribers if subscribers > 0 else 0.0
    return score_quality(subscribers, reach, 0.0)


# ── Цена размещения ───────────────────────────────────────────────────────

# CPM в рублях (цена тысячи просмотров) — средние рыночные ставки по нишам.
# Это ОЦЕНКА для сравнения каналов между собой, а не прайс канала.
NICHE_CPM_RUB: dict[str, int] = {
    "крипта": 1600, "финансы": 1500, "гемблинг": 2000, "заработок": 1100,
    "IT": 1300, "обучение": 1100, "недвижимость": 1400, "авто": 1000,
    "бьюти": 900, "здоровье": 900, "товарка": 700, "adult": 600,
}
DEFAULT_CPM_RUB = 900


def estimate_ad_price_rub(views_avg: int, niches: list[str] | None = None) -> int:
    """Оценка цены одного поста в рублях: просмотры × CPM ниши ÷ 1000."""
    if views_avg <= 0:
        return 0
    cpm = NICHE_CPM_RUB.get((niches or [""])[0], DEFAULT_CPM_RUB)
    price = views_avg * cpm / 1000
    # Округляем до сотен: точнее оценка не бывает.
    return int(max(100, round(price / 100) * 100))


def _estimate_ad_price(subscribers: int, er_rate: float) -> int:
    """Совместимость: оценка в рублях по подписчикам и охвату."""
    return estimate_ad_price_rub(int(subscribers * max(er_rate, 0.0)))



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


_PUBLIC_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_TME_PREFIX = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/(?:s/)?", re.I)


# Служебные разделы t.me — не имена каналов (t.me/joinchat/HASH и т. п.).
_TME_SERVICE_PATHS = frozenset({
    "joinchat", "addlist", "share", "proxy", "socks", "addstickers",
    "addemoji", "login", "setlanguage", "addtheme", "boost", "giftcode",
})


def channel_username_from(text) -> str:
    """Имя публичного канала из того, что вставил человек: `@name`, `name`,
    `t.me/name`, `https://t.me/name/123`, `t.me/s/name`. Пустая строка — если
    это не публичное имя (приватная ссылка-приглашение, мусор).

    Раньше с ввода срезалась только «@», и вставленная ссылка уходила в
    Telegram как «@https://t.me/name»: сканирование падало с английским
    «Cannot find any entity», хотя канал существует."""
    s = str(text or "").strip()
    s = _TME_PREFIX.sub("", s.lstrip("@")).lstrip("@")
    s = re.split(r"[/?#\s]", s, maxsplit=1)[0]
    if s.lower() in _TME_SERVICE_PATHS:
        return ""
    return s if _PUBLIC_USERNAME.fullmatch(s) else ""


def channel_open_error(channel_username: str, exc: BaseException) -> str:
    """Причина по-русски, почему канал не открылся (владелец не читает
    английский, а сырой текст Telethon — английский)."""
    name, low = type(exc).__name__, str(exc).lower()
    if (name in ("UsernameNotOccupiedError", "UsernameInvalidError")
            or "cannot find any entity" in low or "no user has" in low
            or "nobody is using" in low):
        return (f"Канал @{channel_username} не найден. Проверьте имя: нужен "
                "публичный канал, например @durov или ссылка t.me/durov")
    if name in ("ChannelPrivateError", "ChannelBannedError"):
        return (f"Канал @{channel_username} закрыт или недоступен этому аккаунту")
    if name == "FloodWaitError":
        return ("Telegram просит подождать: аккаунт сделал слишком много "
                f"запросов. Повторите через {getattr(exc, 'seconds', 0) or 60} с")
    if name in ("TimeoutError", "CancelledError") or isinstance(exc, asyncio.TimeoutError):
        return "Telegram не ответил вовремя. Повторите попытку позже"
    return f"Не удалось открыть канал @{channel_username}. Повторите попытку позже"




# ── Разбор сообщения Telethon ─────────────────────────────────────────────


def _msg_links(msg) -> tuple[list[str], list[str]]:
    """(скрытые ссылки из разметки текста, адреса кнопок под постом)."""
    links: list[str] = []
    for ent in getattr(msg, "entities", None) or []:
        url = getattr(ent, "url", None)
        if isinstance(url, str) and url:
            links.append(url)
    buttons: list[str] = []
    markup = getattr(msg, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for btn in getattr(row, "buttons", None) or []:
            url = getattr(btn, "url", None)
            if isinstance(url, str) and url:
                buttons.append(url)
    return links, buttons


def _msg_text(msg) -> str:
    return getattr(msg, "message", None) or getattr(msg, "text", None) or ""


def _msg_reactions(msg) -> int:
    res = getattr(getattr(msg, "reactions", None), "results", None) or []
    total = 0
    for r in res:
        try:
            total += int(getattr(r, "count", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _msg_forwarded_from_other(msg, own_channel_id) -> bool:
    fwd = getattr(msg, "fwd_from", None)
    if not fwd:
        return False
    peer = getattr(fwd, "from_id", None)
    cid = getattr(peer, "channel_id", None)
    return bool(cid) and cid != own_channel_id


def _as_utc(dt) -> Optional[datetime]:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Сканирование канала ───────────────────────────────────────────────────


async def scan_channel_ads(
    pool: asyncpg.Pool,
    channel_username: str,
    account_id: int,
    owner_id: int,
) -> dict:
    """
    Читает последние SCAN_POSTS_LIMIT постов канала через Telethon, считает
    метрики, находит рекламу и рекламодателей, сохраняет без дублей.

    Возвращает dict: status, ad_posts_found, new_ad_posts, placement_id,
    channel_title, subscribers, quality_score, error.
    """
    from services import account_manager

    _given = str(channel_username or "").strip()
    channel_username = channel_username_from(_given)
    if not channel_username:
        return {"status": "error", "error": (
            "Укажите публичный канал: @имя или ссылку t.me/имя. "
            "Закрытые каналы по ссылке-приглашению не сканируются"
            if _given else "Не указан канал")}

    # Получаем аккаунт из пула
    async with pool.acquire() as conn:
        acc = await conn.fetchrow(
            "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts "
            "WHERE id = $1 AND owner_id = $2 AND is_active = TRUE "
            # Названный вызывающим аккаунт не берём под ограничением или на
            # паузе: экран бота выбирал «первый активный», не спрашивая ни о
            # том, ни о другом. Тогда ниже аккаунт подберёт общий подбор.
            "AND COALESCE(acc_status, 'active') NOT IN ("
            + _acc_status.sql_dead_list() + ") "
            "AND (cooldown_until IS NULL OR cooldown_until <= now())",
            account_id,
            owner_id,
        )

    if not acc:
        # Одна дверь: любой активный аккаунт — через флуд-осознанный выбор
        # (не берём кулдаун/мёртвый по статусу), а не ORDER BY RANDOM().
        from services import resource_selector as _rsel
        acc = await _rsel.select_account(pool, owner_id, action_type="parse", min_trust_score=0.0)

    if not acc:
        return {"status": "error", "error": "Нет доступных аккаунтов Telegram"}

    client = None
    try:
        client = account_manager._make_client(acc["session_str"], acc)
        await asyncio.wait_for(client.connect(), timeout=15)

        try:
            entity = await asyncio.wait_for(
                client.get_entity(f"@{channel_username}"),
                timeout=account_manager._OP_TIMEOUT)
        except Exception as exc:
            log.info("scan_channel_ads: @%s не открылся: %s: %s",
                     channel_username, type(exc).__name__, str(exc)[:200])
            err = channel_open_error(channel_username, exc)
            await _record_scan_error(pool, owner_id, channel_username, err)
            return {"status": "error", "error": err}

        channel_title = getattr(entity, "title", None) or channel_username
        own_id = getattr(entity, "id", None)
        subscribers = getattr(entity, "participants_count", 0) or 0
        if not subscribers:
            # get_entity число подписчиков отдаёт не всегда — берём полную карточку.
            try:
                from telethon.tl.functions.channels import GetFullChannelRequest
                full = await asyncio.wait_for(
                    client(GetFullChannelRequest(entity)),
                    timeout=account_manager._OP_TIMEOUT)
                subscribers = int(getattr(full.full_chat, "participants_count", 0) or 0)
            except Exception:
                log_exc_swallow(log, "scan_channel_ads: full channel")

        messages = []
        try:
            async for msg in client.iter_messages(entity, limit=SCAN_POSTS_LIMIT):
                if msg is not None and getattr(msg, "id", None):
                    messages.append(msg)
        except Exception as exc:
            log.warning("scan_channel_ads: iter_messages error %s", type(exc).__name__)

        stats: list[PostStat] = []
        ads: list[tuple[object, AdSignal]] = []
        texts: list[str] = []
        for msg in messages:
            text = _msg_text(msg)
            links, buttons = _msg_links(msg)
            sig = analyze_ad_post(
                text, own_username=channel_username, links=links, buttons=buttons,
                forwarded_from_other=_msg_forwarded_from_other(msg, own_id),
            )
            replies = getattr(getattr(msg, "replies", None), "replies", 0) or 0
            stats.append(PostStat(
                views=int(getattr(msg, "views", 0) or 0),
                reactions=_msg_reactions(msg),
                forwards=int(getattr(msg, "forwards", 0) or 0),
                replies=int(replies),
                date=_as_utc(getattr(msg, "date", None)),
                is_ad=sig.is_ad,
            ))
            if text:
                texts.append(text)
            if sig.is_ad:
                ads.append((msg, sig))

        m = channel_metrics(stats, subscribers)
        niches = detect_niches(channel_title, texts)
        quality = score_quality(subscribers, m["reach_rate"], m["engagement_rate"],
                                m["fraud_flags"], m["ad_share"])
        price_rub = estimate_ad_price_rub(m["views_avg"], niches)

        async with pool.acquire() as conn:
            async with conn.transaction():
                placement_id = await _upsert_placement(
                    conn, owner_id, channel_username, channel_title, subscribers,
                    m, quality, price_rub, niches, len(messages),
                    _as_utc(getattr(ads[0][0], "date", None)) if ads else None,
                )
                # Кто рекламировался здесь ДО скана: если его пост ушёл из
                # журнала, его счётчики тоже надо пересчитать.
                touched: set[str] = await _advertisers_of_placement(conn, placement_id)
                # Записи старых сканов без msg_id — дубли: их заменит этот скан.
                await conn.execute(
                    "DELETE FROM ad_posts_log WHERE placement_id=$1 AND msg_id IS NULL",
                    placement_id)
                new_posts = 0
                for msg, sig in ads:
                    views = int(getattr(msg, "views", 0) or 0)
                    eng = 0.0
                    if views > 0:
                        replies = getattr(getattr(msg, "replies", None), "replies", 0) or 0
                        eng = (_msg_reactions(msg) + int(getattr(msg, "forwards", 0) or 0)
                               + int(replies)) / views
                    inserted = await _save_ad_post(
                        conn, placement_id, owner_id, int(msg.id),
                        _as_utc(getattr(msg, "date", None)), sig,
                        _msg_text(msg)[:2000], views, round(eng, 4),
                    )
                    new_posts += 1 if inserted else 0
                    if sig.advertiser:
                        touched.add(sig.advertiser)
                # Посты, которые разбор больше рекламой не считает, уходят из
                # журнала, иначе прежние ложные находки жили бы вечно.
                if messages:
                    await conn.execute(
                        "DELETE FROM ad_posts_log WHERE placement_id=$1 "
                        "AND msg_id >= $2 AND NOT (msg_id = ANY($3::bigint[]))",
                        placement_id,
                        min(int(x.id) for x in messages),
                        [int(msg.id) for msg, _ in ads])
                await refresh_advertisers(conn, owner_id, touched,
                                          niches[0] if niches else "")
                await conn.execute(
                    "UPDATE ad_placements SET ad_posts_count = (SELECT COUNT(*) "
                    "FROM ad_posts_log WHERE placement_id=$1) WHERE id=$1",
                    placement_id)

        return {
            "status": "ok",
            "ad_posts_found": len(ads),
            "new_ad_posts": new_posts,
            "advertisers_found": len({s.advertiser for _, s in ads if s.advertiser}),
            "placement_id": placement_id,
            "channel_title": channel_title,
            "subscribers": subscribers,
            "quality_score": quality,
            "fraud_flags": m["fraud_flags"],
        }

    except asyncio.TimeoutError:
        await _record_scan_error(pool, owner_id, channel_username,
                                 "Telegram не ответил вовремя")
        return {"status": "error", "error": "Таймаут подключения к Telegram"}
    except Exception as exc:
        log.exception("scan_channel_ads unexpected error")
        err = channel_open_error(channel_username, exc)
        await _record_scan_error(pool, owner_id, channel_username, err)
        return {"status": "error", "error": err}
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log_exc_swallow(log, "scan_channel_ads: disconnect")


# ── DB-хелперы ───────────────────────────────────────────────────────────


async def _record_scan_error(pool, owner_id: int, channel_username: str, err: str) -> None:
    """Причина последнего сбоя — на строку канала, чтобы экран её показал."""
    try:
        await pool.execute(
            # last_scanned_at — время ПОПЫТКИ: иначе автообновление ставило бы
            # сломанный канал в очередь каждый час.
            "UPDATE ad_placements SET last_error=$3, last_scanned_at=NOW() "
            "WHERE owner_id=$1 AND channel_username=$2",
            owner_id, channel_username, (err or "")[:300])
    except Exception:
        log_exc_swallow(log, "ad_intel: record scan error")


async def _upsert_placement(
    conn,
    owner_id: int,
    channel_username: str,
    channel_title: str,
    subscribers: int,
    m: dict,
    quality_score: float,
    price_rub: int,
    niches: list[str],
    posts_scanned: int,
    last_ad_seen_at: Optional[datetime],
) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO ad_placements
            (owner_id, channel_username, channel_title, subscribers,
             views_avg, er_rate, reach_rate, engagement_rate, ad_price_est,
             ad_price_rub, quality_score, niches, posts_scanned, posts_per_day,
             ads_per_week, ad_share, fraud_flags, last_ad_seen_at,
             last_scanned_at, last_error)
        VALUES ($1,$2,$3,$4,$5,$6,$6,$7,$8,$8,$9,$10,$11,$12,$13,$14,$15,$16,NOW(),NULL)
        ON CONFLICT (owner_id, channel_username) DO UPDATE SET
            channel_title   = EXCLUDED.channel_title,
            subscribers     = EXCLUDED.subscribers,
            views_avg       = EXCLUDED.views_avg,
            er_rate         = EXCLUDED.er_rate,
            reach_rate      = EXCLUDED.reach_rate,
            engagement_rate = EXCLUDED.engagement_rate,
            ad_price_est    = EXCLUDED.ad_price_est,
            ad_price_rub    = EXCLUDED.ad_price_rub,
            quality_score   = EXCLUDED.quality_score,
            niches          = CASE WHEN array_length(EXCLUDED.niches,1) > 0
                                   THEN EXCLUDED.niches
                                   ELSE ad_placements.niches END,
            posts_scanned   = EXCLUDED.posts_scanned,
            posts_per_day   = EXCLUDED.posts_per_day,
            ads_per_week    = EXCLUDED.ads_per_week,
            ad_share        = EXCLUDED.ad_share,
            fraud_flags     = EXCLUDED.fraud_flags,
            last_ad_seen_at = GREATEST(EXCLUDED.last_ad_seen_at, ad_placements.last_ad_seen_at),
            last_scanned_at = NOW(),
            last_error      = NULL
        RETURNING id
        """,
        owner_id, channel_username, (channel_title or "")[:256], int(subscribers),
        int(m.get("views_avg") or 0), float(m.get("reach_rate") or 0),
        float(m.get("engagement_rate") or 0), int(price_rub),
        float(quality_score), list(niches or []), int(posts_scanned),
        float(m.get("posts_per_day") or 0), float(m.get("ads_per_week") or 0),
        float(m.get("ad_share") or 0), list(m.get("fraud_flags") or []),
        last_ad_seen_at,
    )
    return int(row["id"])


async def _save_ad_post(
    conn,
    placement_id: int,
    owner_id: int,
    msg_id: int,
    post_date: Optional[datetime],
    sig: AdSignal,
    post_text: str,
    post_views: int,
    post_er: float,
) -> bool:
    """Записать рекламный пост; повтор того же поста обновляет просмотры.
    True — пост новый."""
    inserted = await conn.fetchval(
        """
        INSERT INTO ad_posts_log
            (placement_id, owner_id, msg_id, post_date, advertiser_username,
             advertiser_kind, ad_link, has_erid, confidence, reasons,
             post_text, post_views, post_er)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        ON CONFLICT (placement_id, msg_id) WHERE msg_id IS NOT NULL DO UPDATE SET
            post_views = EXCLUDED.post_views,
            post_er    = EXCLUDED.post_er,
            advertiser_username = EXCLUDED.advertiser_username,
            advertiser_kind     = EXCLUDED.advertiser_kind,
            ad_link    = EXCLUDED.ad_link,
            has_erid   = EXCLUDED.has_erid,
            confidence = EXCLUDED.confidence,
            reasons    = EXCLUDED.reasons
        RETURNING (xmax = 0)
        """,
        placement_id, owner_id, msg_id, post_date, sig.advertiser,
        sig.advertiser_kind, sig.link, sig.has_erid, int(sig.confidence),
        list(sig.reasons), post_text, int(post_views), float(post_er),
    )
    return bool(inserted)


async def _advertisers_of_placement(conn, placement_id: int) -> set[str]:
    rows = await conn.fetch(
        "SELECT DISTINCT advertiser_username FROM ad_posts_log "
        "WHERE placement_id=$1 AND advertiser_username IS NOT NULL",
        placement_id)
    return {r["advertiser_username"] for r in rows if r["advertiser_username"]}


async def refresh_advertisers(conn, owner_id: int, usernames: Iterable[str],
                              niche: str = "") -> None:
    """Пересчитать рекламодателей по журналу постов, а не прибавлять.

    Рекламодатель, у которого в журнале не осталось ни одного поста (канал
    удалён с разведки, пост больше не считается рекламой), удаляется."""
    names = sorted({str(u) for u in usernames if u})
    if not names:
        return
    await conn.execute(
        """
        INSERT INTO ad_advertisers
            (owner_id, advertiser_username, niche, kind, placements_count,
             channels_count, total_views, first_seen_at, last_seen_at)
        SELECT $1, l.advertiser_username, $3, MAX(l.advertiser_kind), COUNT(*),
               COUNT(DISTINCT l.placement_id), COALESCE(SUM(l.post_views), 0),
               MIN(COALESCE(l.post_date, l.detected_at)),
               MAX(COALESCE(l.post_date, l.detected_at))
        FROM ad_posts_log l
        WHERE l.owner_id = $1 AND l.advertiser_username = ANY($2::text[])
        GROUP BY l.advertiser_username
        ON CONFLICT (owner_id, advertiser_username) DO UPDATE SET
            placements_count = EXCLUDED.placements_count,
            channels_count   = EXCLUDED.channels_count,
            total_views      = EXCLUDED.total_views,
            kind             = COALESCE(EXCLUDED.kind, ad_advertisers.kind),
            first_seen_at    = LEAST(EXCLUDED.first_seen_at, ad_advertisers.first_seen_at),
            last_seen_at     = EXCLUDED.last_seen_at,
            niche            = CASE WHEN COALESCE(ad_advertisers.niche, '') = ''
                                    THEN EXCLUDED.niche ELSE ad_advertisers.niche END
        """,
        owner_id, names, niche or "",
    )
    await conn.execute(
        "DELETE FROM ad_advertisers a WHERE a.owner_id=$1 "
        "AND a.advertiser_username = ANY($2::text[]) "
        "AND NOT EXISTS (SELECT 1 FROM ad_posts_log l WHERE l.owner_id=$1 "
        "AND l.advertiser_username = a.advertiser_username)",
        owner_id, names)


# ── Чтение для экранов ────────────────────────────────────────────────────

# Сортировки списка каналов: ключ с экрана → выражение ORDER BY. Только из
# этого словаря: строка с экрана в запрос не попадает никогда.
CHANNEL_SORTS: dict[str, str] = {
    "quality": "quality_score DESC NULLS LAST",
    "subscribers": "subscribers DESC NULLS LAST",
    "reach": "COALESCE(reach_rate, er_rate) DESC NULLS LAST",
    "engagement": "engagement_rate DESC NULLS LAST",
    "ads": "ads_per_week DESC NULLS LAST",
    "price": "ad_price_rub ASC NULLS LAST",
    "recent": "last_scanned_at DESC NULLS LAST",
}
ADVERTISER_SORTS: dict[str, str] = {
    "placements": "placements_count DESC",
    "channels": "channels_count DESC",
    "views": "total_views DESC",
    "recent": "last_seen_at DESC NULLS LAST",
}
ADVERTISER_KINDS = ("channel", "bot", "site", "invite")
KIND_LABELS = {"channel": "канал", "bot": "бот", "site": "сайт", "invite": "закрытый канал"}


def _like(q: str) -> str:
    q = (q or "").strip().lstrip("@").lower()[:64]
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def placement_row(r) -> dict:
    """Строка канала для экрана. Доли — долями (0.35 = 35%)."""
    reach = r["reach_rate"] if r["reach_rate"] else (r["er_rate"] or 0)
    return {
        "username": r["channel_username"] or "",
        "title": r["channel_title"] or "",
        "subscribers": int(r["subscribers"] or 0),
        "views_avg": int(r["views_avg"] or 0),
        "reach_rate": round(float(reach or 0), 4),
        "engagement_rate": round(float(r["engagement_rate"] or 0), 4),
        # er_rate — прежнее имя охвата; экран бота и старые клиенты читают его.
        "er_rate": round(float(reach or 0), 4),
        "quality_score": round(float(r["quality_score"] or 0), 1),
        "ad_price_rub": int(r["ad_price_rub"] or 0),
        "ad_price_est": int(r["ad_price_rub"] or 0),
        "ads_per_week": round(float(r["ads_per_week"] or 0), 1),
        "ad_share": round(float(r["ad_share"] or 0), 3),
        "ad_posts": int(r["ad_posts_count"] or 0),
        "posts_scanned": int(r["posts_scanned"] or 0),
        "niches": list(r["niches"] or []),
        "fraud_flags": list(r["fraud_flags"] or []),
        "auto_rescan": bool(r["auto_rescan"]) if r["auto_rescan"] is not None else True,
        "last_error": r["last_error"] or "",
        "last_scanned": _iso(r["last_scanned_at"]),
        "last_ad_seen": _iso(r["last_ad_seen_at"]),
    }


_PLACEMENT_COLS = (
    "channel_username, channel_title, subscribers, views_avg, er_rate, reach_rate, "
    "engagement_rate, quality_score, ad_price_rub, ads_per_week, ad_share, "
    "ad_posts_count, posts_scanned, niches, fraud_flags, auto_rescan, last_error, "
    "last_scanned_at, last_ad_seen_at"
)


async def list_channels(pool, owner_id: int, *, q: str = "", niche: str = "",
                        sort: str = "quality", only_clean: bool = False,
                        limit: int = 50, offset: int = 0) -> dict:
    order = CHANNEL_SORTS.get(sort, CHANNEL_SORTS["quality"])
    where = ["owner_id = $1"]
    args: list = [owner_id]
    if q:
        args.append(_like(q))
        where.append(f"(lower(channel_username) LIKE ${len(args)} "
                     f"OR lower(COALESCE(channel_title,'')) LIKE ${len(args)})")
    if niche:
        args.append(niche)
        where.append(f"${len(args)} = ANY(niches)")
    if only_clean:
        where.append("COALESCE(array_length(fraud_flags, 1), 0) = 0")
    w = " AND ".join(where)
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    rows = await pool.fetch(
        f"SELECT {_PLACEMENT_COLS} FROM ad_placements WHERE {w} "
        f"ORDER BY {order}, id DESC LIMIT {limit} OFFSET {offset}", *args)
    total = await pool.fetchval(f"SELECT COUNT(*) FROM ad_placements WHERE {w}", *args)
    facets = await pool.fetch(
        "SELECT n AS niche, COUNT(*) AS cnt FROM ad_placements, unnest(niches) n "
        "WHERE owner_id=$1 GROUP BY n ORDER BY cnt DESC, n LIMIT 20", owner_id)
    return {
        "items": [placement_row(r) for r in rows],
        "total": int(total or 0),
        "has_more": offset + len(rows) < int(total or 0),
        "niches": [{"niche": f["niche"], "count": int(f["cnt"])} for f in facets],
    }


def _ad_row(r) -> dict:
    return {
        "msg_id": int(r["msg_id"]) if r["msg_id"] is not None else None,
        "channel": r["channel_username"] or "",
        "channel_title": r["channel_title"] or "",
        "advertiser": r["advertiser_username"] or "",
        "advertiser_kind": r["advertiser_kind"] or "",
        "advertiser_kind_label": KIND_LABELS.get(r["advertiser_kind"] or "", ""),
        "link": r["ad_link"] or "",
        "has_erid": bool(r["has_erid"]),
        "confidence": int(r["confidence"] or 0),
        "reasons": list(r["reasons"] or []),
        "text": (r["post_text"] or "")[:600],
        "views": int(r["post_views"] or 0),
        "er": round(float(r["post_er"] or 0), 4),
        "date": _iso(r["post_date"] or r["detected_at"]),
    }


_AD_COLS = (
    "l.msg_id, p.channel_username, p.channel_title, l.advertiser_username, "
    "l.advertiser_kind, l.ad_link, l.has_erid, l.confidence, l.reasons, "
    "l.post_text, l.post_views, l.post_er, l.post_date, l.detected_at"
)


async def channel_detail(pool, owner_id: int, username: str) -> Optional[dict]:
    name = channel_username_from(username)
    if not name:
        return None
    r = await pool.fetchrow(
        f"SELECT id, {_PLACEMENT_COLS} FROM ad_placements "
        "WHERE owner_id=$1 AND lower(channel_username)=lower($2)", owner_id, name)
    if not r:
        return None
    ads = await pool.fetch(
        f"SELECT {_AD_COLS} FROM ad_posts_log l JOIN ad_placements p ON p.id=l.placement_id "
        "WHERE l.owner_id=$1 AND l.placement_id=$2 "
        "ORDER BY COALESCE(l.post_date, l.detected_at) DESC LIMIT 60", owner_id, r["id"])
    advs = await pool.fetch(
        "SELECT advertiser_username, MAX(advertiser_kind) AS kind, COUNT(*) AS posts, "
        "COALESCE(SUM(post_views),0) AS views, MAX(COALESCE(post_date, detected_at)) AS last "
        "FROM ad_posts_log WHERE owner_id=$1 AND placement_id=$2 "
        "AND advertiser_username IS NOT NULL GROUP BY advertiser_username "
        "ORDER BY posts DESC, last DESC LIMIT 30", owner_id, r["id"])
    out = placement_row(r)
    out["ads"] = [_ad_row(a) for a in ads]
    out["advertisers"] = [{
        "username": a["advertiser_username"], "kind": a["kind"] or "",
        "kind_label": KIND_LABELS.get(a["kind"] or "", ""),
        "posts": int(a["posts"]), "views": int(a["views"]), "last_seen": _iso(a["last"]),
    } for a in advs]
    return out


def _advertiser_row(r) -> dict:
    return {
        "username": r["advertiser_username"] or "",
        "kind": r["kind"] or "",
        "kind_label": KIND_LABELS.get(r["kind"] or "", ""),
        "niche": r["niche"] or "",
        "placements": int(r["placements_count"] or 0),
        "channels": int(r["channels_count"] or 0),
        "views": int(r["total_views"] or 0),
        "first_seen": _iso(r["first_seen_at"]),
        "last_seen": _iso(r["last_seen_at"]),
    }


async def list_advertisers(pool, owner_id: int, *, q: str = "", kind: str = "",
                           sort: str = "placements", limit: int = 50,
                           offset: int = 0) -> dict:
    order = ADVERTISER_SORTS.get(sort, ADVERTISER_SORTS["placements"])
    where = ["owner_id = $1", "placements_count > 0"]
    args: list = [owner_id]
    if q:
        args.append(_like(q))
        where.append(f"lower(advertiser_username) LIKE ${len(args)}")
    if kind in ADVERTISER_KINDS:
        args.append(kind)
        where.append(f"kind = ${len(args)}")
    w = " AND ".join(where)
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    rows = await pool.fetch(
        "SELECT advertiser_username, kind, niche, placements_count, channels_count, "
        f"total_views, first_seen_at, last_seen_at FROM ad_advertisers WHERE {w} "
        f"ORDER BY {order}, id DESC LIMIT {limit} OFFSET {offset}", *args)
    total = await pool.fetchval(f"SELECT COUNT(*) FROM ad_advertisers WHERE {w}", *args)
    return {"items": [_advertiser_row(r) for r in rows], "total": int(total or 0),
            "has_more": offset + len(rows) < int(total or 0)}


async def advertiser_detail(pool, owner_id: int, username: str) -> Optional[dict]:
    name = str(username or "").strip().lstrip("@").lower()[:128]
    if not name:
        return None
    r = await pool.fetchrow(
        "SELECT advertiser_username, kind, niche, placements_count, channels_count, "
        "total_views, first_seen_at, last_seen_at FROM ad_advertisers "
        "WHERE owner_id=$1 AND advertiser_username=$2", owner_id, name)
    if not r:
        return None
    chans = await pool.fetch(
        "SELECT p.channel_username, p.channel_title, p.subscribers, p.quality_score, "
        "p.ad_price_rub, COUNT(*) AS posts, COALESCE(SUM(l.post_views),0) AS views, "
        "MAX(COALESCE(l.post_date, l.detected_at)) AS last "
        "FROM ad_posts_log l JOIN ad_placements p ON p.id=l.placement_id "
        "WHERE l.owner_id=$1 AND l.advertiser_username=$2 "
        "GROUP BY p.id ORDER BY posts DESC, last DESC LIMIT 50", owner_id, name)
    ads = await pool.fetch(
        f"SELECT {_AD_COLS} FROM ad_posts_log l JOIN ad_placements p ON p.id=l.placement_id "
        "WHERE l.owner_id=$1 AND l.advertiser_username=$2 "
        "ORDER BY COALESCE(l.post_date, l.detected_at) DESC LIMIT 40", owner_id, name)
    out = _advertiser_row(r)
    out["channels_list"] = [{
        "username": c["channel_username"], "title": c["channel_title"] or "",
        "subscribers": int(c["subscribers"] or 0),
        "quality_score": round(float(c["quality_score"] or 0), 1),
        "ad_price_rub": int(c["ad_price_rub"] or 0),
        "posts": int(c["posts"]), "views": int(c["views"]), "last_seen": _iso(c["last"]),
    } for c in chans]
    out["ads"] = [_ad_row(a) for a in ads]
    # Канал-рекламодатель можно самому поставить на разведку: там его
    # рекламодатели — следующий слой рынка.
    out["can_track"] = (r["kind"] in ("channel", None)
                        and bool(channel_username_from(name)))
    out["tracked"] = bool(await pool.fetchval(
        "SELECT 1 FROM ad_placements WHERE owner_id=$1 AND lower(channel_username)=$2",
        owner_id, name))
    return out


async def list_ads(pool, owner_id: int, *, q: str = "", erid_only: bool = False,
                   days: int = 0, limit: int = 50, offset: int = 0) -> dict:
    where = ["l.owner_id = $1"]
    args: list = [owner_id]
    if q:
        args.append(_like(q))
        where.append(f"(lower(COALESCE(l.post_text,'')) LIKE ${len(args)} "
                     f"OR lower(COALESCE(l.advertiser_username,'')) LIKE ${len(args)})")
    if erid_only:
        where.append("l.has_erid")
    if days and int(days) > 0:
        args.append(int(days))
        where.append(f"COALESCE(l.post_date, l.detected_at) >= NOW() - make_interval(days => ${len(args)})")
    w = " AND ".join(where)
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    rows = await pool.fetch(
        f"SELECT {_AD_COLS} FROM ad_posts_log l JOIN ad_placements p ON p.id=l.placement_id "
        f"WHERE {w} ORDER BY COALESCE(l.post_date, l.detected_at) DESC, l.id DESC "
        f"LIMIT {limit} OFFSET {offset}", *args)
    total = await pool.fetchval(
        f"SELECT COUNT(*) FROM ad_posts_log l WHERE {w}", *args)
    return {"items": [_ad_row(r) for r in rows], "total": int(total or 0),
            "has_more": offset + len(rows) < int(total or 0)}


DISCOVERY_LIMIT = 10


async def discovery_candidates(pool, owner_id: int, *,
                               limit: int = DISCOVERY_LIMIT) -> list[dict]:
    """Следующий слой рынка: каналы, которые РЕКЛАМИРУЮТСЯ в разведанных, но
    сами ещё не разведаны.

    Рекламодатель-канал покупает рекламу — значит, сам продаёт её или держит
    аудиторию той же ниши; его рекламодатели — это ещё один круг рынка. Раньше
    поставить его на разведку можно было только по одному, открыв карточку
    рекламодателя, и никто не подсказывал, с кого начать. Порядок: чем в
    большем числе каналов и чаще он рекламируется, тем он важнее для ниши.

    Закрытые каналы, боты и сайты сюда не попадают: их не сканировать.
    """
    limit = max(1, min(int(limit or DISCOVERY_LIMIT), 50))
    rows = await pool.fetch(
        "SELECT a.advertiser_username, a.placements_count, a.channels_count, "
        "a.total_views, a.last_seen_at FROM ad_advertisers a "
        "WHERE a.owner_id=$1 AND a.placements_count > 0 "
        "AND COALESCE(a.kind, 'channel') = 'channel' "
        "AND NOT EXISTS (SELECT 1 FROM ad_placements p WHERE p.owner_id=a.owner_id "
        "                AND lower(p.channel_username)=lower(a.advertiser_username)) "
        "ORDER BY a.channels_count DESC NULLS LAST, a.placements_count DESC, "
        "a.total_views DESC NULLS LAST, a.last_seen_at DESC NULLS LAST "
        "LIMIT $2", owner_id, limit * 2)
    out: list[dict] = []
    for r in rows:
        name = channel_username_from(r["advertiser_username"] or "")
        if not name:
            continue
        out.append({
            "username": name,
            "placements": int(r["placements_count"] or 0),
            "channels": int(r["channels_count"] or 0),
            "views": int(r["total_views"] or 0),
            "last_seen": _iso(r["last_seen_at"]),
        })
        if len(out) >= limit:
            break
    return out


async def delete_channel(pool, owner_id: int, username: str) -> bool:
    """Снять канал с разведки: его посты уходят, рекламодатели пересчитываются."""
    name = channel_username_from(username)
    if not name:
        return False
    async with pool.acquire() as conn:
        async with conn.transaction():
            pid = await conn.fetchval(
                "SELECT id FROM ad_placements WHERE owner_id=$1 "
                "AND lower(channel_username)=lower($2)", owner_id, name)
            if not pid:
                return False
            touched = await _advertisers_of_placement(conn, pid)
            await conn.execute("DELETE FROM ad_posts_log WHERE placement_id=$1", pid)
            await conn.execute(
                "DELETE FROM ad_placements WHERE id=$1 AND owner_id=$2", pid, owner_id)
            await refresh_advertisers(conn, owner_id, touched)
    return True


async def set_auto_rescan(pool, owner_id: int, username: str, on: bool) -> bool:
    name = channel_username_from(username)
    if not name:
        return False
    res = await pool.execute(
        "UPDATE ad_placements SET auto_rescan=$3 WHERE owner_id=$1 "
        "AND lower(channel_username)=lower($2)", owner_id, name, bool(on))
    return str(res).endswith(" 1")


async def channels_due_for_rescan(pool, *, hours: int = AUTO_RESCAN_HOURS,
                                  per_owner: int = 5, limit: int = 200) -> list[dict]:
    """Каналы, которые пора обновить: автообновление включено, скан старше
    `hours`, не больше `per_owner` на владельца за раз (чтобы один большой
    список не съел флот), самые давние первыми."""
    rows = await pool.fetch(
        """
        SELECT owner_id, channel_username FROM (
            SELECT owner_id, channel_username, last_scanned_at,
                   ROW_NUMBER() OVER (PARTITION BY owner_id
                                      ORDER BY last_scanned_at NULLS FIRST) AS rn
            FROM ad_placements
            WHERE COALESCE(auto_rescan, TRUE)
              AND (last_scanned_at IS NULL
                   OR last_scanned_at < NOW() - make_interval(hours => $1))
        ) t WHERE rn <= $2
        ORDER BY last_scanned_at NULLS FIRST LIMIT $3
        """,
        int(hours), int(per_owner), int(limit))
    return [{"owner_id": int(r["owner_id"]), "channel": r["channel_username"]} for r in rows]




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
