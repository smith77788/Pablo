"""Регрессия: upsert_managed_channels() удаляет ВСЕ каналы аккаунта перед
вставкой переданного списка (DELETE по owner_id+acc_id, затем INSERT) — она
рассчитана на ПОЛНЫЙ ре-импорт. Несколько мест в проекте вызывали её с
ЧАСТИЧНЫМИ данными:

  - bot/handlers/channel_factory.py cb_chanf_do_create: создание ОДНОГО нового
    канала персистилось списком из одного элемента — каждое создание канала
    стирало весь ранее импортированный список каналов этого аккаунта.
  - bot/handlers/channel_factory.py / group_factory.py import_acc и
    services/op_worker.py _exec_channel_import_all / _exec_group_import_all:
    account_manager.get_dialogs(limit=200) отдаёт максимум 200 ДИАЛОГОВ (не
    200 каналов/групп) без пагинации по страницам — у аккаунта с >200
    диалогами часть каналов не попадает в срез и стирается.
  - services/op_worker.py _exec_scan_owned_resources: to_import = owned[:slots_remaining]
    осознанно обрезает список по остатку квоты подписки — при повторном
    скане с меньшей квотой (понижение тарифа/квота выбрана другими
    аккаунтами раньше в цикле) урезанный срез стирал уже сохранённые каналы.

Фикс: добавлена add_managed_channels() — точечный INSERT...ON CONFLICT DO
UPDATE без предварительного DELETE. Все перечисленные точки вызова
переключены на неё; upsert_managed_channels() оставлена только там, где вход
действительно является полным списком (принудительная пересборка "Загрузить
из Telegram" в channel_ops.py, где стейл-записи и должны быть удалены).

Примечание про стиль тестов ниже: поведенческие тесты db.add_managed_channels /
db.upsert_managed_channels запускают корутины через asyncio.run() напрямую
(а не через @pytest.mark.asyncio) — в этом окружении пакет pytest-asyncio не
установлен (известное, не относящееся к этой задаче ограничение окружения,
см. CLAUDE.md п.5), из-за чего @pytest.mark.asyncio тесты по всему репозиторию
сейчас молча не выполняются. asyncio.run() не зависит от плагина и реально
исполняет проверку в этом окружении.

Проверки самих точек вызова (что хендлер использует add_managed_channels, а
не upsert_managed_channels) сделаны через разбор исходного текста файла по
AST, а не через inspect.getsource(живой_объект) — функции, обёрнутые
`@router.callback_query(...)`, в tests/conftest.py подменяются стабом
`_Any`, на котором inspect.getsource падает с TypeError независимо от этой
задачи (тот же сбой воспроизводится и на уже существующем
tests/test_parser_commenters_wired.py::test_start_filter_includes_commenters).
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib

import pytest

from database import db

_TG_MANAGER_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _func_source(relpath: str, funcname: str) -> str:
    """Возвращает исходный текст функции `funcname` из файла `relpath`,

    разобранного через ast — работает даже если рантайм-импорт модуля
    подменяет декорированную функцию стабом (см. модуль-докстринг).
    """
    path = _TG_MANAGER_ROOT / relpath
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == funcname:
            segment = ast.get_source_segment(src, node)
            assert segment is not None
            return segment
    raise AssertionError(f"функция {funcname!r} не найдена в {relpath}")


def _without_comment_lines(src: str) -> str:
    """Убирает строки-комментарии (наши же пояснительные комментарии о фиксе

    упоминают "upsert_managed_channels(" текстом — без этого фильтра поиск
    вызова функции ложно матчился бы на собственный объясняющий комментарий).
    """
    return "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )


class _FakeConn:
    def __init__(self, log: list[tuple[str, tuple]]):
        self._log = log

    async def execute(self, query, *args):
        self._log.append((query, args))
        return "OK"

    async def executemany(self, query, rows):
        for row in rows:
            self._log.append((query, row))

    def transaction(self):
        return _NullCtx()


class _NullCtx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _AcquireCtx:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    """Отслеживает все execute/executemany — и через conn (acquire), и напрямую."""

    def __init__(self):
        self.log: list[tuple[str, tuple]] = []
        self._conn = _FakeConn(self.log)

    def acquire(self):
        return _AcquireCtx(self._conn)

    async def executemany(self, query, rows):
        for row in rows:
            self.log.append((query, row))

    async def execute(self, query, *args):
        self.log.append((query, args))
        return "OK"


def _channels(*ids: int) -> list[dict]:
    return [{"id": i, "title": f"Ch{i}", "type": "channel"} for i in ids]


# ── Регрессия: 0 участников у ВСЕХ импортированных каналов ──────────────────
#
# account_manager.get_dialogs() уже кладёт реальное число участников в каждый
# диалог (entity.participants_count → ключ "members"), но add_managed_channels()
# роняло его на пути в БД — колонка managed_channels.members_count никогда не
# получала значение при импорте/реклассификации, поэтому у мини-аппа ВСЕ
# каналы показывали «0 участников» независимо от реального размера.


def test_add_managed_channels_persists_member_count():
    pool = _FakePool()
    ch = [{"id": 1, "title": "Ch1", "type": "channel", "members": 4321}]

    async def _run():
        await db.add_managed_channels(pool, owner_id=1, acc_id=10, channels=ch)

    asyncio.run(_run())
    inserts = [(q, args) for q, args in pool.log if "INSERT INTO managed_channels" in q]
    assert len(inserts) == 1
    q, args = inserts[0]
    assert "members_count" in q
    assert 4321 in args, "реальное число участников не попало в параметры INSERT"


def test_add_managed_channels_missing_members_key_defaults_to_zero_not_crash():
    """Часть точек вызова (например _exec_channel_add) передаёт словари без
    ключа "members" вовсе — не должно падать с KeyError."""
    pool = _FakePool()
    ch = [{"id": 1, "title": "Ch1", "type": "channel"}]

    async def _run():
        await db.add_managed_channels(pool, owner_id=1, acc_id=10, channels=ch)

    asyncio.run(_run())  # не должно бросить исключение


def test_add_managed_channels_upsert_never_regresses_count_to_zero():
    """SQL обязан беречь уже известный счётчик от нуля/временного сбоя —
    иначе частичная страница диалогов откатывала бы счётчик назад."""
    pool = _FakePool()

    async def _run():
        await db.add_managed_channels(
            pool, owner_id=1, acc_id=10, channels=[{"id": 1, "title": "Ch1", "members": 0}])

    asyncio.run(_run())
    q = pool.log[0][0]
    assert "CASE WHEN EXCLUDED.members_count > 0" in q
    assert "managed_channels.members_count END" in q


def test_op_worker_channel_add_persists_member_count():
    src = _func_source("services/op_worker.py", "_exec_channel_add")
    assert "members_count" in src
    assert 'res.get("members")' in src


def test_op_worker_reclassify_channels_persists_member_counts():
    """Реклассификация («Только мои» / «Обновить данные», см. meta_map) уже
    проходит ВСЕ диалоги живыми сессиями ради роли — тот же проход обязан
    чинить и застрявшие нули счётчика для уже накопленной инфраструктуры,
    без отдельной операции «обновить» с нуля.

    Реализовано соседней сессией как часть общего обновления карточки канала
    (meta_map: title/username/access_hash/members) — а не отдельным узким
    members_map, как в первой версии этого фикса; тест сверяется с тем, что
    реально в коде, а не с конкретным именем переменной."""
    src = _func_source("services/op_worker.py", "_exec_reclassify_channels")
    assert "meta_map" in src
    assert "members_count" in src
    # Нулевое/отсутствующее значение не должно откатывать уже известный
    # счётчик назад — COALESCE($5, members_count) именно это и гарантирует.
    assert "members_count = COALESCE($5, members_count)" in src or \
        "members_count" in src[src.index("COALESCE"):src.index("COALESCE") + 400]


def test_upsert_managed_channels_still_deletes_first():
    """Документирует опасный контракт: DELETE по (owner_id, acc_id) ПЕРЕД INSERT.
    Если это когда-нибудь изменится молча — все вызовы add_managed_channels()
    вместо неё нужно будет пересмотреть заново."""
    pool = _FakePool()

    async def _run():
        await db.upsert_managed_channels(pool, owner_id=1, acc_id=10, channels=_channels(100))

    asyncio.run(_run())
    deletes = [q for q, _ in pool.log if q.strip().startswith("DELETE")]
    assert deletes, "upsert_managed_channels должна по-прежнему удалять существующие строки"


def test_add_managed_channels_never_deletes():
    """Точечная функция не должна выпускать ни одного DELETE — это и есть фикс."""
    pool = _FakePool()

    async def _run():
        await db.add_managed_channels(pool, owner_id=1, acc_id=10, channels=_channels(100))

    asyncio.run(_run())
    deletes = [q for q, _ in pool.log if q.strip().startswith("DELETE")]
    assert not deletes, "add_managed_channels не должна удалять существующие строки"
    inserts = [q for q, _ in pool.log if "INSERT INTO managed_channels" in q]
    assert len(inserts) == 1


def test_add_managed_channels_empty_list_is_noop():
    pool = _FakePool()

    async def _run():
        return await db.add_managed_channels(pool, owner_id=1, acc_id=10, channels=[])

    result = asyncio.run(_run())
    assert result == 0
    assert pool.log == []


def test_add_managed_channels_partial_call_does_not_wipe_existing_rows():
    """Сценарий бага: аккаунт уже имеет канал 1 (ранее импортирован полным
    упсертом), затем частичный довоз канала 2 через add_managed_channels не
    должен стирать канал 1 (в отличие от upsert_managed_channels, который
    выпустил бы DELETE по тому же ключу и потерял бы канал 1)."""
    pool = _FakePool()

    async def _run():
        # Полный первичный импорт — легитимный вызов upsert_managed_channels.
        await db.upsert_managed_channels(pool, owner_id=1, acc_id=10, channels=_channels(1))
        pool.log.clear()
        # Частичный довоз (например, только что созданный новый канал).
        await db.add_managed_channels(pool, owner_id=1, acc_id=10, channels=_channels(2))

    asyncio.run(_run())
    assert not any(q.strip().startswith("DELETE") for q, _ in pool.log), (
        "довоз одного канала не должен трогать (и тем более удалять) остальные "
        "ранее сохранённые каналы того же аккаунта"
    )


# ── Проверка, что реальные точки вызова переключены на безопасную функцию ──


def test_channel_factory_create_uses_point_wise_upsert():
    """cb_chanf_do_create персистит РОВНО ОДИН только что созданный канал —
    вызов upsert_managed_channels() здесь стирал бы весь остальной список."""
    src = _func_source("bot/handlers/channel_factory.py", "cb_chanf_do_create")
    assert "add_managed_channels(" in src
    assert "upsert_managed_channels(" not in _without_comment_lines(src)


def test_channel_factory_import_acc_uses_point_wise_upsert():
    """get_dialogs(limit=200) — частичный срез диалогов, не гарантированно
    полный список каналов аккаунта."""
    src = _func_source("bot/handlers/channel_factory.py", "cb_chanf_import_acc")
    assert "add_managed_channels(" in src
    assert "upsert_managed_channels(" not in _without_comment_lines(src)


def test_group_factory_import_acc_uses_point_wise_upsert():
    src = _func_source("bot/handlers/group_factory.py", "cb_group_import_acc")
    assert "add_managed_channels(" in src
    assert "upsert_managed_channels(" not in _without_comment_lines(src)


def test_op_worker_channel_import_all_uses_point_wise_upsert():
    src = _func_source("services/op_worker.py", "_exec_channel_import_all")
    assert "add_managed_channels(" in src
    assert "upsert_managed_channels(" not in _without_comment_lines(src)


def test_op_worker_group_import_all_uses_point_wise_upsert():
    src = _func_source("services/op_worker.py", "_exec_group_import_all")
    assert "add_managed_channels(" in src
    assert "upsert_managed_channels(" not in _without_comment_lines(src)


def test_op_worker_scan_owned_resources_uses_point_wise_upsert():
    """to_import = owned[:slots_remaining] — срез, обрезанный квотой подписки,
    не полный список ресурсов аккаунта."""
    src = _func_source("services/op_worker.py", "_exec_scan_owned_resources")
    assert "add_managed_channels(" in src
    assert "upsert_managed_channels(" not in _without_comment_lines(src)


def test_op_worker_channel_add_already_used_point_wise_insert():
    """Контроль соседнего кода: _exec_channel_add (вступление в один канал по
    ссылке) уже делал точечный INSERT...ON CONFLICT вручную (без вызова
    upsert_managed_channels/add_managed_channels) — не должен был превратиться
    обратно в полный upsert_managed_channels."""
    src = _func_source("services/op_worker.py", "_exec_channel_add")
    assert "upsert_managed_channels(" not in _without_comment_lines(src)
    assert "ON CONFLICT (owner_id, channel_id) DO UPDATE" in src


def test_channel_ops_live_reload_still_uses_full_resync_intentionally():
    """Контроль: 'Загрузить из Telegram' в channel_ops.py — намеренный полный
    ре-импорт (scan_owned_assets отдаёт актуальный набор admin/creator
    каналов аккаунта), стейл-записи ДОЛЖНЫ удаляться. Если это когда-нибудь
    поменяют на частичные данные — тест не даст молча потерять инвариант."""
    src = _func_source("bot/handlers/channel_ops.py", "cb_manage_show_dialogs_live")
    assert "upsert_managed_channels(" in src


# ── Реальный Postgres: CASE-логика UPSERT'а действительно так себя ведёт ────
#
# _FakePool выше только логирует SQL-текст, не исполняет его — CASE-выражение
# внутри INSERT...ON CONFLICT нужно проверить на настоящем движке.
#
# ОДИН цикл на модуль: соединения asyncpg привязаны к тому циклу, в котором
# созданы — отдельный asyncio.run() на фикстуру и на тело теста даёт
# "attached to a different loop" при первом же реальном запросе из теста.

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")

_PG_LOOP: "asyncio.AbstractEventLoop | None" = None


def _pg_run(coro):
    global _PG_LOOP
    if _PG_LOOP is None or _PG_LOOP.is_closed():
        _PG_LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_PG_LOOP)
    return _PG_LOOP.run_until_complete(coro)


@pytest.fixture
def pg_pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        for f in ("schema_v19.sql", "schema_v91.sql"):
            path = _TG_MANAGER_ROOT / f
            for chunk in path.read_text(encoding="utf-8").split(";"):
                body = "\n".join(
                    ln for ln in chunk.split("\n") if not ln.strip().startswith("--")
                ).strip()
                if body:
                    try:
                        await conn.execute(body)
                    except Exception:
                        pass
        await conn.execute("DELETE FROM managed_channels WHERE owner_id=990201")
        return conn

    try:
        conn = _pg_run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield conn
    _pg_run(conn.execute("DELETE FROM managed_channels WHERE owner_id=990201"))
    _pg_run(conn.close())


def test_real_postgres_member_count_persists_and_never_regresses(pg_pool):
    async def _run():
        await db.add_managed_channels(
            pg_pool, owner_id=990201, acc_id=1,
            channels=[{"id": 555, "title": "Ch", "members": 777}])
        row = await pg_pool.fetchrow(
            "SELECT members_count FROM managed_channels WHERE owner_id=990201 AND channel_id=555")
        assert row["members_count"] == 777

        # Повторный (частичный/сбойный) проход с members=0 не должен откатить счётчик.
        await db.add_managed_channels(
            pg_pool, owner_id=990201, acc_id=1,
            channels=[{"id": 555, "title": "Ch", "members": 0}])
        row = await pg_pool.fetchrow(
            "SELECT members_count FROM managed_channels WHERE owner_id=990201 AND channel_id=555")
        assert row["members_count"] == 777, "нулевое значение откатило уже известный счётчик"

        # А настоящий рост — обязан примениться.
        await db.add_managed_channels(
            pg_pool, owner_id=990201, acc_id=1,
            channels=[{"id": 555, "title": "Ch", "members": 900}])
        row = await pg_pool.fetchrow(
            "SELECT members_count FROM managed_channels WHERE owner_id=990201 AND channel_id=555")
        assert row["members_count"] == 900

    _pg_run(_run())
