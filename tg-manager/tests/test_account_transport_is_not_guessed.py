"""Транспорт аккаунта не должен зависеть от того, кто написал SELECT.

ПОЧЕМУ ЭТО ВАЖНО. `_make_client` выбирает выход (назначенный прокси → CF-релей →
IPv6 → прямой host-IP) ПО ПОЛЯМ переданного словаря аккаунта. В коде больше сотни
собственных выборок из tg_accounts, и почти ни одна не берёт cf_relay_url, а
часть не берёт и proxy_id. Такой словарь молча уводит аккаунт НАПРЯМУЮ с
host-IP, тогда как канонический путь (db.telethon_accounts_query) ведёт тот же
аккаунт через релей. Одна сессия с двух адресов — это AUTH_KEY_DUPLICATED:
Telegram отзывает ключ, аккаунт умирает, а снаружи это выглядит как «флот сам
отваливается».

Здесь проверяется страховка: карта транспорта добирает то, чего в словаре НЕТ,
и не трогает то, что вызывающий передал явно.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import account_manager as am  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_map():
    am._ACC_TRANSPORT.clear()
    yield
    am._ACC_TRANSPORT.clear()


class _Pool:
    def __init__(self, rows):
        self.rows = rows
        self.queries: list[str] = []

    async def fetch(self, q, *a):
        self.queries.append(q)
        return list(self.rows)


def test_missing_relay_is_filled_from_the_map():
    """Словарь без cf_relay_url обязан получить релей, а не уйти напрямую."""
    am.set_account_transport(7, cf_relay_url="https://relay.example/w",
                             proxy_id=None, proxy_url=None)
    d = {"id": 7, "session_str": "x"}          # выборка как в op_worker
    filled = am._fill_transport_fields(d)

    assert "cf_relay_url" in filled
    assert d["cf_relay_url"] == "https://relay.example/w"


def test_explicit_empty_value_wins_over_the_map():
    """Пустое значение в словаре — это ОТВЕТ выборки «релея нет», а не пробел.

    Подменять его кэшем нельзя: иначе снятый релей вернётся с того света и
    уведёт аккаунт не тем выходом.
    """
    am.set_account_transport(7, cf_relay_url="https://relay.example/w")
    d = {"id": 7, "cf_relay_url": None, "proxy_id": None}
    filled = am._fill_transport_fields(d)

    assert filled == []
    assert d["cf_relay_url"] is None


def test_unknown_account_changes_nothing():
    d = {"id": 999}
    assert am._fill_transport_fields(d) == []
    assert d == {"id": 999}


def test_dict_without_id_changes_nothing():
    """Без id аккаунт не опознать — молча подставлять чужой транспорт нельзя."""
    am.set_account_transport(7, cf_relay_url="https://relay.example/w")
    d = {"session_str": "x"}
    assert am._fill_transport_fields(d) == []
    assert "cf_relay_url" not in d


def test_bound_proxy_is_filled_too():
    """Аккаунт с назначенным прокси не должен идти с host-IP.

    proxy_id важен сам по себе: _resolve_client_proxy по нему понимает, что
    аккаунт привязан к IP прокси, и запрещает прямое подключение.
    """
    am.set_account_transport(5, cf_relay_url=None, proxy_id=42,
                             proxy_url="socks5://u:p@1.2.3.4:1080")
    d = {"id": 5}
    filled = am._fill_transport_fields(d)

    assert set(filled) >= {"proxy_id", "proxy_url"}
    assert d["proxy_id"] == 42


def test_prime_reads_only_accounts_with_a_non_default_exit():
    pool = _Pool([{"id": 1, "owner_id": 10, "cf_relay_url": "https://r/1",
                   "proxy_id": None, "proxy_url": None}])
    n = asyncio.run(am.prime_account_transport(pool, 10))

    assert n == 1
    q = " ".join(pool.queries[0].split())
    assert "cf_relay_url IS NOT NULL" in q and "proxy_id IS NOT NULL" in q, (
        "прайминг обязан отбирать аккаунты с недефолтным выходом")
    assert am._ACC_TRANSPORT[1]["cf_relay_url"] == "https://r/1"


def test_prime_drops_entries_the_database_no_longer_confirms():
    """Снятый релей обязан исчезнуть и из памяти.

    Иначе кэш сам становится источником рассинхрона: база говорит «напрямую»,
    память — «через релей», и это ровно та же авария, с другой стороны.
    """
    am.set_account_transport(1, owner_id=10, cf_relay_url="https://r/1")
    am.set_account_transport(2, owner_id=10, cf_relay_url="https://r/2")

    pool = _Pool([{"id": 1, "owner_id": 10, "cf_relay_url": "https://r/1",
                   "proxy_id": None, "proxy_url": None}])
    asyncio.run(am.prime_account_transport(pool, 10))

    assert 1 in am._ACC_TRANSPORT
    assert 2 not in am._ACC_TRANSPORT, "устаревшая запись пережила перечитывание"


def test_prime_for_one_owner_does_not_touch_another():
    """Чистка устаревшего ограничена перечитанной областью."""
    am.set_account_transport(1, owner_id=10, cf_relay_url="https://r/1")
    am.set_account_transport(9, owner_id=99, cf_relay_url="https://r/9")

    pool = _Pool([])
    asyncio.run(am.prime_account_transport(pool, 10))

    assert 1 not in am._ACC_TRANSPORT
    assert am._ACC_TRANSPORT[9]["cf_relay_url"] == "https://r/9", (
        "прайминг одного владельца стёр транспорт другого")


def test_prime_failure_does_not_look_like_an_empty_map():
    """Сбой чтения не должен превратиться в «ни у кого нет релея».

    Пустая карта неотличима от «добирать нечего» и снова уводит аккаунты
    напрямую — поэтому ошибка обязана быть видимой, а прежняя карта уцелеть.
    """
    class _Broken:
        async def fetch(self, q, *a):
            raise RuntimeError("db down")

    am.set_account_transport(1, owner_id=10, cf_relay_url="https://r/1")
    with pytest.raises(RuntimeError):
        asyncio.run(am.prime_account_transport(_Broken(), 10))

    assert am._ACC_TRANSPORT[1]["cf_relay_url"] == "https://r/1"


def _fn_src(path: str, name: str) -> str:
    src = open(path, encoding="utf-8").read()
    lines = src.split("\n")
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return "\n".join(lines[n.lineno - 1:n.end_lineno])
    raise AssertionError(f"{name} не найдена в {path}")


def test_make_client_fills_before_choosing_the_transport():
    """Добор обязан случиться ДО выбора прокси и релея, иначе он бесполезен."""
    body = _fn_src(os.path.join(ROOT, "services", "account_manager.py"),
                   "_make_client")
    i_fill = body.index("_fill_transport_fields")
    i_proxy = body.index("_resolve_client_proxy(")
    i_relay = body.index("acc_relay =")

    assert i_fill < i_proxy, "поля добираются после выбора прокси — поздно"
    assert i_fill < i_relay, "поля добираются после выбора релея — поздно"


def test_make_client_reads_the_enriched_dict_for_the_relay():
    """Релей читается из обогащённого словаря, а не из исходного.

    Взять здесь исходный device — значит пойти напрямую ровно в том случае,
    ради которого добор и делается.
    """
    body = _fn_src(os.path.join(ROOT, "services", "account_manager.py"),
                   "_make_client")
    line = [ln for ln in body.split("\n")
            if "acc_relay = str(" in ln]
    assert line, "строка выбора релея не найдена"
    assert 'd.get("cf_relay_url")' in line[0], (
        "релей берётся из device, а не из обогащённого d — добор ничего не даёт")


def test_operation_start_primes_the_map():
    """Массовые операции ходят по своим выборкам — карта нужна им в первую очередь."""
    src = open(os.path.join(ROOT, "services", "op_worker.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    called = any(
        isinstance(n, ast.Call)
        and ((isinstance(n.func, ast.Attribute) and n.func.attr == "prime_account_transport")
             or (isinstance(n.func, ast.Name) and n.func.id == "prime_account_transport"))
        for n in ast.walk(tree))
    assert called, (
        "op_worker не праймит карту транспорта — аккаунт с релеем уйдёт в "
        "операции напрямую, а одиночные вызовы через релей (AUTH_KEY_DUPLICATED)")


def test_web_role_also_keeps_the_map_fresh():
    """Под ROLE=web фоновые циклы не запускаются — карта обязана обновляться иначе.

    Мини-апп и бот живут именно в этой роли и ходят к аккаунтам по собственным
    выборкам. Если запустить обновление через гейтованный `_resilient`, карта в
    вебе останется пустой, и аккаунт с релеем уйдёт напрямую.
    """
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    starts: list[str] = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and n.args):
            continue
        f = n.func
        name = f.id if isinstance(f, ast.Name) else (
            f.attr if isinstance(f, ast.Attribute) else None)
        if name not in ("_resilient", "_web_resilient"):
            continue
        if any(isinstance(a, ast.Name) and a.id == "run_transport_refresh_loop"
               for a in n.args):
            starts.append(name)

    assert starts, "цикл обновления карты транспорта не запускается в main.py"
    assert "_web_resilient" in starts, (
        "цикл повешен на _resilient — он гейтуется ролью и под ROLE=web "
        "не стартует, оставляя карту пустой там, где живут мини-апп и бот")


def test_refresh_loop_survives_its_own_failure():
    """Недоступность БД не должна ронять процесс — карта лишь страховка.

    Но и молчать нельзя: рассинхрон транспорта снова стал бы невидимым.
    """
    body = _fn_src(os.path.join(ROOT, "services", "account_manager.py"),
                   "run_transport_refresh_loop")
    assert "while True" in body, "обновление разовое — карта устареет"
    assert "except Exception" in body, "сбой чтения уронит процесс"
    assert "log_exc_swallow" in body or "log." in body, "сбой обновления не виден в логах"
    assert "CancelledError" in body, "цикл не отпустит остановку процесса"
