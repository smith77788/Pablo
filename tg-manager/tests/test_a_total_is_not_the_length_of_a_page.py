"""Итог в ответе — не длина показанной страницы.

`{"total": len(rows)}` при запросе с `LIMIT` — обещание, а не отчёт: пока
записей меньше потолка, цифра верна, а на потолке она навсегда замирает на нём.
У аккаунта с шестьюстами каналами шапка писала бы ровно «500», и это худший вид
неправды — правдоподобная. Так же было у ссылок бота: плитка «Ссылок» показывала
длину страницы, а «Кликов» складывала клики показанных тридцати, то есть
недосчитывала переходы, по которым решают, какой канал работает.

Правило механическое и узкое: ключ, который НАЗЫВАЕТ СЕБЯ итогом (`total` или
`*_total`), не может быть вычислен как длина выборки, ограниченной `LIMIT`.
Остальные формы (`shown`, `page.has_more`) длину страницы называть обязаны —
они про страницу и говорят.
"""
from __future__ import annotations

import ast
import re

import pytest

from tests.test_api_contract_row_fields import _answers, _sql_text


def _limited_vars(fn: ast.AST) -> dict[str, str]:
    """Переменные, полученные запросом с потолком."""
    out: dict[str, str] = {}
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        call = node.value
        if isinstance(call, ast.Await):
            call = call.value
        if not isinstance(call, ast.Call) or not call.args:
            continue
        name = (call.func.id if isinstance(call.func, ast.Name)
                else getattr(call.func, "attr", ""))
        if name not in ("_safe_fetch", "fetch"):
            continue
        arg = call.args[1] if name == "_safe_fetch" and len(call.args) > 1 else call.args[0]
        sql = _sql_text(arg)
        if re.search(r"(?i)\blimit\b", sql):
            out[node.targets[0].id] = sql
    return out


def _len_of(node: ast.AST) -> str | None:
    """Имя выборки, если выражение — её длина (`len(rows)`, `len(rows or [])`)."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "len" and len(node.args) == 1):
        return None
    arg = node.args[0]
    if isinstance(arg, ast.BoolOp) and isinstance(arg.op, ast.Or) and arg.values:
        arg = arg.values[0]
    return arg.id if isinstance(arg, ast.Name) else None


def _findings(answers=None) -> list[str]:
    bad: list[str] = []
    for fn, answer in (answers if answers is not None else _answers()):
        limited = _limited_vars(fn)
        if not limited:
            continue
        for key, value in zip(answer.keys, answer.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if not (key.value == "total" or key.value.endswith("_total")):
                continue
            var = _len_of(value)
            if var and var in limited:
                bad.append(f"{getattr(fn, 'name', '?')}: {key.value} = len({var}), "
                           f"а выборка ограничена потолком")
    return bad


def test_the_rule_is_red_on_a_total_that_counts_a_page():
    """Обратный контроль: иначе зелёное ниже не значит ничего."""
    src = """
async def h(request):
    rows = await _safe_fetch(pool, "SELECT id FROM managed_channels WHERE owner_id=$1 LIMIT 500")
    return _json_resp({"channels": rows, "total": len(rows or [])})
"""
    fn = ast.parse(src).body[0]
    answer = next(n.args[0] for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id == "_json_resp")
    assert _findings([(fn, answer)]), "правило не видит итога, равного длине страницы"

    ok = """
async def h(request):
    rows = await _safe_fetch(pool, "SELECT id FROM managed_channels WHERE owner_id=$1 LIMIT 500")
    total = await _safe_count(pool, "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1")
    return _json_resp({"channels": rows, "total": total, "shown": len(rows or [])})
"""
    fn2 = ast.parse(ok).body[0]
    answer2 = next(n.args[0] for n in ast.walk(fn2)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "_json_resp")
    assert not _findings([(fn2, answer2)]), (
        "правило ругается на честный ответ: итог запросом, длина страницы под "
        "своим именем")


def test_the_rule_looks_at_the_whole_layer():
    """Пустая выдача разбора сделала бы правило зелёным ни на чём."""
    answers = _answers()
    assert len(answers) >= 300, f"разобрано лишь {len(answers)} ответов слоя"
    with_limit = sum(1 for fn, _ in answers if _limited_vars(fn))
    assert with_limit >= 40, f"выборок с потолком найдено лишь {with_limit}"


def test_no_total_in_the_layer_is_the_length_of_a_page():
    bad = _findings()
    assert not bad, (
        "итог в ответе посчитан как длина ограниченной выборки: на потолке "
        "цифра замрёт на нём и будет правдоподобной неправдой. Считайте итог "
        "запросом, а длину страницы называйте своим именем (shown / "
        "page.has_more):\n  " + "\n  ".join(bad))


# --- Честный итог, который никто не видит, честным не становится -------------
#
# Оба обработчика ниже чинились в один заход, и после починки `total` был верен,
# а на экране его не было ни в каком виде: `openAccountChannels` рисовал
# `d.channels` строками, `openAccountBots` — `d.bots`, и оба молча обрывались на
# пятисотой записи. Для владельца разницы с прежней ложью нет: он видит полный
# на вид список. Поэтому у ручки с потолком итог обязан доходить до глаз.
_CAPPED_SCREENS = (
    ("openAccountChannels", "d.total"),
    ("openAccountBots", "d.total"),
)


@pytest.mark.parametrize("fn_name,total_expr", _CAPPED_SCREENS)
def test_the_capped_screen_says_how_much_is_left(fn_name, total_expr):
    from tests.miniapp_source import js_function

    js = js_function(fn_name)
    assert "anCap(" in js, (
        f"{fn_name}: выборка ограничена потолком, а экран не говорит, сколько "
        "записей осталось за ним — список выглядит полным")
    cap = re.search(r"anCap\(\s*([^,]+),\s*([^,]+),", js)
    assert cap, f"{fn_name}: вызов anCap разобрать не удалось"
    shown, total = cap.group(1).strip(), cap.group(2).strip()
    assert total == total_expr, (
        f"{fn_name}: в anCap передан {total!r}, а итог сервера — {total_expr!r}; "
        "сноска должна считать по серверному итогу, а не по длине страницы")
    assert ".length" in shown, (
        f"{fn_name}: первым аргументом anCap должна идти длина показанного "
        f"списка, а не {shown!r}")
