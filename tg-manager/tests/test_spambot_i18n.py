"""Регресс: мультиязычная классификация ответов @SpamBot + fail-safe.

Первопричина: @SpamBot отвечает на языке аккаунта (lang_code), а классификатор
понимал только en+ru. Наш генератор отпечатков раздаёт 10 локалей → для 8 из
них ответ не распознавался, и check_account_status_full объявлял реально
ЗАБЛОКИРОВАННЫЙ аккаунт «активным» (он оставался в ротации и копил страйки).
"""
from __future__ import annotations

import os

from services.spambot_i18n import (
    classify_reply,
    classify_restriction,
    OK_PATTERNS,
    LIMIT_PATTERNS,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# Реалистичные формулировки @SpamBot: (язык, «всё чисто», «ограничен»)
CASES = [
    ("en",
     "Good news, no limits are currently applied to your account. You're free as a bird!",
     "I'm afraid some Telegram features may be unavailable to you until 20 August 2026."),
    ("ru",
     "Хорошие новости, никаких ограничений на ваш аккаунт сейчас не наложено. Свободны, как птица!",
     "К сожалению, ваш аккаунт ограничен: некоторые функции будут недоступны до 20 августа 2026."),
    ("uk",
     "Гарні новини, жодних обмежень на ваш акаунт зараз не накладено.",
     "На жаль, ваш акаунт обмежено: деякі функції будуть недоступні до 20 серпня 2026."),
    ("be",
     "Добрыя навіны, ніякіх абмежаванняў на ваш акаўнт зараз не накладзена.",
     "На жаль, ваш акаўнт абмежаваны: некаторыя функцыі будуць недаступны."),
    ("de",
     "Gute Nachrichten, keine Einschränkungen sind derzeit für dein Konto aktiv.",
     "Leider ist dein Konto eingeschränkt: einige Funktionen sind bis zum 20. August 2026 nicht verfügbar."),
    ("fr",
     "Bonne nouvelle, aucune restriction n'est actuellement appliquée à votre compte.",
     "Votre compte est limité : certaines fonctionnalités seront indisponibles jusqu'au 20 août 2026."),
    ("it",
     "Buone notizie, nessuna limitazione è attualmente applicata al tuo account.",
     "Il tuo account è limitato: alcune funzioni non saranno disponibili fino al 20 agosto 2026."),
    ("es",
     "Buenas noticias, sin limitaciones aplicadas a tu cuenta en este momento.",
     "Tu cuenta está limitada: algunas funciones no estarán disponibles hasta el 20 de agosto de 2026."),
    ("pl",
     "Dobre wiadomości, brak ograniczeń na Twoim koncie.",
     "Twoje konto jest ograniczone: niektóre funkcje będą niedostępne do dnia 20 sierpnia 2026."),
    ("tr",
     "İyi haber, hesabınızda şu anda herhangi bir kısıtlama yok.",
     "Hesabınız kısıtlandı: bazı özellikler 20 Ağustos 2026 tarihine kadar kullanılamaz."),
]


def test_all_locales_ok_replies_classified_active():
    for lang, ok_text, _ in CASES:
        assert classify_reply(ok_text) == "active", f"[{lang}] «всё чисто» не распознано"


def test_all_locales_limited_replies_classified_spamblock():
    for lang, _, bad_text in CASES:
        assert classify_reply(bad_text) == "spamblock", f"[{lang}] спамблок не распознан"


def test_ok_checked_before_limit_no_collision():
    """«Нет ограничений» содержит корень LIMIT-паттерна — OK обязан иметь приоритет."""
    for lang, ok_text, _ in CASES:
        assert classify_reply(ok_text) != "spamblock", f"[{lang}] ложный спамблок на чистом ответе"


def test_unknown_reply_returns_none():
    assert classify_reply("") is None
    assert classify_reply(None) is None
    assert classify_reply("完全に無関係なテキスト") is None


def test_restriction_temp_vs_perm_multilang():
    # временный — назван срок
    assert classify_restriction("unavailable to you until 20 August 2026") == "temp"
    assert classify_restriction("nicht verfügbar bis zum 20. August 2026") == "temp"
    assert classify_restriction("niedostępne do dnia 20 sierpnia") == "temp"
    # вечный — приоритет над temp
    assert classify_restriction("This is not going to be lifted automatically") == "perm"
    assert classify_restriction("Ограничение не будет снято автоматически") == "perm"
    assert classify_restriction("wird nicht aufgehoben") == "perm"
    assert classify_restriction("kalıcı olarak kısıtlandı") == "perm"


def test_no_regression_on_historic_en_ru_patterns():
    """Все исторические EN/RU-паттерны остаются рабочими (i18n — надмножество)."""
    for p in ("no limits", "no complaints", "good standing", "not limited",
              "нет ограничений", "нет жалоб", "свободен"):
        assert classify_reply(f"... {p} ...") == "active", p
    for p in ("limited", "restricted", "ограничен", "недоступны"):
        assert classify_reply(f"... {p} ...") == "spamblock", p


def test_account_manager_delegates_and_failsafe():
    src = _read("services/account_manager.py")
    # классификация делегирована в i18n
    assert "from services.spambot_i18n import classify_reply" in src
    assert "from services.spambot_i18n import classify_restriction" in src
    # fail-safe: нераспознанный ответ НЕ выдаётся за «активен»
    assert '"status": "unknown"' in src
    assert "spambot_unparsed" in src
    # кнопки аппеляции локализованы
    assert "das ist ein fehler" in src and "bu bir hata" in src


def test_unknown_status_is_not_persisted():
    """'unknown' не должен перезаписывать реальный acc_status в БД."""
    os.environ.setdefault("MANAGER_BOT_TOKEN", "x")
    os.environ.setdefault("TG_API_ID", "1")
    os.environ.setdefault("TG_API_HASH", "x")
    from services.account_manager import should_persist_account_status

    assert should_persist_account_status("unknown") is False
    assert should_persist_account_status("active") is True
    assert should_persist_account_status("spamblock") is True


def test_locale_coverage_matches_fingerprint_locales():
    """Покрываем ровно те языки, которые раздаёт генератор отпечатков."""
    am = _read("services/account_manager.py")
    seg = am[am.index("_COUNTRY_LOCALES"):]
    seg = seg[:seg.index("}")]
    langs = set()
    for line in seg.splitlines():
        if '("' in line:
            langs.add(line.split('("')[1].split('"')[0])
    covered = {lang for lang, _, _ in CASES}
    missing = langs - covered
    assert not missing, f"локали без покрытия классификатора: {missing}"
    assert len(OK_PATTERNS) > 40 and len(LIMIT_PATTERNS) > 25
