"""«Похожие контакты»: тот же ответ, но без SELECT * по всей базе в цикле.

`_fuzzy_search` читал все столбцы всех контактов владельца и считал
SequenceMatcher/Левенштейн прямо в цикле событий: на 30 000 контактов бот
стоял ~0,9 с на каждый поиск; теперь счёт в потоке (55 мс задержки), полные
строки читаются только у десяти лучших. Сверяем с прежним алгоритмом.
"""
from __future__ import annotations

import asyncio
import os
import threading
import uuid

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")
OWNER = 995811

NAMES = [("Иван", "Иванов", "ООО Ромашка"), ("Иванна", "Петрова", None),
         ("Ивар", "Сидоров", "Ромашка"), ("Пётр", "Иванченко", None),
         ("Мария", "Кузнецова", "Лютик"), ("Олег", "Ивашов", None)] + [
         (f"Имя{i}", f"Фам{i}", None) for i in range(40)]


def _old(rows, tokens, threshold):
    from services.contacts_hub.search_engine import _fuzzy_score
    out = []
    for d in rows:
        tf = [d.get(k) or "" for k in ("first_name", "last_name", "username",
                                        "display_name", "company", "position")]
        comb = " ".join(tf).lower()
        best = 0.0
        for t in tokens:
            for v in tf:
                if v:
                    best = max(best, _fuzzy_score(t, v.lower()))
            if t in comb:
                best = max(best, 1.0)
        if best >= threshold:
            out.append((round(best * 10, 2), d.get("first_name") or "", d["id"]))
    out.sort(key=lambda x: (-x[0], x[1]))
    return out[:10]


def test_same_answer_scored_off_the_loop(monkeypatch):
    import asyncpg

    from services.contacts_hub import search_engine as se

    seen = {}
    real = se._fuzzy_rank

    def _spy(*a, **kw):
        seen["thread"] = threading.current_thread()
        return real(*a, **kw)

    monkeypatch.setattr(se, "_fuzzy_rank", _spy)

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)
            await pool.executemany(
                "INSERT INTO unified_contacts(id, owner_id, first_name, last_name, display_name, company) "
                "VALUES($1,$2,$3,$4,$5,$6)",
                [(uuid.uuid4(), OWNER, f, l, f"{f} {l}", c) for f, l, c in NAMES])
            rows = [dict(r) for r in await pool.fetch(
                "SELECT * FROM unified_contacts WHERE owner_id=$1", OWNER)]
            got = await se._fuzzy_search(pool, OWNER, ["иванв"], 0.6, set())
            return rows, got
        finally:
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)
            await pool.close()

    loop = asyncio.new_event_loop()
    try:
        rows, got = loop.run_until_complete(_go())
    finally:
        loop.close()
    want = _old(rows, ["иванв"], 0.6)
    assert want, "проверка ничего не находит — тест ничего не сверяет"
    assert [(d["match_score"], d["id"]) for d in got] == [(s, i) for s, _, i in want]
    assert all("phones" in d for d in got), "победители отдаются полной строкой"
    assert seen["thread"] is not threading.main_thread()
