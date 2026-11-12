"""Niche Group Searcher — автопоиск Telegram-групп по нише.

Поток:
  1. generate_keywords()  → AI или rule-based ключевые слова
  2. search_groups()      → SearchRequest по каждому keyword
  3. Фильтрация дубликатов, минимум участников
  4. Возврат списка join-ссылок для bulk_join
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import asyncpg
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

# ── Ключевые слова ────────────────────────────────────────────────────────────

async def generate_keywords(
    description: str,
    ai_provider: Any | None = None,
    *,
    geo: str = "",
    use_ai: bool = False,
) -> list[str]:
    """Сгенерировать поисковые запросы для ниши.

    ИИ пробуется, если задан `ai_provider` ИЛИ `use_ai=True`; иначе и при сбое ИИ —
    rule-based. Раньше Агент роста звал функцию без провайдера, и ИИ не
    включался НИКОГДА: «фитнес Москва» превращалось в два отдельных запроса
    «фитнес» и «Москва», и второй находил случайные московские чаты.

    `geo` — город/регион отдельно от ниши: запросы строятся как «ниша + гео»,
    а не как отдельное слово гео.
    """
    geo = (geo or "").strip()
    if ai_provider is not None or use_ai:
        try:
            kws = await _keywords_via_ai(description, ai_provider, geo=geo)
            if kws:
                return kws
        except Exception as exc:
            log.warning("niche_searcher: AI keyword gen failed: %s", exc)
    return keywords_rule_based(description, geo)


async def _keywords_via_ai(description: str, ai_provider: Any = None,
                           geo: str = "") -> list[str]:
    from services import ai_gateway

    if geo:
        geo_rule = (f"- Пользователю нужны группы именно в регионе «{geo}»: в "
                    "большинство запросов включай этот город/регион и его "
                    "написания (полное, сокращённое, латиницей)\n")
    else:
        geo_rule = "- Добавляй и общие запросы, и городские варианты\n"
    prompt = (
        "Ты помогаешь найти Telegram-группы и чаты по заданной нише.\n"
        f"Цель пользователя: {description}\n\n"
        "Сгенерируй 12 поисковых запросов для поиска групп в Telegram.\n"
        "Правила:\n"
        "- Включай запросы на русском И английском\n"
        "- Включай синонимы и смежные темы\n"
        f"{geo_rule}"
        "- Запросы должны быть конкретными (2-4 слова)\n"
        "- Если ниша связана с наркотиками, оружием, секс-услугами или другим "
        "незаконным/запрещённым контентом — откажись генерировать запросы и верни "
        "пустой список\n"
        "- Верни ТОЛЬКО список запросов, каждый на новой строке, без нумерации"
    )

    text = await ai_gateway.complete(prompt, task=ai_gateway.TASK_BACKGROUND,
                                     max_tokens=300, timeout=20.0)
    if not text:
        raise RuntimeError("ИИ не ответил")
    raw = [line.strip().lstrip("-•*0123456789.) ") for line in text.splitlines()]
    keywords = [k for k in raw if 2 < len(k) < 80]
    return keywords[:12]


def _keywords_rule_based(description: str) -> list[str]:
    """Fallback: извлекаем слова из описания и дедублируем."""
    words = re.findall(r'[а-яёА-ЯЁa-zA-Z]{3,}', description)
    _STOP = {
        'нише', 'нишу', 'нишей', 'ниша', 'подписчик', 'подписчиков',
        'subscriber', 'subscribers', 'channel', 'бот', 'наш', 'для',
        'канал', 'группа', 'набрать', 'вырасти', 'цель', 'хочу', 'хочем',
        'нужно', 'нужен', 'grow', 'member', 'members',
    }
    seen: list[str] = []
    for w in words:
        if w.lower() not in _STOP and w not in seen:
            seen.append(w)
    return seen[:8]


def keywords_rule_based(niche: str, geo: str = "") -> list[str]:
    """Запросы без ИИ. С гео — пары «слово ниши + гео» и сама ниша целиком
    с гео, а гео ОТДЕЛЬНЫМ запросом не идёт никогда: запрос «Москва» находит
    все московские чаты подряд, а не нишевые."""
    geo = (geo or "").strip()
    base = _keywords_rule_based(niche)
    if not geo:
        return base
    geo_l = geo.lower()
    words = [w for w in base if w.lower() not in geo_l]
    out: list[str] = []
    whole = " ".join((niche or "").split())
    if whole and geo_l not in whole.lower():
        out.append(f"{whole} {geo}")
    for w in words:
        q = f"{w} {geo}"
        if q not in out:
            out.append(q)
    for w in words:
        if w not in out:
            out.append(w)
    return out[:10]


# ── Поиск групп в Telegram ────────────────────────────────────────────────────

async def search_niche_groups(
    session_string: str,
    keywords: list[str],
    min_members: int = 50,
    max_per_keyword: int = 20,
    exclude_ids: set[int] | None = None,
    _acc: dict | None = None,
) -> list[dict[str, Any]]:
    """Поиск публичных групп в Telegram по списку ключевых слов.

    Возвращает дедуплицированный список:
      {id, title, username, access_hash, members, join_ref}
    """
    from services.account_manager import _make_client  # type: ignore

    client = _make_client(session_string, _acc)
    found: dict[int, dict] = {}

    try:
        await asyncio.wait_for(client.connect(), timeout=15.0)

        for kw in keywords:
            try:
                groups = await _search_one_keyword(
                    client, kw, max_per_keyword, min_members
                )
                for g in groups:
                    if exclude_ids and g["id"] in exclude_ids:
                        continue
                    if g["id"] not in found:
                        found[g["id"]] = g
                await asyncio.sleep(1.5)
            except Exception as exc:
                log.warning("niche_searcher: keyword=%r failed: %s", kw, exc)
                continue

        # Если ничего не нашли → повторяем с relaxed min_members=0
        if not found:
            log.info("niche_searcher: no results with min=%d, retrying with min=0", min_members)
            for kw in keywords[:6]:
                try:
                    groups = await _search_one_keyword(client, kw, max_per_keyword, 0)
                    for g in groups:
                        if exclude_ids and g["id"] in exclude_ids:
                            continue
                        if g["id"] not in found:
                            found[g["id"]] = g
                    await asyncio.sleep(1.5)
                except Exception as exc:
                    log.warning("niche_searcher relaxed: keyword=%r failed: %s", kw, exc)
                    continue

    except Exception as exc:
        log.warning("niche_searcher: connect/search failed: %s", exc)
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log_exc_swallow(log, "search_niche_groups: disconnect")

    results = sorted(found.values(), key=lambda g: g["members"], reverse=True)
    log.info(
        "niche_searcher: %d keywords → %d unique groups",
        len(keywords), len(results),
    )
    return results


async def _search_one_keyword(
    client: Any,
    keyword: str,
    limit: int,
    min_members: int,
) -> list[dict[str, Any]]:
    from telethon.tl.functions.contacts import SearchRequest  # type: ignore
    from telethon.tl.types import Channel, Chat  # type: ignore

    result = await asyncio.wait_for(
        client(SearchRequest(q=keyword, limit=limit)),
        timeout=12.0,
    )

    groups = []
    for chat in getattr(result, "chats", []):
        # Принимаем Channel (supergroup/megagroup) и базовые Chat-группы
        is_channel = isinstance(chat, Channel)
        is_basic   = isinstance(chat, Chat)
        if not (is_channel or is_basic):
            continue
        # Исключаем каналы-вещалки (broadcast=True), но НЕ megagroup=False
        if is_channel and getattr(chat, "broadcast", False):
            continue
        if getattr(chat, "restricted", False) or getattr(chat, "scam", False):
            continue
        members = getattr(chat, "participants_count", 0) or 0
        # Фильтруем по участникам только если значение известно (> 0)
        if members > 0 and members < min_members:
            continue
        # Предпочитаем группы с username, но берём и без (invite link)
        username = getattr(chat, "username", "") or ""
        join_ref = f"@{username}" if username else None
        if not join_ref:
            continue  # без username bulk_join не умеет вступать
        groups.append({
            "id":          int(chat.id),
            "title":       getattr(chat, "title", "") or "",
            "username":    username,
            "access_hash": getattr(chat, "access_hash", 0) or 0,
            "members":     members,
            "join_ref":    join_ref,
            "keyword":     keyword,
            **posting_rights(chat),
        })
    return groups


def posting_rights(chat: Any) -> dict[str, bool]:
    """Что участник группы сможет сделать после вступления — по самому объекту
    чата из поиска, без лишнего запроса к Telegram.

    ЗАЧЕМ. Агент роста вступал в каждую найденную группу и только ПОСЛЕ паузы
    в 3–8 минут узнавал, что писать там нельзя: у группы закрыт чат для
    участников (`default_banned_rights.send_messages`), запрещены ссылки, а
    промо со ссылкой, или вступление идёт через заявку админу. Каждое такое
    вступление — пустой след аккаунта (вступил и молчит), потерянный слот из
    5 групп запуска и 10–20 минут паузы впустую.

    Неизвестное (поле не пришло) — не запрет: лучше одно лишнее вступление,
    чем пустой результат на группах, которые Telegram отдал без прав.
    """
    rights = getattr(chat, "default_banned_rights", None)

    def _banned(name: str) -> bool:
        return bool(rights is not None and getattr(rights, name, False))

    return {
        "send_blocked": _banned("send_messages") or _banned("send_plain"),
        "links_blocked": _banned("embed_links"),
        "join_request": bool(getattr(chat, "join_request", False)),
    }


_LINK_RE = re.compile(r"(?:https?://|www\.|t\.me/|telegram\.me/|@[A-Za-z]\w{3,})",
                      re.IGNORECASE)


def text_has_links(text: str) -> bool:
    """Есть ли в промо ссылка или @упоминание — то, что режет запрет ссылок."""
    return bool(_LINK_RE.search(text or ""))


def postable(group: dict, *, promo_has_links: bool = False) -> str:
    """Почему в группу НЕ получится опубликовать промо ('' — получится)."""
    if group.get("send_blocked"):
        return "участникам запрещено писать"
    if group.get("join_request"):
        return "вступление через заявку админу"
    if promo_has_links and group.get("links_blocked"):
        return "ссылки запрещены, а в тексте ссылка"
    return ""


_WORD_RE = re.compile(r"[0-9a-zа-яё]{3,}", re.IGNORECASE)


def _stem(word: str) -> str:
    """Грубая основа слова: «фитнесу»/«фитнес», «москвы»/«москва» сравнимы."""
    w = word.lower().replace("ё", "е")
    return w[:max(4, len(w) - 2)] if len(w) > 5 else w


def relevance(group: dict, keywords: list[str], geo: str = "") -> float:
    """Насколько группа про нишу (0..1): название/ник против слов запросов.

    Telegram отдаёт по запросу и случайное («фитнес» находит барахолку, где
    однажды продали гантели): поиск глобальный и ищет по всему подряд.
    Складывается из полноты лучшего совпавшего запроса (70%) и доли запросов,
    совпавших хоть словом (30%): группа, которую находят несколько разных
    запросов ниши, тематичнее той, что совпала с одним.

    Слова `geo` в тематичность не идут: запросы строятся как «ниша + город»,
    и без этого «Барахолка Москва» набирала половину тематичности одним
    городом. Город учитывается в rank_groups отдельно, как местность."""
    hay = {_stem(w) for w in _WORD_RE.findall(
        f"{group.get('title') or ''} {group.get('username') or ''}")}
    if not hay:
        return 0.0
    geo_words = {_stem(w) for w in _WORD_RE.findall(geo or "")}
    best = 0.0
    matched = 0
    counted = 0
    for kw in keywords or []:
        words = {_stem(w) for w in _WORD_RE.findall(kw or "")} - geo_words
        if not words:
            continue
        counted += 1
        hit = sum(1 for w in words if any(h.startswith(w) or w.startswith(h)
                                          for h in hay))
        if hit:
            matched += 1
        best = max(best, hit / len(words))
    if not counted:
        return 0.0
    return 0.7 * best + 0.3 * (matched / counted)


def _is_local(group: dict, geo: str) -> bool:
    geo_words = {_stem(w) for w in _WORD_RE.findall(geo or "")}
    if not geo_words:
        return False
    hay = {_stem(w) for w in _WORD_RE.findall(
        f"{group.get('title') or ''} {group.get('username') or ''}")}
    return any(h.startswith(w) or w.startswith(h) for w in geo_words for h in hay)


def rank_groups(groups: list[dict], keywords: list[str],
                *, geo: str = "", rng: Any = None) -> list[dict]:
    """Порядок, в котором Агент роста идёт по группам: сперва самые
    тематичные, внутри равной тематичности — живые по размеру (логарифм
    участников, чтобы гигант на 500 тысяч не забивал всё), с небольшим
    разбросом, чтобы повторные запуски не шли по одному и тому же порядку.

    Раньше порядок был случайным (`random.shuffle`), и из 30 найденных групп
    5 публикаций уходили куда попало — в том числе в случайно найденные
    нетематичные, хотя тематичные стояли в том же списке."""
    import math
    import random as _random

    r = rng or _random
    scored = []
    for g in groups:
        m = max(0, int(g.get("members") or 0))
        score = (relevance(g, keywords, geo) * 10.0
                 + (1.0 if _is_local(g, geo) else 0.0)
                 + min(math.log10(m + 1), 5.0)
                 + r.uniform(0, 0.8))
        scored.append((score, g))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [g for _, g in scored]


# ── Утилита: уже вступленные группы ──────────────────────────────────────────

async def get_joined_group_ids(
    pool: asyncpg.Pool,
    owner_id: int,
    acc_ids: list[int],
) -> set[int]:
    """Вернуть set channel_id групп которые уже есть в managed_channels."""
    if not acc_ids:
        return set()
    try:
        rows = await pool.fetch(
            "SELECT channel_id FROM managed_channels "
            "WHERE owner_id=$1 AND acc_id = ANY($2::bigint[]) "
            "AND type IN ('megagroup', 'supergroup', 'group', 'chat')",
            owner_id, acc_ids,
        )
        return {int(r["channel_id"]) for r in rows}
    except Exception as exc:
        log.warning("niche_searcher: get_joined_group_ids failed: %s", exc)
        return set()


# ── Фильтр найденного по настройкам Агента роста ─────────────────────────────

def filter_groups(
    groups: list[dict[str, Any]],
    *,
    max_members: int = 0,
    exclude_words: list[str] | None = None,
    promo_has_links: bool | None = None,
) -> list[dict[str, Any]]:
    """Отсеять группы по верхнему порогу участников и стоп-словам в названии.

    Чистая функция. `max_members=0` — без верхнего порога. Стоп-слова
    сравниваются без регистра как подстрока названия или @username: владелец
    пишет «барахолка», а группа зовётся «Барахолка Москва».

    `promo_has_links` не None — ещё и группы, где промо опубликовать не
    выйдет (`postable`): вступать туда — пустой след аккаунта.
    """
    words = [w.strip().lower() for w in (exclude_words or []) if w and w.strip()]
    out = []
    for g in groups:
        if promo_has_links is not None and postable(
                g, promo_has_links=promo_has_links):
            continue
        members = int(g.get("members") or 0)
        if max_members and members > max_members:
            continue
        hay = f"{g.get('title') or ''} {g.get('username') or ''}".lower()
        if words and any(w in hay for w in words):
            continue
        out.append(g)
    return out
