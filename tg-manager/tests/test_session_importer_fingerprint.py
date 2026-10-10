"""Регресс: импортные сессии получают УНИКАЛЬНЫЙ device-отпечаток на аккаунт.

Раньше session_importer вставлял аккаунт без device_* → на _make_client все
импортные сессии получали один дефолтный отпечаток (Samsung SM-S911B / en-US),
что давало массовую коллизию фингерпринтов (самый частый путь — покупные
сессии). Фикс: генерируем отпечаток на каждый аккаунт из страны номера.
"""
from __future__ import annotations

import os

from services.account_manager import generate_device_fingerprint

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_fingerprint_varies_across_accounts():
    # не константа — иначе коллизия отпечатков всего флота
    models = {generate_device_fingerprint()["device_model"] for _ in range(60)}
    assert len(models) > 1


def test_fingerprint_has_all_fields_and_locale():
    fp = generate_device_fingerprint("DE")
    for k in ("device_model", "system_version", "app_version",
              "lang_code", "system_lang_code"):
        assert fp.get(k), f"пустое поле отпечатка: {k}"


def test_fingerprint_locale_binds_to_country():
    # locale выводится из страны — разные страны могут давать разный язык
    de = generate_device_fingerprint("DE")["lang_code"]
    ru = generate_device_fingerprint("RU")["lang_code"]
    assert isinstance(de, str) and isinstance(ru, str) and de and ru


def test_importer_assigns_device_fields_on_insert():
    with open(os.path.join(ROOT, "services/session_importer.py"), encoding="utf-8") as f:
        src = f.read()
    # использует генератор отпечатка с локалью по стране номера
    assert "generate_device_fingerprint" in src
    assert "country_code_from_phone" in src
    # все device-колонки реально попадают в INSERT tg_accounts
    ins = src[src.index("INSERT INTO tg_accounts"):]
    ins = ins[:ins.index(")", ins.index("VALUES"))]
    for col in ("device_model", "system_version", "app_version",
                "lang_code", "system_lang_code"):
        assert col in ins, f"колонка {col} не попала в INSERT импорта"
