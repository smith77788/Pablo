"""Окно идемпотентности длиннее, чем клиент ждёт ответа.

ЧЕМ БЫЛО ПЛОХО 20 СЕКУНД. Окно существует ради двух случаев: двойной тап и
ПОВТОР КЛИЕНТА ПОСЛЕ ТАЙМАУТА. Второй случай оно поймать не могло по
построению: мини-апп ждёт ответа 30 секунд (FETCH_TIMEOUT_MS), затем показывает
«сервер не отвечает», и владелец жмёт снова — его второй запрос приходит позже
тридцатой секунды, то есть за окном. При этом первый запрос сервер уже
обработал и операцию поставил: рассылка уходит подписчикам дважды.

ЧТО СДЕЛАНО. У операций, чей дубль нельзя отменить (сообщение ушло, инвайт
отправлен, пост опубликован), своё окно — 90 секунд: 30 секунд ожидания
клиента плюс минута на то, чтобы владелец увидел ошибку и нажал заново.

ПОЧЕМУ НЕ ВСЕМ. Длинное окно подавляет и НАМЕРЕННЫЙ повтор: второй сабмит
вернёт id первого, и второе действие молча не выполнится. Для рассылки по той
же аудитории тем же текстом это и нужно; для операции, где повтор — отдельное
полезное действие, это была бы потеря действия. Поэтому точечно.

Нужен живой Postgres: проверяется настоящее окно в запросе дедупа. Время не
выжидаем — сдвигаем created_at поставленной операции назад.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993620

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


def _submit(pool, op_type: str, params: dict, **kw) -> int:
    from services import operation_bus

    return _run(operation_bus.submit(
        pool, OWNER, op_type, params, total_items=1,
        bypass_plan_check=True, **kw))


def _age(pool, op_id: int, seconds: int) -> None:
    _run(pool.execute(
        "UPDATE operation_queue SET created_at = created_at "
        "- make_interval(secs => $2) WHERE id=$1", op_id, float(seconds)))


def test_a_retap_after_the_client_gave_up_is_still_one_broadcast(pool):
    params = {"bot_id": 81, "text": "Один залп"}
    first = _submit(pool, "run_broadcast", dict(params))
    _age(pool, first, 45)         # клиент сдался на 30-й секунде, владелец нажал снова
    assert _submit(pool, "run_broadcast", dict(params)) == first, (
        "повтор после таймаута клиента поставил вторую рассылку — подписчики "
        "получат сообщение дважды"
    )


def test_an_ordinary_operation_keeps_the_short_window(pool):
    params = {"account_ids": [1, 2]}
    first = _submit(pool, "check_accounts_health", dict(params))
    _age(pool, first, 45)
    assert _submit(pool, "check_accounts_health", dict(params)) != first, (
        "обычная операция получила длинное окно — намеренный повтор молча "
        "подавляется там, где подавлять нечего"
    )


def test_an_explicit_window_still_wins(pool):
    params = {"bot_id": 81, "text": "Серия"}
    first = _submit(pool, "run_broadcast", dict(params), dedup_window_sec=0)
    second = _submit(pool, "run_broadcast", dict(params), dedup_window_sec=0)
    assert first != second, (
        "dedup_window_sec=0 перестал отключать дедуп — намеренные серии "
        "идентичных операций сломаны"
    )


# ── Сторож: окно обязано быть длиннее клиентского таймаута ──────────────────

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _client_timeout_sec() -> float:
    src = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    m = re.search(r"FETCH_TIMEOUT_MS\s*=\s*(\d+)", src)
    assert m, ("в мини-аппе не нашёлся FETCH_TIMEOUT_MS — связь окна дедупа с "
               "ожиданием клиента надо перечитать, а не считать выполненной")
    return int(m.group(1)) / 1000.0


def test_the_window_is_longer_than_the_client_waits():
    from services import operation_bus as ob

    client = _client_timeout_sec()
    assert ob.IRREVERSIBLE_DEDUP_WINDOW_SEC > client, (
        f"окно {ob.IRREVERSIBLE_DEDUP_WINDOW_SEC}с короче, чем клиент ждёт "
        f"ответа ({client}с): повтор после таймаута приходит за окном, и "
        "необратимая операция выполнится дважды"
    )
    assert ob.IRREVERSIBLE_DEDUP_WINDOW_SEC >= client + 30, (
        "между таймаутом клиента и концом окна не осталось времени на то, "
        "чтобы владелец увидел ошибку и нажал заново"
    )
    assert ob.IRREVERSIBLE_DEDUP_WINDOW_SEC > ob.DEFAULT_DEDUP_WINDOW_SEC


def test_the_irreversible_types_actually_carry_the_window():
    from services import operation_bus as ob

    expected = (
        # Сообщение/пост уходит людям
        "run_broadcast", "network_broadcast", "mass_publish",
        "bulk_post_to_channel", "bulk_post_chans", "group_announce",
        "bulk_dm_adhoc", "dm_campaign", "mass_invite",
        "self_promo_blast", "niche_growth_post", "quick_post",
        "seed_presence_pack", "content_clone", "clone_adapt", "crosspost_run",
        "community_liven", "ai_comment",
        # Жалоба: второй раз — вторая жалоба на то же, уже от флота
        "mass_report", "report_peer", "strike",
        # Правка профилей и оформления видна снаружи и делается флотом
        "gp_bulk_apply", "bulk_seo_apply", "bulk_bot_profile",
        "bulk_update_profile", "bulk_set_profile", "profile_setter",
        # Развёртывание связки работает по живым каналам
        "deploy_network",
    )
    missing = [t for t in expected
               if ob.OP_REGISTRY.get(t, {}).get("dedup_window_sec")
               != ob.IRREVERSIBLE_DEDUP_WINDOW_SEC]
    assert not missing, (
        "у необратимой операции нет длинного окна: повтор после таймаута "
        "клиента выполнит её второй раз: " + ", ".join(missing)
    )


def test_the_deliberate_repeat_types_keep_the_short_window():
    """Длинное окно подавляет и НАМЕРЕННЫЙ повтор — поэтому не всем.

    Эти типы владелец запускает ПОВТОРНО по делу, с теми же параметрами:
    «ещё 100 просмотров», «ещё один такой же канал», «ещё одного бота». Для
    них длинное окно означало бы молча не выполненное действие — то есть
    болезнь хуже исходной. Список держится здесь, чтобы следующий заход не
    расширил окно «на всё подряд».
    """
    from services import operation_bus as ob

    deliberate = ("boost_views", "boost_reactions", "boost_stories",
                  "boost_subscribers", "boost_bot_starts",
                  "create_channel", "create_group", "bulk_create_channels",
                  "bot_factory", "gift_transfer", "account_warmup")
    widened = [t for t in deliberate
               if ob.OP_REGISTRY.get(t, {}).get("dedup_window_sec")]
    assert not widened, (
        "у операции, которую владелец повторяет по делу, окно расширено — "
        "второй запуск вернёт id первого и молча не выполнится: "
        + ", ".join(widened)
    )


def test_a_retry_can_never_be_swallowed_by_the_window():
    """Повтор не должен слиться с исходной операцией — у него другие params.

    Это главный риск длинного окна: кнопка «Повторить» вернула бы id уже
    закрытой операции, и владелец увидел бы «поставлено», а работа бы не
    пошла. Держится на том, что повтор помечается ссылкой на предка.
    """
    from services import operation_bus as ob

    base = {"channel_ids": [1, 2, 3]}
    retried = ob.params_for_retry(4242, dict(base))
    assert retried != base, (
        "повтор идёт с теми же params, что и предок: внутри длинного окна он "
        "сольётся с ним, и работа молча не выполнится"
    )
    added = set(retried) - set(base)
    assert added, "повтор не добавил ни одного ключа — сливание неизбежно"
    assert not (added & set(ob.DEDUP_COSMETIC_KEYS)), (
        "повтор отличается от предка только косметическим ключом, который из "
        "дедуп-идентичности вычёркивается — то есть не отличается вовсе"
    )
