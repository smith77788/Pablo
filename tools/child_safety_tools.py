"""
Инструменты защиты детей ("Guardian") для Telegram-бота BASIC.FOOD.

НАЗНАЧЕНИЕ
    Обнаружение и блокировка контента, связанного с вербовкой, грумингом и
    сексуальной эксплуатацией несовершеннолетних, в чатах, которые
    администрирует бот, плюс подготовка доказательной базы для передачи в
    компетентные органы.

ЖЁСТКИЕ ГРАНИЦЫ (встроены в код намеренно, не обходить)
    1. Модуль НИКОГДА не скачивает, не хранит и не пересылает сам материал
       (изображения/видео CSAM). Хранение такого материала — преступление
       даже с благими намерениями. Регистрируются ТОЛЬКО метаданные:
       ссылка, идентификатор, юзернейм, время, фрагмент текста, сигналы.
    2. Никаких атак, DoS или массовых автоматических жалоб на Telegram или
       каналы. Единственные каналы воздействия — удаление/бан в чатах, где
       бот админ, и передача доказательств в органы и в abuse Telegram.
    3. Человек проверяет каждый случай перед отправкой внешнего отчёта.
       Автоматика только помечает и складывает в очередь на проверку.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from tools.telegram_tools import _call as _tg_call
from database.models import get_client


# ---------------------------------------------------------------------------
# Официальные каналы приёма сообщений о CSAM (для передачи доказательств людьми)
# ---------------------------------------------------------------------------
# Это публичные точки приёма жалоб профильных организаций. Модуль не
# отправляет туда автоматически — он готовит структурированный отчёт, чтобы
# человек подал его через официальную форму.

REPORTING_AUTHORITIES: dict[str, dict[str, str]] = {
    "ncmec": {
        "name": "NCMEC CyberTipline (США, международный приём)",
        "url": "https://report.cybertip.org",
        "note": "Основной международный приёмник. Принимает сообщения о CSAM из любой страны.",
    },
    "iwf": {
        "name": "Internet Watch Foundation (Великобритания)",
        "url": "https://report.iwf.org.uk",
        "note": "Специализируется на удалении CSAM-контента с хостингов.",
    },
    "inhope": {
        "name": "INHOPE — сеть национальных горячих линий",
        "url": "https://www.inhope.org/EN/report-here",
        "note": "Перенаправляет на горячую линию нужной страны.",
    },
    "ua_cyberpolice": {
        "name": "Киберполиция Украины",
        "url": "https://cyberpolice.gov.ua/contacts/",
        "note": "Официальный приём обращений Департамента киберполиции Нацполиции Украины.",
    },
    "ua_stop_sexting": {
        "name": "StopSexting / ла Страда Украина (детская линия)",
        "url": "https://stop-sexting.in.ua",
        "note": "Украинская горячая линия по онлайн-эксплуатации детей.",
    },
    "telegram_abuse": {
        "name": "Telegram Abuse",
        "url": "mailto:abuse@telegram.org",
        "note": "Прямой канал жалоб Telegram. Также @notoscam / внутренняя кнопка «Пожаловаться».",
    },
}


# ---------------------------------------------------------------------------
# Эвристики обнаружения (только текстовые сигналы, для пометки на проверку)
# ---------------------------------------------------------------------------
# Осознанно НЕ содержит поисковых терминов, которыми ищут сам материал.
# Ловит индикаторы ВЕРБОВКИ, ГРУМИНГА и ТОРГОВЛИ, а не помогает найти контент.
# Каждый сигнал — это (регэксп, вес, категория). Одного слова недостаточно
# для действия; решение принимается по сумме весов и требует проверки.

_SIGNALS: list[tuple[str, int, str]] = [
    # --- Вербовка / поиск «моделей», «работы» с несовершеннолетними ---
    (r"\b(ищу|шукаю|нужн[аы]|потрібн[оі])\b.{0,40}\b(девоч|мальчик|подрост|неповнолітн|малолет|школьниц|тін[ей])", 4, "recruitment"),
    (r"\b(модел|съёмк|съемк|зйомк|фотосесс|кастинг)\b.{0,40}\b(без.?одежд|18\s*\-|younger|мелк|юны)", 4, "recruitment"),
    (r"\b(быстр|лёгк|легк|швидк)[а-яіїєґ]*\s+(деньги|заработ|заробіт)\b.{0,40}\b(интим|приват|фото|видео|відео)", 3, "recruitment"),
    # --- Груминг / изоляция ребёнка ---
    (r"\b(никому|нікому)\s+(не\s+говор|не\s+кажи|не\s+расск)", 3, "grooming"),
    (r"\b(наш[аи]?\s+секрет|це\s+наш\s+секрет|это\s+наш\s+секрет)\b", 3, "grooming"),
    (r"\b(сколько|скільки)\s+(тебе|тобі)\s+лет\b.{0,30}\b(пришли|скинь|надішли|покажи)", 4, "grooming"),
    (r"\b(вебк|webcam|видеозвон|відеодзв)\b.{0,30}\b(один|одна|наедине|наодинці|без\s+родител)", 3, "grooming"),
    # --- Прямые предложения торговли / «услуг» ---
    # Основы намеренно без завершающего \b, чтобы ловить и существительные
    # («продажа»), и глаголы («продаётся»). Одиночный сигнал = «review»:
    # автоблокировка (high) наступает только при комбинации, чтобы не банить
    # безобидные фразы вроде «корм для детей питомцев».
    (r"\b(прода|продаж|торг)[а-яіїєґ]*\b.{0,30}\b(дет[еяи]|дит[иья]|реб[её]н|дитин)", 3, "trafficking"),
    (r"\b(услуг|послуг)[а-яіїєґ]*\s+.{0,15}(девоч|мальчик|дет[еяи]|дит[иья]|малолет|неповнолітн)", 4, "trafficking"),
    (r"\b(на\s+орган[ыи]|на\s+органів|донор.{0,10}орган)\b", 3, "trafficking"),
    (r"\b(дет[еяи]|дит[иья]|реб[её]н|дитин)[а-яіїєґ]*\b.{0,20}\bна\s+орган", 4, "trafficking"),
    # --- Терминология CSAM-сообществ (индикаторы каналов, без инструкций) ---
    (r"\b(cp|цп)\b.{0,15}\b(канал|channel|ссылк|посилан|слив|архив|архів)", 5, "csam_channel"),
    (r"\b(0\-?12|preteen|препубер|мала я|малолет)\b.{0,20}\b(контент|канал|архив|архів)", 5, "csam_channel"),
]

_COMPILED_SIGNALS = [(re.compile(pat, re.IGNORECASE | re.UNICODE), w, cat) for pat, w, cat in _SIGNALS]

# Порог суммарного веса, при котором сообщение помечается высоким риском.
HIGH_RISK_THRESHOLD = 5
REVIEW_THRESHOLD = 3


def scan_text(text: str) -> dict[str, Any]:
    """
    Прогнать текст через эвристики. Возвращает score, категории и уровень риска.
    НИКОГДА не принимает и не обрабатывает медиа — только текст сообщения/подписи.
    """
    if not text:
        return {"score": 0, "categories": [], "risk": "none", "matched": []}

    score = 0
    categories: set[str] = set()
    matched: list[str] = []
    for rx, weight, cat in _COMPILED_SIGNALS:
        if rx.search(text):
            score += weight
            categories.add(cat)
            matched.append(cat)

    if score >= HIGH_RISK_THRESHOLD:
        risk = "high"
    elif score >= REVIEW_THRESHOLD:
        risk = "review"
    else:
        risk = "low" if score > 0 else "none"

    return {
        "score": score,
        "categories": sorted(categories),
        "risk": risk,
        "matched": matched,
    }


# ---------------------------------------------------------------------------
# Регистрация доказательств (ТОЛЬКО метаданные, без медиа)
# ---------------------------------------------------------------------------

# Поля, которые разрешено сохранять. Всё остальное отбрасывается, чтобы
# исключить случайное сохранение медиа/бинарных данных.
_ALLOWED_EVIDENCE_FIELDS = {
    "source_type",      # 'chat_message' | 'channel' | 'user_report'
    "chat_id",
    "message_id",
    "channel_username",
    "channel_title",
    "offender_user_id",
    "offender_username",
    "text_snippet",     # короткий фрагмент текста (не медиа)
    "signals",          # результат scan_text
    "reporter_note",    # заметка человека
}


def record_evidence(**fields: Any) -> str:
    """
    Записать факт в очередь на проверку человеком. Хранит ТОЛЬКО метаданные.

    Любые поля вне _ALLOWED_EVIDENCE_FIELDS (в т.ч. любые медиа/файлы)
    отбрасываются намеренно — модуль не должен хранить сам материал.
    """
    payload: dict[str, Any] = {
        k: v for k, v in fields.items() if k in _ALLOWED_EVIDENCE_FIELDS
    }

    # Ограничиваем длину текстового фрагмента, чтобы это была улика, а не
    # хранилище контента.
    if "text_snippet" in payload and isinstance(payload["text_snippet"], str):
        payload["text_snippet"] = payload["text_snippet"][:500]

    if "signals" in payload and not isinstance(payload["signals"], str):
        payload["signals"] = json.dumps(payload["signals"], ensure_ascii=False)

    payload.setdefault("status", "pending_review")
    payload.setdefault("created_at", datetime.now(timezone.utc).isoformat())

    res = get_client().table("pablo_csam_reports").insert(payload).execute()
    return res.data[0]["id"] if res.data else ""


def list_pending_reports(limit: int = 50) -> list[dict]:
    """Очередь доказательств, ожидающих проверки человеком."""
    res = (
        get_client()
        .table("pablo_csam_reports")
        .select("*")
        .eq("status", "pending_review")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def mark_report_status(report_id: str, status: str, reviewer_note: str = "") -> bool:
    """
    Обновить статус доказательства после проверки человеком.
    status: 'pending_review' | 'confirmed' | 'dismissed' | 'submitted_to_authority'
    """
    valid = {"pending_review", "confirmed", "dismissed", "submitted_to_authority"}
    if status not in valid:
        return False
    update: dict[str, Any] = {
        "status": status,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    if reviewer_note:
        update["reviewer_note"] = reviewer_note
    get_client().table("pablo_csam_reports").update(update).eq("id", report_id).execute()
    return True


# ---------------------------------------------------------------------------
# Блокировка в чатах, где бот — администратор
# ---------------------------------------------------------------------------
# Это единственная «блокировка», которую бот реально может выполнить: удалить
# сообщение и забанить пользователя там, где у бота есть права админа.
# Чужие каналы бот удалить не может — для них есть путь через органы.

def delete_message(chat_id: int | str, message_id: int) -> dict:
    """Удалить сообщение в чате, где бот админ."""
    return _tg_call("deleteMessage", chat_id=chat_id, message_id=message_id)


def ban_user(chat_id: int | str, user_id: int, revoke_messages: bool = True) -> dict:
    """Забанить пользователя в чате, где бот админ (с удалением его сообщений)."""
    return _tg_call(
        "banChatMember",
        chat_id=chat_id,
        user_id=user_id,
        revoke_messages=revoke_messages,
    )


def enforce_in_managed_chat(chat_id: int | str, message_id: int, user_id: int) -> dict:
    """
    Полная реакция в управляемом чате: удалить сообщение и забанить отправителя.
    Возвращает результат обеих операций (best-effort — каждая независимо).
    """
    result: dict[str, Any] = {}
    try:
        result["deleted"] = delete_message(chat_id, message_id)
    except Exception as e:  # noqa: BLE001 — фиксируем, но не роняем реакцию
        result["deleted"] = {"error": str(e)}
    try:
        result["banned"] = ban_user(chat_id, user_id)
    except Exception as e:  # noqa: BLE001
        result["banned"] = {"error": str(e)}

    _log_action(chat_id=chat_id, message_id=message_id, user_id=user_id, action="delete_and_ban", result=result)
    return result


def _log_action(chat_id: Any, message_id: Any, user_id: Any, action: str, result: dict) -> None:
    """Записать действие модерации в журнал (для отчётности и аудита)."""
    try:
        get_client().table("pablo_moderation_actions").insert({
            "chat_id": chat_id,
            "message_id": message_id,
            "target_user_id": user_id,
            "action": action,
            "result": json.dumps(result, ensure_ascii=False, default=str)[:2000],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception:  # noqa: BLE001 — журнал не должен ломать основную реакцию
        pass


# ---------------------------------------------------------------------------
# Сборка отчёта для передачи в органы (готовит текст, НЕ отправляет сам)
# ---------------------------------------------------------------------------

def build_authority_report(report_id: str) -> dict[str, Any]:
    """
    Собрать структурированный отчёт по подтверждённому случаю для ручной
    подачи в официальные приёмники. Возвращает текст отчёта и список контактов.
    НЕ отправляет автоматически — подача выполняется человеком.
    """
    res = (
        get_client()
        .table("pablo_csam_reports")
        .select("*")
        .eq("id", report_id)
        .single()
        .execute()
    )
    ev = res.data
    if not ev:
        return {"error": "report not found"}

    lines = [
        "=== СООБЩЕНИЕ О ПОДОЗРЕНИИ НА ЭКСПЛУАТАЦИЮ НЕСОВЕРШЕННОЛЕТНИХ ===",
        f"Зафиксировано (UTC): {ev.get('created_at', '')}",
        f"Тип источника: {ev.get('source_type', '')}",
        f"Канал/чат: {ev.get('channel_title', '') or ev.get('chat_id', '')}"
        f" (@{ev.get('channel_username', '')})" if ev.get("channel_username") else "",
        f"Подозреваемый пользователь: @{ev.get('offender_username', '') or ''} (id: {ev.get('offender_user_id', '') or ''})",
        f"Сработавшие сигналы: {ev.get('signals', '')}",
        "",
        "Фрагмент текста (доказательство, без медиа):",
        f"  {ev.get('text_snippet', '')}",
        "",
        "Заметка проверяющего:",
        f"  {ev.get('reviewer_note', '') or ev.get('reporter_note', '')}",
        "",
        "ВАЖНО: сам материал (медиа) НЕ приложен и не хранился намеренно.",
        "Передайте ссылку/идентификаторы в приёмник — у органов есть защищённый",
        "путь для работы с самим контентом.",
    ]
    report_text = "\n".join(l for l in lines if l is not None)

    return {
        "report_id": report_id,
        "report_text": report_text,
        "submit_to": REPORTING_AUTHORITIES,
        "submission_links": _build_submission_links(ev, report_text),
    }


def _build_submission_links(ev: dict, report_text: str) -> dict[str, str]:
    """
    Готовые ссылки для РУЧНОЙ подачи человеком. Веб-формы открываются, чтобы
    оператор заполнил и отправил; письмо в abuse Telegram — с предзаполненным
    текстом. Модуль ничего не отправляет сам.
    """
    subject = "Report: suspected child sexual exploitation content on Telegram"
    body = report_text
    telegram_mailto = (
        "mailto:abuse@telegram.org"
        f"?subject={quote(subject)}&body={quote(body)}"
    )
    return {
        # Веб-формы приёма — открыть и подать вручную:
        "ncmec": REPORTING_AUTHORITIES["ncmec"]["url"],
        "iwf": REPORTING_AUTHORITIES["iwf"]["url"],
        "inhope": REPORTING_AUTHORITIES["inhope"]["url"],
        "ua_cyberpolice": REPORTING_AUTHORITIES["ua_cyberpolice"]["url"],
        # Письмо в Telegram с уже вставленным текстом отчёта:
        "telegram_abuse_email": telegram_mailto,
    }


def submit_report_manually(report_id: str, authority_key: str, operator_note: str = "") -> dict:
    """
    Зафиксировать, что человек-оператор подал отчёт в конкретный приёмник.
    Меняет статус доказательства на 'submitted_to_authority'. НЕ отправляет
    ничего сам — это отметка о факте ручной подачи для аудита.
    """
    if authority_key not in REPORTING_AUTHORITIES:
        return {"error": f"unknown authority: {authority_key}"}

    note = f"Подано вручную в {REPORTING_AUTHORITIES[authority_key]['name']}."
    if operator_note:
        note += f" {operator_note}"

    get_client().table("pablo_csam_reports").update({
        "status": "submitted_to_authority",
        "submitted_to": authority_key,
        "reviewer_note": note,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", report_id).execute()

    return {"report_id": report_id, "submitted_to": authority_key, "status": "submitted_to_authority"}
