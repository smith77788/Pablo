"""jsonb-поля контакта отдаются массивом/объектом, а не строкой.

asyncpg без кодека возвращает jsonb СТРОКОЙ ('["+7"]'), а фронт зовёт
c.phones.join(', ') → «c.phones.join is not a function» и карточка контакта
падала. Разбираем jsonb на границе чтения (repository/search) — единый контракт
для всех читателей. Доказано на живом Postgres: jsonb приходит как str.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = (ROOT / "services" / "contacts_hub" / "repository.py").read_text(encoding="utf-8")
SEARCH = (ROOT / "services" / "contacts_hub" / "search_engine.py").read_text(encoding="utf-8")


def test_parser_converts_jsonb_strings():
    from services.contacts_hub.repository import _parse_json_fields
    r = _parse_json_fields({
        "phones": '["+79991234567"]', "emails": None,
        "websites": '["site.com"]', "custom_fields": '{"k":1}',
        "digital_footprint": '{}',
    })
    assert r["phones"] == ["+79991234567"]
    assert r["emails"] == []                 # None → []
    assert r["websites"] == ["site.com"]
    assert r["custom_fields"] == {"k": 1}


def test_parser_is_defensive():
    from services.contacts_hub.repository import _parse_json_fields
    assert _parse_json_fields({"phones": ["+7"]})["phones"] == ["+7"]   # уже список
    assert _parse_json_fields({"phones": "битый json"})["phones"] == []  # мусор → []
    assert _parse_json_fields({})["phones"] == []                        # нет ключа → []


def test_read_paths_apply_parser():
    """Все читающие функции пропускают строки через парсер — иначе .join упадёт."""
    # repository: список и деталь
    assert REPO.count("_parse_json_fields(dict(r))") >= 1, "get_contacts не парсит"
    assert "_parse_json_fields(dict(row))" in REPO, "get_contact не парсит"
    # search: оба возврата
    assert SEARCH.count("_parse_json_fields(dict(r))") >= 2, "search не парсит оба пути"


def test_frontend_guards_phones_array():
    """Фронт защищён от не-массива: Array.isArray перед .join (двойная защита)."""
    html = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "Array.isArray(c.phones)" in html, "нет защиты .join на карточке контакта"
