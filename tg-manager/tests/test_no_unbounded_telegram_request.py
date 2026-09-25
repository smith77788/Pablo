"""Храповик: запрос к Telegram в пути операции обязан иметь потолок.

Что этот тест не даёт сделать: добавить в модуль, который зовёт исполнитель
операций, вызов к Telegram без ограничения по времени.

Почему это не стилевое требование. Коннект давно ограничен
(`_connect_and_track` оборачивает `client.connect()`), но мёртвый прокси чаще
отдаёт не отказ, а half-open сокет: TCP установлен, коннект прошёл, ответа нет
и не будет. Такой запрос не возвращается НИКОГДА — ни ошибкой, ни успехом.

Цена ровно та, из-за которой это попало в надёжность операций: массовые
операции идут циклом accounts × цели, последовательно. Один повисший аккаунт
останавливал ВСЮ операцию, и она держала слот параллельности (один из восьми) и
арендованный флот до потолка прогона, то есть часы, не делая ничего. Владелец
видел «выполняется» с замершим счётчиком.

Потолок обязан быть ЩЕДРЫМ. Живой Telegram отвечает за секунды, поэтому 45
секунд означают мёртвый сокет, а не медленный ответ. Занижать нельзя: ложный
таймаут на успешном действии уводит цель в повтор, то есть в лишнее действие по
Telegram, а для вступления это прямое давление к PEER_FLOOD. Перенос медиа —
отдельный случай (`_MEDIA_TIMEOUT`): видео законно едет минутами.
"""
from __future__ import annotations

import ast
import glob
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Модули, которые пока не пройдены целиком.
#
# Прежняя формулировка этого списка звучала «фоновый цикл — зависание там
# останавливает СВОЙ цикл, а не операцию владельца». Она была НЕВЕРНА и стоила
# продукту флота. Фоновые циклы берут аккаунт у ТОГО ЖЕ арбитра
# (op_worker.try_claim_account) и отпускают его в finally. Повисший запрос из
# finally не возвращается никогда, аккаунт остаётся в
# op_worker._accounts_in_use, renew_leases() продлевает аренду вечно, а
# реконсилер такую строку намеренно не чистит — для него живая память и есть
# доказательство занятости. Аккаунт выпадал из ВСЕГО до рестарта процесса.
# Поэтому прогрев, призрак, активность, сетка контента, прогрев чатов и консоль
# аккаунта из списка убраны и пройдены; держит их
# tests/test_background_hang_releases_account.py. Оттуда же ушёл
# invite_behavior: он вообще не фоновый — `humanize()` зовут прямо из
# `_exec_mass_invite`.
#
# Остались два класса. Первый — путь HTTP-запроса: там у шлюза есть свой
# таймаут, ответ владельцу не зависнет навсегда, и аккаунт эти модули у арбитра
# не берут. Второй — фоновая уборка, которая тоже ничего не держит.
ALLOWED_MODULES = {
    "account_cleaner": "фоновая уборка аккаунта, аккаунт у арбитра не берёт",
    "rest_api": "внешний REST: путь HTTP-запроса, у шлюза свой таймаут",
    "session_importer": "импорт сессий: путь HTTP-запроса",
    "entity_analyzer": "анализ сущности: путь HTTP-запроса",
    # Особый случай: модуль огромный (свыше сотни функций) и обслуживает и
    # операции, и мини-апп. Пройден НЕ целиком, а по функциям, которые зовёт
    # исполнитель операций, и сторожит их поимённо соседний тест
    # tests/test_join_leave_request_timeout.py. Целиком — отдельной задачей;
    # у функций пути HTTP-запроса есть таймаут шлюза, у операции его нет.
    "account_manager": "пройден по функциям пути операций; сторожит "
                       "tests/test_join_leave_request_timeout.py",
}

_CLIENT_NAMES = {"client", "cl", "c"}
# Не запросы к сети: локальное закрытие сокета и чтение состояния.
_LOCAL_METHODS = {"disconnect", "is_connected", "session", "is_user_authorized"}


def _is_client_call(node) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Name) and f.id in _CLIENT_NAMES:
        return True
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
            and f.value.id in _CLIENT_NAMES:
        return f.attr not in _LOCAL_METHODS
    return False


def _is_wait_for(node) -> bool:
    f = getattr(node, "func", None)
    return (isinstance(f, ast.Attribute) and f.attr == "wait_for") or \
           (isinstance(f, ast.Name) and f.id == "wait_for")


def _is_client_context(node) -> bool:
    """`async with client:` — коннект без потолка, который детектор долго не видел.

    Менеджер контекста телетона зовёт `client.start()`: коннект плюс запрос
    getMe. Обернуть это в `asyncio.wait_for` по месту нельзя — обёртки требует
    сама форма записи, поэтому такая строка считается голой всегда. Канонический
    путь — `account_manager._connect_and_track`, у него есть `_CONNECT_TIMEOUT`,
    и он же пишет успех прокси в infra_memory.
    """
    if not isinstance(node, ast.AsyncWith):
        return False
    return any(isinstance(it.context_expr, ast.Name)
               and it.context_expr.id in _CLIENT_NAMES for it in node.items)


def naked_in_scope(scope) -> list[int]:
    """Строки запросов к Telegram внутри узла, не обёрнутых в asyncio.wait_for."""
    wrapped: set[int] = set()
    for n in ast.walk(scope):
        if isinstance(n, ast.Call) and _is_wait_for(n) and n.args:
            for sub in ast.walk(n.args[0]):
                if _is_client_call(sub):
                    wrapped.add(id(sub))
    return sorted([n.lineno for n in ast.walk(scope)
                   if isinstance(n, ast.Await) and _is_client_call(n.value)
                   and id(n.value) not in wrapped]
                  + [n.lineno for n in ast.walk(scope) if _is_client_context(n)])


def naked_requests(src: str) -> list[int]:
    return naked_in_scope(ast.parse(src))


def _modules_with_clients() -> dict[str, str]:
    out = {}
    for path in sorted(glob.glob(os.path.join(ROOT, "services", "*.py"))):
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if "_make_client(" in src or "TelegramClient(" in src:
            out[os.path.basename(path)[:-3]] = src
    return out


# ── Сам храповик ─────────────────────────────────────────────────────────────

def test_operation_path_has_no_unbounded_requests():
    offenders = {}
    for mod, src in _modules_with_clients().items():
        if mod in ALLOWED_MODULES:
            continue
        naked = naked_requests(src)
        if naked:
            offenders[mod] = naked
    assert not offenders, (
        "запросы к Telegram без потолка:\n  "
        + "\n  ".join(f"services/{m}.py строки {ls}" for m, ls in sorted(offenders.items()))
        + "\n\nОберните в asyncio.wait_for с потолком модуля (45с — штатный для "
          "одиночного запроса, _MEDIA_TIMEOUT — для переноса медиа). Если модуль "
          "не в пути операций, внесите его в ALLOWED_MODULES с объяснением."
    )


# ── Сторожа самого храповика ─────────────────────────────────────────────────

def test_detector_finds_the_modules_at_all():
    mods = _modules_with_clients()
    assert len(mods) > 15, f"модулей с клиентом найдено {len(mods)} — разбор сломан"


def test_detector_catches_a_naked_request():
    """Детектор, который ничего не находит, выглядит зелёным и потому опасен."""
    assert naked_requests(
        "async def f(client):\n"
        "    return await client.get_entity('x')\n") == [2]


def test_detector_accepts_a_wrapped_request():
    assert naked_requests(
        "async def f(client):\n"
        "    return await asyncio.wait_for(client.get_entity('x'), timeout=45)\n") == []


def test_detector_catches_async_with_client():
    """`async with client` прятался от детектора и стоил трёх мест в продукте."""
    assert naked_requests(
        "async def f(client):\n"
        "    async with client:\n"
        "        pass\n") == [2]


def test_detector_ignores_local_calls():
    """disconnect закрывает сокет локально — ждать там нечего."""
    assert naked_requests(
        "async def f(client):\n"
        "    await client.disconnect()\n") == []


def test_allowlist_has_no_stale_entries():
    mods = _modules_with_clients()
    gone = sorted(m for m in ALLOWED_MODULES if m not in mods)
    assert not gone, f"в ALLOWED_MODULES остались несуществующие модули: {gone}"


def test_allowlist_has_no_clean_entries():
    """Модуль вычистили — строка в ALLOWED_MODULES больше не нужна и врёт."""
    mods = _modules_with_clients()
    clean = sorted(m for m in ALLOWED_MODULES
                   if m in mods and not naked_requests(mods[m]))
    assert not clean, (
        f"эти модули уже без голых запросов, уберите их из ALLOWED_MODULES: {clean}")


# ── Потолки ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mod,const", [
    ("account_manager", "_OP_TIMEOUT"),
    ("parser", "_OP_TIMEOUT"),
    ("account_warmer", "_TG_TIMEOUT"),
    ("activity_engine", "_TG_TIMEOUT"),
    ("ghost_engine", "_TG_TIMEOUT"),
    ("content_mesh", "_TG_TIMEOUT"),
    ("chat_warmup", "_TG_TIMEOUT"),
    ("invite_behavior", "_TG_TIMEOUT"),
])
def test_single_request_timeout_is_generous(mod, const):
    """Занижение опаснее отсутствия: ложный таймаут уводит цель в лишний повтор."""
    ns: dict = {}
    src = _modules_with_clients()[mod]
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == const:
            ns[const] = ast.literal_eval(node.value)
    assert ns.get(const, 0) >= 30, (
        f"{mod}.{const} опустили ниже 30с: живой ответ через медленный прокси "
        f"начнёт считаться сбоем")


def test_media_transfer_has_its_own_larger_ceiling():
    """Видео законно едет минутами — общий потолок обрывал бы здоровую работу."""
    src = _modules_with_clients()["content_cloner_engine"]
    media = next(ast.literal_eval(n.value) for n in ast.parse(src).body
                 if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                 and n.targets[0].id == "_MEDIA_TIMEOUT")
    assert media >= 120, "потолок переноса медиа занижен — оборвёт здоровую загрузку"
    assert "timeout=_MEDIA_TIMEOUT" in src


def test_account_manager_operation_path_is_actually_guarded():
    """Оговорка про account_manager обязана быть правдой.

    Модуль стоит в ALLOWED_MODULES не потому, что его можно не чинить, а
    потому, что он пройден по функциям и сторожится поимённо. Если тот тест
    исчезнет или перестанет покрывать путь операций, эта строка превратится в
    тихую дыру — проверяем связку напрямую.
    """
    with open(os.path.join(ROOT, "tests", "test_join_leave_request_timeout.py"),
              encoding="utf-8") as f:
        guard_src = f.read()
    guarded = next(
        ast.literal_eval(n.value) for n in ast.parse(guard_src).body
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id == "GUARDED")
    assert len(guarded) >= 20, "список поимённой охраны подозрительно короткий"

    tree = ast.parse(_modules_with_clients()["account_manager"])
    seen, dirty = set(), []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name in guarded:
            seen.add(fn.name)
            if naked_in_scope(fn):
                dirty.append(fn.name)
    assert not dirty, f"функции пути операций снова без потолка: {dirty}"
    missing = sorted(set(guarded) - seen)
    assert not missing, f"охраняемые функции исчезли из модуля: {missing}"
