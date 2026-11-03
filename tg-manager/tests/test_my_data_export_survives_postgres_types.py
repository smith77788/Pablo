"""Выгрузка «Мои данные» не падает на типах Postgres, которых не знает json.

`owner_data._jsonable` переводил даты, Decimal и байты, а всё остальное
отдавал как есть. Первая же колонка uuid (контакты хаба, воронка, сделки)
роняла `json.dumps` — и выгрузка целиком отвечала ошибкой: у владельца с
контактами архива не было ни в мини-аппе, ни командой /my_data. Поймал это
`test_every_screen_survives_an_empty_account_postgres` на владельце с данными.
"""
from __future__ import annotations

import ipaddress
import json
import uuid
from datetime import timedelta
from decimal import Decimal

from services.owner_data import _jsonable


def test_every_postgres_type_becomes_json():
    row = {
        "id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "ip": ipaddress.ip_address("10.0.0.1"),
        "net": ipaddress.ip_network("10.0.0.0/24"),
        "wait": timedelta(minutes=5),
        "sum": Decimal("1.50"),
        "nested": {"ids": [uuid.UUID(int=1)]},
        "plain": [1, "a", None, True, 2.5],
    }
    out = json.loads(json.dumps(_jsonable(row), ensure_ascii=False))
    assert out["id"] == "12345678-1234-5678-1234-567812345678"
    assert out["ip"] == "10.0.0.1" and out["net"] == "10.0.0.0/24"
    assert out["nested"]["ids"] == [str(uuid.UUID(int=1))]
    assert out["plain"] == [1, "a", None, True, 2.5]
    assert out["sum"] == "1.50"


def test_secrets_are_still_hidden():
    assert _jsonable("ENC:abc") == "<зашифровано>"
