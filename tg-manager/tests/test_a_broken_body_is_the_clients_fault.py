"""Битое тело запроса — вина клиента, а не наша пятисотка.

ЧТО ЗАЩИЩАЕМ. Пятисотка означает «сломались мы, идите смотреть»; четырёхсотка
— «вход негодный». Когда разбор тела падает внутрь общего `except` и отвечает
500 с `log.exception`, обрезанный на мобильной сети запрос становится записью
ERROR с трассировкой. Такие записи не просто лишние: они топят в себе
настоящие пятисотки, то есть отключают сигнал, ради которого он и ведётся.
Этот заход был намечен прямо в CLAUDE.md («битое тело запроса тоже отвечает
пятисоткой, это следующий заход») — здесь он сделан.

ЧТО ИЗМЕРЕНО. 248 мест читают тело запроса. 211 отвечают четырёхсоткой (чаще
всего через общий помощник `bad_body_response`, который разбирает причину и
переводит её в отказ клиенту), 25 осознанно считают пустое тело допустимым
(`except: body = {}` — у ручки нет обязательных полей). Пятисоткой битое тело
становилось у 11.

Десять из одиннадцати — ручки без двери: их не зовёт ни один экран
(`tests/test_every_route_has_a_door.py`). Одиннадцатая, `uch_crm_upsert` на
`/api/miniapp/uch/contacts/{contact_id}/crm`, достижима с экрана контактов, и
это была настоящая дыра — она исправлена в том же коммите вызовом того же
помощника.

Нашло её это правило, и нашло ОШИБКУ В МОЕЙ ЖЕ ПРЕДВАРИТЕЛЬНОЙ ПРИКИДКЕ: я
сверял достижимость, ища во фронте путь целиком, вместе с `{contact_id}`.
Такой строки во фронте нет ни у одной параметрической ручки, и прикидка
объявила недостижимыми все одиннадцать. Путь надо обрезать по первой `{` —
именно так считает дверь `_doorless`, и именно поэтому мерка из правила
оказалась права, а глазомер нет.

ПОЧЕМУ ЭТО ПРАВИЛО, А НЕ ЗАПИСЬ В ЖУРНАЛ. Список исключений здесь не написан
руками, а ВЫЧИСЛЯЕТСЯ: ручка освобождается ровно до тех пор, пока у её
маршрута нет двери. Как только экран её позовёт, правило покраснеет и
потребует честную четырёхсотку — то есть долг не сможет приехать к владельцу
вместе с новым экраном. Два класса долга (ручка без двери и пятисотка на
битом теле) оказались одним и тем же списком, и держать их связанными
правильнее, чем описывать по отдельности.
"""
from __future__ import annotations

import ast
import re

from tests.miniapp_source import api_files
from tests.test_every_route_has_a_door import _front_source

_FUNC = (ast.AsyncFunctionDef, ast.FunctionDef)
# Помощник, который разбирает причину падения и переводит её в отказ клиенту.
# Признаётся наравне с прямым `_err(...)`: в этом и смысл общего помощника.
_CLIENT_FAULT = re.compile(r"bad_body_response|_err\(")


def _is_body_read(node: ast.AST) -> bool:
    return (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "json"
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "request")


def _innermost(tree: ast.AST, node: ast.AST):
    """Самая узкая функция, накрывающая узел.

    Ручки мини-аппа объявлены ВНУТРИ `setup_routes`, поэтому «первая функция,
    в которую попал узел» — это всегда `setup_routes`, и находка называлась бы
    её именем. Ровно на этом спотыкался храповик про потолок в подписи, и то же
    правило здесь: имя находки — имя ручки.
    """
    best = None
    for f in ast.walk(tree):
        if not isinstance(f, _FUNC):
            continue
        if f.lineno <= node.lineno <= (f.end_lineno or 0):
            if best is None or (f.end_lineno - f.lineno) < (best.end_lineno - best.lineno):
                best = f
    return best


def _verdict(handlers: list[ast.ExceptHandler], src: str) -> str:
    seg = "\n".join(ast.get_source_segment(src, s) or ""
                    for h in handlers for s in h.body)
    if "500" not in seg and _CLIENT_FAULT.search(seg):
        return "4xx"
    if _CLIENT_FAULT.search(seg) and "bad_body_response" in seg:
        return "4xx"          # помощник отвечает раньше, чем доходит до 500
    for h in handlers:
        for s in h.body:
            # Пустое значение или `pass` — осознанное «тело не обязательно».
            if isinstance(s, ast.Assign) and isinstance(s.value, (ast.Dict, ast.Constant)):
                return "пустое"
            if isinstance(s, ast.Pass):
                return "пустое"
    if "500" in seg:
        return "500"
    return "иной"


def _readers(extra: tuple[str, ...] = ()) -> tuple[dict[str, str], int]:
    """{имя ручки: вердикт} и сколько чтений тела всего отобрано."""
    out: dict[str, str] = {}
    total = 0
    sources = [p.read_text(encoding="utf-8") for p in api_files()] + list(extra)
    for src in sources:
        tree = ast.parse(src)
        tries = [t for t in ast.walk(tree) if isinstance(t, ast.Try)]
        for node in ast.walk(tree):
            if not _is_body_read(node):
                continue
            total += 1
            fn = _innermost(tree, node)
            name = fn.name if fn else "?"
            cover = [t for t in tries
                     if t.lineno <= node.lineno <= (t.end_lineno or 0)
                     and not any(h.lineno <= node.lineno <= (h.end_lineno or 0)
                                 for h in t.handlers)]
            if not cover:
                out[name] = "вне try"
                continue
            narrow = min(cover, key=lambda t: (t.end_lineno or 0) - t.lineno)
            v = _verdict(narrow.handlers, src)
            # У ручки может быть несколько чтений; беда важнее благополучия.
            if out.get(name) not in ("500", "вне try"):
                out[name] = v
    return out, total


def _handler_paths() -> dict[str, str]:
    """{имя ручки: путь маршрута} по строкам регистрации всего API-слоя."""
    reg = re.compile(r"""add_(?:get|post|put|patch|delete)\(\s*["']"""
                     r"""(/api/miniapp/[^"']*)["']\s*,\s*([A-Za-z_][\w]*)""")
    out: dict[str, str] = {}
    for p in api_files():
        for path, name in reg.findall(p.read_text(encoding="utf-8")):
            out.setdefault(name, path)
    return out


def _reachable_offenders() -> list[str]:
    """Ручки, которые на битом теле отвечают пятисоткой И достижимы с экрана."""
    readers, _total = _readers()
    paths = _handler_paths()
    front = _front_source()
    bad = []
    for name, verdict in sorted(readers.items()):
        if verdict in ("4xx", "пустое"):
            continue
        path = paths.get(name)
        if path is None:
            continue                      # не зарегистрирована — звать нечем
        if path.split("{")[0].rstrip("/") not in front:
            continue                      # двери нет: долг описан отдельным правилом
        bad.append(f"{name} ({path})")
    return bad


def test_a_reachable_handler_blames_the_client_for_a_broken_body():
    bad = _reachable_offenders()
    assert bad == [], (
        "битое тело становится пятисоткой с log.exception: обрезанный на "
        "мобильной сети запрос топит настоящие пятисотки в ложных — "
        + ", ".join(bad))


def test_the_measure_still_finds_the_body_readers():
    _readers_map, total = _readers()
    assert total >= 200, (
        f"отбор чтений тела обмелел до {total} — скорее всего сломан он, а не API")


def test_the_exemption_is_computed_and_not_hand_written():
    """Освобождение держится на отсутствии двери, а не на списке имён.

    Если у освобождённой ручки появится экран, правило обязано покраснеть:
    иначе долг приедет к владельцу вместе с новым экраном и будет выглядеть
    внутренней ошибкой приложения.
    """
    readers, _t = _readers()
    paths = _handler_paths()
    front = _front_source()
    exempt = [n for n, v in readers.items()
              if v not in ("4xx", "пустое") and n in paths
              and paths[n].split("{")[0].rstrip("/") not in front]
    assert exempt, "освобождённых нет — проверка ниже стала бы пустой"
    for name in exempt:
        assert paths[name].split("{")[0].rstrip("/") not in front, name
    # И каждая освобождённая обязана быть в описи ручек без двери.
    from tests.test_every_route_has_a_door import _KNOWN_DOORLESS
    for name in exempt:
        assert paths[name] in _KNOWN_DOORLESS, (
            f"{name} освобождена как ручка без двери, но в описи "
            "tests/test_every_route_has_a_door.py её нет — один из двух "
            "списков устарел")


# ── Самопроверки отбора ────────────────────────────────────────────────────

_PROBE_500 = '''
def setup_routes(app, pool):
    async def probe_blames_itself(request):
        try:
            data = await request.json()
            return _json_resp({"ok": True, "n": len(data)})
        except Exception:
            log.exception("probe_blames_itself")
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_post("/api/miniapp/probe/blame", probe_blames_itself)
'''

_PROBE_HELPER = '''
def setup_routes(app, pool):
    async def probe_uses_the_helper(request):
        try:
            data = await request.json()
            return _json_resp({"ok": True, "n": len(data)})
        except Exception as exc:
            _bad = bad_body_response(request, exc)
            if _bad is not None:
                return _bad
            log.exception("probe_uses_the_helper")
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_post("/api/miniapp/probe/helper", probe_uses_the_helper)
'''

_PROBE_EMPTY_OK = '''
def setup_routes(app, pool):
    async def probe_allows_an_empty_body(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        return _json_resp({"ok": True, "n": len(body)})

    app.router.add_post("/api/miniapp/probe/empty", probe_allows_an_empty_body)
'''


def test_a_fabricated_five_hundred_is_caught():
    readers, _t = _readers((_PROBE_500,))
    assert readers.get("probe_blames_itself") == "500", (
        "правило не видит пятисотку на битом теле — оно пустое")


def test_the_shared_helper_counts_as_blaming_the_client():
    readers, _t = _readers((_PROBE_HELPER,))
    assert readers.get("probe_uses_the_helper") == "4xx", (
        "общий помощник не зачтён: ровно этот промах дал 38 ложных находок "
        "вместо 11 настоящих")


def test_an_allowed_empty_body_is_not_a_defect():
    readers, _t = _readers((_PROBE_EMPTY_OK,))
    assert readers.get("probe_allows_an_empty_body") == "пустое", (
        "ручка без обязательных полей объявлена дырой")


def test_the_finding_names_the_handler_not_setup_routes():
    readers, _t = _readers((_PROBE_500,))
    assert "setup_routes" not in readers, (
        "находка названа именем внешней функции — имя ручки потеряно")
