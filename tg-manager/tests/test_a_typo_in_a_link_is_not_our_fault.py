"""Мусор в адресе — это 400 по-русски, а не 500 «что-то пошло не так».

Ручка берёт идентификатор из пути (`/api/miniapp/uch/group/{group_id}`) и
приводит его к числу. Если в пути не число, `int()` бросает ValueError. Когда
этот разбор стоит ВНУТРИ общего `try`, у которого внизу `except Exception:
return _err(_INTERNAL_ERROR, 500)`, опечатка в ссылке превращается в отчёт о
поломке сервера. Это не косметика, а три разных вреда:

  * владелец видит «Что-то пошло не так, попробуйте ещё раз» и пробует ещё
    раз — совет неверный, повтор того же адреса даст то же самое;
  * `log.exception` пишет полную трассировку на каждый такой заход, и в
    журнале настоящие пятисотки тонут среди опечаток;
  * 500 для клиента означает «повтори позже», 400 — «не повторяй». Битая
    ссылка будет ходить по кругу вместо того, чтобы честно отказать сразу.

Честная форма в коде уже есть и взята образцом — `uch_segment_delete`:
отдельный `try` вокруг одного разбора, `except (KeyError, ValueError)` и
русское сообщение с кодом 400. Мерка проверяется на ней же: если проба на
здоровой ручке краснеет, сломана мерка, а не ручка.
"""

from __future__ import annotations

import ast
import functools
import re

from tests.miniapp_source import api_files, api_source, miniapp_source

_REG = re.compile(
    r'app\.router\.add_(?:get|post|put|delete|patch)\(\s*"([^"]+)"\s*,\s*([A-Za-z_]\w*)')

# Исключения, которые ЛОВЯТ плохой идентификатор в пути.
_NARROW = {"ValueError", "KeyError", "TypeError"}


def _catches_narrowly(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return False
    names = ([e.id for e in t.elts if isinstance(e, ast.Name)]
             if isinstance(t, ast.Tuple) else
             [t.id] if isinstance(t, ast.Name) else [])
    return any(n in _NARROW for n in names)


def _is_catch_all_500(node: ast.Try) -> bool:
    """`except Exception:` (или голый), отвечающий пятисоткой."""
    for h in node.handlers:
        if _catches_narrowly(h):
            continue
        bare = h.type is None or (isinstance(h.type, ast.Name)
                                  and h.type.id in ("Exception", "BaseException"))
        if not bare:
            continue
        body = ast.unparse(ast.Module(body=h.body, type_ignores=[]))
        if "500" in body or "_INTERNAL_ERROR" in body:
            return True
    return False


def _is_path_int(node: ast.AST) -> bool:
    """`int(request.match_info[...])` — приведение куска пути к числу."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "int" and node.args):
        return False
    return "match_info" in ast.unparse(node.args[0])


def _unguarded_path_ints(func: ast.AST) -> list[int]:
    """Строки, где путь приводится к числу под общим `except … 500`.

    Обход несёт стек объемлющих `try`: судит БЛИЖАЙШИЙ, который ловит это
    исключение. Внутренний `try` с ValueError делает разбор честным, даже
    если снаружи стоит общий — поэтому порядок важен, а не наличие.
    """
    found: list[int] = []

    def walk(node: ast.AST, stack: list[ast.Try]) -> None:
        if _is_path_int(node):
            for t in reversed(stack):
                if any(_catches_narrowly(h) for h in t.handlers):
                    return          # ближайший ловит узко — честно
                if _is_catch_all_500(t):
                    found.append(node.lineno)
                    return
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(node, ast.Try) and child in node.body:
                walk(child, stack + [node])
            else:
                walk(child, stack)

    walk(func, [])
    return found


@functools.lru_cache(maxsize=1)
def _handlers() -> dict[str, tuple[str, ast.AST, tuple[str, ...]]]:
    """Имя -> (файл, узел, пути). Только зарегистрированные ручки."""
    paths: dict[str, list[str]] = {}
    for path, name in _REG.findall(api_source()):
        paths.setdefault(name, []).append(path)
    out: dict[str, tuple[str, ast.AST, tuple[str, ...]]] = {}
    for p in api_files():
        text = p.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in paths and node.name not in out):
                out[node.name] = (p.name, node, tuple(paths[node.name]))
    return out


def _reachable(route: str) -> bool:
    head = route.split("{")[0].rstrip("/")
    return bool(head) and head in miniapp_source()


def _offenders() -> list[str]:
    bad = []
    for name, (fname, node, routes) in sorted(_handlers().items()):
        if not any(_reachable(r) for r in routes):
            continue
        for line in _unguarded_path_ints(node):
            bad.append(f"{fname}:{line} {name}")
    return bad


def test_the_detector_spares_the_honest_handler():
    """Проба на здоровом: `uch_segment_delete` разбирает путь правильно."""
    hs = _handlers()
    assert "uch_segment_delete" in hs, "образец честного разбора пропал из реестра"
    _f, node, _r = hs["uch_segment_delete"]
    assert not _unguarded_path_ints(node), (
        "мерка считает находкой отдельный `try` с ValueError и ответом 400 — "
        "сломана мерка, а не ручка")


def test_the_detector_sees_a_planted_offender():
    """Проба на больном: без неё правило ниже могло бы быть каким угодно."""
    src = (
        "async def h(request):\n"
        "    try:\n"
        "        gid = int(request.match_info['group_id'])\n"
        "        return await go(gid)\n"
        "    except Exception:\n"
        "        return _err(_INTERNAL_ERROR, 500)\n")
    node = ast.parse(src).body[0]
    assert _unguarded_path_ints(node), "мерка не видит разбор под общим except"


def test_the_detector_counts_the_nearest_try_not_any():
    """Внутренний узкий `try` важнее внешнего общего: судит ближайший."""
    src = (
        "async def h(request):\n"
        "    try:\n"
        "        try:\n"
        "            gid = int(request.match_info['group_id'])\n"
        "        except (KeyError, ValueError):\n"
        "            return _err('Неверный идентификатор', 400)\n"
        "        return await go(gid)\n"
        "    except Exception:\n"
        "        return _err(_INTERNAL_ERROR, 500)\n")
    node = ast.parse(src).body[0]
    assert not _unguarded_path_ints(node), (
        "мерка не различает порядок обработчиков: честный разбор внутри "
        "общего `try` — это честный разбор")
    # И наоборот: узкий обработчик, стоящий ПЕРЕД общим у того же `try`,
    # тоже делает разбор честным — до `except Exception` дело не дойдёт.
    src2 = (
        "async def h(request):\n"
        "    try:\n"
        "        gid = int(request.match_info['group_id'])\n"
        "        return await go(gid)\n"
        "    except ValueError:\n"
        "        return _err('Неверный идентификатор', 400)\n"
        "    except Exception:\n"
        "        return _err(_INTERNAL_ERROR, 500)\n")
    assert not _unguarded_path_ints(ast.parse(src2).body[0]), (
        "узкий обработчик у того же `try` не замечен")


def test_the_detector_sees_the_handlers_at_all():
    """Пустой реестр сделал бы правило зелёным ни на чём."""
    hs = _handlers()
    assert len(hs) >= 200, f"ручек в реестре найдено мало: {len(hs)}"
    with_path_int = [n for n, (_f, node, _r) in hs.items()
                     if any(_is_path_int(x) for x in ast.walk(node))]
    assert len(with_path_int) >= 30, (
        f"ручек, приводящих путь к числу, найдено мало: {len(with_path_int)}")


def test_a_bad_id_in_the_path_answers_four_hundred():
    """Опечатка в адресе обязана отвечать 400 по-русски, а не 500."""
    bad = _offenders()
    assert not bad, (
        "разбор идентификатора из пути стоит под общим `except … 500`: "
        "опечатка в ссылке будет показана владельцу как поломка сервера, "
        "насоветует повторить и зальёт журнал трассировками:\n  "
        + "\n  ".join(bad))
