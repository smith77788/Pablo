"""«Хранилище» (Echo Vault): проводка business-апдейтов и чистые хелперы разбора.

Гейт держит цепочку целой: allowed_updates → роутер зарегистрирован → хендлеры на
все 4 типа business-апдейтов → API-маршруты. Без allowed_updates Telegram НЕ
доставит апдейты и архив будет пустым — это главный молчаливый провал такой фичи.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace as N

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
HANDLER = (ROOT / "bot" / "handlers" / "business_vault.py").read_text(encoding="utf-8")


# ── проводка ──────────────────────────────────────────────────────────────────

def test_allowed_updates_include_business():
    for u in ("business_connection", "business_message",
              "edited_business_message", "deleted_business_messages"):
        assert f'"{u}"' in MAIN, f"allowed_updates не содержит {u} — Telegram не доставит апдейт"


def test_router_registered():
    assert "business_vault_handler" in MAIN
    assert "dp.include_router(business_vault_handler.router)" in MAIN


def test_all_four_business_handlers_present():
    for dec in ("@router.business_connection()", "@router.business_message()",
                "@router.edited_business_message()", "@router.deleted_business_messages()"):
        assert dec in HANDLER, f"нет хендлера {dec}"


def test_api_routes_registered():
    for route in (
        '"/api/miniapp/vault/status"',
        '"/api/miniapp/vault/chats"',
        '"/api/miniapp/vault/chat/{chat_id}/messages"',
        '"/api/miniapp/vault/search"',
        '"/api/miniapp/vault/chat/{chat_id}/reply"',
    ):
        assert route in API, f"маршрут не зарегистрирован: {route}"


def test_api_owner_scoped():
    """Все vault-эндпоинты берут uid из токена и скоупят по нему (не по query)."""
    m = re.search(r"async def vault_chats\(.*?async def vault_messages\(", API, re.S)
    assert m and "_get_uid(request)" in m.group(0), "vault_chats не скоупится по владельцу"


# ── чистые хелперы ────────────────────────────────────────────────────────────

def _chat(cid, first="Боб", uname="bob"):
    return N(id=cid, first_name=first, last_name=None, username=uname, title=None, full_name=first)


def test_media_of_picks_largest_photo():
    from services import vault_service as v
    msg = N(photo=[N(file_id="s", file_unique_id="u1", file_size=10, mime_type=None, file_name=None),
                   N(file_id="big", file_unique_id="u2", file_size=999, mime_type=None, file_name=None)],
            text=None, caption=None)
    assert v.media_of(msg)["media_file_id"] == "big"


def test_media_of_document_and_none():
    from services import vault_service as v
    doc = N(document=N(file_id="d", file_unique_id="du", file_size=5, mime_type="application/pdf",
                       file_name="a.pdf"), text=None, caption=None)
    got = v.media_of(doc)
    assert got["media_type"] == "document" and got["media_name"] == "a.pdf"
    plain = N(text="hi", caption=None)
    assert v.media_of(plain)["media_type"] is None


def test_direction_and_text():
    from services import vault_service as v
    m = N(from_user=N(id=5), chat=_chat(9), text="  ", caption="подпись")
    assert v.direction_of(m, 5) == "out" and v.direction_of(m, 7) == "in"
    # текст берётся из caption, если text пустой
    assert v.text_of(N(text=None, caption="подпись")) == "подпись"
    assert v.text_of(N(text="", caption=None)) is None


def test_peer_of():
    from services import vault_service as v
    p = v.peer_of(N(chat=_chat(9, "Ян", "yan")))
    assert p == {"peer_user_id": 9, "peer_name": "Ян", "peer_username": "yan"}


def test_can_reply_prefers_rights():
    """can_reply читается из rights.can_reply (Bot API 9.0), с фолбэком на legacy."""
    from bot.handlers.business_vault import _can_reply
    assert _can_reply(N(rights=N(can_reply=True), can_reply=False)) is True   # rights приоритет
    assert _can_reply(N(rights=None, can_reply=True)) is True                 # legacy-фолбэк
    assert _can_reply(N(rights=N(can_reply=False), can_reply=True)) is False
