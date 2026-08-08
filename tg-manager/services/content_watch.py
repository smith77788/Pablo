"""Resource Compliance Scanner — детект запрещённой тематики в ресурсах.

Пиллар «снос из поиска / блокирование ресурсов с запрещённой тематикой», но в
БЕЗОПАСНОЙ форме: это ДЕТЕКТОР + доказательное досье, а НЕ автомат массовых жалоб.

Что делает: подключается read-only к каналу/чату, тянет заголовок/описание и
недавние сообщения, прогоняет каждый текст через уже существующий
`content_safety.scan_text` (детерминированный детект CSAM/терроризма с анти-обфускацией)
и собирает досье: вердикт + категории + цитаты-улики. По этому досье оператор
ПРИНИМАЕТ РЕШЕНИЕ вручную (в т.ч. через существующий strike_engine для адресной
жалобы) — здесь НЕТ авто-отправки жалоб и НЕТ массового taкедауна по списку.

Граница (см. OPERATOR_TOP1_ROADMAP «Strike граница по этике» и CLAUDE.md):
не строим оружие ложных массовых жалоб на чужие ресурсы. Детект настоящей
запрещёнки (CSAM/террор) + улики для осознанного действия — да; авто-снос — нет.

Модуль изолирован (свой файл), чтобы не конфликтовать с параллельными агентами.
"""
from __future__ import annotations

import asyncio
import logging

from services import content_safety

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 15.0
_ACTION_TIMEOUT = 25.0
_EXCERPT_LEN = 160
# Человекочитаемые ярлыки категорий детектора (для досье оператору).
CATEGORY_LABELS = {
    content_safety.CATEGORY_CSAM: "CSAM (эксплуатация несовершеннолетних)",
    content_safety.CATEGORY_TERROR: "терроризм/экстремизм",
}


def _excerpt(text: str) -> str:
    """Короткая цитата-улика: схлопнуть пробелы, обрезать."""
    t = " ".join((text or "").split())
    return t[:_EXCERPT_LEN]


def classify_texts(items: list[tuple[str, str]]) -> dict:
    """Прогнать (label, text)-пары через content_safety и собрать досье.

    Чистая функция — тестируется без сети. Каждый текст проверяется отдельно
    (scan_text короткозамыкается на первом совпадении внутри одного текста),
    улики агрегируются по всем текстам.

    Возвращает:
      {
        "verdict":   "prohibited" | "clean",
        "categories": ["csam", ...],          # уникальные, порядок обнаружения
        "hits": [{"label","category","category_label","rule","excerpt"}...],
        "scanned":   <сколько текстов проверено>,
      }
    """
    hits: list[dict] = []
    categories: list[str] = []
    scanned = 0
    for label, text in items or []:
        if not text or not str(text).strip():
            continue
        scanned += 1
        v = content_safety.scan_text(str(text))
        if v.blocked:
            cat = v.category or "unknown"
            if cat not in categories:
                categories.append(cat)
            hits.append({
                "label": label,
                "category": cat,
                "category_label": CATEGORY_LABELS.get(cat, cat),
                "rule": v.rule,
                "excerpt": _excerpt(str(text)),
            })
    return {
        "verdict": "prohibited" if hits else "clean",
        "categories": categories,
        "hits": hits,
        "scanned": scanned,
    }


async def scan_resource(
    session_string: str,
    _acc: dict | None,
    resource_ref: str,
    limit: int = 50,
    low_risk: bool = True,
) -> dict:
    """Read-only: подключиться, собрать заголовок/описание + недавние сообщения
    ресурса и классифицировать через content_safety. Ничего не постит и не жалуется.

    Возвращает досье: {ok, resource, verdict, categories, hits, scanned, error}.
    low_risk=True: чистое чтение — политике прокси разрешён fallback для аккаунтов
    БЕЗ назначенного прокси (для назначенных прокси-изоляция сохраняется)."""
    from telethon.tl.functions.channels import GetFullChannelRequest
    from services.account_manager import _make_client

    ref = (resource_ref or "").strip()
    out = {"ok": False, "resource": ref, "verdict": None, "categories": [],
           "hits": [], "scanned": 0, "error": None}
    if not ref:
        out["error"] = "пустая ссылка"
        return out

    client = _make_client(session_string, _acc, low_risk=low_risk)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(ref)
        items: list[tuple[str, str]] = []
        title = getattr(entity, "title", None) or getattr(entity, "username", "") or ""
        if title:
            items.append(("заголовок", title))
        # Описание (about) — только для каналов/супергрупп.
        try:
            full = await asyncio.wait_for(client(GetFullChannelRequest(entity)),
                                          timeout=_ACTION_TIMEOUT)
            about = getattr(getattr(full, "full_chat", None), "about", "") or ""
            if about:
                items.append(("описание", about))
        except Exception:
            pass  # не канал / нет прав на full — не критично, идём по сообщениям
        try:
            msgs = await asyncio.wait_for(
                client.get_messages(entity, limit=max(1, min(int(limit or 50), 200))),
                timeout=_ACTION_TIMEOUT)
            for m in (msgs or []):
                txt = (getattr(m, "message", "") or "").strip()
                if txt:
                    items.append((f"сообщение #{getattr(m, 'id', '?')}", txt))
        except Exception as exc:
            log.debug("content_watch get_messages: %s", exc)

        dossier = classify_texts(items)
        out.update({
            "ok": True,
            "resource": getattr(entity, "username", None) or title or ref,
            "verdict": dossier["verdict"],
            "categories": dossier["categories"],
            "hits": dossier["hits"],
            "scanned": dossier["scanned"],
        })
        return out
    except Exception as exc:
        out["error"] = str(exc)[:150]
        return out
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
