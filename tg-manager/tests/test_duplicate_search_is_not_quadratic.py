"""Поиск дублей: ложный «один телефон» и сравнение всех пар.

НАХОДКА ПЕРВАЯ, и она важнее скорости. asyncpg отдаёт jsonb СТРОКОЙ (кодек
json на пуле нигде не ставится), а `compute_merge_confidence` делала
`set(contact['phones'])` — то есть превращала строку `'["+79990000001"]'` в
набор СИМВОЛОВ. У любых двух контактов с телефонами пересекались `+`, `7`,
`9`, `"`, `[`, движок ставил причину `same_phone` (+0.3), и пара с СОВЕРШЕННО
разными номерами получала 0.55 вместе с одинаковым именем и компанией — выше
порога 0.5. Экран «🔍 Дубликаты» показывал посторонних людей как одного
человека и объяснял это «одним телефоном», а кнопка «Объединить» рядом удаляет
вторую запись. До порога пакетного слияния (0.9) ложного признака не хватает
(максимум с ним 0.8), поэтому массового разрушения не было — но одно нажатие
было. Нашлось это не глазами: наивный перебор и отбор по группам разошлись на
169 пар, потому что группировка читала телефоны правильно, а сравнение — нет.

НАХОДКА ВТОРАЯ: сравнивались ВСЕ пары контактов владельца.

НАХОДКА (23.10.2026). `detect_smart_duplicates` читала все контакты владельца и
сравнивала все пары: n*(n-1)/2 вызовов `compute_merge_confidence` в одном
запросе. На 10 000 контактах это ~50 млн сравнений на каждое открытие экрана
«🔍 Дубликаты» — экран не «медленный», а недоступный: запрос не возвращается, и
владелец видит ошибку вместо списка. Ни потолка, ни LIMIT в запросе не было.

ПОЧЕМУ ОТБОР ПО ГРУППАМ ТОЧНЫЙ. Порог 0.5, веса compute_merge_confidence:
telegram_user_id 0.5, телефон 0.3, ник 0.25, имя 0.15 (или только имя без
фамилии 0.05), компания 0.1. Без первых трёх максимум = 0.15 + 0.1 = 0.25.
Значит каждая пара с confidence >= 0.5 обязана совпасть хотя бы по одному из
{telegram_user_id, телефон, ник}, и сравнение только внутри групп с общим
значением одного из этих полей даёт ТОТ ЖЕ результат. Это проверяется двумя
способами: перебором форм пары (оценка сверху) и сравнением с наивной
реализацией на живых данных (результат совпадает поэлементно).

ВТОРАЯ НАХОДКА. Экран печатал `dp.reasons.join(', ')`, то есть владелец читал
`same_telegram_id, same_phone` — коды из кода, по-английски. Подписи теперь на
сервере (`REASON_LABELS`), как ACTIVITY_LABELS и STAGE_LABELS: две карты
расходятся молча.

ТРЕТЬЯ. Группа кандидатов ограничена потолком (мусорное значение вроде одного
служебного номера на тысяче записей вернуло бы квадратичность внутри группы), и
число несравнённых групп уходит наружу: молча срезанный список дублей читается
как «дублей больше нет».

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся):
  0. вернуть `set(contact['phones'])` вместо `_as_list` -> красные
     test_different_phones_are_not_the_same_phone и
     test_the_bucketed_answer_equals_the_naive_one_postgres;
  1. вернуть сравнение всех пар -> красный
     test_only_candidate_pairs_are_compared. ВАЖНО: с первой попытки этот
     контроль не покраснел ничего — сложив все контакты в ОДНУ группу, я
     упёрся в её же потолок, и группа целиком пропустилась. Контроль обязан
     снимать и потолок, иначе он проверяет не то, что кажется;
  2. убрать ник из ключей группировки -> красные
     test_the_pairs_that_matter_are_still_found,
     test_a_skipped_group_is_not_silent,
     test_the_bucketed_answer_equals_the_naive_one_postgres;
  3. убрать reason_labels из ответа -> красный
     test_the_owner_reads_why_in_russian;
  4. молча пропускать большую группу (без skipped_groups) -> красный
     test_a_skipped_group_is_not_silent.
"""
from __future__ import annotations

import asyncio
import itertools
import os
import uuid

import pytest

from services.contacts_hub import trust_engine
from services.contacts_hub.trust_engine import compute_merge_confidence

DSN = os.environ.get("INFRAGRAM_TEST_DSN")
OWNER = 991801


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_duplicate_detection_is_exact_not_approximate():
    """Без tg/телефона/ника пара не может дойти до порога 0.5.

    Это та оценка сверху, на которой держится отбор по группам. Добавят признак
    с большим весом — тест покраснеет здесь, а не в виде пропавших дублей.
    """
    best = 0.0
    names = [("Иван", "Петров"), ("Иван", "Сидоров"), ("", "")]
    comps = ["acme", "other", ""]
    for (fa, la), (fb, lb) in itertools.product(names, repeat=2):
        for ca, cb in itertools.product(comps, repeat=2):
            a = {"telegram_user_id": 1, "username": "a", "phones": ["1"],
                 "first_name": fa, "last_name": la, "company": ca}
            b = {"telegram_user_id": 2, "username": "b", "phones": ["2"],
                 "first_name": fb, "last_name": lb, "company": cb}
            best = max(best, compute_merge_confidence(a, b)["confidence"])
    assert best < 0.5, (
        f"пара без общего tg/телефона/ника дотягивает до {best}: отбор по "
        "группам перестал быть точным, и дубли молча пропадут")


def test_different_phones_are_not_the_same_phone():
    """Два РАЗНЫХ номера — не причина считать людей одним человеком."""
    a = {"telegram_user_id": 1, "username": "x", "phones": '["+79990000001"]',
         "first_name": "Иван", "last_name": "Петров", "company": "acme"}
    b = {"telegram_user_id": 2, "username": "y", "phones": '["+79995555555"]',
         "first_name": "Иван", "last_name": "Петров", "company": "acme"}
    r = compute_merge_confidence(a, b)
    assert "same_phone" not in r["reasons"], (
        "jsonb приехал строкой, и set() разобрал её на символы: причина "
        f"«один телефон» ставится паре с разными номерами ({r})")
    assert r["confidence"] < 0.5, (
        f"посторонние люди снова предлагаются к слиянию: {r}")


def test_a_real_shared_phone_with_a_name_reaches_the_screen():
    """Починка не должна выключить настоящий признак.

    Пока телефоны сравнивались как наборы символов, вес 0.3 не имел значения:
    признак стоял почти на каждой паре. Когда он стал настоящим, стало видно,
    что вес мал — один и тот же номер плюс одинаковое имя давали 0.45, то есть
    НАСТОЯЩИЙ двойник оставался ниже порога и исчезал с экрана вместе с
    ложными. Вес поднят до 0.4; общий номер сам по себе (семейный, рабочий)
    порога по-прежнему не берёт.
    """
    a = {"telegram_user_id": 1, "username": "x", "phones": '["+79990000001"]',
         "first_name": "Иван", "last_name": "Петров", "company": None}
    b = {"telegram_user_id": 2, "username": "y", "phones": '["+79990000001"]',
         "first_name": "Иван", "last_name": "Петров", "company": None}
    r = compute_merge_confidence(a, b)
    assert "same_phone" in r["reasons"], r
    assert r["confidence"] >= 0.5, (
        f"настоящий двойник с тем же номером не доходит до экрана: {r}")


def test_a_shared_phone_alone_is_not_a_duplicate():
    """Семейный или рабочий номер на двух разных людях — не дубль."""
    a = {"telegram_user_id": 1, "username": "x", "phones": '["+79990000001"]',
         "first_name": "Иван", "last_name": "Петров", "company": None}
    b = {"telegram_user_id": 2, "username": "y", "phones": '["+79990000001"]',
         "first_name": "Мария", "last_name": "Сидорова", "company": None}
    r = compute_merge_confidence(a, b)
    assert r["confidence"] < 0.5, f"один номер сам по себе стал дублем: {r}"


class _CountingPool:
    """Пул на готовых строках: нужен только счёт сравнений, не БД."""

    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, query, *args):
        return self._rows


def _contact(i, **kw):
    d = {"id": str(uuid.UUID(int=i)), "telegram_user_id": None,
         "username": None, "first_name": f"Имя{i}", "last_name": f"Фам{i}",
         "phones": [], "company": None}
    d.update(kw)
    return d


def test_only_candidate_pairs_are_compared(monkeypatch):
    """1000 разных контактов — это 499 500 пар наивно и ноль по делу."""
    rows = [_contact(i) for i in range(1, 1001)]
    calls = []
    real = trust_engine.compute_merge_confidence

    def counting(a, b):
        calls.append(1)
        return real(a, b)
    monkeypatch.setattr(trust_engine, "compute_merge_confidence", counting)

    res = _run(trust_engine.detect_smart_duplicates(_CountingPool(rows), OWNER))
    assert res["duplicates"] == []
    naive = len(rows) * (len(rows) - 1) // 2
    assert len(calls) < naive / 100, (
        f"сравнений {len(calls)} при наивных {naive}: экран дублей снова "
        "упирается в квадрат числа контактов")


def test_the_pairs_that_matter_are_still_found():
    rows = [
        _contact(1, telegram_user_id=777),
        _contact(2, telegram_user_id=777),
        _contact(3, username="ivan", first_name="Иван", last_name="Петров",
                 company="acme"),
        _contact(4, username="ivan", first_name="Иван", last_name="Петров",
                 company="acme"),
        _contact(5, phones=["+79990000001"], first_name="Пётр",
                 last_name="Смирнов", company="acme"),
        _contact(6, phones=["+79990000001"], first_name="Пётр",
                 last_name="Смирнов", company="acme"),
    ]
    res = _run(trust_engine.detect_smart_duplicates(_CountingPool(rows), OWNER))
    pairs = {tuple(sorted([d["contact_a"]["id"], d["contact_b"]["id"]]))
             for d in res["duplicates"]}
    assert len(pairs) == 3, res["duplicates"]


def test_the_owner_reads_why_in_russian():
    rows = [_contact(1, telegram_user_id=777), _contact(2, telegram_user_id=777)]
    res = _run(trust_engine.detect_smart_duplicates(_CountingPool(rows), OWNER))
    d = res["duplicates"][0]
    assert d["reason_labels"], "причины совпадения без подписей"
    for code, label in zip(d["reasons"], d["reason_labels"]):
        # Собственные имена (Telegram, ID) в подписи законны; код из кода — нет.
        assert label != code and "_" not in label, (
            f"владелец читает код вместо причины: {label}")
        assert any("а" <= c.lower() <= "я" for c in label), (
            f"подпись без русских слов: {label}")


def test_a_skipped_group_is_not_silent(monkeypatch):
    monkeypatch.setattr(trust_engine, "_DUP_GROUP_CAP", 3)
    rows = [_contact(i, username="dup") for i in range(1, 11)]
    res = _run(trust_engine.detect_smart_duplicates(_CountingPool(rows), OWNER))
    assert res["skipped_groups"] == 1, (
        "группа не сравнивалась, и ответ об этом молчит: срезанный список "
        "читается как «дублей больше нет»")


def test_the_screen_shows_the_russian_reason():
    from tests.miniapp_source import js_function
    body = js_function("detectDuplicates")
    assert "reason_labels" in body, "экран снова печатает коды причин"
    assert "skipped_groups" in body, "экран молчит о несравнённых группах"


def test_the_dead_twins_are_gone():
    for dead in ("compute_merge_confidence_improved",
                 "detect_smart_duplicates_improved",
                 "get_conflict_resolution_suggestions"):
        assert not hasattr(trust_engine, dead), (
            f"{dead} вернулась: агент выберет её по имени вместо той, которую "
            "зовёт продукт")


# ─────────────────────────── живые данные ───────────────────────────

@pytest.mark.skipif(not DSN, reason="нужен живой Postgres")
def test_the_bucketed_answer_equals_the_naive_one_postgres():
    """Отбор по группам обязан совпасть с наивным перебором ПОЭЛЕМЕНТНО."""
    import asyncpg

    async def naive(pool, owner_id):
        contacts = await pool.fetch(
            'SELECT id, telegram_user_id, username, first_name, last_name, '
            'phones, company FROM unified_contacts WHERE owner_id=$1', owner_id)
        rows = [dict(c) for c in contacts]
        out = set()
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                r = compute_merge_confidence(rows[i], rows[j])
                if r['confidence'] >= 0.5:
                    out.add((tuple(sorted([str(rows[i]['id']), str(rows[j]['id'])])),
                             round(r['confidence'], 6)))
        return out

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)
            # Смесь: явные двойники, похожие имена без общих полей (не дубли),
            # общий телефон, общий ник, общий telegram_id и просто шум.
            for i in range(60):
                await pool.execute(
                    "INSERT INTO unified_contacts(owner_id, telegram_user_id, username,"
                    " first_name, last_name, phones, company) "
                    "VALUES($1,$2,$3,$4,$5,$6::jsonb,$7)",
                    OWNER,
                    7000 + (i % 25),
                    f"nick{i % 20}",
                    "Иван" if i % 3 else f"Имя{i}",
                    "Петров" if i % 4 else f"Фам{i}",
                    f'["+7999000{i % 15:04d}"]',
                    "acme" if i % 2 else None)
            got = await trust_engine.detect_smart_duplicates(pool, OWNER)
            mine = {(tuple(sorted([str(d['contact_a']['id']), str(d['contact_b']['id'])])),
                     round(d['confidence'], 6)) for d in got['duplicates']}
            return mine, await naive(pool, OWNER)
        finally:
            await pool.close()

    mine, ref = _run(go())
    assert mine == ref, (
        f"отбор по группам разошёлся с перебором: пропущено {len(ref - mine)}, "
        f"лишних {len(mine - ref)}")
    assert ref, "в данных теста нет ни одного дубля — проверка была бы пустой"
