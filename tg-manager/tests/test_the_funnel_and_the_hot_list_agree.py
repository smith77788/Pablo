"""Воронка на экране и список «кого дожимать» считали по разным правилам.

ОДИН ЭКРАН, ДВА ЧИСЛА. Сводка слоя (`virtual_layer.overview`) отдаёт
распределение воронки и список «кого дожимать первыми». Список просроченные
состояния отбрасывает — и правильно: просроченный «готов купить» это уже не
«готов», и человек, пропавший три недели назад, стоял в этом списке первым.
А распределение считалось по СЫРОМУ значению, без оглядки на срок.

В итоге на одном экране из одной таблицы получались два несогласных числа:
полоска «Готов купить: 12» и пустой список под ней. По воронке планируют
работу — она показывала аудиторию, которой уже нет.

Распад доводит строки до правды сам, но идёт пачками и раз в 15 минут: экран
обязан показывать правду сейчас, а не к следующему проходу. Складываем через
ту же дверь, что и каскад температуры (`effective_value`), иначе воронка и
температура под ней снова разойдутся.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from services import virtual_layer as vl


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


NOW = datetime.now(timezone.utc)


class _Pool:
    """Пул сводки: раздаёт ответы по виду запроса, а не по порядку вызовов."""

    def __init__(self, states):
        self.states = states

    async def fetch(self, sql, *a):
        if "GROUP BY value" in sql:
            agg: dict[tuple[str, bool], int] = {}
            for st in self.states:
                expired = (st["expires_at"] is not None
                           and st["expires_at"] <= NOW)
                key = (st["value"], expired)
                agg[key] = agg.get(key, 0) + 1
            return [{"value": v, "expired": e, "c": c}
                    for (v, e), c in agg.items()]
        if "value = ANY" in sql:
            wanted = set(a[2])
            return [{"entity_type": vl.USER, "entity_id": st["entity_id"],
                     "value": st["value"], "confidence": 0.8,
                     "updated_at": NOW}
                    for st in self.states
                    if st["value"] in wanted
                    and (st["expires_at"] is None or st["expires_at"] > NOW)]
        return []

    async def fetchrow(self, sql, *a):
        return None


def _state(entity_id: str, value: str, *, expired: bool):
    return {"entity_id": entity_id, "value": value,
            "expires_at": (NOW - timedelta(hours=1) if expired
                           else NOW + timedelta(hours=1))}


def _funnel(out) -> dict[str, int]:
    return {row["value"]: row["count"] for row in out["funnel"]}


def test_an_expired_ready_contact_is_not_counted_as_ready():
    out = _run(vl.overview(_Pool([_state("a", "ready", expired=True)]), 7))
    assert _funnel(out).get("ready", 0) == 0, (
        "в воронке «готов купить» стоит человек, чьё состояние просрочено")


def test_the_expired_contact_lands_one_rung_lower():
    """Он не исчезает из воронки — он остыл на один рунг, как при распаде."""
    out = _run(vl.overview(_Pool([_state("a", "ready", expired=True)]), 7))
    assert _funnel(out) == {"qualified": 1}
    assert out["total"] == 1, "человек пропал из воронки целиком"


def test_the_funnel_and_the_hot_list_agree():
    states = [_state("a", "ready", expired=False),
              _state("b", "ready", expired=True),
              _state("c", "qualified", expired=False)]
    out = _run(vl.overview(_Pool(states), 7))
    funnel = _funnel(out)
    hot_ready = sum(1 for h in out["hot"] if h["value"] == "ready")
    assert funnel.get("ready", 0) == hot_ready == 1, (
        f"воронка говорит {funnel.get('ready', 0)}, список — {hot_ready}")


def test_a_fresh_funnel_is_untouched():
    """Самопроверка пробника: живые состояния не трогаем."""
    states = [_state("a", "ready", expired=False),
              _state("b", "interested", expired=False)]
    out = _run(vl.overview(_Pool(states), 7))
    assert _funnel(out) == {"interested": 1, "ready": 1}


def test_the_bottom_of_the_ladder_does_not_fall_through():
    out = _run(vl.overview(_Pool([_state("a", "new", expired=True)]), 7))
    assert _funnel(out) == {"new": 1}


def test_a_terminal_state_is_not_cooled_by_a_stale_row():
    """«Купил» и «потерян» срока не имеют — остужать их нечем."""
    for value in ("purchased", vl.LOST):
        state = {"entity_id": "a", "value": value, "expires_at": None}
        out = _run(vl.overview(_Pool([state]), 7))
        assert _funnel(out) == {value: 1}, value


def test_the_funnel_uses_the_same_door_as_the_cascade():
    """Храповик: оба считают через effective_value, а не каждый по-своему."""
    import inspect

    src = inspect.getsource(vl.overview)
    assert "effective_value(" in src, (
        "сводка снова считает воронку по сырому значению")
