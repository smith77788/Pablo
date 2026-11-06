"""Второй тап не превращается во вторую публикацию от всех аккаунтов.

ЧТО БЫЛО. Окно идемпотентности в `operation_bus.submit` сравнивает params
целиком: одинаковые params внутри окна — это один и тот же сабмит, разные —
две разные операции. Два места в продукте кладут в params указатель на
сообщение владельца с прогресс-баром (`chat_id` + `message_id`), и сообщение
это создаётся ДО постановки — то есть у каждого тапа оно СВОЁ. Значит params
двух тапов не совпадали никогда, дедуп не срабатывал ни разу, и обе операции
уезжали в работу: `bulk_post_to_channel` публиковал текст в канал от всех
аккаунтов ДВАЖДЫ, `bulk_update_profile` дважды переписывал профили.

Снаружи это выглядело как работающая защита: дедуп есть, тест на двойной тап
есть, окно настроено. Не работало оно именно там, где цена двойного пуска
самая высокая — массовая операция от всех аккаунтов сразу.

ЧТО ЗАКРЕПЛЕНО. Указатель на прогресс-бар не входит в личность операции:
ключи `_progress_chat_id` / `_progress_message_id` исключены из дедуп-идентичности
(и из отпечатка чувствительных params), поэтому два тапа, отличающиеся только
этим указателем, дают ОДНУ операцию. Любое настоящее отличие params (другой
текст, другой канал, другие аккаунты) по-прежнему даёт две операции — иначе
владелец не смог бы отправить два разных поста подряд.

Нужен живой Postgres: проверяется настоящее сравнение jsonb в запросе дедупа,
а не его пересказ подставным пулом.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993617

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
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    yield


def _params(progress_msg_id: int, text: str = "Привет, канал") -> dict:
    return {
        "account_ids": [11, 22, 33],
        "channel_ref": "@infragram_probe",
        "text_to_post": text,
        "bulk_access_hash": 0,
        "_progress_chat_id": 500,
        "_progress_message_id": progress_msg_id,
    }


def _submit(pool, params: dict) -> int:
    from services import operation_bus

    return _run(operation_bus.submit(
        pool, OWNER, "bulk_post_to_channel", params,
        total_items=3, bypass_plan_check=True))


def test_two_taps_with_their_own_progress_bars_are_one_operation(pool):
    first = _submit(pool, _params(1001))
    second = _submit(pool, _params(1002))     # второй тап: своё сообщение
    assert second == first, (
        "второй тап поставил ВТОРУЮ операцию — публикация уйдёт в канал дважды "
        "от всех аккаунтов"
    )
    rows = _run(pool.fetchval(
        "SELECT count(*) FROM operation_queue WHERE owner_id=$1 AND op_type=$2",
        OWNER, "bulk_post_to_channel"))
    assert int(rows) == 1, f"в очереди {rows} операций вместо одной"


def test_a_different_post_is_still_a_different_operation(pool):
    first = _submit(pool, _params(1001, "Первый пост"))
    second = _submit(pool, _params(1002, "Второй пост"))
    assert second != first, (
        "два РАЗНЫХ поста слиплись в одну операцию — владелец не сможет "
        "отправить второй текст сразу после первого"
    )


def test_the_pointer_is_still_stored_for_the_worker(pool):
    op_id = _submit(pool, _params(1001))
    row = _run(pool.fetchrow(
        "SELECT params->>'_progress_message_id' AS m, params->>'_progress_chat_id' AS c "
        "FROM operation_queue WHERE id=$1", op_id))
    assert (row["c"], row["m"]) == ("500", "1001"), (
        "указатель на прогресс-бар не сохранён в params — исполнителю нечего "
        "будет редактировать"
    )
