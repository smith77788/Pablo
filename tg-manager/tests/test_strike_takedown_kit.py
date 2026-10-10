"""Возможность: готовый «пакет жалобы» по цели — честная версия «жалобы в стор».

Пользователь: у конкурентов ресурсы удаляются «на 100%». Правда о механике:
массовые in-app жалобы почти не работают; реально удаляют/блокируют публичный канал
ДАВЛЕНИЕ ЧЕРЕЗ СТОРЫ (Apple/Google требуют от Telegram убирать нелегальный контент,
иначе приложение под угрозой в App Store/Google Play → Telegram скрывает канал, часто
на iOS) + DMCA + NCMEC/IWF для CSAM. Эти каналы нельзя честно автоматизировать (вход
+ captcha, ботов отклоняют), поэтому даём ГОТОВЫЙ текст жалобы + куда подать.

build_takedown_kit собирает по цели список каналов с приоритетом, ссылкой,
инструкцией, темой и телом. Эндпойнт /api/miniapp/strike/takedown_kit отдаёт его.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api
from services.strike_engine import build_takedown_kit


def test_kit_contains_store_and_legal_channels():
    kit = build_takedown_kit("@baddrugs", "drugs", title="Shop", subscribers=5000)
    keys = [c["key"] for c in kit["channels"]]
    # Стор-давление + юридический email — то, что реально работает
    assert "apple" in keys and "google" in keys and "telegram_abuse" in keys
    assert kit["target"] == "baddrugs"
    # каждый канал — самодостаточная заготовка
    for c in kit["channels"]:
        assert c["subject"] and c["body"] and c["how"]
        assert "t.me/baddrugs" in c["body"], "тело жалобы должно содержать ссылку на цель"


def test_csam_prioritizes_ncmec_iwf():
    kit = build_takedown_kit("@x", "csam")
    keys = [c["key"] for c in kit["channels"]]
    assert keys[0] == "ncmec", "для CSAM самый действенный путь (NCMEC) — первым"
    assert "iwf" in keys


def test_non_csam_has_no_ncmec():
    kit = build_takedown_kit("@x", "spam")
    keys = [c["key"] for c in kit["channels"]]
    assert "ncmec" not in keys and "iwf" not in keys, (
        "NCMEC/IWF — только для CSAM (ложные CSAM-репорты недопустимы)"
    )


def test_kit_does_not_promise_guaranteed_removal():
    """Честность: в ТЕКСТЕ, который видит пользователь, нет обещаний «100%/гарантия»
    удаления — решение всегда за площадкой."""
    kit = build_takedown_kit("@x", "drugs")
    user_text = " ".join(
        f"{c.get('title','')} {c.get('how','')} {c.get('subject','')} {c.get('body','')}"
        for c in kit["channels"]
    ).lower()
    assert "100%" not in user_text and "гарант" not in user_text, (
        "пакет жалобы не должен обещать гарантию удаления"
    )


def test_takedown_kit_endpoint_registered_and_scoped():
    src = inspect.getsource(mini_app_api)
    assert 'add_get("/api/miniapp/strike/takedown_kit", strike_takedown_kit)' in src
    m = re.search(r"async def strike_takedown_kit\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "strike_takedown_kit не найден"
    body = m.group(1)
    assert "if not uid" in body and "401" in body, "эндпойнт должен требовать авторизацию"
    assert "build_takedown_kit" in body


def test_ui_has_takedown_kit_entry():
    html = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "openTakedownKit(" in html, "нет кнопки/функции пакета жалобы в UI"
    assert "takedown_kit" in html, "UI должен запрашивать эндпойнт пакета жалобы"
