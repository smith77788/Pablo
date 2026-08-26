"""Каждый вызов API из мини-аппа обязан иметь маршрут — и нужный метод.

ЗАЧЕМ. Кнопка, которая шлёт запрос в несуществующий маршрут, не падает заметно:
пользователь видит «Ошибка» или просто ничего. Так и было с добавлением ключевого
слова на экране «Рейтинг»: фронт слал POST на /api/miniapp/ranking/keywords, а
маршрут был зарегистрирован ТОЛЬКО на GET — 405, и экран нельзя было наполнить.
Метод здесь важен не меньше пути: совпадающий путь с чужим методом выглядит как
рабочая ссылка и не работает.

Проверка статическая и быстрая: разбираем вызовы `api(...)` в index.html и
таблицу `app.router.add_*` в mini_app_api. Пути с подстановкой (`${id}`)
сопоставляются с шаблонами маршрутов (`{acc_id}`).
"""
from __future__ import annotations

import collections
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MINI_APP = os.path.join(ROOT, "mini_app")
_HTML = os.path.join(_MINI_APP, "index.html")
_SCREENS = os.path.join(_MINI_APP, "screens")
_API = os.path.join(ROOT, "services", "mini_app_api.py")

_ROUTE = re.compile(
    r'app\.router\.add_(get|post|put|patch|delete)\(\s*"([^"]+)"')
# api('/path')  |  api(`/path/${x}`, {method:'POST'})
_CALL = re.compile(
    r"api\(\s*(`[^`]+`|'[^']+'|\"[^\"]+\")\s*(?:,\s*\{(.{0,200}?)\})?\s*\)", re.S)
_METHOD = re.compile(r"method\s*:\s*['\"](\w+)['\"]")


def _routes() -> dict[str, set[str]]:
    src = open(_API, encoding="utf-8").read()
    out: dict[str, set[str]] = collections.defaultdict(set)
    for m in _ROUTE.finditer(src):
        out[m.group(2)].add(m.group(1).upper())
    return out


def _matcher(template: str) -> re.Pattern:
    """Шаблон маршрута → регулярка. `{acc_id}` совпадает с одним сегментом."""
    parts = re.split(r"(\{[^}]+\})", template)
    body = "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts)
    return re.compile("^" + body + "$")


def _sources() -> list[tuple[str, str]]:
    """(имя файла, текст) — index.html и вынесенные экраны screens/*.js.

    Экраны вынесены в отдельные файлы и подключаются <script src>; проверка,
    которая смотрит только в index.html, их вызовы просто не увидит.
    """
    out = [("index.html", open(_HTML, encoding="utf-8").read())]
    if os.path.isdir(_SCREENS):
        for fname in sorted(os.listdir(_SCREENS)):
            if fname.endswith(".js"):
                out.append((f"screens/{fname}",
                            open(os.path.join(_SCREENS, fname),
                                 encoding="utf-8").read()))
    return out


def _calls() -> list[tuple[str, str, str, int]]:
    out = []
    for name, text in _sources():
        for m in _CALL.finditer(text):
            raw = m.group(1)[1:-1]
            mm = _METHOD.search(m.group(2) or "")
            method = (mm.group(1) if mm else "GET").upper()
            # `${...}` — подстановка значения: один сегмент пути.
            path = re.sub(r"\$\{[^}]*\}", "X", raw).split("?")[0]
            if path.startswith("/api/"):
                out.append((path, method, name, text[:m.start()].count("\n") + 1))
    return out


def _problems() -> list[str]:
    routes = _routes()
    pats = [(_matcher(t), t, ms) for t, ms in routes.items()]
    bad = []
    for path, method, where, line in _calls():
        hit = [(t, ms) for rx, t, ms in pats if rx.match(path)]
        if not hit:
            bad.append(f"{where}:{line}: {method} {path} — маршрута нет вовсе")
        elif not any(method in ms for _t, ms in hit):
            have = sorted(set().union(*[ms for _t, ms in hit]))
            bad.append(
                f"{where}:{line}: {method} {path} — маршрут есть, "
                f"но только на {', '.join(have)}")
    return sorted(set(bad))


def test_every_miniapp_call_reaches_a_route():
    bad = _problems()
    assert not bad, (
        "мини-апп зовёт то, чего на бэкенде нет:\n  " + "\n  ".join(bad)
        + "\n\nСнаружи это «кнопка не работает»: ответ 404/405 экран показывает "
          "как ошибку или как пустоту."
    )


def test_parser_sees_both_sides():
    """Пустая таблица маршрутов или вызовов сделала бы тест зелёным навсегда."""
    routes, calls = _routes(), _calls()
    assert len(routes) > 300, f"разобрано всего {len(routes)} маршрутов"
    assert len(calls) > 200, f"разобрано всего {len(calls)} вызовов из мини-аппа"
    assert any(w.startswith("screens/") for _p, _m, w, _l in calls), (
        "вызовы из вынесенных экранов не попали в проверку — их маршруты "
        "никто не проверяет")


def test_matcher_handles_path_parameters():
    rx = _matcher("/api/miniapp/accounts/{acc_id}/health")
    assert rx.match("/api/miniapp/accounts/X/health")
    assert not rx.match("/api/miniapp/accounts/X/Y/health"), "шаблон не должен ловить лишний сегмент"
    assert not rx.match("/api/miniapp/accounts/health")


def test_detector_would_catch_a_method_mismatch():
    """Ровно тот случай, ради которого тест написан (POST на GET-маршрут)."""
    pats = [(_matcher("/api/miniapp/ranking/keywords"),
             "/api/miniapp/ranking/keywords", {"GET"})]
    path, method = "/api/miniapp/ranking/keywords", "POST"
    hit = [(t, ms) for rx, t, ms in pats if rx.match(path)]
    assert hit and not any(method in ms for _t, ms in hit)
