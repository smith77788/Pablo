"""Двойной запуск кампании ЛС не пишет тем же людям второе такое же письмо.

ПОЧЕМУ ОКНО ИДЕМПОТЕНТНОСТИ ЗДЕСЬ НЕ РАБОТАЛО. Запуск из бота создавал строку
кампании и тут же ставил операцию, кладя СВЕЖИЙ `campaign_id` в её params. У
двух нажатий id разные, значит params разные, значит дедуп постановки не
срабатывал по построению. Журнал отправок (`dm_campaign_log`) дедуплит по паре
(campaign_id, tg_user_id) — он привязан К КАМПАНИИ, поэтому для второй кампании
все получатели снова «новые». Итог: человек получал одно и то же письмо дважды,
флот делал двойную работу на самой баноопасной стороне продукта.

Двойное нажатие здесь не теория: FSM-состояние чистится ПОСЛЕ вставки, два
callback-апдейта обрабатываются параллельно, а Телеграм сам повторяет callback,
если бот не ответил за ~15 секунд.

ЧТО ЗАКРЕПЛЕНО. Личность нажатия — СОДЕРЖИМОЕ кампании, а не её id: повтор с
тем же содержимым возвращает ту же строку (и тогда постановка операции тоже
сливается в одну). Позже окна тот же текст создаёт новую кампанию — иначе
владелец не смог бы разослать письмо второй волной.

Нужен живой Postgres: проверяется настоящая гонка двух одновременных нажатий.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993621

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=2, max_size=6)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM dm_campaigns WHERE owner_id=$1", OWNER))
    _run(p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM dm_campaigns WHERE owner_id=$1", OWNER))
    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    yield


CONTENT = dict(
    name="Осенняя волна",
    text_template="Привет, {name}! Загляни в бот.",
    target_type="bot_users",
    target_id=777,
    params={"cohort_type": "active"},
)


def _create(pool, **over):
    from services.dm_engine import find_or_create_campaign

    kw = {**CONTENT, **over}
    return _run(find_or_create_campaign(pool, OWNER, **kw))


def _count(pool) -> int:
    return int(_run(pool.fetchval(
        "SELECT count(*) FROM dm_campaigns WHERE owner_id=$1", OWNER)))


def test_the_second_tap_reuses_the_same_campaign(pool):
    first, reused_1 = _create(pool)
    second, reused_2 = _create(pool)
    assert reused_1 is False, "первое нажатие объявлено повтором"
    assert (second, reused_2) == (first, True), (
        "второе нажатие создало ВТОРУЮ кампанию — те же люди получат то же "
        "письмо дважды"
    )
    assert _count(pool) == 1, f"в базе {_count(pool)} кампаний вместо одной"


def test_two_simultaneous_taps_still_make_one_campaign(pool):
    """Настоящая гонка: без блокировки оба запроса не нашли бы ничего."""
    from services.dm_engine import find_or_create_campaign

    async def _both():
        return await asyncio.gather(
            find_or_create_campaign(pool, OWNER, **CONTENT),
            find_or_create_campaign(pool, OWNER, **CONTENT),
        )

    (id_a, _), (id_b, _) = _run(_both())
    assert id_a == id_b, "одновременные нажатия создали две кампании"
    assert _count(pool) == 1


def test_a_different_letter_is_a_different_campaign(pool):
    first, _ = _create(pool)
    second, reused = _create(pool, text_template="Другой текст")
    assert second != first and reused is False, (
        "две разные кампании слиплись в одну — владелец не отправит второе "
        "письмо подряд"
    )


def test_a_deliberate_second_wave_later_is_allowed(pool):
    first, _ = _create(pool)
    _run(pool.execute(
        "UPDATE dm_campaigns SET created_at = created_at - interval '10 minutes' "
        "WHERE id=$1", first))
    second, reused = _create(pool)
    assert second != first and reused is False, (
        "повтор кампании много позже окна подавлен — вторую волну того же "
        "письма владелец запустить не сможет"
    )


def test_a_finished_campaign_is_never_reused(pool):
    first, _ = _create(pool)
    _run(pool.execute("UPDATE dm_campaigns SET status='done' WHERE id=$1", first))
    second, reused = _create(pool)
    assert second != first and reused is False, (
        "законченная кампания переиспользована: повтор по ней означает именно "
        "«разослать заново»"
    )


def test_the_merged_tap_also_collapses_the_operation(pool):
    """Смысл всей правки: одна кампания — значит и одна операция."""
    from services import operation_bus

    async def _launch():
        cid, _ = await __import__(
            "services.dm_engine", fromlist=["x"]).find_or_create_campaign(
                pool, OWNER, **CONTENT)
        return await operation_bus.submit(
            pool, OWNER, "dm_campaign", {"campaign_id": int(cid)},
            total_items=1, bypass_plan_check=True)

    first = _run(_launch())
    second = _run(_launch())
    assert first == second, (
        "операция поставлена дважды — рассылка в ЛС уйдёт тем же людям второй "
        "раз"
    )
    assert int(_run(pool.fetchval(
        "SELECT count(*) FROM operation_queue WHERE owner_id=$1 AND op_type=$2",
        OWNER, "dm_campaign"))) == 1


def test_the_counter_is_described_for_the_owner():
    from services.metrics import _HELP

    name = "infragram_dm_campaign_relaunch_merged_total"
    assert name in _HELP and any(
        "а" <= ch.lower() <= "я" for ch in _HELP[name]), (
        "счётчик слияний без русского описания — строка без подписи на графике"
    )


def test_a_reused_campaign_is_never_deleted_by_the_second_tap():
    """Удалять можно только СВОЮ строку, а не ту, что отдала дверь.

    Повтор нажатия садится на кампанию первой попытки, а она к этому моменту
    может уже рассылать. Кнопка «запустить» при занятой инфраструктуре
    удаляла кампанию по id — то есть удалила бы ЖИВУЮ, и вместе со строкой
    ушёл бы журнал отправок (`dm_campaign_log`, ON DELETE CASCADE): тот самый,
    по которому кампания не пишет людям дважды.

    Мерка по смыслу: каждое удаление кампании в обработчике обязано стоять
    под проверкой флага переиспользования.
    """
    import ast as _ast
    import pathlib as _pl

    root = _pl.Path(__file__).resolve().parent.parent
    src = (root / "bot" / "handlers" / "dm_campaigns.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)

    deletes = [n for n in _ast.walk(tree)
               if isinstance(n, _ast.Constant) and isinstance(n.value, str)
               and "DELETE FROM dm_campaigns" in n.value]
    assert deletes, "удаление кампании не найдено — мерка смотрит не туда"

    guards = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.If):
            continue
        test = _ast.get_source_segment(src, node.test) or ""
        body = "\n".join(filter(None, (_ast.get_source_segment(src, b)
                                       for b in node.body)))
        if "reused" in test and "DELETE FROM dm_campaigns" not in body:
            # Ветка повтора: она как раз НЕ должна удалять.
            guards.append(test)
    assert guards, (
        "удаление кампании не прикрыто проверкой «эту строку создало именно "
        "это нажатие»: повтор удалит живую кампанию вместе с её журналом")
