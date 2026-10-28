"""Служебная строка журнала — это шаг работы, а не цель (живой Postgres).

Исполнитель инвайта пишет в `operation_log` не только людей, но и ШАГИ: выдачу
админки ('promote'), промоут-трюк ('promote_trick'), фолбэк ссылкой в ЛС
('link_fallback'). Шесть читателей журнала считали их целями, и это стоило
продукту трёх вещей сразу:

* прогресс обгонял аудиторию — «16 из 15» при пятнадцати приглашённых;
* размер повтора считается как «всего минус закрытые», то есть выходил МЕНЬШЕ
  остатка: до трёх настоящих целей не пробовались повторно никогда;
* `collect_failed_targets` возвращал «promote_trick» как цель, и повтор шёл
  приглашать пользователя с таким именем.

Запросы идут в базу, поэтому проверка живая: заглушка пула вернула бы, что ей
скажут, и ничего бы не доказала. Без INFRAGRAM_TEST_DSN — skip (как поднять
Postgres — в docstring tests/test_invite_e2e_postgres.py).
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark_db = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 997701
_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture()
def pool():
    """Пул на тест: модульный пул переживал смену цикла событий и падал на
    «Future attached to a different loop» — связь дешевле, чем это лечить."""
    import asyncpg

    async def _boot():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        await p.execute(
            """
            CREATE TABLE IF NOT EXISTS operation_queue (
                id BIGSERIAL PRIMARY KEY, owner_id BIGINT, op_type TEXT,
                status TEXT, params JSONB, total_items INT DEFAULT 0,
                done_items INT DEFAULT 0, label TEXT,
                created_at TIMESTAMPTZ DEFAULT now());
            CREATE TABLE IF NOT EXISTS operation_log (
                id BIGSERIAL PRIMARY KEY, op_id BIGINT, step_num INT,
                target TEXT, status TEXT, message TEXT,
                created_at TIMESTAMPTZ DEFAULT now());
            """)
        return p

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


@pytest.fixture()
def op_with_journal(pool):
    """Операция, у которой закрыто 3 настоящие цели, 1 упала и 3 служебных шага."""
    async def _seed():
        await pool.execute("DELETE FROM operation_log WHERE op_id IN "
                           "(SELECT id FROM operation_queue WHERE owner_id=$1)", OWNER)
        await pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items) "
            "VALUES($1,'mass_invite','done','{}'::jsonb,4) RETURNING id", OWNER)
        rows = [
            (1, "500001", "ok", "joined"),
            (2, "500002", "ok", "joined"),
            (3, "500003", "ok", "joined"),
            (4, "500004", "error", "privacy"),
            # служебные — это шаги, не люди
            (0, "promote", "ok", "выдана админка"),
            (0, "promote_trick", "ok", "добавлено промоут-трюком: 3/3"),
            (0, "link_fallback", "ok", "ссылка отправлена: 1"),
        ]
        for step, target, status, msg in rows:
            await pool.execute(
                "INSERT INTO operation_log(op_id,step_num,target,status,message) "
                "VALUES($1,$2,$3,$4,$5)", op_id, step, target, status, msg)
        return op_id
    return _run(_seed())


@pytestmark_db
def test_closed_targets_counts_people_not_steps(pool, op_with_journal):
    """Закрыто три цели, а не шесть: иначе повтор получит размер меньше остатка."""
    from services import operation_bus as ob
    n = _run(ob.closed_targets_count(pool, op_with_journal))
    assert n == 3, (
        f"закрытыми считаются {n} целей вместо 3 — служебные строки журнала "
        "('promote', 'promote_trick', 'link_fallback') попали в счёт")


@pytestmark_db
def test_retry_size_is_the_real_remainder(pool, op_with_journal):
    """Остаток — одна упавшая цель. Со служебными строками выходил ноль."""
    from services import operation_bus as ob
    left = _run(ob._retry_total_items(pool, op_with_journal, 4))
    assert left == 1, (
        f"повтор получает размер {left} вместо 1: при счёте служебных строк "
        "остаток обнуляется и настоящая цель не пробуется повторно никогда")


@pytestmark_db
def test_failed_targets_never_include_a_step_name(pool, op_with_journal):
    """Повтор не должен идти приглашать «promote_trick»."""
    from services import operation_bus as ob
    failed = _run(ob.collect_failed_targets(pool, op_with_journal, "mass_invite"))
    as_text = {str(x) for x in failed}
    assert not (as_text & set(ob.SERVICE_TARGETS)), (
        f"в цели повтора попало служебное имя: {sorted(as_text)}")


@pytestmark_db
def test_progress_never_overtakes_the_audience(pool, op_with_journal):
    """Покрытие журнала = 4 (3 успеха + 1 ошибка), а не 7.

    Именно это число доводит `done_items` на финише операции
    (`done_items=GREATEST(done_items, $5)`), поэтому полоса и показывала
    «16 из 15».
    """
    from services import op_worker
    n = _run(op_worker._own_journal_coverage(pool, op_with_journal))
    assert n == 4, (
        f"покрытие журнала {n} вместо 4 — прогресс обгонит аудиторию")


# ── храповик: новый читатель журнала не должен быть слепым ───────────────────

_ROOT = pathlib.Path(__file__).resolve().parent.parent
# Читатели, которым служебные строки не мешают по своей природе:
#   * `completed_steps` считает step_num, а не цели;
#   * сам insert служебных строк в op_worker — это запись, а не чтение.
_ALLOW = {
    # (файл, фрагмент запроса) — объяснённые исключения
    ("op_worker.py", "AND message='joined'"),  # фильтрует служебные иначе — списком SERVICE_TARGETS
}


def _queries_reading_targets() -> list[tuple[str, str]]:
    """Все SQL-запросы кода, которые читают колонку target из operation_log."""
    out = []
    for name in ("operation_bus.py", "op_worker.py"):
        txt = (_ROOT / "services" / name).read_text(encoding="utf-8")
        for m in re.finditer(r'"SELECT[^;]{0,600}?FROM operation_log', txt):
            # дочитываем до закрывающей запятой вызова (конец склейки строк)
            tail = txt[m.start():m.start() + 900]
            end = tail.find("\n        )")
            chunk = tail[:end if end > 0 else 900]
            if "target" not in chunk.split("FROM operation_log")[0]:
                continue  # не выбирает target
            out.append((name, " ".join(chunk.split())))
    return out


def test_measurer_sees_the_readers_it_is_supposed_to_check():
    """Самопроверка: пробник обязан найти известных читателей журнала целей."""
    qs = _queries_reading_targets()
    assert len(qs) >= 5, (
        f"пробник нашёл всего {len(qs)} запросов — он сломан, а не код "
        "(читателей журнала целей в двух модулях заведомо больше)")


def test_every_target_reader_excludes_service_rows():
    """Новый запрос по целям обязан отсекать служебные строки."""
    blind = []
    for name, q in _queries_reading_targets():
        if "REAL_TARGET_SQL" in q:
            continue
        if any(name == a and frag in q for a, frag in _ALLOW):
            continue
        blind.append((name, q[:120]))
    assert not blind, (
        "запрос по целям журнала не отсекает служебные строки "
        "(добавьте REAL_TARGET_SQL из services/operation_bus.py): " + str(blind))


@pytestmark_db
def test_the_check_really_measures_the_filter(pool, op_with_journal, monkeypatch):
    """Обратный контроль: снимаем отсечку — и счёт снова врёт.

    Так выглядел продукт до правки: шесть «закрытых целей» при четырёх людях.
    Без этого теста предыдущие проверки могли бы остаться зелёными по любой
    другой причине.
    """
    from services import operation_bus as ob
    monkeypatch.setattr(ob, "REAL_TARGET_SQL", " ")
    n = _run(ob.closed_targets_count(pool, op_with_journal))
    assert n == 6, (
        f"без отсечки ожидались 6 «закрытых целей» (3 человека + 3 служебных "
        f"шага), получено {n} — значит проверка меряет не отсечку")
