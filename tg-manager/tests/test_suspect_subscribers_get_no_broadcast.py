"""Накрученных подписчиков не берёт ни одна отправка.

ЧТО БЫЛО. `flood_guard` существует ровно затем, чтобы всплеск накрутки не
попадал в рассылки: при режиме `protect`/`block` он ставит подписчикам
`bot_users.suspect=TRUE` — «вон из аудитории и статистики» (см. шапку
`services/flood_guard.py` и `purge_suspects`, которая заодно вычищает их из
`funnel_subscriptions`).

Флаг читали только `db.get_audience_count` / `get_audience_user_ids`, счётчик в
`broadcaster.mass_broadcast_with_scheduling` и повтор недоставленным. А вот
исполнитель, который реально отправляет, — `op_worker._exec_run_broadcast` —
брал аудиторию БЕЗ этого условия. Значит защита от накрутки не защищала: экран
обещал «получат N», операция уходила по N + накрученные, и владелец платил
репутацией бота за рассылку по фальшивым аккаунтам. То же самое в сетевой
рассылке, в воронках, в сегментах «новые»/«по языку» и в личных сообщениях.

ЧТО СТАЛО. Отбор адресатов и счётчик «сколько получат» смотрят на один и тот
же набор условий.
"""
from __future__ import annotations

import asyncio
import inspect
import pathlib

from database import db


def _run(coro):
    return asyncio.run(coro)


class _RecordingPool:
    def __init__(self):
        self.queries: list[str] = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        return []

    def sql(self) -> str:
        return "\n".join(self.queries)


def _flat(text: str) -> str:
    return " ".join(text.split())


# ── отправка ─────────────────────────────────────────────────────────────────

def test_broadcast_executor_skips_suspects():
    from services import op_worker

    src = inspect.getsource(op_worker._exec_run_broadcast)
    sel = src[src.index("FROM bot_users"):]
    assert "suspect" in sel[:400], (
        "исполнитель рассылки шлёт накрученным — защита flood_guard не "
        "работает там, где она и нужна:\n" + sel[:400])


def test_network_broadcast_skips_suspects():
    from services import op_worker

    src = pathlib.Path(op_worker.__file__).read_text()
    picks = [ln for ln in src.splitlines()
             if "SELECT user_id FROM bot_users" in ln]
    assert picks, "отбор адресатов из bot_users исчез — проверьте тест"
    for line in picks:
        idx = src.index(line)
        assert "suspect" in src[idx: idx + len(line) + 200], (
            "отбор адресатов не отсекает накрученных:\n" + line)


def test_funnels_skip_suspects():
    """flood_guard чистит funnel_subscriptions у suspect — значит и не заводить."""
    from services import auto_funnel

    src = pathlib.Path(auto_funnel.__file__).read_text()
    picks = [ln for ln in src.splitlines()
             if "FROM bot_users" in ln and "SELECT user_id" in ln]
    assert len(picks) >= 4, f"сегментов воронки стало меньше: {len(picks)}"
    for line in picks:
        assert "suspect=FALSE" in line, "воронка заводит накрученных:\n" + line


def test_bot_segments_skip_suspects():
    for call in (
        lambda p: db.get_audience_new_users(p, 10, 7),
        lambda p: db.get_audience_by_language(p, 10, "ru"),
    ):
        pool = _RecordingPool()
        _run(call(pool))
        assert "suspect" in pool.sql(), pool.sql()


def test_direct_messages_skip_suspects():
    from services import dm_engine

    src = inspect.getsource(dm_engine._get_targets)
    for marker in ("bu.bot_id=$1 AND mb.added_by=$2", "mb.added_by=$1"):
        assert marker in src, marker
        tail = src[src.index(marker): src.index(marker) + 400]
        assert "suspect" in tail, f"источник {marker!r} тянет накрученных:\n{tail}"


# ── обещание на экране ───────────────────────────────────────────────────────

def test_promised_total_matches_what_is_sent():
    """Счётчик и отправка должны считать одно и то же множество."""
    from services import broadcaster, op_worker

    cnt = inspect.getsource(broadcaster.mass_broadcast_with_scheduling)
    cnt = _flat(cnt[cnt.index("COUNT(*) FROM bot_users"):][:300])
    send = inspect.getsource(op_worker._exec_run_broadcast)
    send = _flat(send[send.index("FROM bot_users"):][:300])
    for cond in ("is_active", "is_blocked", "suspect"):
        assert cond in cnt, f"счётчик не учитывает {cond}: {cnt}"
        assert cond in send, f"отправка не учитывает {cond}: {send}"


def test_miniapp_preview_counts_the_same_audience():
    """«Сколько получат» в мини-аппе не должно обещать больше, чем уйдёт.

    Берём именно два обработчика рассылки — создание и предпросмотр. Общий
    поиск по файлу здесь не годится: тем же запросом считают подписчиков для
    карточки бота и для аналитики, и там накрученные как раз нужны.
    """
    from services import mini_app_api

    src = pathlib.Path(mini_app_api.__file__).read_text()
    for handler in ("create_broadcast", "broadcast_recipients"):
        start = src.index(f"async def {handler}(request")
        body = src[start: src.index("    async def ", start + 10)]
        i = body.index("COUNT(*) FROM bot_users")
        chunk = _flat(body[i: i + 220])
        assert "is_blocked=false" in chunk and "suspect=false" in chunk, (
            f"{handler}: счётчик обещает больше, чем уйдёт:\n{chunk}")
