"""У каждого запроса в Telegram должен быть потолок времени.

Мёртвый сокет не возвращает ошибку — он молчит. Запрос без потолка висит,
держа всё, что за ним стоит: слот параллельности операции (один из восьми) и
арендованные аккаунты, или кнопку, которая никогда не ответит владельцу.
Сторож зависших операций снимет такую не раньше, чем через полтора часа
бездействия, и всё это время флот простаивает.

В слое для этого есть два способа, и оба законные:

  * `account_manager.timeboxed(client)` — обёртка, которая вешает потолок на
    КАЖДЫЙ запрос этого клиента;
  * свой `asyncio.wait_for(...)` на конкретном вызове — так сделано там, где
    потолки разные (подключение щедрее, запрос строже).

Перепись ниже требует одного из двух. Её первая версия спрашивала не то:
«зовёт ли функция `timeboxed`» — и выдала 58 находок из 72, потому что
большинство функций ставят потолок на каждый вызов сами. По существу
(есть ли у ЗАПРОСА хоть какой-то потолок) осталось четыре, и все четыре —
один и тот же недосмотр: `connect()` обёрнут, а следующий за ним `get_me()`
или `is_user_authorized()` нет.

Урок тот же, что уже записан в CLAUDE.md про `await`/`wait` и про реестр
дверей: перепись обязана спрашивать про СВОЙСТВО, а не про способ его
достижения, иначе она считает находкой чужой стиль.
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "account_manager.py")

# Запросы, которым потолок не ставится СОЗНАТЕЛЬНО, с причиной на каждый.
# Ключ — (функция, текст вызова). Пусто — и это правильное состояние: пустой
# реестр значит, что непокрытых запросов нет, а не что про них забыли.
#
# Законный случай, который сюда попадёт первым: перенос медиа. Видео легально
# едет минутами, и потолок запроса его оборвёт — об этом прямо сказано в
# докстринге `_Timeboxed`.
NO_CEILING_ON_PURPOSE: dict[tuple[str, str], str] = {}


def _naked_requests(src: str) -> list[tuple[str, int, str]]:
    """Запросы к Telegram без всякого потолка: (функция, строка, вызов).

    Считается по дереву. «Запрос» — это `await client(...)` или
    `await client.<что-то>(...)`: именно они уходят в сеть.
    """
    tree = ast.parse(src)
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))]
    out: list[tuple[str, int, str]] = []
    for f in funcs:
        # Вложенная функция — чужое тело: её запросы принадлежат ей.
        inner = {id(x) for g in funcs if g is not f for x in ast.walk(g)}
        nodes = [x for x in ast.walk(f) if id(x) not in inner]
        calls = [c for c in nodes if isinstance(c, ast.Call)]
        names = {ast.unparse(c.func) for c in calls}
        if "_make_client" not in names:
            continue
        if "timeboxed" in names:
            continue  # потолок на каждый запрос ставит обёртка
        # Всё, что лежит внутри asyncio.wait_for(...), уже под потолком.
        guarded: set[int] = set()
        for c in calls:
            if ast.unparse(c.func).endswith("wait_for"):
                for sub in ast.walk(c):
                    if isinstance(sub, ast.Call) and sub is not c:
                        guarded.add(id(sub))
        for n in nodes:
            if not isinstance(n, ast.Await) or not isinstance(n.value, ast.Call):
                continue
            call = n.value
            target = ast.unparse(call.func)
            if not (target == "client" or target.startswith("client.")):
                continue
            if id(call) in guarded:
                continue
            out.append((f.name, n.lineno, target))
    return out


def test_the_census_sees_a_real_offender():
    """Измеритель на заведомо БОЛЬНОМ примере."""
    sick = (
        "import asyncio\n"
        "async def f(s):\n"
        "    client = _make_client(s)\n"
        "    me = await client.get_me()\n"
        "    return me\n"
    )
    assert _naked_requests(sick), "перепись не видит запрос без потолка"


def test_the_census_accepts_the_wrapper():
    """Измеритель на заведомо ЗДОРОВОМ примере: обёртка — это потолок."""
    ok = (
        "import asyncio\n"
        "async def f(s):\n"
        "    client = _make_client(s)\n"
        "    client = timeboxed(client)\n"
        "    me = await client.get_me()\n"
        "    return me\n"
    )
    assert _naked_requests(ok) == [], (
        "функция с обёрнутым клиентом принята за находку: перепись спрашивает "
        "про способ, а не про свойство")


def test_the_census_accepts_a_per_call_ceiling():
    """И второй законный способ — свой потолок на вызове."""
    ok = (
        "import asyncio\n"
        "async def f(s):\n"
        "    client = _make_client(s)\n"
        "    me = await asyncio.wait_for(client.get_me(), timeout=45)\n"
        "    return me\n"
    )
    assert _naked_requests(ok) == [], (
        "запрос под собственным wait_for принят за находку")


def test_no_telegram_request_is_left_without_a_ceiling():
    """Перепись: ни одного запроса без потолка."""
    with open(SRC, encoding="utf-8") as fh:
        found = _naked_requests(fh.read())
    naked = [(fn, line, call) for fn, line, call in found
             if (fn, call) not in NO_CEILING_ON_PURPOSE]
    assert naked == [], (
        "эти запросы в Telegram висят без потолка: мёртвый сокет не вернёт "
        "ошибку, он промолчит, и за запросом будет стоять слот операции с "
        "арендованными аккаунтами или кнопка, которая не ответит:\n  "
        + "\n  ".join(f"{fn} стр.{line}: await {call}(...)" for fn, line, call in naked)
        + "\nПоставьте `asyncio.wait_for(..., timeout=_OP_TIMEOUT)` или "
        "оберните клиент через `timeboxed(client)`. Если потолок не нужен по "
        "существу (легально долгий перенос медиа) — внесите в "
        "NO_CEILING_ON_PURPOSE с причиной.")


def test_the_exemptions_are_still_real():
    """Самопроверка реестра: мёртвая запись маскирует новую находку."""
    with open(SRC, encoding="utf-8") as fh:
        keys = {(fn, call) for fn, _line, call in _naked_requests(fh.read())}
    stale = sorted(set(NO_CEILING_ON_PURPOSE) - keys)
    assert not stale, (
        f"этих запросов без потолка в коде больше нет: {stale} — уберите их "
        "из NO_CEILING_ON_PURPOSE")
