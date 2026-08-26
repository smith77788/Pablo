"""Экран «Рейтинг» обязан работать на той схеме, которая реально есть.

ЧТО БЫЛО СЛОМАНО. `services/ranking_engine` жил по СОБСТВЕННОЙ модели данных —
«владелец + канал»: `tracked_keywords(owner_id, channel_id, check_interval)` и
`search_rankings(owner_id, channel_id, keyword, position, previous_position)`.
Таких колонок в схеме нет и не было никогда: настоящая подсистема рейтинга (её
пишут `ranking_checker`, бот и соседние маршруты мини-аппа) устроена как
«бот + ключевое слово».

Каждый запрос модуля падал, а `except` возвращал пустой список — экран показывал
«Нет ключевых слов» независимо от того, сколько их добавлено, и не показывал ни
одной позиции. Добавить ключ было тоже нельзя: фронт шлёт POST на
/api/miniapp/ranking/keywords, а маршрут был зарегистрирован только на GET (405).

Найдено сверкой колонок в INSERT-ах с настоящей схемой. На заглушке пула это
невидимо: SQL там не выполняется. Запуск — INFRAGRAM_TEST_DSN.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 660011
OTHER = 660022
BOT = 990011

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    """Настоящая схема рейтинга + миграция, которую мы добавили."""
    import asyncpg

    async def _mk():
        setup = await asyncpg.connect(DSN)
        try:
            await setup.execute("CREATE SCHEMA IF NOT EXISTS rktest")
        finally:
            await setup.close()
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4,
                                      server_settings={"search_path": "rktest"})
        # Боевые определения (как в public), без лишних колонок.
        await p.execute("""CREATE TABLE IF NOT EXISTS managed_bots (
                               bot_id BIGINT PRIMARY KEY,
                               added_by BIGINT NOT NULL,
                               username TEXT)""")
        await p.execute("""CREATE TABLE IF NOT EXISTS tracked_keywords (
                               id SERIAL PRIMARY KEY,
                               bot_id BIGINT NOT NULL,
                               owner_id BIGINT NOT NULL,
                               keyword TEXT NOT NULL,
                               is_active BOOLEAN DEFAULT TRUE,
                               created_at TIMESTAMPTZ DEFAULT now(),
                               notify_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                               UNIQUE (bot_id, keyword))""")
        await p.execute("""CREATE TABLE IF NOT EXISTS search_rankings (
                               id SERIAL PRIMARY KEY,
                               keyword_id INTEGER NOT NULL
                                   REFERENCES tracked_keywords(id) ON DELETE CASCADE,
                               bot_id BIGINT NOT NULL,
                               position INTEGER,
                               checked_at TIMESTAMPTZ DEFAULT now())""")
        # Ровно та миграция, что уезжает в прод (ranking_alerts + колонка region).
        with open(os.path.join(ROOT, "schema_v184.sql"), encoding="utf-8") as f:
            for chunk in f.read().split(";"):
                body = "\n".join(ln for ln in chunk.split("\n")
                                 if not ln.strip().startswith("--")).strip()
                if body:
                    await p.execute(body)
        await p.execute(
            "INSERT INTO managed_bots(bot_id, added_by, username) VALUES($1,$2,$3) "
            "ON CONFLICT (bot_id) DO NOTHING", BOT, OWNER, "my_bot")
        return p

    p = _run(_mk())
    yield p
    for t in ("ranking_alerts", "search_rankings", "tracked_keywords",
              "managed_bots", "strike_appeals", "notification_dedup"):
        _run(p.execute(f"DROP TABLE IF EXISTS {t} CASCADE"))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM ranking_alerts"))
    _run(pool.execute("DELETE FROM search_rankings"))
    _run(pool.execute("DELETE FROM tracked_keywords"))
    yield


def _track(pool, kw="боты", region="ru", owner=OWNER, bot=BOT):
    from services import ranking_engine as re_
    return _run(re_.track_keyword(pool, owner, kw, bot, region))


# ── добавление ключа ─────────────────────────────────────────────────────────

def test_tracking_a_keyword_actually_writes_a_row(pool):
    """Главное: ключ сохраняется. Раньше INSERT падал на несуществующих колонках."""
    res = _track(pool)
    assert res.get("ok"), f"ключ не добавился: {res}"
    row = _run(pool.fetchrow(
        "SELECT bot_id, owner_id, keyword, region, is_active "
        "FROM tracked_keywords WHERE id=$1", res["id"]))
    assert (row["bot_id"], row["owner_id"], row["keyword"]) == (BOT, OWNER, "боты")
    assert row["region"] == "ru" and row["is_active"] is True


def test_keyword_shows_up_on_the_screen(pool):
    """Список экрана обязан вернуть добавленный ключ вместе с именем бота."""
    from services import ranking_engine as re_
    _track(pool)
    got = _run(re_.get_tracked_keywords(pool, OWNER))
    assert [g["keyword"] for g in got] == ["боты"], (
        "экран показывает «Нет ключевых слов», хотя ключ добавлен")
    assert got[0]["bot_username"] == "my_bot", "экран показывает имя бота"


def test_someone_elses_bot_cannot_be_tracked(pool):
    res = _track(pool, owner=OTHER)
    assert not res.get("ok") and "не найден" in res.get("error", "").lower(), res


def test_keyword_without_bot_is_refused_with_a_readable_reason(pool):
    from services import ranking_engine as re_
    res = _run(re_.track_keyword(pool, OWNER, "боты", None))
    assert not res.get("ok")
    assert "бот" in res.get("error", "").lower(), (
        "отказ обязан объяснять, чего не хватает, а не просто 'ok: false'")


def test_re_tracking_reactivates_instead_of_failing(pool):
    """Повторное добавление того же ключа — не ошибка, а включение обратно."""
    first = _track(pool)
    _run(pool.execute("UPDATE tracked_keywords SET is_active=FALSE"))
    again = _track(pool, region="kz")
    assert again.get("ok") and again["id"] == first["id"]
    row = _run(pool.fetchrow("SELECT is_active, region FROM tracked_keywords"))
    assert row["is_active"] is True and row["region"] == "kz"


# ── позиции и тренд ──────────────────────────────────────────────────────────

def test_positions_and_trend_reach_the_screen(pool):
    """Экран рисует стрелку по разнице с предыдущим замером."""
    from services import ranking_engine as re_
    kid = _track(pool)["id"]
    _run(re_.record_position(pool, OWNER, kid, 20))
    _run(re_.record_position(pool, OWNER, kid, 12))

    pos = {p["keyword_id"]: p for p in _run(re_.get_all_positions(pool, OWNER))}
    assert pos[kid]["position"] == 12
    assert pos[kid]["previous_position"] == 20, "предыдущая позиция нужна для стрелки"
    assert pos[kid]["last_checked"] is not None


def test_history_is_scoped_to_the_owner(pool):
    from services import ranking_engine as re_
    kid = _track(pool)["id"]
    _run(re_.record_position(pool, OWNER, kid, 5))
    assert len(_run(re_.get_position_history(pool, OWNER, kid))) == 1
    assert _run(re_.get_position_history(pool, OTHER, kid)) == [], (
        "история чужого ключа не должна отдаваться")


def test_recording_for_someone_elses_keyword_is_refused(pool):
    from services import ranking_engine as re_
    kid = _track(pool)["id"]
    res = _run(re_.record_position(pool, OTHER, kid, 3))
    assert not res.get("ok")
    assert _run(pool.fetchval("SELECT COUNT(*) FROM search_rankings")) == 0


# ── оповещения ───────────────────────────────────────────────────────────────

def test_significant_move_raises_an_alert_small_one_does_not(pool):
    """Дрожание выдачи не должно засорять экран — иначе его перестают смотреть."""
    from services import ranking_engine as re_
    kid = _track(pool)["id"]
    _run(re_.record_position(pool, OWNER, kid, 20))
    assert _run(re_.record_position(pool, OWNER, kid, 19))["alert"] is None
    assert _run(re_.record_position(pool, OWNER, kid, 5))["alert"] == "improved"
    assert _run(re_.record_position(pool, OWNER, kid, 40))["alert"] == "dropped"


def test_entering_and_leaving_the_results_are_their_own_events(pool):
    from services import ranking_engine as re_
    kid = _track(pool)["id"]
    assert _run(re_.record_position(pool, OWNER, kid, 7))["alert"] == "entered"
    assert _run(re_.record_position(pool, OWNER, kid, None))["alert"] == "lost"


def test_alerts_carry_type_and_ready_text_for_the_screen(pool):
    from services import ranking_engine as re_
    kid = _track(pool)["id"]
    _run(re_.record_position(pool, OWNER, kid, 30))
    _run(re_.record_position(pool, OWNER, kid, 4))

    alerts = _run(re_.get_alerts(pool, OWNER))
    assert alerts, "оповещение не записалось"
    a = alerts[0]
    assert a["type"] == "improved", "фронт читает поле type"
    assert "30" in a["message"] and "4" in a["message"], (
        f"текст должен называть обе позиции: {a['message']!r}")


def test_acknowledge_only_touches_your_own_alert(pool):
    from services import ranking_engine as re_
    kid = _track(pool)["id"]
    _run(re_.record_position(pool, OWNER, kid, 30))
    _run(re_.record_position(pool, OWNER, kid, 4))
    # Событий два: «появился в выдаче» и «вырос» — гасим адресно одно.
    aid = _run(re_.get_alerts(pool, OWNER))[0]["id"]

    assert _run(re_.acknowledge_alert(pool, OTHER, aid))["ok"] is False
    assert _run(re_.acknowledge_alert(pool, OWNER, aid))["ok"] is True
    left = [a["id"] for a in _run(re_.get_alerts(pool, OWNER, unacknowledged_only=True))]
    assert aid not in left, "погашенное оповещение всё ещё считается непрочитанным"
    assert left, "погашено больше, чем просили"


# ── сводка в шапке ───────────────────────────────────────────────────────────

def test_stats_survive_a_broken_part(pool):
    """Один упавший показатель не должен обнулять всю шапку.

    Раньше вся сводка стояла в одном try: падение на несуществовавшей
    ranking_alerts возвращало ошибку вместо четырёх чисел.
    """
    from services import ranking_engine as re_
    kid = _track(pool)["id"]
    _run(re_.record_position(pool, OWNER, kid, 8))

    _run(pool.execute("ALTER TABLE ranking_alerts RENAME TO ranking_alerts_hidden"))
    try:
        stats = _run(re_.get_ranking_stats(pool, OWNER))
    finally:
        _run(pool.execute("ALTER TABLE ranking_alerts_hidden RENAME TO ranking_alerts"))
    assert stats["total_tracked"] == 1, f"шапка обнулилась целиком: {stats}"
    assert stats["total_checks"] == 1
    assert stats["alerts_pending"] == 0


# ── маршрут, которого не было ────────────────────────────────────────────────

def test_post_route_for_adding_a_keyword_is_registered():
    """Фронт добавляет ключ POST-ом; без маршрута кнопка отвечала 405."""
    src = open(os.path.join(ROOT, "services", "mini_app_api.py"),
               encoding="utf-8").read()
    assert 'add_post("/api/miniapp/ranking/keywords"' in src, (
        "маршрут POST /api/miniapp/ranking/keywords не зарегистрирован — "
        "экран «Рейтинг» нельзя наполнить")
