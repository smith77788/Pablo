"""Запрос к Telegram в массовом вступлении/выходе обязан иметь потолок.

Разрыв. Потолок стоял только на КОННЕКТЕ (`_connect_and_track` оборачивает
`client.connect()` в `wait_for`). Сами запросы после коннекта — `get_entity`,
`JoinChannelRequest`, `ImportChatInviteRequest`, `LeaveChannelRequest` — шли без
ограничения.

Разница существенна именно для прокси. Мёртвый прокси чаще отдаёт не отказ, а
half-open сокет: TCP установлен, коннект прошёл, ответа нет и не будет. Такой
запрос не возвращается НИКОГДА.

Цена. Вступление и выход идут циклом accounts × каналы, последовательно. Один
повисший аккаунт останавливал ВСЮ операцию: она держала слот параллельности
(один из восьми) и арендованный флот до потолка прогона, то есть часы, ничего
при этом не делая. Владелец видел операцию «выполняется» с замершим счётчиком.

Почему потолок именно щедрый. Живой Telegram отвечает за секунды, и 45 секунд
(`_OP_TIMEOUT`) — это заведомо мёртвый сокет, а не медленный ответ. Взять
меньше означало бы ловить ложные таймауты на успешных вступлениях, а ложная
неудача здесь хуже задержки: цель уходит в повтор, то есть в ЛИШНИЙ
joinChannel — прямое давление к PEER_FLOOD, от которого защищает весь пейсинг
вокруг. Вступление — самый баноопасный класс операций продукта.
"""
from __future__ import annotations

import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Функции, которые зовёт исполнитель операций: у них зависший запрос стоит
# слота параллельности и арендованного флота на часы, а не одного HTTP-ответа.
GUARDED = (
    "join_channel",
    "leave_channel",
    "send_dm",
    "report_peer",
    "create_channel",
    "create_channel_invite_link",
    "create_forum_supergroup",
    "create_shared_folder_link",
    "create_bot_via_botfather",
    "scan_owned_bots",
    "fetch_bot_tokens_via_botfather",
    "edit_channel_title",
    "edit_channel_about",
    "set_channel_photo",
    "set_channel_username",
    "set_discussion_group",
    "promote_to_admin_ex",
    "demote_from_admin",
    "demote_from_admin_batch",
    "get_channels_full_info",
    "forward_new_posts",
    "update_profile",
    "update_account_username",
    # Добавлено после разбора: исполнитель зовёт их НЕ напрямую, а через движок
    # (strike_engine, channel_ranking), и рукописный список это пропустил.
    # Чтобы пропуск не повторился, соседний тест ниже считает этот список сам.
    "report_peer_deep_v2",
    "strike_map_target",
    "search_global_ranked",
    "get_contacts",
)


def _tree():
    with open(os.path.join(ROOT, "services", "account_manager.py"), encoding="utf-8") as f:
        src = f.read()
    return src, ast.parse(src)


def _is_client_call(node) -> bool:
    """await client(...) либо await client.<метод>(...) — запрос в Telegram."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Name) and f.id == "client":
        return True
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "client":
        # disconnect закрывает сокет локально, ждать нечего.
        return f.attr not in ("disconnect", "is_connected", "session")
    return False


def _is_wait_for(node) -> bool:
    f = getattr(node, "func", None)
    return (isinstance(f, ast.Attribute) and f.attr == "wait_for") or \
           (isinstance(f, ast.Name) and f.id == "wait_for")


def _naked_requests(fn_name: str) -> list[int]:
    """Строки запросов к Telegram, не обёрнутых в wait_for."""
    _src, tree = _tree()
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == fn_name)
    wrapped: set[int] = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and _is_wait_for(n) and n.args:
            for sub in ast.walk(n.args[0]):
                if _is_client_call(sub):
                    wrapped.add(id(sub))
    return [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Await) and _is_client_call(n.value)
            and id(n.value) not in wrapped]


# ── Сам инвариант ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", GUARDED)
def test_every_request_is_bounded(name):
    naked = _naked_requests(name)
    assert not naked, (
        f"{name}: запросы без потолка в строках {naked} — half-open сокет "
        f"мёртвого прокси остановит всю массовую операцию на часы"
    )


@pytest.mark.parametrize("name", GUARDED)
def test_timeout_is_generous_enough_not_to_fail_live_requests(name):
    """Слишком короткий потолок опаснее его отсутствия.

    Ложный таймаут на успешном вступлении уводит цель в повтор, то есть в
    лишний joinChannel — давление к PEER_FLOOD.
    """
    src, tree = _tree()
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    body = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])
    assert "timeout=_OP_TIMEOUT" in body, (
        f"{name}: потолок задан не общей константой — он разъедется с остальными"
    )
    from services import account_manager
    assert account_manager._OP_TIMEOUT >= 30, (
        "потолок одиночной операции опустили ниже 30с: живой ответ через "
        "медленный прокси начнёт считаться сбоем"
    )


@pytest.mark.parametrize("name", ("join_channel", "leave_channel"))
def test_timeout_is_classified_as_a_proxy_failure(name):
    """Таймаут обязан разбираться, а не всплывать сырым исключением.

    Исполнитель по признаку proxy_error останавливает остальные цели этого
    аккаунта — это и есть правильная реакция на мёртвый прокси.
    """
    src, tree = _tree()
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    body = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])
    assert "except asyncio.TimeoutError:" in body
    assert "proxy_error" in body


# ── Список GUARDED считается, а не пишется от руки ───────────────────────────

def _modules_executors_delegate_to() -> set[str]:
    """Модули, которые исполнитель операций зовёт ИЗ ТЕЛА `_exec_*`.

    Именно они продолжают путь операции: зависание внутри них стоит слота
    параллельности и арендованного флота, а не одного HTTP-ответа.
    """
    with open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    mods: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.startswith("_exec"):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "services":
                    mods |= {a.name for a in node.names}
                elif node.module.startswith("services."):
                    mods.add(node.module.split(".", 1)[1])
    return {m for m in mods
            if os.path.exists(os.path.join(ROOT, "services", f"{m}.py"))}


def _public_am_functions() -> dict:
    _src, tree = _tree()
    # Только async: запрос в Telegram делается через await, синхронные
    # помощники (форматирование ссылок и т.п.) к сети не ходят.
    return {n.name: n for n in tree.body
            if isinstance(n, ast.AsyncFunctionDef) and not n.name.startswith("_")}


def test_no_operation_path_function_was_left_out_of_guarded():
    """Список выше однажды оказался неполным, и это стоило двадцати дыр.

    `_exec_strike` зовёт `strike_engine`, а тот — `account_manager.
    report_peer_deep_v2` и `strike_map_target`: двадцать запросов без потолка
    прямо в пути самой тяжёлой операции. Рукописный список их не видел, потому
    что перечислял только то, что исполнитель зовёт НАПРЯМУЮ.

    Поэтому список теперь проверяется вычислением: берём модули, которые зовут
    тела `_exec_*`, смотрим, какие публичные функции `account_manager` они
    зовут, и требуем, чтобы у голых запросов среди них не осталось.
    """
    import re

    funcs = _public_am_functions()
    offenders: dict[str, list[str]] = {}
    for mod in sorted(_modules_executors_delegate_to() | {"op_worker"}):
        if mod == "account_manager":
            continue
        with open(os.path.join(ROOT, "services", f"{mod}.py"), encoding="utf-8") as f:
            src = f.read()
        for name, node in funcs.items():
            if name in GUARDED:
                continue
            if not re.search(rf"[\w_]*\.{re.escape(name)}\s*\(", src):
                continue
            if _naked_requests(name):
                offenders.setdefault(name, []).append(mod)
    assert not offenders, (
        "в пути операции есть функции account_manager без потолка:\n  "
        + "\n  ".join(f"{n} ← {', '.join(m)}" for n, m in sorted(offenders.items()))
        + "\n\nОберните запросы в asyncio.wait_for(..., timeout=_OP_TIMEOUT) "
          "и внесите имя в GUARDED."
    )


def test_the_computed_check_can_actually_flag_something():
    """Проверка, которая ничего не способна найти, вечно зелёная и потому лживая.

    `report_peer_deep` (первая версия, без вызывающих) держит два десятка голых
    запросов. Детектор обязан их видеть — и при этом не жаловаться на них: в
    путь операции эта функция не входит, её никто не зовёт. Обе половины важны:
    первая доказывает, что механизм работает, вторая — что он не шумит.
    """
    assert len(_naked_requests("report_peer_deep")) > 10, (
        "детектор перестал видеть голые запросы — проверка выше стала пустой")
    delegated = _modules_executors_delegate_to() | {"op_worker"}
    import re
    callers = [m for m in delegated
               if m != "account_manager"
               and re.search(r"[\w_]*\.report_peer_deep\s*\(",
                             open(os.path.join(ROOT, "services", f"{m}.py"),
                                  encoding="utf-8").read())]
    assert not callers, (
        f"report_peer_deep снова в пути операции ({callers}) — обвяжите её "
        f"потолками и внесите в GUARDED")


def test_delegation_detector_sees_the_engines():
    """Пустой список модулей сделал бы проверку выше вечно зелёной."""
    mods = _modules_executors_delegate_to()
    assert len(mods) > 20, f"модулей найдено {len(mods)} — разбор op_worker сломан"
    assert "strike_engine" in mods, (
        "strike_engine пропал из разбора — именно на нём проверка и была нужна")


# ── Сторож самой проверки ────────────────────────────────────────────────────

def test_detector_would_catch_a_naked_request():
    """Детектор, ничего не находящий, выглядит зелёным и потому опасен."""
    sample = ast.parse(
        "async def _sample(client):\n"
        "    entity = await client.get_entity('x')\n"
        "    await asyncio.wait_for(client(Req()), timeout=1)\n"
    )
    fn = sample.body[0]
    wrapped = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and _is_wait_for(n) and n.args:
            for sub in ast.walk(n.args[0]):
                if _is_client_call(sub):
                    wrapped.add(id(sub))
    naked = [n.lineno for n in ast.walk(fn)
             if isinstance(n, ast.Await) and _is_client_call(n.value)
             and id(n.value) not in wrapped]
    assert naked == [2], naked


def test_connect_itself_stays_bounded():
    """Потолок на запрос не заменяет потолок на коннект — нужны оба."""
    src, tree = _tree()
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_connect_and_track")
    body = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])
    assert "wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)" in body


@pytest.mark.parametrize("name", GUARDED)
def test_timeout_never_escapes_as_a_raw_error(name):
    """Таймаут обязан быть разобран, иначе он утечёт из исполнителя наверх.

    Цена разницы велика: разобранный таймаут — это провал ОДНОЙ цели, а
    необработанное исключение прерывает операцию целиком, уже после того как
    часть целей отработана.
    """
    src, tree = _tree()
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    body = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])
    assert ("except asyncio.TimeoutError" in body
            or "except Exception" in body
            or "except BaseException" in body), (
        f"{name}: таймаут некому поймать — он оборвёт всю операцию")
