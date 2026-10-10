"""Уровень 9: умное распределение аудитории по источникам.

ЧТО БЫЛО СЛОМАНО (снова не «падало», а палило). Аудитория приходит из БД
сгруппированной: `ORDER BY parsed_at DESC` выдаёт сначала весь спаршенный канал
@a, потом весь @b. Очередь брала этот порядок как есть, поэтому аккаунт подряд
приглашал сорок человек из одного канала, следующий — сорок из другого.

Почему это плохо именно для инвайта: люди из одного источника похожи между собой
(смежные id, общая подписка, общий интерес), и залп из них в новую группу
читается ровно как то, чем является, — выгрузили список и залили пачкой. Причём
информация об источнике у нас БЫЛА (`parsed_audiences.source_username`), её
просто не выбирали из таблицы.
"""
from __future__ import annotations

import re
from pathlib import Path

from services.op_worker import interleave_by_source

WORKER = Path(__file__).resolve().parents[1] / "services" / "op_worker.py"


def _max_run(seq_sources) -> int:
    """Самая длинная цепочка подряд идущих целей из одного источника."""
    best = run = 0
    prev = object()
    for s in seq_sources:
        run = run + 1 if s == prev else 1
        prev = s
        best = max(best, run)
    return best


def _pairs(spec: dict) -> list:
    """{'@a': 10, '@b': 10} → [(ref, src), …] в «сгруппированном» порядке из БД."""
    out = []
    for src, n in spec.items():
        out.extend([(f"{src}#{i}", src) for i in range(n)])
    return out


def _sources_of(refs) -> list:
    return [r.split("#")[0] for r in refs]


# ── собственно раскладка ─────────────────────────────────────────────────────

def test_sources_are_not_clumped():
    pairs = _pairs({"@a": 20, "@b": 20})
    assert _max_run(_sources_of([r for r, _ in pairs])) == 20, "исходно — сплошные блоки"
    assert _max_run(_sources_of(interleave_by_source(pairs))) <= 2, (
        "после раскладки соседние цели должны быть из разных источников"
    )


def test_nothing_is_lost_or_duplicated():
    pairs = _pairs({"@a": 7, "@b": 3, "@c": 11})
    out = interleave_by_source(pairs)
    assert sorted(out) == sorted(r for r, _ in pairs), (
        "раскладка не имеет права терять или дублировать цели"
    )


def test_big_source_does_not_settle_as_a_tail():
    """Простой круговой обход оставил бы хвост из крупного источника, когда
    мелкие кончились, — то есть ровно ту же подпись, только в конце."""
    out = interleave_by_source(_pairs({"@big": 100, "@small": 4}))
    tail = _sources_of(out[-40:])
    assert _max_run(tail) < 40, "крупный источник не должен осесть сплошным хвостом"
    # мелкий источник должен быть размазан, а не сгруппирован в начале
    positions = [i for i, s in enumerate(_sources_of(out)) if s == "@small"]
    assert max(positions) > len(out) * 0.6, "мелкий источник обязан доставать до конца"


def test_single_source_order_is_preserved():
    """Перетасовывать нечего — и не надо: пользователь видит понятный порядок."""
    pairs = [(f"u{i}", "@only") for i in range(10)]
    assert interleave_by_source(pairs) == [r for r, _ in pairs]


def test_missing_source_is_one_bucket():
    pairs = [(f"u{i}", None) for i in range(5)]
    assert interleave_by_source(pairs) == [r for r, _ in pairs]


def test_mixed_known_and_unknown_sources():
    pairs = _pairs({"@a": 6}) + [(f"u{i}", None) for i in range(6)]
    out = interleave_by_source(pairs)
    assert len(out) == 12
    srcs = ["@a" if r.startswith("@a") else "" for r in out]
    assert _max_run(srcs) <= 2, "цели без источника — тоже отдельное ведро"


def test_empty_input():
    assert interleave_by_source([]) == []


# ── проводка: источник обязан доезжать из БД ─────────────────────────────────

def _exec_src() -> str:
    src = WORKER.read_text(encoding="utf-8")
    m = re.search(r"async def _exec_mass_invite\(.*?(?=\nasync def )", src, re.DOTALL)
    assert m
    return m.group(0)


def test_parsed_audience_carries_its_source():
    body = _exec_src()
    assert "source_username" in body, (
        "источник каждой цели лежал в parsed_audiences и просто не выбирался — "
        "без него раскладывать нечего"
    )
    assert body.count("interleave_by_source") >= 2, (
        "раскладка нужна и для спаршенной аудитории, и для пользователей ботов"
    )


def test_bot_users_source_is_the_bot():
    body = _exec_src()
    assert "bu.bot_id" in body, "разные боты — разные источники аудитории"
