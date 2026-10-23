"""Регресс на «пустой список каналов при счётчике 164», и правило вокруг него.

Причина того бага: запрос был `SELECT DISTINCT …, COALESCE(members_count,0) AS
member_count, … ORDER BY members_count DESC`. При `SELECT DISTINCT` Postgres
требует, чтобы выражение из ORDER BY стояло в списке выборки; голого
`members_count` там не было, только алиас. Запрос падал, `_safe_fetch` глотал
исключение и возвращал [], а `_safe_count` (без ORDER BY) честно отдавал 164:
список пуст, счётчик нет.

Прежняя версия файла стерегла ровно этот запрос ровно по этому тексту и
дважды ослепла молча: сперва когда запрос стал f-строкой, потом когда список
переехал на `DISTINCT ON`. Мерка, которая НАХОДИТ проверяемое по прежнему
тексту, рано или поздно перестаёт находить — и выглядит зелёной.

Поэтому правило здесь общее и на весь слой API, а не про один запрос:

  * `SELECT DISTINCT` — каждый столбец из ORDER BY обязан быть в списке
    выборки (сам или как алиас);
  * `SELECT DISTINCT ON (x)` — ORDER BY обязан НАЧИНАТЬСЯ с тех же x. Это
    более строгое требование, и именно оно действует сейчас: список каналов
    читается по одной строке на канал.

А сверх правил стоит прямая проверка: запросы, которые ручка каналов реально
отправляет, отдаются живому Postgres на разбор. Текстовое правило описывает
опасность, а разбор — доказывает, что её нет.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re

import pytest

from tests.miniapp_source import api_files

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
live = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

_COLS = re.compile(r"[a-z_][a-z0-9_]*")


def _is_str_expr(node: ast.AST) -> bool:
    """Узел, который целиком и есть текст запроса."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_str_expr(node.left) or _is_str_expr(node.right)
    return False


def _sql_of(node: ast.AST) -> str:
    """Текст запроса, собранный ПО ДЕРЕВУ, а не вырезанный из исходника.

    Вырезать исходник и вычищать из него кавычки, префиксы f и плюсы — путь,
    на котором эта мерка уже один раз испортила границу между ORDER BY и
    LIMIT. По дереву склейка видна точно: соседние куски f-строки и слагаемые
    `+` соединяются встык, а подстановка становится `{}` — она уезжает в
    WHERE, а стеречь надо сортировку и список выборки.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else " {} "
    if isinstance(node, ast.JoinedStr):
        return "".join(_sql_of(v) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _sql_of(node.left) + _sql_of(node.right)
    return " {} "


def _sql_expressions() -> list[tuple[str, int, str]]:
    """Файл, строка и текст каждого SQL-выражения слоя с DISTINCT.

    Берётся ВЫРАЖЕНИЕ целиком, и обход в него не спускается. Две причины, и
    каждая уже успела ослепить эту проверку молча:

      * `ast.walk` выдаёт и f-строку, и её внутренние куски; у куска с
        `DISTINCT ON` сортировка осталась в соседнем куске, и правило судило
        запрос без его же ORDER BY;
      * запрос бывает собран через `+` из двух выражений, и ORDER BY стоит во
        втором — по одному литералу его не видно вовсе.
    """
    out = []
    for path in api_files():
        src = path.read_text(encoding="utf-8")

        def walk(node):
            if _is_str_expr(node):
                text = " ".join(_sql_of(node).split())
                low = text.lower()
                if "select" in low and "distinct" in low:
                    out.append((path.name, node.lineno, text))
                return
            for child in ast.iter_child_nodes(node):
                walk(child)

        walk(ast.parse(src))
    return out


def _order_cols(low: str) -> list[str]:
    m = re.search(r"\border\s+by\s+(.*?)(?=\blimit\b|\boffset\b|$)", low, re.S)
    if not m:
        return []
    cols = []
    for part in m.group(1).split(","):
        part = part.strip()
        if not part or part.startswith("("):
            cols.append("")
            continue
        g = _COLS.match(part.split(".")[-1])
        cols.append(g.group(0) if g else "")
    return cols


def test_the_rule_sees_the_distinct_queries():
    """Пустая выдача сделала бы правила ниже зелёными ни на чём."""
    lits = _sql_expressions()
    assert len(lits) >= 3, f"запросов с DISTINCT в слое найдено мало: {len(lits)}"
    assert any("distinct on" in t.lower() for _f, _l, t in lits), (
        "запросов с `DISTINCT ON` не найдено — правило ниже ничего не стережёт")


def test_distinct_on_is_ordered_by_its_own_keys_first():
    """`DISTINCT ON (x)` без ORDER BY, начинающегося с x, Postgres отклонит."""
    bad = []
    for fname, line, text in _sql_expressions():
        low = " ".join(text.lower().split())
        m = re.search(r"distinct\s+on\s*\(([^)]*)\)", low)
        if not m:
            continue
        keys = [c.strip().split(".")[-1] for c in m.group(1).split(",") if c.strip()]
        head = _order_cols(low)[:len(keys)]
        if head != keys:
            bad.append(f"{fname}:{line}: DISTINCT ON ({', '.join(keys)}), "
                       f"а ORDER BY начинается с {head or '—'}")
    assert not bad, (
        "Postgres отклонит такой запрос целиком, а `_safe_fetch` проглотит "
        "исключение и вернёт пустой список рядом с ненулевым счётчиком:\n  "
        + "\n  ".join(bad))


def test_plain_distinct_orders_only_by_selected_columns():
    """При `SELECT DISTINCT` столбец сортировки обязан быть в выборке."""
    bad = []
    for fname, line, text in _sql_expressions():
        low = " ".join(text.lower().split())
        if "distinct on" in low or "select distinct" not in low or "from" not in low:
            continue
        head = low.index("select distinct") + len("select distinct")
        select_list = low[head:low.index("from", head)]
        for col in _order_cols(low):
            if not col:
                continue
            if not re.search(rf"\b{re.escape(col)}\b", select_list):
                bad.append(f"{fname}:{line}: ORDER BY {col} нет в списке выборки")
    assert not bad, (
        "Postgres отклонит запрос, список окажется пустым при ненулевом "
        "счётчике:\n  " + "\n  ".join(bad))


# ── Доказательство вместо правила: живой разбор того, что ручка отправляет ──

@live
def test_postgres_accepts_the_queries_the_handler_sends():
    """Запросы ручки каналов отдаются Postgres на разбор — и принимаются.

    Текстовые правила выше описывают опасность. Эта проверка её закрывает: она
    берёт SQL, который ручка РЕАЛЬНО отправляет (через подставной пул, со всеми
    подстановками, какие соберутся в бою), и просит Postgres его разобрать.
    Исходный баг она ловит напрямую, без знания про DISTINCT и алиасы.
    """
    import asyncpg

    from tests.test_channels_search_is_server_side import _Pool, _Req, _handler
    from services import mini_app_api as M

    pool = _Pool()
    real_uid = M._get_uid
    M._get_uid = lambda r: 424242
    M._cache.clear()
    try:
        asyncio.run(_handler(pool)(_Req(kind="group", role="mine", search="abc")))
    finally:
        M._get_uid = real_uid
        M._cache.clear()

    sent = [(q, a) for q, a in pool.calls if "SELECT" in q.upper()]
    assert sent, "ручка не отправила ни одного запроса — проверка ослепла"

    async def check():
        conn = await asyncpg.connect(DSN)
        try:
            broken = []
            for q, args in sent:
                try:
                    await conn.execute(f"PREPARE _probe AS {q}")
                    await conn.execute("DEALLOCATE _probe")
                except Exception as exc:
                    broken.append(f"{str(exc)[:160]}\n      {' '.join(q.split())[:200]}")
            return broken
        finally:
            await conn.close()

    broken = asyncio.run(check())
    assert not broken, (
        "Postgres не принял запрос ручки каналов — в бою `_safe_fetch` "
        "проглотит это и отдаст пустой список:\n  " + "\n  ".join(broken))
