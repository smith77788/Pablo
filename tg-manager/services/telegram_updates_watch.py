"""Сторож обновлений Telegram для разработчиков: новая версия Bot API — сразу админу.

ЗАЧЕМ. Владелец попросил внедрить HTMLBubbles — интерактивные HTML-виджеты
прямо в чатах (Telegram 13.2). Изучение документации 05.11.2026 показало:
приложения их уже показывают, но публичного API, чтобы их ОТПРАВЛЯТЬ, нет.
Официальный журнал Bot API заканчивается версией 10.3 (24 августа 2026), в
нём нет ни одного метода или типа про HTML-виджеты; в слоях MTProto тоже;
Telegram Info прямо пишет «публичный API для разработчиков пока не
представлен», а пресса говорит о готовящемся Bot API 10.4. Отправлять такой
виджет продукту сегодня нечем.

Поэтому вместо догадок о формате, который ещё не опубликован, — сторож:
раз в несколько часов он читает официальный журнал Bot API и, как только там
появляется новая версия, пишет администраторам платформы, что вышло и есть
ли в ней HTML-виджеты. Узнать об этом в день выхода, а не из чужого канала
через неделю, — ровно то, что нужно, чтобы внедрить первыми.

Устройство:
* разбор журнала — ЧИСТЫЕ функции (тестируются на сохранённом HTML);
* последняя увиденная версия лежит в platform_settings (переживает деплой);
* первый запуск только запоминает текущую версию — без сообщения: иначе
  каждая новая установка «объявила» бы давно вышедшую 10.3;
* получатели — общая дверь platform_alert_recipients. Если сообщить некому,
  отметка НЕ двигается: новость не теряется, её получит первый появившийся
  админ.
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import re

log = logging.getLogger(__name__)

CHANGELOG_URL = "https://core.telegram.org/bots/api-changelog"
STATE_KEY = "tg_bot_api_seen_version"
CHECK_EVERY_S = 6 * 3600
_FETCH_TIMEOUT_S = 30

_H4_RE = re.compile(r"<h4>(.*?)</h4>", re.S | re.I)
_VER_RE = re.compile(r"<strong>\s*Bot API\s+(\d+(?:\.\d+)*)\s*</strong>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_LI_RE = re.compile(r"<li>(.*?)</li>", re.S | re.I)
# HTMLBubbles в документации будут называться виджетами или «пузырями»; слово
# «html» одно не годится — журнал давно пишет про «HTML formatting».
_WIDGET_RE = re.compile(r"html\s*bubble|bubble|widget", re.I)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub(" ", fragment or ""))).strip()


def parse_changelog(page: str) -> list[dict]:
    """Релизы журнала, от новых к старым: [{version, date, items, widgets}].

    Раздел — от заголовка даты (<h4>) до следующего. Раздел без «Bot API X.Y»
    (старые записи без номера) пропускается.
    """
    out: list[dict] = []
    heads = list(_H4_RE.finditer(page or ""))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(page)
        body = page[h.end():end]
        ver = _VER_RE.search(body)
        if not ver:
            continue
        items = [_text(x) for x in _LI_RE.findall(body)]
        out.append({
            "version": ver.group(1),
            "date": _text(h.group(1)),
            "items": [x for x in items if x],
            "widgets": bool(_WIDGET_RE.search(_text(body))),
        })
    return out


def version_key(v: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in str(v or "").split("."))
    except ValueError:
        return ()


def newer_than(releases: list[dict], seen: str | None) -> list[dict]:
    """Релизы новее увиденного, от старых к новым (в таком порядке и сообщать)."""
    s = version_key(seen)
    uniq: dict[tuple[int, ...], dict] = {}
    for r in releases:  # одна версия дважды (вёрстка, якоря) — одно сообщение
        k = version_key(r["version"])
        if k > s:
            uniq.setdefault(k, r)
    return [uniq[k] for k in sorted(uniq)]


_MONTHS_RU = {"january": "января", "february": "февраля", "march": "марта", "april": "апреля",
              "may": "мая", "june": "июня", "july": "июля", "august": "августа",
              "september": "сентября", "october": "октября", "november": "ноября",
              "december": "декабря"}


def ru_date(date_en: str) -> str:
    """«August 24, 2026» → «24 августа 2026». Незнакомый вид — как есть."""
    m = re.match(r"\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", date_en or "")
    if not m or m.group(1).lower() not in _MONTHS_RU:
        return date_en or ""
    return f"{int(m.group(2))} {_MONTHS_RU[m.group(1).lower()]} {m.group(3)}"


def alert_text(rel: dict) -> str:
    """Сообщение админу по-русски. Пункты журнала — на английском, поэтому их
    не пересказываем, а называем число и даём ссылку."""
    lines = [f"🆕 Telegram выпустил Bot API {rel['version']} ({ru_date(rel['date'])})."]
    if rel.get("widgets"):
        lines.append("В обновлении есть HTML-виджеты (HTMLBubbles) — интерактивные "
                     "сообщения в чатах. Можно внедрять их отправку в Infragram.")
    else:
        lines.append("HTML-виджетов (HTMLBubbles) в этом обновлении пока нет.")
    n = len(rel.get("items") or [])
    if n:
        lines.append(f"Изменений в журнале: {n}.")
    lines.append(f"Подробности: {CHANGELOG_URL}")
    return "\n".join(lines)


# ── Состояние и рассылка ────────────────────────────────────────────────────

async def _seen(pool) -> str | None:
    return await pool.fetchval("SELECT value FROM platform_settings WHERE key=$1", STATE_KEY)


async def _remember(pool, version: str) -> None:
    await pool.execute(
        "INSERT INTO platform_settings(key, value, updated_at) VALUES($1,$2,NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
        STATE_KEY, version)


async def _fetch_page() -> str:
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(CHANGELOG_URL) as r:
            r.raise_for_status()
            return await r.text()


async def check_once(pool, bot, *, fetch=None, recipients=None) -> list[dict]:
    """Одна проверка. Возвращает релизы, о которых сообщили (для тестов и журнала).

    fetch / recipients — подмены для тестов; по умолчанию сеть и общая дверь
    получателей платформенных алертов.
    """
    page = await (fetch or _fetch_page)()
    releases = parse_changelog(page)
    if not releases:
        # Страница сменила вёрстку — молчать нельзя: сторож без единой записи
        # выглядит как «обновлений нет».
        log.warning("telegram_updates_watch: в журнале Bot API не найдено ни одной версии")
        return []
    latest = max(releases, key=lambda r: version_key(r["version"]))
    seen = await _seen(pool)
    if not seen:
        await _remember(pool, latest["version"])
        log.info("telegram_updates_watch: точка отсчёта — Bot API %s", latest["version"])
        return []
    fresh = newer_than(releases, seen)
    if not fresh:
        return []
    if recipients is None:
        from bot.utils.subscription import platform_alert_recipients
        recipients = await platform_alert_recipients(pool)
    if not recipients:
        log.warning("telegram_updates_watch: вышел Bot API %s, но сообщить некому — "
                    "отметка не сдвинута", latest["version"])
        return []
    delivered = False
    for rel in fresh:
        text = alert_text(rel)
        for uid in recipients:
            try:
                await bot.send_message(int(uid), text)
                delivered = True
            except Exception:
                log.warning("telegram_updates_watch: не доставлено uid=%s", uid, exc_info=True)
    if delivered:
        await _remember(pool, latest["version"])
    return fresh


async def run(pool, bot) -> None:
    """Фоновый цикл: проверка раз в CHECK_EVERY_S. Сбой проверки — не сбой цикла."""
    log.info("telegram_updates_watch: запущен")
    while True:
        try:
            await check_once(pool, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("telegram_updates_watch: проверка не удалась", exc_info=True)
        await asyncio.sleep(CHECK_EVERY_S)
