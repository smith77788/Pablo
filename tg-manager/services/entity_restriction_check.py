"""Проверка НАШИХ каналов/чатов/ботов на ограничения и теневой бан Telegram.

Отличие от `check_account_status_full` (services/account_manager.py): та проверяет
АККАУНТЫ флота через @SpamBot. Здесь — публичные СУЩНОСТИ владельца (каналы, чаты,
боты) на то, что реально отдаёт Telegram API в 2026:

  • официальное ограничение — флаг `restricted` + `restriction_reason` на сущности
    (порно/копирайт/блок по стране). Приходит при резолве, 100% достоверно;
  • резолвится ли `@username` снаружи вообще — если по прямой ссылке с чистого
    аккаунта сущность не открывается, она скрыта/удалена/забанена;
  • видимость в ГЛОБАЛЬНОМ поиске (`contacts.Search`) с аккаунта, не связанного с
    сущностью — если по ссылке резолвится, но в поиске не находится, это и есть
    «скрыт из поиска» (теневой бан публичной сущности);
  • для бота — отвечает ли он на `/start` (жив и может писать).

Чего здесь НЕТ намеренно: «процента понижения охвата» и точной причины теневого
бана — таких данных Telegram не отдаёт, выдумывать их нельзя. Приватные сущности
(без @username) в поиске не участвуют по определению — для них проверяется только
`restricted`.

Модуль — ЧИСТАЯ логика классификации (без сети/БД): резолв и Telethon-вызовы
делает исполнитель операции, а сюда передаёт снятые сигналы. Так классификатор
детерминирован и полностью тестируем. Находки исполнитель пишет в
`restriction_events` (общий пульс), чтобы `shadowban_monitor`/`get_account_health`
оставались единственным источником правды, а не появился второй.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# Ошибки резолва, означающие, что сущности снаружи НЕТ (скрыта/удалена/забанена),
# а не что сбоит наш аккаунт. Разделение критично: флуд/бан НАШЕЙ сессии нельзя
# засчитывать сущности как «недоступна» (это был бы ложный вердикт).
_UNREACHABLE_MARKERS = (
    "no user has", "nobody is using", "username_invalid", "not occupied",
    "username_not_occupied", "cannot find any entity", "cannot find the input entity",
    "not found", "no such", "channel_invalid", "channel_private", "peer_id_invalid",
    "the channel specified is private", "unacceptable",
)
# Ошибки НАШЕГО аккаунта/сети — это сбой проверки, НЕ вердикт по сущности.
_OWN_FAULT_MARKERS = (
    "flood", "wait of", "too many", "auth_key", "session_revoked", "session_expired",
    "user_deactivated", "phone_number_banned", "timeout", "connection", "proxy",
)


def _classify_resolve_error(err: str) -> tuple[bool, Optional[str]]:
    """Ошибка резолва → (resolved_false, probe_error).

    resolved_false=True  → сущности снаружи нет (вердикт «недоступна»).
    probe_error=str      → сбой нашей стороны (проверку не выполнили).
    Своя-ошибка проверяется ПЕРВОЙ: FloodWait нашей сессии не должен ложно
    зачитаться сущности как недоступность.
    """
    low = (err or "").lower()
    if any(m in low for m in _OWN_FAULT_MARKERS):
        return (False, err[:160] or "ошибка подключения")
    if any(m in low for m in _UNREACHABLE_MARKERS):
        return (True, None)
    # Незнакомая ошибка — честнее пометить как сбой проверки, чем выдать вердикт.
    return (False, err[:160] or "неизвестная ошибка резолва")

# ── Статусы на сущность ──────────────────────────────────────────────────────
STATUS_CLEAN = "clean"            # ограничений не обнаружено
STATUS_RESTRICTED = "restricted"  # официальное ограничение Telegram (с причиной)
STATUS_HIDDEN = "hidden"          # резолвится по ссылке, но скрыт из поиска
STATUS_UNREACHABLE = "unreachable"  # не резолвится снаружи (скрыт/удалён/забанен)
STATUS_CHECK_FAILED = "check_failed"  # проверку не удалось выполнить (сеть/сессия)

# Порядок = приоритет отображения (от худшего к лучшему), для сводки и сортировки.
STATUS_ORDER = (
    STATUS_RESTRICTED,
    STATUS_UNREACHABLE,
    STATUS_HIDDEN,
    STATUS_CHECK_FAILED,
    STATUS_CLEAN,
)

_STATUS_LABELS = {
    STATUS_CLEAN: "✅ Чисто",
    STATUS_RESTRICTED: "🚫 Ограничение",
    STATUS_HIDDEN: "🕶 Скрыт из поиска",
    STATUS_UNREACHABLE: "⛔️ Недоступен снаружи",
    STATUS_CHECK_FAILED: "❓ Не удалось проверить",
}

# «Проблемный» статус = попадает в restriction_events и в тревожную часть сводки.
# check_failed НЕ проблемный (это наш сбой проверки, а не ограничение сущности).
_PROBLEM_STATUSES = frozenset({STATUS_RESTRICTED, STATUS_HIDDEN, STATUS_UNREACHABLE})

_SEVERITY_BY_STATUS = {
    STATUS_RESTRICTED: "critical",
    STATUS_UNREACHABLE: "critical",
    STATUS_HIDDEN: "warning",
}


def status_label(status: str) -> str:
    """Русская подпись статуса с эмодзи (для лога операции и сводки)."""
    return _STATUS_LABELS.get(status, status)


def is_problem(status: str) -> bool:
    """Статус означает реальное ограничение сущности (а не сбой проверки)."""
    return status in _PROBLEM_STATUSES


def severity_for(status: str) -> str:
    """Уровень для restriction_events: critical | warning | info."""
    return _SEVERITY_BY_STATUS.get(status, "info")


def event_type_for(kind: str, status: str) -> str:
    """Тип события для restriction_events по виду сущности и статусу.

    Согласовано с существующими типами в schema_v26.sql ('search_drop',
    'account_restricted', …): `<kind>_<что случилось>`.
    """
    k = kind if kind in ("channel", "group", "bot") else "entity"
    suffix = {
        STATUS_RESTRICTED: "restricted",
        STATUS_HIDDEN: "search_hidden",
        STATUS_UNREACHABLE: "unreachable",
    }.get(status, status)
    return f"{k}_{suffix}"


def parse_restriction_reasons(raw: Any) -> list[dict]:
    """Список Telethon `RestrictionReason` (или похожих) → [{platform, reason, text}].

    Устойчиво к None, строкам и объектам без части полей: причина ограничения
    уходит владельцу текстом, поэтому важно не упасть на нестандартной форме.
    """
    if not raw:
        return []
    out: list[dict] = []
    for item in raw:
        if item is None:
            continue
        if isinstance(item, str):
            out.append({"platform": "", "reason": "", "text": item})
            continue
        platform = str(getattr(item, "platform", "") or "")
        reason = str(getattr(item, "reason", "") or "")
        text = str(getattr(item, "text", "") or "")
        if not (platform or reason or text):
            # Совсем незнакомая форма — сохраняем её строковое представление,
            # чтобы причина не потерялась молча.
            text = str(item)
        out.append({"platform": platform, "reason": reason, "text": text})
    return out


def _reasons_to_ru(reasons: list[dict]) -> str:
    """Причины ограничения в одну читаемую строку (для владельца)."""
    parts: list[str] = []
    for r in reasons:
        txt = (r.get("text") or "").strip()
        reason = (r.get("reason") or "").strip()
        platform = (r.get("platform") or "").strip()
        # Текст Telegram информативнее машинного кода — предпочитаем его.
        if txt:
            label = txt
        elif reason:
            label = reason
        else:
            continue
        if platform and platform.lower() not in ("all", ""):
            label = f"{label} ({platform})"
        parts.append(label)
    return "; ".join(dict.fromkeys(parts))  # dedup, порядок сохранён


def classify(kind: str, signals: dict) -> dict:
    """Свести снятые сигналы в вердикт по сущности.

    signals (всё опционально, отсутствие = «не проверяли»):
      probe_error: str | None       — проверка не выполнилась (сеть/сессия/флуд)
      resolved: bool | None         — резолвится ли по @username/ссылке снаружи
      has_username: bool            — публичная (есть @username) → участвует в поиске
      restricted: bool              — флаг restricted на сущности
      restriction_reasons: list[dict] — из parse_restriction_reasons
      found_in_search: bool | None  — найдена ли в глобальном поиске (None = не искали)
      responds: bool | None         — для бота: ответил ли на /start (None = не слали)

    Возвращает {status, label, reason_ru, signals}. Приоритеты — от худшего:
    сбой проверки → официальное ограничение → недоступность → скрыт из поиска →
    чисто. «Официальное ограничение» приоритетнее недоступности: если Telegram
    прямо назвал причину, её и показываем, даже если сущность заодно не в поиске.
    """
    probe_error = signals.get("probe_error")
    if probe_error:
        return {
            "status": STATUS_CHECK_FAILED,
            "label": _STATUS_LABELS[STATUS_CHECK_FAILED],
            "reason_ru": f"Проверка не выполнена: {str(probe_error)[:160]}",
            "signals": signals,
        }

    reasons = signals.get("restriction_reasons") or []
    restricted = bool(signals.get("restricted")) or bool(reasons)
    if restricted:
        reason_ru = _reasons_to_ru(reasons) or "Telegram пометил сущность как ограниченную"
        return {
            "status": STATUS_RESTRICTED,
            "label": _STATUS_LABELS[STATUS_RESTRICTED],
            "reason_ru": reason_ru,
            "signals": signals,
        }

    resolved = signals.get("resolved")
    if resolved is False:
        return {
            "status": STATUS_UNREACHABLE,
            "label": _STATUS_LABELS[STATUS_UNREACHABLE],
            "reason_ru": "Сущность не открывается по прямой ссылке с чистого аккаунта "
                         "— скрыта, удалена или заблокирована",
            "signals": signals,
        }

    # Бот резолвится, но молчит на /start — жив в каталоге, но недоступен для
    # диалога (остановлен владельцем или ограничен). Это тоже недоступность.
    if kind == "bot" and signals.get("responds") is False:
        return {
            "status": STATUS_UNREACHABLE,
            "label": _STATUS_LABELS[STATUS_UNREACHABLE],
            "reason_ru": "Бот не отвечает на /start — остановлен или ограничен",
            "signals": signals,
        }

    # Скрыт из поиска — только для публичных (у приватных поиска нет по определению).
    has_username = bool(signals.get("has_username"))
    found_in_search = signals.get("found_in_search")
    if has_username and found_in_search is False:
        return {
            "status": STATUS_HIDDEN,
            "label": _STATUS_LABELS[STATUS_HIDDEN],
            "reason_ru": "Публичный @username резолвится по ссылке, но не находится "
                         "в глобальном поиске — скрыт из поиска (теневой бан)",
            "signals": signals,
        }

    return {
        "status": STATUS_CLEAN,
        "label": _STATUS_LABELS[STATUS_CLEAN],
        "reason_ru": "Ограничений не обнаружено",
        "signals": signals,
    }


def build_summary(counts: dict, total: int, *, checked: Optional[int] = None) -> str:
    """Русская сводка по итогам массовой проверки (в operation.result.summary).

    Порядок строк — от худшего к лучшему (STATUS_ORDER), нулевые статусы опущены.
    """
    n = checked if checked is not None else total
    header = f"🛡 Проверено сущностей: {n}"
    lines = [header]
    problems = sum(int(counts.get(s, 0)) for s in _PROBLEM_STATUSES)
    for status in STATUS_ORDER:
        c = int(counts.get(status, 0))
        if c:
            lines.append(f"{_STATUS_LABELS[status]}: {c}")
    if not problems:
        lines.append("Ограничений не обнаружено ни у одной сущности.")
    return "\n".join(lines)


# ── Сетевая проба (работает на УЖЕ подключённом Telethon-клиенте) ─────────────
#
# Клиент открывает исполнитель операции ОДИН раз на аккаунт-наблюдатель и передаёт
# сюда — так на все сущности приходится один коннект наблюдателя, минимум следа.
# Второй коннект той же сессии здесь открывать нельзя (AUTH_KEY_DUPLICATED),
# поэтому поиск делаем прямым вызовом на переданном client, а не через
# global_search_engine.search_public (та открывает собственный коннект).

_RESOLVE_TIMEOUT = 20.0
_SEARCH_TIMEOUT = 25.0
_BOT_PING_TIMEOUT = 8.0


async def probe_entity(
    client,
    *,
    kind: str,
    entity_id: Optional[int] = None,
    username: Optional[str] = None,
    do_search: bool = True,
    do_bot_ping: bool = True,
) -> dict:
    """Снять сигналы ограничений по одной сущности и классифицировать.

    kind: 'channel' | 'group' | 'bot'. Возвращает результат classify() плюс
    поле 'meta' с фактами сущности (title/username/id/verified/scam) для лога.
    Сеть/ошибки не глотаются молча — уходят в signals.probe_error и дают статус
    «Не удалось проверить», а не ложное «Чисто».
    """
    username = (username or "").strip().lstrip("@") or None
    has_username = bool(username)
    signals: dict = {"has_username": has_username}
    meta: dict = {"id": entity_id, "username": username, "title": None,
                  "verified": False, "scam": False}

    ref: Any = f"@{username}" if username else entity_id
    if ref is None:
        signals["probe_error"] = "нет ни @username, ни id для проверки"
        out = classify(kind, signals)
        out["meta"] = meta
        return out

    # 1) Резолв сущности снаружи + флаги ограничения.
    try:
        entity = await asyncio.wait_for(client.get_entity(ref), timeout=_RESOLVE_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — ошибку классифицируем, не роняем прогон
        resolved_false, probe_error = _classify_resolve_error(str(e))
        signals["resolved"] = not resolved_false
        if probe_error:
            signals["probe_error"] = probe_error
        out = classify(kind, signals)
        out["meta"] = meta
        return out

    if entity is None:
        signals["resolved"] = False
        out = classify(kind, signals)
        out["meta"] = meta
        return out

    signals["resolved"] = True
    signals["restricted"] = bool(getattr(entity, "restricted", False))
    signals["restriction_reasons"] = parse_restriction_reasons(
        getattr(entity, "restriction_reason", None))
    meta["title"] = getattr(entity, "title", None) or getattr(entity, "first_name", None)
    meta["verified"] = bool(getattr(entity, "verified", False))
    meta["scam"] = bool(getattr(entity, "scam", False))
    if entity_id is None:
        meta["id"] = getattr(entity, "id", None)

    # Официальное ограничение перекрывает всё — лишние запросы (поиск/пинг) не шлём.
    if signals["restricted"] or signals["restriction_reasons"]:
        out = classify(kind, signals)
        out["meta"] = meta
        return out

    # 2) Видимость в глобальном поиске — только для публичных сущностей.
    if has_username and do_search:
        try:
            from telethon.tl.functions.contacts import SearchRequest
            found = await asyncio.wait_for(
                client(SearchRequest(q=username, limit=30)), timeout=_SEARCH_TIMEOUT)
            hit = False
            target_id = meta["id"]
            uname_low = username.lower()
            for coll in (getattr(found, "chats", None) or [],
                         getattr(found, "users", None) or []):
                for ent in coll:
                    if target_id is not None and int(getattr(ent, "id", 0)) == int(target_id):
                        hit = True
                        break
                    ent_uname = (getattr(ent, "username", None) or "").lower()
                    if ent_uname and ent_uname == uname_low:
                        hit = True
                        break
                if hit:
                    break
            signals["found_in_search"] = hit
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # Сбой поиска НЕ делает вердикт «скрыт» — это лишь пропуск сигнала.
            log.debug("probe_entity search failed for @%s: %s", username, e)

    # 3) Отвечает ли бот на /start (жив и может писать). Свой бот владельца.
    if kind == "bot" and do_bot_ping:
        try:
            await asyncio.wait_for(
                client.send_message(entity, "/start"), timeout=_BOT_PING_TIMEOUT)
            await asyncio.sleep(2.5)
            msgs = await asyncio.wait_for(
                client.get_messages(entity, limit=1), timeout=_BOT_PING_TIMEOUT)
            signals["responds"] = bool(msgs)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # Не смогли пропинговать — не выносим вердикт по этому сигналу.
            log.debug("probe_entity bot ping failed for @%s: %s", username, e)

    out = classify(kind, signals)
    out["meta"] = meta
    return out
