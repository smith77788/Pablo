"""Поле, которое читает экран, обработчик обязан отдавать.

ЧТО ЭТО ЛОВИТ. Фронт читает `d.deleted`, обработчик отдаёт `{"removed": …}` —
синтаксис цел, тесты обработчика зелёные, тесты экрана зелёные, а на экране
`undefined` или пустой список. Для владельца это «кнопка не работает», и
причину видно только в браузере. Переименовать ключ ответа при уборке в
сервисном слое — самый дешёвый способ так сломать экран.

ПОЧЕМУ ЗДЕСЬ 0 НАХОДОК И ЭТО НЕ ПОВОД УДАЛЯТЬ ФАЙЛ. Измеритель написан не по
следам поломки, а до неё: на 22.10.2026 все 139 измеримых пар сходятся. Он
сторожит СЛЕДУЮЩЕЕ переименование. Обратный контроль (22.10.2026): замена
`"deleted"` на `"removed"` в `bots_bulk_delete` даёт ровно одну находку —
`bulkDeleteBots … нет: ['deleted']`. То есть он красный, когда должен.

ЧТО ИЗМЕРЯЕТСЯ, А ЧТО НЕТ. Пара «экран — обработчик» не угадывается, а
берётся только там, где она однозначна:

* в функции фронта РОВНО один вызов `api(`, и путь — целый литерал.
  `api('/api/miniapp/uch/contacts/' + id + '/timeline')` не берётся: по
  префиксу находится обработчик соседнего маршрута. `vaultDoSearch` зовёт и
  `chats`, и `search` — чтения второго ответа сравнились бы с ключами первого;
* маршрут ищется по МЕТОДУ и пути. Без метода POST-обработчик затирает
  GET-овый на том же пути, и экран сравнивается со сохранялкой;
* ключи ответа видны, только если обработчик отдаёт словарь-литерал без `**`.
  `{**stats, 'account_stats': …}` и `return _json_resp(data)` — это «ключей не
  видно», а не «ключей нет».

История измерителя (она же причина правила «детектор с десятками находок в
зрелом коде почти всегда сломан»): первый прогон дал 57 находок из 228. Все
57 — брак сопоставления: сначала метод, потом целые пути, потом видимость
ключей. Осталось 0 из 139. Если этот файл однажды даст десятки находок,
сначала проверьте измеритель на заведомо здоровой паре.
"""
from __future__ import annotations

import ast
import functools
import re

from tests.miniapp_source import api_files, js_functions

_ROUTE = re.compile(
    r'app\.router\.add_(get|post|put|patch|delete)\(\s*"([^"]+)"\s*,\s*(\w+)')
# Методы массива и общие поля ответа — не поля предметной модели.
_NOT_FIELDS = frozenset((
    "length", "map", "filter", "forEach", "join", "slice", "sort", "ok",
    "error", "push", "find", "some", "every", "reduce", "includes",
))


@functools.lru_cache(maxsize=1)
def _routes() -> dict[tuple[str, str], tuple[str, str]]:
    out = {}
    for path in api_files():
        for m in _ROUTE.finditer(path.read_text(encoding="utf-8")):
            out[(m.group(1).upper(), m.group(2))] = (str(path), m.group(3))
    return out


def _handler_of(method: str, path: str) -> tuple[str | None, str | None]:
    path = path.split("?")[0]
    table = _routes()
    for candidate in (path, path.rstrip("/")):
        if (method, candidate) in table:
            return table[(method, candidate)]
    best: tuple[str | None, str | None] = (None, None)
    best_len = -1
    for (verb, route), value in table.items():        # /api/x/{id}
        if verb != method or "{" not in route:
            continue
        head = route.split("{")[0]
        if path.startswith(head) and len(head) > best_len:
            best, best_len = value, len(head)
    return best


@functools.lru_cache(maxsize=None)
def _tree(path: str) -> ast.Module:
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _handler_node(path: str, name: str):
    for node in ast.walk(_tree(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _keys_are_visible(node) -> bool:
    """Отдаёт ли обработчик словарь-литерал без `**` (иначе ключей не видно)."""
    literal = hidden = False
    for n in ast.walk(node):
        if (isinstance(n, ast.Call)
                and getattr(n.func, "id", "") in ("_json_resp", "json_response")
                and n.args):
            arg = n.args[0]
            if isinstance(arg, ast.Dict):
                if any(k is None for k in arg.keys):
                    hidden = True
                else:
                    literal = True
            elif isinstance(arg, (ast.Name, ast.Await, ast.Subscript)):
                hidden = True
    return literal and not hidden


def _response_keys(node) -> set[str]:
    keys: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Dict):
            keys |= {k.value for k in n.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if isinstance(n, ast.Call):
            keys |= {kw.arg for kw in n.keywords if kw.arg}
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "SELECT" in n.value.upper():
            sql = n.value
            keys |= set(re.findall(r'AS\s+"?([a-z_]\w*)', sql, re.I))
            for chunk in re.findall(r"SELECT(.*?)\bFROM\b", sql, re.S | re.I):
                if "*" in chunk:
                    keys.add("*")
                for item in chunk.split(","):
                    item = item.strip()
                    m = (re.fullmatch(r"\w+\.([a-z_]\w*)", item)
                         or re.fullmatch(r"([a-z_]\w*)", item))
                    if m:
                        keys.add(m.group(1))
    return keys


@functools.lru_cache(maxsize=1)
def _pairs() -> tuple[tuple[str, str, str, str, str, frozenset[str], frozenset[str]], ...]:
    """Однозначные пары: (функция, метод, путь, файл, обработчик, чтения, ключи)."""
    out = []
    for name, body in sorted(js_functions().items()):
        if len(re.findall(r"\bapi\(", body)) != 1:
            continue
        literal = re.findall(r"api\(\s*'([^'\\$]+)'\s*(?=[,)])", body)
        if len(literal) != 1:
            continue
        path = literal[0]
        method = "POST" if re.search(
            r"api\(\s*'" + re.escape(path) + r"'\s*,\s*\{[^}]*method\s*:\s*'(POST|PUT|PATCH|DELETE)'",
            body) else "GET"
        file_path, handler = _handler_of(method, path)
        if not file_path:
            continue
        node = _handler_node(file_path, handler)
        if node is None or not _keys_are_visible(node):
            continue
        keys = _response_keys(node)
        if not keys or "*" in keys:
            continue
        var = re.search(r"const\s+(\w+)\s*=\s*await\s+api\(\s*'" + re.escape(path) + r"'", body)
        if not var:
            continue
        reads = set(re.findall(r"\b" + var.group(1) + r"\.([a-z_]\w*)", body)) - _NOT_FIELDS
        if not reads:
            continue
        out.append((name, method, path, file_path.rsplit("/", 1)[-1], handler,
                    frozenset(reads), frozenset(keys)))
    return tuple(out)


def test_there_is_something_to_compare():
    """Сузился отбор — проверка становится вакуумной молча. Было 139 пар."""
    pairs = _pairs()
    assert len(pairs) > 100, (
        f"однозначных пар «экран — обработчик» осталось {len(pairs)} вместо ~139: "
        "либо отбор стал строже, либо сломался разбор маршрутов")
    # 711 на 22.10.2026. Здесь меньше, чем в слепке маршрутов (1004): разбор
    # берёт только литеральные add_* в модулях мини-аппа, а слепок обходит
    # роутер целиком. Порог — страховка от «регексп перестал находить
    # маршруты», а не вторая инвентаризация.
    assert len(_routes()) > 600, f"маршрутов разобрано {len(_routes())}"


def test_every_field_the_front_reads_is_a_key_the_handler_returns():
    bad = []
    for name, method, path, where, handler, reads, keys in _pairs():
        missing = sorted(reads - keys)
        if missing:
            bad.append(f"{name} читает {missing} из {method} {path} "
                       f"({where}:{handler}), а обработчик такого не отдаёт")
    assert not bad, (
        "экран читает поля, которых в ответе нет — для владельца это «кнопка "
        "не работает», и причину видно только в браузере:\n  "
        + "\n  ".join(bad))
