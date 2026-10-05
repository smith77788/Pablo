"""Семантическая память: факты о человеке, а не о номере.

Экран фактов показывал «👤 User 783215441» и под ним строки вида
«pain_points: не успевает отвечать клиентам». Смысл этой памяти в том, что
факты собраны О ЧЕЛОВЕКЕ, — а по номеру человека не узнать ни владельцу, ни
кому-либо ещё; ключи же задаёт промпт извлечения, и все семь английские.

Третье: стереть то, что бот запомнил о собеседнике, умел только
Telegram-бот (semantic_memory_hub). Это не удобство, а обязанность — бот
хранит имя, город, боли и цели живого человека.

И четвёртое: рядом с каждым фактом стояло «(90%)». Уверенность пишется
константой 0.9 на вставке и на конфликте, другого писателя у таблицы нет, то
есть число было одинаковым всегда и лишь выглядело измерением.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
SM = open(os.path.join(ROOT, "services", "semantic_memory.py"), encoding="utf-8").read()
SNAPSHOT = open(os.path.join(ROOT, "tests", "miniapp_routes_snapshot.txt"),
                encoding="utf-8").read()


def _fn(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена"
    i = HTML.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:j + 1]
    raise AssertionError(name)


def _handler(name: str) -> str:
    i = API.index(f"async def {name}(request")
    j = API.index("\n    async def ", i + 10)
    return API[i:j]


def test_route_exists():
    assert "DELETE /api/miniapp/semantic_memory/{bot_id}/user/{user_id}" in SNAPSHOT


def test_every_prompt_key_has_a_russian_label():
    """Измеритель смотрит на сам промпт: ключи берутся оттуда, а не из головы."""
    i = SM.index("_EXTRACT_SYSTEM = (")
    prompt = SM[i:SM.index("\n)", i)]
    # Поля перечислены с отступом в две пробела внутри литерала; «Ты — CRM-агент»
    # в первой строке отступа не имеет и полем не является.
    keys = re.findall(r'"\s{2,}([a-z_]+)\s+—', prompt)
    assert len(keys) >= 7, f"ключи промпта не распознались: {keys}"
    j = HTML.index("const SEM_FACT_RU")
    block = HTML[j:j + 400]
    for k in keys:
        assert f"{k}:" in block, f"нет русской подписи для факта «{k}»"


def test_person_is_named_not_numbered():
    h = _handler("semantic_memory_bot")
    assert "LEFT JOIN bot_users bu" in h, "имя собеседника не подтягивается"
    assert '"name"' in h and '"username"' in h
    f = _fn("openSemFacts")
    assert "User ${" not in f and "👤 User" not in f, (
        "на экране снова номер вместо человека")
    assert "f0.name" in f, "имя не выводится"


def test_fact_keys_are_translated_on_screen():
    f = _fn("openSemFacts")
    assert "semFactRu(f.fact_key)" in f, (
        "ключ факта уезжает владельцу английским, как пришёл из промпта")


def test_constant_confidence_is_not_shown_as_data():
    """0.9 на вставке и на конфликте — одно и то же число всегда."""
    assert SM.count("confidence = 0.9") + SM.count("0.9)") >= 1
    others = [m for m in re.finditer(r"INSERT INTO bot_user_facts", SM)]
    assert len(others) == 1, (
        "у таблицы появился второй писатель — проверьте, не стала ли "
        "уверенность настоящей величиной")
    f = _fn("openSemFacts")
    assert "f.confidence" not in f, (
        "вернулся процент уверенности, который всегда одинаковый")


def test_forgetting_a_person_is_reachable_and_scoped():
    h = _handler("semantic_memory_forget")
    assert "SELECT added_by FROM managed_bots WHERE bot_id=$1" in h, (
        "чужого бота можно вычистить по номеру")
    assert "clear_user_memory" in h, (
        "стирание идёт мимо общей функции — переписка останется")
    f = _fn("forgetSemUser")
    assert "confirmDelete(" in f, "стирание без подтверждения"
    assert "Вернуть нельзя" in f, "не сказано, что это необратимо"


def test_empty_state_explains_itself():
    f = _fn("openSemFacts")
    assert "Бот ещё ничего не запомнил" in f
    assert "Фактов нет'" not in f, "вернулась пустая подпись без объяснения"


def test_grouping_keeps_the_query_order():
    """Запрос отдаёт свежие факты первыми — группировка не должна это терять.

    У объекта ключи-числа браузер перечисляет по возрастанию, поэтому
    группировка через Object.entries выстраивала собеседников по номеру, а не
    по свежести. Map сохраняет порядок вставки.
    """
    h = _handler("semantic_memory_bot")
    assert "ORDER BY f.updated_at DESC" in h, "запрос больше не сортирует по свежести"
    f = _fn("openSemFacts")
    assert "new Map()" in f, (
        "группировка снова через объект — собеседники выстроятся по номеру")
    assert "Object.entries(byUser)" not in f
