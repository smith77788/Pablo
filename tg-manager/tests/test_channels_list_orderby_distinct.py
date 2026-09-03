"""Регресс на «пустой список каналов при счётчике 164».

Причина бага: в endpoint `channels` запрос был вида
    SELECT DISTINCT ..., COALESCE(members_count,0) AS member_count, ...
    ORDER BY members_count DESC
С `SELECT DISTINCT` Postgres требует, чтобы выражение из `ORDER BY`
присутствовало в списке выборки. Голого `members_count` там не было (был
только его алиас `member_count`), поэтому весь запрос падал, `_safe_fetch`
глотал исключение и возвращал [] — список пуст, тогда как `_safe_count`
(без ORDER BY) честно отдавал 164.

Тест точечно вычленяет SQL-запрос из функции `channels` и проверяет, что
он сортируется по ВЫБРАННОМУ столбцу (алиасу `member_count`), а не по
голому `members_count`, которого нет в списке `SELECT DISTINCT`.
"""
import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "services" / "mini_app_api.py"


def _channels_fetch_sql() -> str:
    """Достаёт первый большой SQL-литерал из тела вложенной функции `channels`."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "channels":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    if "managed_channels" in sub.value and "ORDER BY" in sub.value.upper():
                        return sub.value
    raise AssertionError("не нашли SQL-запрос списка каналов в функции channels")


def test_channels_orderby_uses_selected_alias():
    sql = _channels_fetch_sql()
    low = sql.lower()
    assert "select distinct" in low, "запрос должен быть SELECT DISTINCT"

    m = re.search(r"order\s+by\s+([a-z_][a-z0-9_]*)", low)
    assert m, "не нашли ORDER BY в запросе"
    order_col = m.group(1)

    # список выборки — от SELECT DISTINCT до первого FROM верхнего уровня
    select_list = low[low.index("select distinct") + len("select distinct"): low.index("from")]

    in_select = bool(re.search(rf"\b{re.escape(order_col)}\b", select_list))
    is_alias = bool(re.search(rf"\bas\s+{re.escape(order_col)}\b", select_list))
    assert in_select or is_alias, (
        f"ORDER BY {order_col} — колонки нет в списке SELECT DISTINCT; "
        "Postgres отклонит запрос и список каналов окажется пустым при ненулевом счётчике"
    )
    # прямой запрет вернувшегося бага
    assert "order by members_count" not in low, (
        "ORDER BY members_count вернулся — это и есть баг (алиас в select — member_count)"
    )
