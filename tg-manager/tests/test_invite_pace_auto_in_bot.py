"""Режим темпа «авто» доступен на обеих поверхностях, а не только в мини-аппе.

ЧТО БЫЛО СЛОМАНО. flood_engine.auto_strategy подбирает темп по состоянию ВСЕГО
флота за сегодня и пересматривает его на каждом прогоне — в отличие от ручных
slow/normal/fast, которые оператор выбирает вслепую, не зная, сколько флудов флот
словил за последний час. Исполнитель этот режим поддерживает, мини-апп его
предлагает — а в боте, главной поверхности продукта, кнопки просто не было.

Плюс подпись режима бралась как `{...}[pace]`: значение приходит из состояния
FSM, которое переживает рестарты и обновления, и незнакомая строка роняла бы
хендлер KeyError'ом уже ПОСЛЕ того, как пользователь всё настроил.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "bot" / "handlers" / "mass_inviter.py").read_text(
    encoding="utf-8")


def test_auto_pace_button_exists():
    assert 'InviterCb(action="setpace", item="auto")' in SRC, (
        "в боте нет кнопки авто-темпа, хотя движок и мини-апп его поддерживают"
    )


def test_auto_pace_survives_validation():
    m = re.search(r'pace = \(?callback_data\.item[^\n]*\n?[^\n]*', SRC)
    assert m and '"auto"' in m.group(0), (
        "выбор «авто» должен проходить валидацию, а не молча становиться normal"
    )


def test_pace_label_lookup_cannot_raise():
    assert re.search(r'_pace_ru = \{[^}]*\}\.get\(pace', SRC, re.S), (
        "подпись темпа обязана браться через .get — значение приходит из FSM"
    )
    assert not re.search(r'_pace_ru = \{[^}]*\}\[pace\]', SRC, re.S)


def test_all_offered_paces_have_labels():
    """Каждый режим с кнопкой должен иметь подпись — иначе экран подтверждения
    покажет сырой код режима."""
    offered = set(re.findall(r'action="setpace", item="([a-z]+)"', SRC))
    labels = re.search(r'_pace_ru = \{(.*?)\}\.get\(pace', SRC, re.S).group(1)
    labelled = set(re.findall(r'"([a-z]+)":', labels))
    assert offered <= labelled, f"без подписи остались режимы: {sorted(offered - labelled)}"


def test_executor_understands_every_offered_pace():
    """Режим, который можно выбрать, обязан что-то значить для исполнителя."""
    worker = (Path(__file__).resolve().parents[1] / "services" / "op_worker.py").read_text(
        encoding="utf-8")
    i = worker.index("async def _exec_mass_invite")
    seg = worker[i:worker.index("\nasync def _exec_", i + 1)]
    known = set(re.findall(r'"(slow|normal|fast)":', seg)) | {"auto"}
    offered = set(re.findall(r'action="setpace", item="([a-z]+)"', SRC))
    assert offered <= known, f"исполнитель не знает режимы: {sorted(offered - known)}"
