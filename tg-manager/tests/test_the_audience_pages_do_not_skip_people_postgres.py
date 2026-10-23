"""Аудитория инвайта читается страницами и никого не теряет.

ПОЧЕМУ ЭТО ВАЖНО ИМЕННО ЗДЕСЬ. Источник аудитории читается сдвигом (OFFSET) по
страницам в 2000 строк, а порядок внутри РАВНЫХ значений сортировки Postgres не
обещает. Для спарсенной аудитории равенство — не редкость, а правило:
`parsed_at TIMESTAMPTZ DEFAULT now()` — время ТРАНЗАКЦИИ, поэтому у всех строк
одного прогона парсера оно одинаковое, и ключ `ORDER BY parsed_at DESC`
вырождается целиком. Достаточно, чтобы между запросами двух страниц одна строка
изменилась (её физическое место в таблице сдвигается), и порядок внутри равных
поедет: одна строка придёт дважды, другая не придёт вообще.

Повтор внутри прогона съедает дедуп `_seen_keys`. А пропуск не ловит НИКТО:
человек просто не получает приглашения, и итог операции при этом говорит, что
аудитория вычитана до конца. Для операции, которая стоит дневных лимитов и
банов, «молча позвали не всех» — это потерянная работа владельца.

Лечится добивочным уникальным ключом в сортировке. Здесь это проверяется на
живом Postgres, потому что речь ровно о том, что обещает и чего не обещает он:
на заглушке вопрос не существует.

Рецепт запуска — в docstring tests/test_op_finish_paths_e2e_postgres.py.
"""
from __future__ import annotations

import ast
import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 889911
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

    async def _boot():
        conn = await asyncpg.connect(DSN)
        for f in ["schema.sql"] + sorted(
                glob.glob("schema_v*.sql"),
                key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass          # схемы идемпотентны
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=2)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


# ── поведение на живой базе ─────────────────────────────────────────────────

_PAGE = 4
_ROWS = 12


async def _seed(pool) -> None:
    """Прогон парсера: все строки с ОДНИМ parsed_at, как его пишет now()."""
    await pool.execute("DELETE FROM parsed_audiences WHERE owner_id=$1", OWNER)
    await pool.execute(
        """INSERT INTO parsed_audiences
               (owner_id, source_type, source_id, source_username,
                parse_run_id, tg_user_id, username, parsed_at)
           SELECT $1, 'channel', 777, 'src', 555, 1000 + g, 'user' || g, now()
             FROM generate_series(1, $2) AS g""",
        OWNER, _ROWS)
    n = await pool.fetchval(
        "SELECT count(DISTINCT parsed_at) FROM parsed_audiences WHERE owner_id=$1",
        OWNER)
    assert n == 1, (
        "стенд не воспроизводит боевой случай: у строк одного прогона парсера "
        f"должно быть ОДНО время, а их {n}")


def _order_by() -> str:
    """Сортировка источника — из самого исполнителя, не переписанная здесь."""
    src = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    m = re.search(r'"ORDER BY parsed_at DESC([^"]*)"', src)
    return "parsed_at DESC" + (m.group(1) if m else "")


def test_a_row_changing_between_pages_costs_nobody_their_invite(pool):
    """Читаем страницами, правя строку между ними — каждый человек ровно один раз."""
    _run(_seed(pool))
    sql = ("SELECT id, tg_user_id FROM parsed_audiences "
           f"WHERE owner_id=$1 AND parse_run_id=555 ORDER BY {_order_by()} ")

    async def _walk() -> list[int]:
        seen: list[int] = []
        offset = 0
        while True:
            rows = await pool.fetch(sql + f"LIMIT {_PAGE} OFFSET {offset}", OWNER)
            if not rows:
                break
            seen.extend(int(r["tg_user_id"]) for r in rows)
            offset += len(rows)
            # Между страницами строка меняется — ровно то, что бывает в бою:
            # парсер дописывает метку, владелец правит имя. Физическое место
            # строки в таблице при этом сдвигается.
            if rows:
                await pool.execute(
                    "UPDATE parsed_audiences SET first_name = first_name || 'x' "
                    "WHERE id = $1", int(rows[0]["id"]))
            if len(rows) < _PAGE:
                break
        return seen

    seen = _run(_walk())
    dupes = sorted({x for x in seen if seen.count(x) > 1})
    missing = sorted(set(range(1001, 1001 + _ROWS)) - set(seen))
    assert not dupes, f"эти цели пришли дважды: {dupes}"
    assert not missing, (
        f"эти цели не пришли НИ РАЗУ — их не позовут, а итог скажет, что "
        f"аудитория вычитана до конца: {missing}")


def test_the_bench_would_notice_a_degenerate_sort(pool):
    """Проверка выше имеет смысл только если стенд ловит вырожденный ключ."""
    _run(_seed(pool))
    sql = ("SELECT id, tg_user_id FROM parsed_audiences "
           "WHERE owner_id=$1 AND parse_run_id=555 ORDER BY parsed_at DESC ")

    async def _walk() -> list[int]:
        seen: list[int] = []
        offset = 0
        while True:
            rows = await pool.fetch(sql + f"LIMIT {_PAGE} OFFSET {offset}", OWNER)
            if not rows:
                break
            seen.extend(int(r["tg_user_id"]) for r in rows)
            offset += len(rows)
            if rows:
                await pool.execute(
                    "UPDATE parsed_audiences SET first_name = first_name || 'x' "
                    "WHERE id = $1", int(rows[0]["id"]))
            if len(rows) < _PAGE:
                break
        return seen

    seen = _run(_walk())
    broke = (len(set(seen)) != _ROWS) or (len(seen) != _ROWS)
    assert broke, (
        "без добивочного ключа обход прошёл ровно — значит стенд не "
        "воспроизводит сдвиг порядка, и проверка выше ничего не стережёт"
    )


# ── храповик на исходник: идёт в любом прогоне, без базы ────────────────────

# Чем заканчивается сортировка страничного источника, чтобы ключ был уникален.
_UNIQUE_TAILS = ("id desc", "id asc", "id", "bu.bot_id")


def _literal(node) -> str:
    """Собрать строку запроса из узла, СОХРАНЯЯ порядок частей.

    f-строка даёт JoinedStr, подстановка фильтров внутри неё — FormattedValue
    (её содержимое к сортировке не относится, поэтому пробел). Обход обязан
    быть рекурсивным по порядку: при перестановке частей «ORDER BY» уезжал в
    начало, и проверка ругалась на здоровый запрос — то есть измеритель врал.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal(v) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        return " "
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal(node.left) + _literal(node.right)
    return " "


def _paged_sources() -> list[str]:
    """SQL каждого вызова _page_source — по дереву разбора, не регуляркой."""
    src = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    out: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "_page_source"):
            continue
        if not node.args:
            continue
        out.append(_literal(node.args[0]))
    return out


def test_every_paged_source_sorts_by_a_unique_key():
    sources = _paged_sources()
    bad = []
    for sql in sources:
        m = re.search(r"ORDER BY (.+?)(?:$|\s+LIMIT)", sql, re.I | re.S)
        if not m:
            bad.append(f"без ORDER BY: {sql[:80]}")
            continue
        tail = m.group(1).strip().split(",")[-1].strip().lower()
        if tail not in _UNIQUE_TAILS:
            bad.append(f"ключ сортировки не уникален ({tail}): {sql[:80]}")
    assert not bad, (
        "страницы берутся сдвигом, поэтому при неуникальной сортировке одна "
        "цель придёт дважды, а другая — ни разу (и её не позовут): "
        + "; ".join(bad)
    )


def test_the_detector_sees_the_sources_at_all():
    """Пустой разбор сделал бы храповик вечно зелёным."""
    sources = _paged_sources()
    assert len(sources) >= 4, f"найдено источников: {len(sources)} — разбор сломан"
    assert any("parsed_audiences" in s for s in sources), sources
    assert any("bot_users" in s for s in sources), sources
