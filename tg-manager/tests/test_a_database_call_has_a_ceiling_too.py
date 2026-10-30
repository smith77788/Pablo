"""У обращения к БД тоже есть потолок — и снять его средой нельзя.

ПОЧЕМУ ЭТО ВАЖНЕЕ, ЧЕМ ВЫГЛЯДИТ. Потолки на запросы к Telegram расставлены по
исполнителям поимённо, и их держит отдельная перепись. У обращений к БД потолок
один на всех: `command_timeout` в настройках пула. Если он исчезнет, повисший
запрос (чаще всего — ожидание блокировки) не вернётся в цикл исполнителя
НИКОГДА. А значит:

  * потолок прогона не сработает — его проверяет сам цикл, до которого
    управление не дойдёт;
  * оба сторожа зависших операцию ПОЩАДЯТ — она в `_active_op_ids`, и это
    ровно тот признак, по которому они не трогают живые;
  * алерт о застрявших молчит по той же причине.

То есть операция остаётся 'running' навсегда, держит слот владельца и не видна
как проблема. Из всех способов потерять операцию этот единственный, который не
лечится ни временем, ни рестартом сторожей — только перезапуском процесса.

Поэтому проверяем три вещи: потолок задан, он доезжает до пула, и его нельзя
обнулить переменной окружения.
"""
from __future__ import annotations

import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_the_pool_config_names_a_command_ceiling():
    from database.pool_config import get_pool_config

    cfg = get_pool_config()
    assert "command_timeout" in cfg, (
        "в настройках пула нет `command_timeout` — повисший запрос к БД "
        "остановит операцию навсегда, и ни один сторож её не поднимет")
    assert isinstance(cfg["command_timeout"], (int, float))
    assert cfg["command_timeout"] > 0, (
        f"потолок обращения к БД не положителен: {cfg['command_timeout']}")


def test_the_ceiling_reaches_the_real_pool():
    """Настройка, которую никто не передаёт в пул, — не настройка."""
    src = _src(os.path.join("database", "db.py"))
    node = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "create_pool")
    body = "\n".join(src.splitlines()[node.lineno - 1:node.end_lineno])
    assert "get_pool_config()" in body, (
        "`create_pool` больше не берёт настройки пула — потолок обращения "
        "к БД остался в функции, которую никто не зовёт")
    assert "**pool_config" in body or "command_timeout" in body, (
        "настройки собраны, но в `asyncpg.create_pool` не переданы")


@pytest.mark.parametrize("hostile", ["0", "0.0", "-1", "-0.5"])
def test_the_environment_cannot_switch_the_ceiling_off(hostile, monkeypatch):
    """`DB_CMD_TIMEOUT=0` не должен означать «без потолка».

    Ноль здесь — не «быстро», а «как повезёт»: asyncpg считает нулевой и
    отсутствующий таймаут одинаково безобидными, и мы получаем бесконечное
    ожидание под видом настройки. Опечатка в переменной окружения не вправе
    включать единственный неизлечимый класс потери операции.
    """
    from database import pool_config

    monkeypatch.setenv("DB_CMD_TIMEOUT", hostile)
    cfg = pool_config.get_pool_config()
    assert cfg["command_timeout"] > 0, (
        f"DB_CMD_TIMEOUT={hostile} снял потолок обращения к БД: "
        f"{cfg['command_timeout']}")


@pytest.mark.parametrize("huge", ["86400", "1e9"])
def test_an_enormous_value_is_not_the_same_as_no_ceiling(huge, monkeypatch):
    """«Очень большое» — то же бесконечное ожидание под видом настройки."""
    from database import pool_config

    monkeypatch.setenv("DB_CMD_TIMEOUT", huge)
    assert pool_config.get_pool_config()["command_timeout"] <= 600.0, (
        f"DB_CMD_TIMEOUT={huge} оставляет обращение к БД практически без "
        "потолка")


def test_a_sane_value_from_the_environment_is_honoured():
    """Зажим не должен превращаться в игнорирование настройки."""
    from database import pool_config

    os.environ["DB_CMD_TIMEOUT"] = "90"
    try:
        assert pool_config.get_pool_config()["command_timeout"] == 90.0, (
            "осмысленное значение из окружения перестало учитываться — "
            "зажим съел настройку целиком")
    finally:
        os.environ.pop("DB_CMD_TIMEOUT", None)


def test_the_detector_would_notice_a_missing_ceiling():
    """Самопроверка: проверка выше опирается на имя, которое реально есть."""
    src = _src(os.path.join("database", "pool_config.py"))
    assert "command_timeout" in src, (
        "ключ переименован — проверки выше сторожат пустоту")
