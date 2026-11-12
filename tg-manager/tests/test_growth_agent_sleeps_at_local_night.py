"""Агент роста не вступает и не постит в местную ночь аккаунта.

НАХОДКА. Темп Агента роста (паузы 3–8 и 10–20 минут) не знал времени суток:
запуск вечером уходил вступать и публиковать в 3 часа ночи по времени
аккаунта. Ночная активность — бот-сигнал, а пост в спящей группе к утру
тонет под десятками новых сообщений: охвата нет, риск есть. Прогрев и
кампании ЛС ночь уже уважали (geo_tempo), Агент роста — нет.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся): убрать выбор аккаунта по is_local_night ->
красный test_executor_skips_night_accounts.
"""
from __future__ import annotations

import ast
import datetime as dt
import pathlib

from services import geo_tempo


def test_seconds_until_local_morning():
    # 02:00 UTC = 05:00 в Москве → до 08:00 три часа.
    now = dt.datetime(2026, 11, 12, 2, 0, tzinfo=dt.timezone.utc)
    assert geo_tempo.seconds_until_local_morning("RU", now) == 3 * 3600
    # 06:00 UTC = 09:00 в Москве → до завтрашних 08:00 23 часа.
    now = dt.datetime(2026, 11, 12, 6, 0, tzinfo=dt.timezone.utc)
    assert geo_tempo.seconds_until_local_morning("RU", now) == 23 * 3600
    assert geo_tempo.seconds_until_local_morning(None, now) is None


def test_executor_skips_night_accounts():
    src = (pathlib.Path(__file__).resolve().parents[1] / "services" / "op_worker.py"
           ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    body = next(ast.get_source_segment(src, n) for n in tree.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_exec_niche_growth_post")
    join = body.index("account_manager.join_channel(")
    assert body.index("is_local_night(") < join, "выбор аккаунта не смотрит на местную ночь"
    assert "seconds_until_local_morning(" in body
    assert '"local_night"' in body
