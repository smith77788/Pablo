"""«Не повторять» должно означать «не повторять», а не «повторить трижды».

`max_retries` приходит из `OP_REGISTRY` и честно доезжает до базы: `submit`
пишет `max_retries if max_retries is not None else meta.get(...)`, то есть ноль
сохраняется. Ломались ЧИТАТЕЛИ: `row["max_retries"] or 3` — ноль ложный, и
«ретраев нет» молча превращалось в три автоматических повтора.

Цена видна на том единственном типе, который ретраи отключает осознанно:

    "create_chatlist_folder": {
        # Экспорт не идемпотентен (каждый прогон плодит ссылку) — без ретраев.
        "max_retries": 0,

То есть операция получала ровно то, от чего её автор защищался, причём трижды.
Та же подмена показывала владельцу «Попыток: 1/3» у операции без повторов и
мешала боту считать её исчерпавшей попытки (там проверка `max_retries > 0`).

Найдено прогоном на живом Postgres: падающий исполнитель у операции с
`max_retries=0` уходил в `pending` вместо терминального статуса.
"""
from __future__ import annotations

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Файлы, которые читают предел повторов и решают по нему.
_READERS = (
    "services/op_worker.py",
    "services/recovery_engine.py",
    "bot/handlers/botmother_menu.py",
    "bot/handlers/mass_ops.py",
)


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("rel", _READERS)
def test_no_reader_turns_zero_retries_into_three(rel):
    src = _read(rel)
    bad = [
        m.group(0) for m in re.finditer(
            r'max_retries[^\n]{0,12}\]\s*or\s*[1-9]', src)
    ] + [
        m.group(0) for m in re.finditer(
            r'max_retries"\)\s*or\s*[1-9]', src)
    ]
    assert not bad, (
        f"{rel}: ноль — это «не повторять», а `or` превращает его в число "
        f"по умолчанию: {bad}")


def test_the_detector_sees_the_old_pattern():
    """Детектор, который ничего не ловит, зелёный всегда."""
    sample = 'max_retries = row["max_retries"] or 3\n'
    assert re.search(r'max_retries[^\n]{0,12}\]\s*or\s*[1-9]', sample)
    sample2 = 'max_ret = op.get("max_retries") or 3\n'
    assert re.search(r'max_retries"\)\s*or\s*[1-9]', sample2)


def test_zero_in_the_registry_is_not_an_accident():
    """Если тип перестанет отключать ретраи, правило потеряет единственный повод."""
    src = _read("services/operation_bus.py")
    i = src.index('"create_chatlist_folder"')
    block = src[i:i + 400]
    assert '"max_retries": 0' in block, (
        "никто больше не отключает ретраи — проверьте, нужно ли правило, "
        "прежде чем его убирать")
    assert "не идемпотент" in block, "причина отключения должна оставаться в коде"


def test_the_fallback_still_covers_a_missing_limit():
    """NULL (предел не задан) по-прежнему означает три попытки, а не ноль."""
    src = _read("services/op_worker.py")
    i = src.index("async def _maybe_requeue")
    body = src[i:i + 2500]
    assert "is None" in body and "3" in body, (
        "фолбэк для NULL потерян — операции без заданного предела перестанут "
        "повторяться вообще")
