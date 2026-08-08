"""Инвайтер: адаптивный темп вместо фиксированных пауз + петля обучения.

БЫЛО: пауза между батчами = 3с × множитель режима, который пользователь выбирал
руками. Одинаковая для всех аккаунтов и не зависящая от их состояния — ровно то,
что делают конкуренты. Ровный интервал у десятков аккаунтов сам по себе
координационный признак.

СТАЛО: темп считает `flood_engine.recommended_delay(acc, "invite")` — он уже
учитывает базовую ставку действия, ВЫУЧЕННУЮ поправку по прошлым флудам именно
этого аккаунта, хвост активного cooldown, затухание штрафа со временем и
глобальный флотовый темп (pacing_engine). Поверх — `gaussian_delay`, поэтому
интервалы неровные (18/31/24/42…), а не по метроному.

Главная дыра, которую это закрыло: `record_success` в инвайте не вызывался
НИКОГДА. Система умела только наказывать (record_flood), но не реабилитировать —
аккаунт, однажды словивший флуд, оставался «медленным» навсегда, даже отработав
сотни инвайтов без единой проблемы.
"""
from __future__ import annotations

import re
from pathlib import Path

WORKER = Path(__file__).resolve().parents[1] / "services" / "op_worker.py"


def _exec_src() -> str:
    src = WORKER.read_text(encoding="utf-8")
    m = re.search(r"async def _exec_mass_invite\(.*?(?=\nasync def )", src, re.DOTALL)
    assert m, "_exec_mass_invite не найден"
    return m.group(0)


def _inner_fn(name: str) -> str:
    """Тело вложенной функции по отступу (docstring с пустыми строками внутри
    ломает наивный разбор до `\\n\\n` — на этом тест уже спотыкался)."""
    src = _exec_src()
    lines = src.splitlines()
    start = next((i for i, l in enumerate(lines)
                  if re.match(r"\s*(async )?def " + re.escape(name) + r"\(", l)), None)
    assert start is not None, f"{name} не найдена"
    indent = len(lines[start]) - len(lines[start].lstrip())
    out = [lines[start]]
    for l in lines[start + 1:]:
        if l.strip() and (len(l) - len(l.lstrip())) <= indent:
            break
        out.append(l)
    return "\n".join(out)


def test_no_fixed_batch_sleep_left():
    src = _exec_src()
    assert "sleep(_batch_delay)" not in src, (
        "фиксированная пауза между батчами — метроном, палевно и не учитывает "
        "состояние аккаунта"
    )


def test_pause_is_per_account_and_adaptive():
    src = _exec_src()
    assert "recommended_delay" in src, "темп должен считать риск-движок"
    assert "gaussian_delay" in src, "интервалы обязаны быть неровными, а не по метроному"
    # пауза берётся ДЛЯ КОНКРЕТНОГО аккаунта, а не одна на всех
    assert re.search(r"_invite_pause\(\s*(acc\[.id.\]|acc_id)\s*\)", src), (
        "пауза должна вычисляться на аккаунт, а не глобально"
    )


def test_user_pace_is_bias_not_replacement():
    """Режим пользователя не должен отменять расчёт риск-движка."""
    src = _exec_src()
    body = _inner_fn("_invite_pause")
    assert "_pace_mult" in body and "recommended_delay" in body, (
        "режим применяется ПОВЕРХ безопасного темпа, а не вместо него"
    )


def test_learning_loop_closed():
    src = _exec_src()
    assert "record_success" in src, (
        "без record_success система только наказывает и никогда не реабилитирует "
        "аккаунт — он остаётся медленным навсегда"
    )
    # обучение только на реальном успехе, не на пустом батче
    assert "if ok_count <= 0" in _inner_fn("_invite_learn_ok"), (
        "нулевой результат не должен считаться успехом"
    )


def test_learning_not_called_after_flood():
    """Успех фиксируется ПОСЛЕ проверки флуда: словивший флуд батч не должен
    одновременно понижать штраф."""
    src = _exec_src()
    # Блок-цикл = тот, где есть И обработка флуда, И учёт успеха. Первое
    # совпадение по peer_flood лежит внутри самого _rest_invite_account —
    # это не цикл, его отсеиваем.
    blocks = [b for b in re.findall(r'if res\.get\("peer_flood"\).*?_invite_pause', src, re.DOTALL)
              if "_rest_invite_account" in b and "_invite_learn_ok" in b]
    assert len(blocks) == 1, (
        f"после перехода на общую очередь цикл инвайта один, найдено {len(blocks)}"
    )
    block = blocks[0]
    assert block.find("_rest_invite_account") < block.find("_invite_learn_ok"), (
        "порядок должен быть: обработать флуд → выйти; успех учитывать только "
        "если флуда не было"
    )
    head = block.split("_invite_learn_ok")[0]
    assert "continue" in head or "break" in head, (
        "после обработки флуда обязателен выход из итерации — иначе успех "
        "зачтётся тому же батчу"
    )


def test_pause_fail_safe():
    """Сбой риск-движка не должен ронять операцию — откат на прежнюю паузу."""
    src = _exec_src()
    body = _inner_fn("_invite_pause")
    assert "except Exception" in body and "_batch_delay" in body, (
        "нужен fail-safe откат на фиксированную паузу"
    )
