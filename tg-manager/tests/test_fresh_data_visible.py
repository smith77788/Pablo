"""Владелец должен видеть актуальные данные — числа, имена, ссылки.

Жалоба была одна и повторялась: «количество подписчиков не обновляется, имена
каналов/чатов/ботов тоже». Причин оказалось три, и каждая отдельная.

1. Фоновая сверка карточек каналов вставала намертво из-за ОДНОГО мёртвого
   аккаунта (`drift_detector`).
2. Кнопка «🔄 Обновить данные» брала число участников из списка диалогов, где
   Telegram его почти никогда не присылает, — и молча оставляла старое.
3. Имя и @username бота писались один раз при подключении и больше никогда.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _func_body(src: str, name: str) -> str:
    """Тело функции целиком — от её `def` до следующего верхнеуровневого.

    Раньше здесь стояло «первые N символов»: окно резало функцию посередине, и
    тест падал не потому, что код неверен, а потому что до нужной строки не
    дочитал. В этом проекте это уже случалось не раз — измеритель врал.
    """
    i = src.index(f"async def {name}")
    rest = src[i + 1:]
    m = re.search(r"\n(?:async )?def ", rest)
    return rest[: m.start()] if m else rest


# ── 1. мёртвый аккаунт больше не останавливает обновление всего списка ───────


def test_dead_account_does_not_freeze_the_whole_list():
    """Главная причина «не обновляется, сколько ни проси».

    Каналы заводятся конкретным аккаунтом (`managed_channels.acc_id`). Если тот
    аккаунт забанили или выключили, сверка делала `continue` — и эти каналы:
    не обновлялись никогда; не получали отметку осмотра, поэтому выбирались
    СНОВА каждым циклом; а выборка ограничена 200 строками, так что застрявшие
    занимали её целиком. Один мёртвый аккаунт замораживал весь список владельца.
    """
    src = _read("services/drift_detector.py")
    assert "_fallback_account(" in src, (
        "канал по-прежнему привязан к одной сессии — её смерть заморозит карточку"
    )
    assert "_stamp_checked(" in src, (
        "необновляемые каналы снова будут занимать выборку вечно"
    )


@pytest.mark.asyncio
async def test_channels_of_a_dead_account_stop_blocking_the_queue():
    """Поведенческая проверка того же: владелец, у которого не осталось ни одной
    живой сессии, не должен навсегда занять выборку своими каналами.

    Проверка именно исполнением, а не текстом исходника: наличие функции
    `_fallback_account` ничего не доказывает, если её не зовут в нужной ветке.
    """
    from services import drift_detector as dd

    stamped: list = []

    class _Pool:
        async def fetch(self, sql, *args):
            if "managed_channels" in sql:
                return [
                    {"id": 11, "owner_id": 5, "acc_id": 999, "channel_id": -100_1,
                     "title": "Канал", "username": "ch", "about": "",
                     "access_hash": 0},
                    {"id": 12, "owner_id": 5, "acc_id": 999, "channel_id": -100_2,
                     "title": "Чат", "username": "", "about": "",
                     "access_hash": 0},
                ]
            return []

        async def fetchrow(self, sql, *args):
            return None  # ни «родного», ни запасного аккаунта нет

        async def execute(self, sql, *args):
            if "last_drift_check" in sql:
                stamped.append(args[0])

    await dd._check_all(_Pool(), None)
    assert stamped, "каналы не отмечены — следующий круг выберет их снова, и так вечно"
    assert sorted(stamped[0]) == [11, 12]


def test_fallback_account_belongs_to_the_same_owner():
    """Запасная сессия берётся у ТОГО ЖЕ владельца — чужими каналы не читаем."""
    body = _func_body(_read("services/drift_detector.py"), "_fallback_account")
    assert "owner_id=$1" in body
    assert "is_active=true" in body and "session_str IS NOT NULL" in body
    assert "'banned'" in body, "запасным может оказаться забаненный аккаунт"


@pytest.mark.asyncio
async def test_stamp_marks_every_channel_it_is_given():
    from services import drift_detector as dd

    seen = {}

    class _Pool:
        async def execute(self, sql, *args):
            seen["sql"] = sql
            seen["ids"] = args[0]

    await dd._stamp_checked(_Pool(), [1, 2, 3])
    assert seen["ids"] == [1, 2, 3]
    assert "last_drift_check=now()" in seen["sql"]


@pytest.mark.asyncio
async def test_stamp_survives_a_broken_database():
    """Отметка — служебная: её сбой не должен ронять весь обход."""
    from services import drift_detector as dd

    class _Boom:
        async def execute(self, *a, **k):
            raise RuntimeError("база недоступна")

    await dd._stamp_checked(_Boom(), [1])  # не должно бросать
    await dd._stamp_checked(_Boom(), [])   # пустой список — не запрос


def test_queue_is_not_drained_at_four_hour_pace():
    """167 каналов по 10 за круг раз в 4 часа — это трое суток на полный обход.
    Владелец в такой схеме не увидит свежих чисел никогда."""
    from services import drift_detector as dd

    assert dd._BATCH_SIZE >= 40, "за круг берётся слишком мало каналов"
    assert dd._INTERVAL_BACKLOG <= 30 * 60, (
        "пока очередь не разобрана, ждать часами нечего"
    )
    assert dd._INTERVAL_BACKLOG < dd._INTERVAL


def test_cycle_reports_whether_work_is_left():
    src = _read("services/drift_detector.py")
    assert "return backlog" in src, "круг не сообщает, осталась ли очередь"
    assert "_INTERVAL_BACKLOG if backlog else _INTERVAL" in src, (
        "пауза не зависит от того, разобрана ли очередь"
    )


# ── 2. кнопка «Обновить данные» реально обновляет число участников ───────────


def test_member_count_is_taken_from_a_source_that_has_it():
    """`participants_count` в списке диалогов — необязательное поле, Telegram
    присылает его редко. Проект это уже знал (см. get_channel_members_count),
    но обновление карточек всё равно читало его оттуда — и число участников
    оставалось прежним при каждом нажатии кнопки."""
    ow = _read("services/op_worker.py")
    assert "get_channels_full_info(" in ow, (
        "число участников по-прежнему берётся только из обхода диалогов"
    )
    am = _read("services/account_manager.py")
    body = _func_body(am, "get_channels_full_info")
    assert "GetFullChannelRequest" in body, "счётчик берётся не из полной карточки"


def test_full_info_uses_one_connection_for_many_channels():
    """get_full_channel_info открывает клиент на КАЖДЫЙ канал: при 167 каналах
    это 167 подключений сессии подряд — и долго, и заметно."""
    am = _read("services/account_manager.py")
    body = _func_body(am, "get_channels_full_info")
    assert body.count("_make_client(") == 1
    assert "for idx, cid in enumerate(ids)" in body


def test_full_info_paces_itself_and_stops_on_flood():
    am = _read("services/account_manager.py")
    body = _func_body(am, "get_channels_full_info")
    assert "FloodWaitError" in body and "break" in body, (
        "обход не останавливается на FloodWait — это эскалация лимита"
    )
    assert "asyncio.sleep(pause" in body, "запросы идут без пауз"


def test_refresh_has_a_ceiling():
    from services import op_worker

    assert op_worker._MEMBERS_REFRESH_CAP >= 1
    assert "CHANNELS_MEMBERS_REFRESH_CAP" in _read("services/op_worker.py")


def test_zero_never_overwrites_a_known_count():
    """Ноль от Telegram означает «не сказали», а не «никого нет»."""
    ow = _read("services/op_worker.py")
    i = ow.index("_need_count = [")
    body = ow[i:i + 1400]
    assert 'int(info.get("members_count") or 0) > 0' in body


def test_report_does_not_promise_more_than_it_did():
    """«Обновлено карточек» считало и те каналы, где число участников осталось
    старым. Отчёт обещал больше, чем было сделано."""
    ow = _read("services/op_worker.py")
    assert "Пересчитано участников" in ow
    assert '"members_refreshed": counted' in ow
    assert "Число участников не обновилось" in ow, (
        "при нулевом результате отчёт должен сказать об этом прямо"
    )


# ── 3. имя и ссылка бота сверяются с Telegram ───────────────────────────────


def test_bot_name_is_refreshed_at_all():
    """Имя бота меняют в @BotFather, а не через Infragram: записанное один раз
    при подключении устаревало навсегда."""
    src = _read("services/auto_responder.py")
    assert "bot_profile_refresh" in src, "сверку профилей никто не запускает"


@pytest.mark.parametrize(
    "stored, fresh, expect",
    [
        ({"first_name": "Старое", "username": "old_bot"},
         {"first_name": "Новое", "username": "old_bot"}, {"first_name": "Новое"}),
        ({"first_name": "Имя", "username": "old_bot"},
         {"first_name": "Имя", "username": "new_bot"}, {"username": "new_bot"}),
        ({"first_name": "Имя", "username": "bot"},
         {"first_name": "Имя", "username": "bot"}, {}),
        # @username в базе мог лежать со собачкой — это не изменение
        ({"first_name": "Имя", "username": "@bot"},
         {"first_name": "Имя", "username": "bot"}, {}),
    ],
)
def test_changed_fields_writes_only_real_changes(stored, fresh, expect):
    from services import bot_profile_refresh as bpr

    assert bpr.changed_fields(stored, fresh) == expect


def test_empty_answer_never_wipes_a_known_name():
    """Один неудачный ответ не должен обнулить список ботов."""
    from services import bot_profile_refresh as bpr

    stored = {"first_name": "Кассовые чеки", "username": "Bvffghhfbot"}
    assert bpr.changed_fields(stored, None) == {}
    assert bpr.changed_fields(stored, {}) == {}
    assert bpr.changed_fields(stored, {"first_name": "", "username": ""}) == {}


@pytest.mark.asyncio
async def test_every_checked_bot_gets_a_stamp():
    """Без отметки один и тот же бот попадал бы в каждую выборку, а остальные —
    никогда."""
    from services import bot_profile_refresh as bpr

    seen = []

    class _Pool:
        async def execute(self, sql, *args):
            seen.append((sql, args))

    await bpr._stamp(_Pool(), 7, {})
    await bpr._stamp(_Pool(), 7, {"first_name": "Новое"})
    assert all("profile_checked_at = now()" in s for s, _ in seen)
    assert "first_name = $2" in seen[1][0]


@pytest.mark.asyncio
async def test_dead_token_is_stamped_and_skipped():
    """Бот с отозванным токеном не должен занимать выборку вечно — но и
    эскалировать его здесь незачем, этим занят auto_responder."""
    from services import bot_profile_refresh as bpr

    stamped = []

    class _Pool:
        async def fetch(self, *a, **k):
            return [{"bot_id": 1, "token": "t", "first_name": "A", "username": "a"}]

        async def execute(self, sql, *args):
            stamped.append(args[0])

    import services.bot_api as bot_api

    _orig = bot_api.get_me

    async def _dead(_http, _token):
        return None

    bot_api.get_me = _dead
    try:
        changed = await bpr.refresh_bot_profiles(_Pool(), None)
    finally:
        bot_api.get_me = _orig
    assert changed == 0
    assert stamped == [1], "мёртвый бот не отмечен — застрянет в выборке"


def test_migration_exists():
    sql = list(ROOT.glob("schema_v*_bot_profile_refresh.sql"))
    assert sql, "нет миграции для profile_checked_at"
    text = sql[0].read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS profile_checked_at" in text
