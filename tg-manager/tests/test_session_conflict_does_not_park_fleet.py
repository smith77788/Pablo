"""Разовый конфликт сессии не должен парковать весь флот.

ЧТО БЫЛО. Проверка здоровья при AUTH_KEY_DUPLICATED сразу ставила аккаунту
`acc_status='cooldown'`. Комментарий в самом коде честно говорит: «разовый
флап/мультидевайс не должен выключать флот» — но статус выставлялся с ПЕРВОГО
раза. В логах пользователя один проход монитора пометил cooldown ВСЕ 39
аккаунтов свежего, ни разу не использованного флота:

    check_account_status_full: AUTH_KEY_DUPLICATED (конфликт двух IP) — acc=45
    session_health: acc=45 … status active→cooldown
    … и так 39 раз подряд, checked=39 failed=0

Цена: `acc_status='cooldown'` исключает аккаунт из прогрева (account_warmer
берёт только 'active'), из иммунитета (immunity_engine) и портит оценку
здоровья. Снаружи — «флот свежий, не использовался, и не работает».

ТЕПЕРЬ первый конфликт только запоминается (tg_accounts.session_conflict_at,
schema_v185), статус не меняется. Cooldown — только если конфликт повторился на
следующем проходе, то есть он устойчивый. Успешная проверка снимает отметку.

Отдельно проверяется, что монитор выбирает те же поля транспорта, что и
остальные пути: без `cf_relay_url` он ходил бы НАПРЯМУЮ, пока операции того же
аккаунта идут через релей, — и проверка здоровья сама создавала бы конфликт,
который ищет.

Нужен живой Postgres (INFRAGRAM_TEST_DSN).
"""
from __future__ import annotations

import asyncio
import ast
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 880011

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        setup = await asyncpg.connect(DSN)
        try:
            await setup.execute("CREATE SCHEMA IF NOT EXISTS conflicttest")
        finally:
            await setup.close()
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4,
                                      server_settings={"search_path": "conflicttest"})
        await p.execute("""CREATE TABLE IF NOT EXISTS tg_accounts (
                               id SERIAL PRIMARY KEY,
                               owner_id BIGINT NOT NULL,
                               phone TEXT,
                               first_name TEXT,
                               username TEXT,
                               last_real_check_at TIMESTAMPTZ,
                               session_str TEXT,
                               is_active BOOLEAN DEFAULT TRUE,
                               in_operation BOOLEAN DEFAULT FALSE,
                               acc_status TEXT,
                               status_reason TEXT,
                               device_model TEXT, system_version TEXT,
                               app_version TEXT, lang_code TEXT,
                               system_lang_code TEXT, cf_relay_url TEXT,
                               cooldown_until TIMESTAMPTZ,
                               proxy_id INTEGER)""")
        await p.execute("""CREATE TABLE IF NOT EXISTS user_proxies (
                               id SERIAL PRIMARY KEY, proxy_url TEXT,
                               geo_country TEXT, is_active BOOLEAN DEFAULT TRUE)""")
        await p.execute("""CREATE TABLE IF NOT EXISTS account_status_events (
                               id SERIAL PRIMARY KEY, acc_id INTEGER,
                               new_status TEXT, reason TEXT, source TEXT,
                               context JSONB, processed_at TIMESTAMPTZ)""")
        # Ровно та миграция, что уезжает в прод.
        with open(os.path.join(ROOT, "schema_v185.sql"), encoding="utf-8") as f:
            for chunk in f.read().split(";"):
                body = "\n".join(ln for ln in chunk.split("\n")
                                 if not ln.strip().startswith("--")).strip()
                if body:
                    await p.execute(body)
        return p

    p = _run(_mk())
    yield p
    for t in ("account_status_events", "user_proxies", "tg_accounts"):
        _run(p.execute(f"DROP TABLE IF EXISTS {t} CASCADE"))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM tg_accounts"))
    _run(pool.execute("DELETE FROM account_status_events"))
    yield


def _add(pool, n=1):
    ids = []
    for i in range(n):
        ids.append(_run(pool.fetchval(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, acc_status) "
            "VALUES($1,$2,'sess','active') RETURNING id", OWNER, f"+7900{i:05d}")))
    return ids


def _check_all(pool, result, monkeypatch):
    """Прогнать монитор, подсунув заданный ответ проверки аккаунта."""
    from services import account_manager as am

    async def _fake(_session, _acc=None, check_spambot=False):
        return dict(result)

    monkeypatch.setattr(am, "check_account_status_full", _fake)
    _run(am._check_all_sessions(pool))


_CONFLICT = {
    "status": "cooldown",
    "reason": "Сессия используется с другого IP (AUTH_KEY_DUPLICATED)",
    "display_name": "",
    "session_conflict": True,
}
_OK = {"status": "active", "reason": "", "display_name": ""}


def _statuses(pool):
    return [r["acc_status"] for r in _run(pool.fetch(
        "SELECT acc_status FROM tg_accounts ORDER BY id"))]


# ── ядро ─────────────────────────────────────────────────────────────────────

def test_first_conflict_does_not_touch_the_status(pool, monkeypatch):
    """Главное: один проход монитора не паркует флот."""
    _add(pool, 5)
    _check_all(pool, _CONFLICT, monkeypatch)

    assert _statuses(pool) == ["active"] * 5, (
        "весь флот ушёл в cooldown с первого же конфликта — именно так свежие "
        "39 аккаунтов и выпали из прогрева")
    marked = _run(pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts WHERE session_conflict_at IS NOT NULL"))
    assert marked == 5, "конфликт не запомнен — повтор будет считаться первым"


def test_repeated_conflict_does_park_the_account(pool, monkeypatch):
    """Устойчивый конфликт обязан приводить к cooldown: молчать тоже нельзя."""
    _add(pool, 3)
    _check_all(pool, _CONFLICT, monkeypatch)      # первый проход
    _check_all(pool, _CONFLICT, monkeypatch)      # второй — конфликт устойчив

    assert _statuses(pool) == ["cooldown"] * 3


def test_a_clean_check_clears_the_mark(pool, monkeypatch):
    """Разовый флап не должен копиться: чистая проверка обнуляет счёт."""
    _add(pool, 2)
    _check_all(pool, _CONFLICT, monkeypatch)
    _check_all(pool, _OK, monkeypatch)
    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts WHERE session_conflict_at IS NOT NULL")) == 0

    # и следующий конфликт снова считается первым — статус не трогаем
    _check_all(pool, _CONFLICT, monkeypatch)
    assert _statuses(pool) == ["active"] * 2


def test_other_statuses_are_applied_immediately(pool, monkeypatch):
    """Отсрочка касается ТОЛЬКО конфликта сессии, а не всех проверок подряд."""
    _add(pool, 2)
    _check_all(pool, {"status": "spamblock", "reason": "спамблок",
                      "display_name": ""}, monkeypatch)
    assert _statuses(pool) == ["spamblock"] * 2


def test_reason_tells_the_user_what_to_do(pool):
    """Причина обязана объяснять, где искать вторую сессию и что нажать."""
    src = open(os.path.join(ROOT, "services", "account_manager.py"),
               encoding="utf-8").read()
    i = src.index("AUTH_KEY_DUPLICATED). Аккаунт не ")
    reason = src[i - 400:i + 900]
    for hint in ("Устройства", "Завершить", "перезалить"):
        assert hint in reason, (
            f"в объяснении конфликта нет «{hint}» — пользователю непонятно, "
            "что именно делать")


def test_self_heal_does_not_flip_flop_a_persistent_conflict(pool):
    """Самолечение не должно снимать кулдаун, поставленный за УСТОЙЧИВЫЙ конфликт.

    Иначе статус перебрасывает туда-сюда: проверка ставит cooldown, самолечение
    через минуты снимает — и оператор так и не узнаёт, что сессию надо
    перезалить.
    """
    from services import account_monitor as am

    ids = _add(pool, 2)
    _run(pool.execute(
        "UPDATE tg_accounts SET acc_status='cooldown', session_conflict_at=NOW() "
        "WHERE id=$1", ids[0]))
    _run(pool.execute(
        "UPDATE tg_accounts SET acc_status='cooldown' WHERE id=$1", ids[1]))

    _run(am._heal_expired_cooldowns(pool))

    left = dict(_run(pool.fetchrow(
        "SELECT acc_status FROM tg_accounts WHERE id=$1", ids[0])))
    other = dict(_run(pool.fetchrow(
        "SELECT acc_status FROM tg_accounts WHERE id=$1", ids[1])))
    assert left["acc_status"] == "cooldown", (
        "кулдаун за устойчивый конфликт снят самолечением — статус будет "
        "перебрасывать, и причина останется невидимой")
    assert other["acc_status"] == "active", (
        "обычный истёкший кулдаун обязан сниматься как раньше")



def _calls_canonical_query(rel: str, func: str) -> bool:
    """Вызывает ли функция общий построитель запроса — ПО AST, а не по тексту.

    Подстрокой проверять нельзя: имя `telethon_accounts_query` встречается и в
    комментарии рядом, и такая проверка остаётся зелёной, даже когда запрос
    снова написан вручную. Один раз уже попалась.
    """
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            target = node
    assert target is not None, f"{func} не найдена в {rel}"

    # Имена, под которыми построитель мог быть импортирован (в т.ч. как _tq).
    aliases = {"telethon_accounts_query"}
    for node in ast.walk(target):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "telethon_accounts_query":
                    aliases.add(a.asname or a.name)
    for node in ast.walk(target):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else None)
            if name in aliases:
                return True
    return False


# ── храповик на транспорт ────────────────────────────────────────────────────

def test_monitor_selects_the_same_transport_fields_as_everyone_else():
    """Без cf_relay_url монитор сам создаёт конфликт, который ищет.

    Транспорт выбирается по полям словаря аккаунта. Если проверка здоровья не
    выбрала релей, она пойдёт напрямую с host-IP, тогда как операции того же
    аккаунта идут через релей — одна сессия с двух адресов.
    """
    src = open(os.path.join(ROOT, "services", "account_manager.py"),
               encoding="utf-8").read()
    body = None
    lines = src.split("\n")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "_check_all_sessions":
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
    assert body, "_check_all_sessions не найдена"
    assert _calls_canonical_query("services/account_manager.py", "_check_all_sessions"), (
        "монитор снова пишет свой SELECT — так и потерялся cf_relay_url. "
        "Поля транспорта берутся из database.db.telethon_accounts_query()")

    # И сам общий список обязан содержать всё, по чему выбирается транспорт.
    from database.db import TELETHON_ACC_COLS
    for col in ("cf_relay_url", "proxy_id", "proxy_url", "owner_id"):
        assert col in TELETHON_ACC_COLS, (
            f"в общем списке полей нет {col} — транспорт разъедется у ВСЕХ, "
            "кто им пользуется")


def test_the_frequent_monitor_uses_the_same_source():
    """`_check_dead_sessions` бегает куда чаще шестичасовой проверки.

    Его выборка тоже не содержала ни cf_relay_url, ни proxy_url — а он
    коннектит живые сессии каждые несколько минут.
    """
    assert _calls_canonical_query("services/account_monitor.py", "_check_dead_sessions"), (
        "частая проверка сессий снова строит клиента по своей неполной выборке")


def test_incomplete_account_dict_is_reported_loudly(caplog):
    """Неполный словарь аккаунта обязан быть ЗАМЕТЕН, а не молча менять транспорт.

    Отличаем «поля нет в выборке» (ошибка вызывающего) от «поле пустое»
    (релей просто не назначен) — второе законно и ругаться на него нельзя.
    """
    import logging
    pytest.importorskip("telethon", reason="сборка клиента тянет telethon")
    from services import account_manager as am

    with caplog.at_level(logging.ERROR, logger="services.account_manager"):
        am._make_client("", {"id": 1, "owner_id": 2})          # полей транспорта нет
    assert any("транспорт" in r.message or "cf_relay_url" in r.getMessage()
               for r in caplog.records), "пропуск полей никак не отмечен"

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="services.account_manager"):
        am._make_client("", {"id": 1, "owner_id": 2,
                             "cf_relay_url": "", "proxy_id": None})
    assert not [r for r in caplog.records if "транспорт" in r.getMessage()], (
        "пустые поля — это законное «релей не назначен», ругаться нельзя")
