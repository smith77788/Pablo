"""Мультиязычная классификация ответов @SpamBot.

Первопричина бага: @SpamBot отвечает на языке КЛИЕНТА (lang_code аккаунта), а
классификатор понимал только EN+RU. При этом наш генератор отпечатков
(`account_manager._COUNTRY_LOCALES`) раздаёт 10 локалей: en, ru, uk, be, de,
fr, it, es, pl, tr. Для 8 из них ответ не распознавался → `classify_spambot_reply`
возвращал None → `check_account_status_full` проваливался в общий фолбэк и
объявлял РЕАЛЬНО ЗАБЛОКИРОВАННЫЙ аккаунт «активным». Он оставался в ротации и
копил новые страйки.

Паттерны — устойчивые корни слов («ограничен», «eingeschränkt», «ograniczen»),
а не точные строки Telegram: локализации меняют формулировки, но корень
семантики «ограничен/жалоб нет» стабилен.

ВАЖЕН ПОРЯДОК: сначала OK, потом LIMIT. Фраза «keine Einschränkungen» (нет
ограничений) содержит корень LIMIT-паттерна «einschränkung»; проверка OK
первой снимает эту коллизию во всех языках («no limits», «brak ograniczeń»,
«sin limitaciones», «kısıtlama yok» и т.д.).
"""
from __future__ import annotations

from typing import Optional

# ── «Ограничений нет» ────────────────────────────────────────────────────────
OK_PATTERNS: tuple[str, ...] = (
    # en (существовавшие — сохранены как есть, без регрессии)
    "no limits", "no complaints", "good standing", "good news",
    "not limited", "no reports",
    # ru (существовавшие + усиленные корни: реальный ответ «никаких ограничений…
    # Свободны, как птица» не матчился ни на «нет ограничений», ни на «свободен»)
    "нет ограничений", "нет жалоб", "не было жалоб", "свободен",
    "никаких ограничений", "ограничений нет", "свободн", "жалоб нет",
    # uk
    "немає обмежень", "нема обмежень", "жодних обмежень", "обмежень немає",
    "немає скарг", "скарг не", "не обмежен", "вільн",
    # be
    "няма абмежаванн", "ніякіх абмежаванн", "няма скарг", "не абмежав",
    # de
    "keine einschränkung", "keine beschränkung", "keine beschwerden",
    "gute nachrichten", "nicht eingeschränkt",
    # fr
    "aucune restriction", "aucune limite", "aucune limitation",
    "aucune plainte", "bonne nouvelle", "pas de restriction",
    # it
    "nessuna limitazione", "nessuna restrizione", "nessun limite",
    "nessun reclamo", "buone notizie", "non è limitato",
    # es
    "sin limitaciones", "sin restricciones", "ninguna restricción",
    "sin límites", "ningún límite", "sin quejas", "buenas noticias",
    "no está limitado",
    # pl
    "brak ograniczeń", "żadnych ograniczeń", "bez ograniczeń",
    "brak skarg", "dobre wiadomości", "nie jest ograniczone",
    # tr
    "kısıtlama yok", "herhangi bir kısıtlama yok", "sınırlama yok",
    "şikayet yok", "iyi haber", "kısıtlanmamış",
)

# ── «Аккаунт ограничен / спамблок» ───────────────────────────────────────────
LIMIT_PATTERNS: tuple[str, ...] = (
    # en (существовавшие)
    "limited", "restricted", "unavailable to you", "some telegram features",
    # ru (существовавшие)
    "ограничен", "спам", "недоступны", "ваш аккаунт ограничен",
    # uk
    "обмежен", "обмежено", "недоступн", "спам",
    # be
    "абмежав", "абмежаван", "недаступн",
    # de
    "eingeschränkt", "einschränkung", "nicht verfügbar",
    # fr
    "limité", "restreint", "restriction", "indisponible",
    # it
    "limitat", "limitazion", "non disponibil",
    # es
    "limitad", "restringid", "restricción", "no disponible",
    # pl
    "ograniczen", "ograniczon", "niedostępn",
    # tr
    "kısıtla", "sınırla", "kullanılamaz",
)

# ── Временный спамблок (назван срок снятия) ──────────────────────────────────
TEMP_PATTERNS: tuple[str, ...] = (
    # en (существовавшие)
    "will be automatically released", "automatically released on", "released on",
    "will be able to use it again", "unavailable to you until", "limited until",
    "restricted until", "lifted on", "expires on", "until ",
    # ru (существовавшие)
    "снято", "будет снят", "снимется", "истекает", "ограничено до",
    # uk / be
    "буде знято", "знімет", "до ", "будзе знят",
    # de
    "bis zum", "wird automatisch aufgehoben", "aufgehoben am",
    # fr
    "jusqu'au", "sera automatiquement levée", "levée le",
    # it
    "fino al", "verrà rimossa", "rimossa il",
    # es
    "hasta el", "se levantará", "eliminada el",
    # pl
    "do dnia", "zostanie automatycznie", "zniesione",
    # tr
    "kadar", "otomatik olarak kaldırıl",
)

# ── Вечный/бессрочный спамблок (приоритет над temp) ──────────────────────────
PERM_PATTERNS: tuple[str, ...] = (
    # en (существовавшие)
    "not going to be lifted", "will not be lifted", "won't be lifted",
    "not be lifted automatically", "not going to be released", "no plans to",
    "permanently",
    # ru (существовавшие)
    "не будет снят", "не планируется", "навсегда", "бессрочно",
    # uk / be
    "не буде знято", "назавжди", "не будзе знят",
    # de
    "wird nicht aufgehoben", "dauerhaft", "endgültig",
    # fr
    "ne sera pas levée", "définitivement", "de façon permanente",
    # it
    "non verrà rimossa", "definitivamente", "permanentemente",
    # es
    "no se levantará", "permanentemente", "de forma definitiva",
    # pl
    "nie zostanie zniesione", "na stałe", "trwale",
    # tr
    "kaldırılmayacak", "kalıcı olarak", "süresiz",
)


def classify_reply(reply_text: Optional[str]) -> Optional[str]:
    """'active' | 'spamblock' | None (не распознано).

    OK проверяется ПЕРВЫМ — иначе «нет ограничений» на любом языке поймается
    LIMIT-паттерном по корню слова.
    """
    if not reply_text:
        return None
    low = reply_text.lower()
    if any(p in low for p in OK_PATTERNS):
        return "active"
    if any(p in low for p in LIMIT_PATTERNS):
        return "spamblock"
    return None


def classify_restriction(reply_text: Optional[str]) -> Optional[str]:
    """'perm' | 'temp' | None. Вечные признаки приоритетнее временных."""
    if not reply_text:
        return None
    low = reply_text.lower()
    if any(p in low for p in PERM_PATTERNS):
        return "perm"
    if any(p in low for p in TEMP_PATTERNS):
        return "temp"
    return None
