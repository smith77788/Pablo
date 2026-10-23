"""Выдача тарифа и отметка о ней — одной транзакцией. Живой Postgres.

Обе функции устроены одинаково: сначала ставится отметка «награда выдана» /
«бонус выдан», потом продлевается подписка. Отметка сама себя и отсекает —
второй попытки не будет. Значит обрыв между двумя запросами (рестарт
контейнера на деплое, падение соединения) означает: человек числится
получившим награду и не получит её НИКОГДА.

Проверить это можно только в базе: нужен настоящий откат транзакции. Сбой
подписки подделываем временным триггером на `subscriptions`, ограниченным
нашим тестовым пользователем.

КАК ЗАПУСТИТЬ — см. докстринг tests/test_invite_e2e_postgres.py (нужен
INFRAGRAM_TEST_DSN).
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN"
)

_REFERRER = 888_101
_REFERRED = 888_102
_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)


class _БотЗаглушка:
    """Уведомление к атомарности отношения не имеет — просто не мешаем."""

    async def send_message(self, *a, **kw):
        return None


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        await _очистить(p)
        for uid in (_REFERRER, _REFERRED):
            await p.execute(
                "INSERT INTO platform_users(user_id, first_name) VALUES($1,'тест') "
                "ON CONFLICT (user_id) DO NOTHING", uid)
        return p

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"нет Postgres по INFRAGRAM_TEST_DSN: {exc}")

    yield p

    async def _teardown():
        await _очистить(p)
        await p.close()

    _run(_teardown())


async def _очистить(p):
    await p.execute("DROP TRIGGER IF EXISTS _тест_срыв_подписки ON subscriptions")
    await p.execute("DELETE FROM referral_rewards WHERE user_id = ANY($1::bigint[])",
                    [_REFERRER, _REFERRED])
    await p.execute("DELETE FROM platform_referrals WHERE referrer_id=$1 OR referred_id=$2",
                    _REFERRER, _REFERRED)
    await p.execute("DELETE FROM subscriptions WHERE user_id = ANY($1::bigint[])",
                    [_REFERRER, _REFERRED])
    # platform_users убирается последней: на неё ссылаются строки выше. Без
    # этой строки двое подопытных оставались в базе навсегда, и следующий
    # прогон по той же базе ронял test_admin_broadcast_confirm — та проверяет
    # «рассылка дошла до всех», а «всех» на два человека больше. В CI база
    # каждый раз новая, поэтому пряталось: падало только локально и выглядело
    # как плавающий тест.
    await p.execute("DELETE FROM platform_referral_codes "
                    "WHERE user_id = ANY($1::bigint[])", [_REFERRER, _REFERRED])
    await p.execute("DELETE FROM platform_users WHERE user_id = ANY($1::bigint[])",
                    [_REFERRER, _REFERRED])


async def _срыв_подписки_для(p, user_id: int):
    """Временный триггер: запись подписки этого пользователя падает."""
    await p.execute(
        """CREATE OR REPLACE FUNCTION _тест_срыв() RETURNS trigger AS $f$
           BEGIN RAISE EXCEPTION 'подписка не записалась (тест)'; END $f$ LANGUAGE plpgsql""")
    await p.execute(
        f"""CREATE TRIGGER _тест_срыв_подписки BEFORE INSERT OR UPDATE ON subscriptions
            FOR EACH ROW WHEN (NEW.user_id = {int(user_id)})
            EXECUTE FUNCTION _тест_срыв()""")


def test_сорванный_бонус_не_оставляет_отметку(pool):
    """Приветственный бонус: подписка не записалась — отметка тоже не стоит."""
    from database import db

    _run(pool.execute(
        "INSERT INTO platform_referrals(referrer_id, referred_id, welcome_bonus_given) "
        "VALUES($1,$2,false)", _REFERRER, _REFERRED))
    _run(_срыв_подписки_для(pool, _REFERRED))
    try:
        with pytest.raises(Exception):
            _run(db.give_welcome_bonus(pool, _REFERRED, _БотЗаглушка()))
        отметка = _run(pool.fetchval(
            "SELECT welcome_bonus_given FROM platform_referrals WHERE referred_id=$1",
            _REFERRED))
        assert отметка is False, (
            "отметка осталась без подписки: человек числится получившим бонус, "
            "а дней ему не начислили, и второй попытки не будет")
    finally:
        _run(pool.execute("DROP TRIGGER IF EXISTS _тест_срыв_подписки ON subscriptions"))
        _run(pool.execute("DELETE FROM platform_referrals WHERE referred_id=$1", _REFERRED))


def test_целый_бонус_доезжает_до_обоих_хранилищ(pool):
    """Тот же путь без сбоя: и подписка, и второе хранилище обновлены."""
    from database import db

    _run(pool.execute(
        "INSERT INTO platform_referrals(referrer_id, referred_id, welcome_bonus_given) "
        "VALUES($1,$2,false)", _REFERRER, _REFERRED))
    try:
        assert _run(db.give_welcome_bonus(pool, _REFERRED, _БотЗаглушка())) is True
        sub = _run(pool.fetchrow(
            "SELECT plan, is_active FROM subscriptions WHERE user_id=$1", _REFERRED))
        assert sub and sub["plan"] == "starter" and sub["is_active"]
        assert _run(pool.fetchval(
            "SELECT current_plan FROM platform_users WHERE user_id=$1", _REFERRED)) == "starter"
        assert _run(pool.fetchval(
            "SELECT welcome_bonus_given FROM platform_referrals WHERE referred_id=$1",
            _REFERRED)) is True
    finally:
        _run(pool.execute("DELETE FROM platform_referrals WHERE referred_id=$1", _REFERRED))
        _run(pool.execute("DELETE FROM subscriptions WHERE user_id=$1", _REFERRED))


def test_сорванная_реферальная_награда_не_отмечается_выданной(pool):
    """Реф-награда: подписка не записалась — строки referral_rewards тоже нет.

    Иначе уровень навсегда попадает в existing_levels и награду уже никто
    не выдаст.
    """
    from database import db

    # Пять активированных рефералов — порог уровня basic.
    for i in range(5):
        uid = 888_200 + i
        _run(pool.execute(
            "INSERT INTO platform_users(user_id, first_name) VALUES($1,'тест') "
            "ON CONFLICT (user_id) DO NOTHING", uid))
        _run(pool.execute(
            "INSERT INTO platform_referrals(referrer_id, referred_id, activated_at) "
            "VALUES($1,$2, now())", _REFERRER, uid))
    _run(_срыв_подписки_для(pool, _REFERRER))
    try:
        granted = _run(db.check_and_grant_rewards(pool, _REFERRER, _БотЗаглушка()))
        assert granted == [], granted
        assert _run(pool.fetchval(
            "SELECT COUNT(*) FROM referral_rewards WHERE user_id=$1", _REFERRER)) == 0
    finally:
        _run(pool.execute("DROP TRIGGER IF EXISTS _тест_срыв_подписки ON subscriptions"))
        _run(pool.execute("DELETE FROM platform_referrals WHERE referrer_id=$1", _REFERRER))
        _run(pool.execute("DELETE FROM platform_users WHERE user_id >= 888200 AND user_id < 888300"))


def test_целая_реферальная_награда_выдаётся_один_раз(pool):
    """Без сбоя награда выдаётся, а повторный вызов её не дублирует."""
    from database import db

    for i in range(5):
        uid = 888_300 + i
        _run(pool.execute(
            "INSERT INTO platform_users(user_id, first_name) VALUES($1,'тест') "
            "ON CONFLICT (user_id) DO NOTHING", uid))
        _run(pool.execute(
            "INSERT INTO platform_referrals(referrer_id, referred_id, activated_at) "
            "VALUES($1,$2, now())", _REFERRER, uid))
    try:
        assert _run(db.check_and_grant_rewards(pool, _REFERRER, _БотЗаглушка())) == ["basic"]
        assert _run(db.check_and_grant_rewards(pool, _REFERRER, _БотЗаглушка())) == []
        assert _run(pool.fetchval(
            "SELECT COUNT(*) FROM referral_rewards WHERE user_id=$1", _REFERRER)) == 1
        assert _run(pool.fetchval(
            "SELECT current_plan FROM platform_users WHERE user_id=$1", _REFERRER)) == "starter"
    finally:
        _run(pool.execute("DELETE FROM referral_rewards WHERE user_id=$1", _REFERRER))
        _run(pool.execute("DELETE FROM platform_referrals WHERE referrer_id=$1", _REFERRER))
        _run(pool.execute("DELETE FROM subscriptions WHERE user_id=$1", _REFERRER))
        _run(pool.execute("DELETE FROM platform_users WHERE user_id >= 888300 AND user_id < 888400"))
