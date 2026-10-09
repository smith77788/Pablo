"""Поле, которое экран присылает, обработчик обязан читать.

КЛАСС ОШИБОК. Экран кладёт ключ в тело запроса, бэкенд его не читает, и ответ
при этом «успех». Для владельца это выглядит хуже обычной ошибки: он что-то
выбрал, нажал кнопку, увидел «✅ готово» — и выбор исчез без следа, причём
узнать об этом неоткуда. Класс ловился в проекте трижды, и каждый раз руками:

  * «Воркфлоу» по шаблону. Экран спрашивал «Создать воркфлоу по шаблону
    "💧 Дрип-серия"?», присылал `template` и получал «✅ Воркфлоу по шаблону
    создан». Обработчик `template` не читал: сценарий создавался пустой.
  * Бот сценария. Модалка создания показывает выбор «Бот», не открывается,
    пока боты не загружены, и присылает `bot_id`. Обработчик его не читал, и
    колонки под него не было вовсе: выбор владельца пропадал.
  * Шаги при создании сценария уходили в обработчик позиционным аргументом и
    попадали в `description` — проверенные шаги терялись молча
    (tests/test_workflows_alive_postgres.py хранит отдельный храповик на это).

Проверка сплошная: разбирает КАЖДЫЙ вызов `api(путь, {body: JSON.stringify(
{...})})` во фронте, сопоставляет путь с зарегистрированным маршрутом и
смотрит, упоминается ли каждый ключ в тексте обработчика или в тексте
функций, которые обработчик зовёт (один уровень вглубь — ключи часто читает
помощник: `_gift_selection`, `save_strategy`).

ИЗМЕРЕНИЕ на момент написания: 150 вызовов с телом, все 150 сопоставлены с
маршрутом, ключей-сирот — ноль. Последний был `proxy_id` в повторном запросе
кода: сервер его не читает и не должен (повтор идёт тем же клиентом Telethon,
а значит тем же прокси), поэтому ключ убран из запроса, а не добавлен в
исключения. Исключений у проверки нет намеренно: каждое из них — это место,
где экран и сервер снова разошлись, просто с запиской.

ЧЕГО ПРОВЕРКА НЕ ВИДИТ (честно, чтобы на неё не полагались сверх меры): тело,
собранное не литералом (`JSON.stringify(obj)`), ключ, прочитанный через
`**body` или по вычисленному имени, и ключ, который читается, но не
используется. Последнее — отдельный класс, который так не поймать.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from aiohttp import web

from services import mini_app_api as M

ROOT = Path(__file__).resolve().parents[1]


class _Pool:
    async def fetch(self, q, *a): return []
    async def fetchrow(self, q, *a): return None
    async def fetchval(self, q, *a): return None
    async def execute(self, q, *a): return "OK"


def _front() -> str:
    text = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    screens = ROOT / "mini_app" / "screens"
    if screens.is_dir():
        for p in sorted(screens.glob("*.js")):
            text += "\n" + p.read_text(encoding="utf-8")
    return text


def _balanced(text: str, i: int) -> str:
    """Содержимое фигурных скобок, открывшихся на позиции i."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
    return ""


def _top_level_keys(inner: str) -> set[str]:
    """Левые части пар объекта: и `{a: b}`, и сокращение `{a, b}`."""
    keys, depth, buf, parts = set(), 0, "", []
    for ch in inner:
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    for part in parts:
        head = part.split(":")[0].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", head):
            keys.add(head)
    return keys


def _front_calls() -> list[tuple[str, str, list[str]]]:
    """(метод, путь-пример, ключи тела) для каждого вызова api() с телом."""
    front = _front()
    calls = []
    for m in re.finditer(r"""\bapi\(\s*(['"`])([^'"`]*)\1\s*,""", front):
        path = m.group(2)
        if not path.startswith("/api/"):
            continue
        tail = front[m.end():m.end() + 2000]
        # Объект настроек — ПЕРВАЯ скобка после запятой. Без этого DELETE без
        # тела подхватывал тело соседнего вызова ниже в файле.
        om = re.search(r"\{", tail)
        if not om:
            continue
        opts = _balanced(tail, om.start())
        jm = re.search(r"JSON\.stringify\(\s*\{", opts)
        if not jm:
            continue
        keys = _top_level_keys(_balanced(opts, jm.end() - 1))
        if not keys:
            continue
        method = re.search(r"method:\s*['\"](\w+)['\"]", opts)
        calls.append((method.group(1).upper() if method else "POST",
                      re.sub(r"\$\{[^}]+\}", "1", path.split("?")[0]),
                      sorted(keys)))
    return calls


def _routes() -> dict[tuple[str, str], object]:
    app = web.Application()
    M.setup_routes(app, _Pool())
    out = {}
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or ""
        if path:
            out[(route.method, path)] = route.handler
    return out


def _sources() -> dict[tuple[str, str], str]:
    """Текст каждой функции services/*.py, ключ — (модуль, имя).

    Имя без модуля не годится: одноимённые функции есть в разных модулях, и
    проверка брала бы текст чужой — находка про `add_keyword` была именно
    такой подменой.
    """
    out: dict[tuple[str, str], str] = {}
    for f in sorted((ROOT / "services").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        lines = src.split("\n")
        try:
            tree = ast.parse(src)
        except SyntaxError:                                  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name != "setup_routes":
                out.setdefault(("services." + f.stem, node.name),
                               "\n".join(lines[node.lineno - 1:node.end_lineno]))
    return out


def _match(routes, method: str, path: str):
    for (rm, rp), handler in routes.items():
        if rm != method:
            continue
        rx = "^" + re.sub(r"\{[^}]+\}", "[^/]+",
                          re.escape(rp).replace(r"\{", "{").replace(r"\}", "}")) + "$"
        if re.match(rx, path):
            return handler
    return None


def _reachable_text(sources, handler) -> str | None:
    """Текст обработчика плюс тексты функций, которые он зовёт."""
    name = getattr(handler, "__name__", "")
    body = sources.get((getattr(handler, "__module__", ""), name))
    if body is None:
        return None
    text = body
    for (_mod, other), osrc in sources.items():
        if other != name and (other + "(") in body:
            text += "\n" + osrc
    return text


def _dropped(keys, text: str) -> list[str]:
    return [k for k in keys if f'"{k}"' not in text and f"'{k}'" not in text]


# ── Измерение: проверка смотрит на реальный объём ────────────────────────────

def test_the_measurer_sees_the_calls():
    calls = _front_calls()
    assert len(calls) > 120, (
        f"разобрано всего {len(calls)} вызовов с телом — проверка измеряет не "
        "то, и ключ-сирота пройдёт незамеченным")


def test_every_call_is_matched_to_a_route():
    """Несопоставленный вызов — слепое пятно, а не «ну и ладно»."""
    routes = _routes()
    unmatched = [f"{m} {p}" for m, p, _ in _front_calls()
                 if _match(routes, m, p) is None]
    assert not unmatched, (
        "вызов фронта не нашёл маршрута — это либо мёртвая кнопка, либо "
        "слепое пятно проверки:\n  " + "\n  ".join(sorted(set(unmatched))))


# ── Обратный контроль ────────────────────────────────────────────────────────

def test_the_detector_fires_on_a_dropped_key():
    body = """
    async def handler(request):
        data = await request.json()
        name = data.get("name")
        return _json_resp({"ok": True, "name": name})
    """
    assert _dropped(["name"], body) == []
    assert _dropped(["name", "template"], body) == ["template"], (
        "детектор не видит ключ, которого в обработчике нет — он бесполезен")


def test_the_detector_counts_a_helper_as_reading():
    """Ключ, прочитанный помощником, не находка: так было с inventory_ids."""
    sources = {("services.x", "h"): "def h(r):\n    return pick(r)",
               ("services.x", "pick"): "def pick(r):\n    return r['inventory_ids']"}

    class _H:
        __name__ = "h"
        __module__ = "services.x"

    text = _reachable_text(sources, _H())
    assert _dropped(["inventory_ids"], text) == []


# ── Сама проверка ────────────────────────────────────────────────────────────

def test_no_field_the_screen_sends_is_dropped():
    routes, sources = _routes(), _sources()
    offenders, unreadable = [], []
    for method, path, keys in _front_calls():
        handler = _match(routes, method, path)
        if handler is None:
            continue                       # покрыто отдельной проверкой выше
        text = _reachable_text(sources, handler)
        if text is None:
            unreadable.append(f"{method} {path} → {getattr(handler, '__name__', '?')}")
            continue
        missing = _dropped(keys, text)
        if missing:
            offenders.append(
                f"{method} {path} → {getattr(handler, '__name__', '?')}: "
                + ", ".join(missing))
    assert not unreadable, (
        "исходник обработчика не найден — проверка ослепла на этих маршрутах:\n  "
        + "\n  ".join(sorted(unreadable)))
    assert not offenders, (
        "экран присылает поле, которого обработчик не читает: владелец "
        "выберет его, увидит «готово», и выбор исчезнет без следа:\n  "
        + "\n  ".join(sorted(offenders)))
