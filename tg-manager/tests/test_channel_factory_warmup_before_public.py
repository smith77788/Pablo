"""Фабрика каналов: канал не становится публичным ПУСТЫМ в ту же секунду.

Владелец: «каналы, созданные фабрикой, сразу попадают в теневой бан и их нельзя
использовать для поисковой выдачи — это критическая ошибка». Причина в
_exec_bulk_create_channels_multi: публичный @username назначался СРАЗУ после
создания, ДО первого поста — пустой, только что созданный публичный канал
Telegram распознаёт как спам-фабрику и скрывает из поиска.

Фикс: сперва первый контент (по access_hash, без @), затем прогрев-пауза, и
только потом публичный @username — на канале с постом и с разнесением событий
«создан» / «стал публичным». Проверяем порядок по исходнику исполнителя
(мок-прогон всей фабрики неподъёмен — слишком много живых зависимостей).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _multi_body() -> str:
    i = WORKER.index("async def _exec_bulk_create_channels_multi(")
    j = WORKER.index("\nasync def ", i + 1)
    return WORKER[i:j]


def test_first_post_happens_before_username_assignment():
    body = _multi_body()
    i_post = body.index("# ── Первый контент ДО публичности")
    i_user = body.index("set_channel_username")
    assert 0 < i_post < i_user, (
        "первый пост обязан идти ДО назначения @username — иначе канал становится "
        "публичным пустым (теневой бан свежих каналов)"
    )


def test_first_post_uses_access_hash_not_username():
    body = _multi_body()
    # Блок первого поста — до назначения username — постит по access_hash с
    # username="" (публичного имени ещё нет).
    seg = body[body.index("# ── Первый контент ДО публичности"):body.index("set_channel_username")]
    assert "post_to_channel(" in seg
    assert 'username=""' in seg, "первый пост идёт по access_hash, @username ещё нет"


def test_warmup_pause_before_going_public():
    body = _multi_body()
    # Пауза-прогрев стоит перед перебором @username и настраивается env.
    assert "CHANNEL_WARMUP_MIN_S" in WORKER and "CHANNEL_WARMUP_MAX_S" in WORKER
    i_sleep = body.index("random.uniform(_WARM_MIN_S, _WARM_MAX_S)")
    i_user = body.index("set_channel_username")
    assert i_sleep < i_user, "пауза-прогрев обязана стоять ДО назначения @username"


def test_managed_channels_insert_after_username_assigned():
    body = _multi_body()
    # Запись в БД идёт после назначения @username, чтобы имя попало в managed_channels.
    i_user = body.index("set_channel_username")
    i_insert = body.index("INSERT INTO managed_channels")
    assert i_user < i_insert


def _legacy_body() -> str:
    i = WORKER.index("async def _exec_bulk_create_channels(")
    j = WORKER.index("\nasync def ", i + 1)
    return WORKER[i:j]


def test_legacy_factory_pause_matches_comment_and_scales_by_time():
    # Legacy-режим (одиночный аккаунт) тоже не должен делать канал публичным в
    # первые секунды. Рассинхрон устранён: паузы uniform(30,60) больше нет.
    body = _legacy_body()
    assert "random.uniform(30, 60)" not in body, (
        "рассинхрон комментария и кода устранён — пауза перед публичностью реальная"
    )
    assert "random.uniform(45, 120)" in body
    assert "time_of_day_factor()" in body
