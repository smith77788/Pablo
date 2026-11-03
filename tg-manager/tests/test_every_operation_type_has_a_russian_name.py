"""У каждого типа операции есть русское название на экране.

Окно операций показывает `o.label || opRu(o.op_type)`, а opRu берёт название
из словаря OP_RU и, не найдя, отдаёт сам тип. У 33 из 82 типов названия не
было, и владелец видел в списке, в карточке и в журнале внутренние имена —
«bulk_bot_profile», «bot_factory_multi», «contacts_sync». Русское описание при
этом было у каждого типа в серверном реестре (operation_bus.OP_REGISTRY).

Тест держит словарь полным: новый тип операции без названия на экране —
красное, а не английское слово у владельца.
"""
from __future__ import annotations

import re

from services.operation_bus import OP_REGISTRY
from tests.miniapp_source import miniapp_source


def _op_ru_keys() -> set[str]:
    src = miniapp_source()
    i = src.index("const OP_RU = {")
    j = src.index("};", i)
    body = re.sub(r"//[^\n]*", "", src[i:j])
    return set(re.findall(r"([a-z_0-9]+)\s*:", body))


def test_every_registered_operation_has_a_russian_name():
    keys = _op_ru_keys()
    missing = sorted(t for t in OP_REGISTRY if t.lower() not in keys)
    assert not missing, ("типы операций без русского названия на экране — "
                         f"владелец увидит внутреннее имя: {missing}")


def test_the_names_are_russian():
    src = miniapp_source()
    i = src.index("const OP_RU = {")
    body = re.sub(r"//[^\n]*", "", src[i:src.index("};", i)])
    for key, val in re.findall(r"([a-z_0-9]+)\s*:\s*'([^']*)'", body):
        assert re.search(r"[а-яё]", val.lower()), f"{key}: название не по-русски: {val!r}"


def test_the_measure_reads_the_dictionary():
    # Самопроверка: заведомо известный тип мерка обязана найти.
    assert "mass_invite" in _op_ru_keys()


def test_no_screen_prints_the_raw_operation_type():
    # Словарь полон — но он не поможет, если экран печатает тип мимо opRu:
    # так было у карточки операции («Тип: network_broadcast»), у баннера
    # итога и у полоски прогресса.
    src = re.sub(r"//[^\n]*", "", miniapp_source())
    raw = re.findall(r"\|\|\s*[a-z]+\.op_type\b(?!\s*\))", src)
    raw += [m for m in re.findall(r"label\s*\|\|\s*[a-z]+\.op_type", src)]
    assert not raw, f"тип операции уходит на экран без перевода: {raw}"


def test_the_raw_type_measure_sees_the_old_form():
    probe = "esc(o.label || o.op_type || '')"
    assert re.findall(r"label\s*\|\|\s*[a-z]+\.op_type", probe)
    assert not re.findall(r"label\s*\|\|\s*[a-z]+\.op_type", "esc(o.label || opRu(o.op_type))")
