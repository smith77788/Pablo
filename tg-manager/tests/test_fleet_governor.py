"""Глобальный губернатор темпа: давление флота → множитель паузы + кэш.

Проверяем отображение score→множитель, уровни, кэш (не дёргает БД чаще TTL),
инвалидацию и что op_worker масштабирует ТОЛЬКО базовую паузу (не flood).
"""
from __future__ import annotations

import asyncio

from services import fleet_governor as fg


def test_multiplier_steps():
    assert fg.multiplier_for_score(0) == 1.0
    assert fg.multiplier_for_score(24) == 1.0
    assert fg.multiplier_for_score(25) == 1.2
    assert fg.multiplier_for_score(45) == 1.6
    assert fg.multiplier_for_score(70) == 2.5
    assert fg.multiplier_for_score(95) == 4.0


def test_levels():
    assert fg.level_for_multiplier(1.0) == "green"
    assert fg.level_for_multiplier(1.6) == "amber"
    assert fg.level_for_multiplier(2.5) == "red"
    assert fg.level_for_multiplier(4.0) == "red"


class _CountingPressure:
    """Считает вызовы compute_pressure — для проверки кэша."""
    def __init__(self, score):
        self.score = score
        self.calls = 0


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_multiplier_cached(monkeypatch):
    fg.invalidate(42)
    counter = _CountingPressure(70)

    async def fake_compute(pool, owner_id):
        counter.calls += 1
        return {"score": counter.score}

    import services.infra_pressure as ip
    monkeypatch.setattr(ip, "compute_pressure", fake_compute)

    m1 = _run(fg.tempo_multiplier(None, 42))
    m2 = _run(fg.tempo_multiplier(None, 42))   # из кэша
    assert m1 == 2.5 and m2 == 2.5
    assert counter.calls == 1                   # второй раз БД не трогали
    fg.invalidate(42)
    _run(fg.tempo_multiplier(None, 42))
    assert counter.calls == 2                    # после сброса — снова посчитали


def test_status_shape(monkeypatch):
    fg.invalidate(7)

    async def fake_compute(pool, owner_id):
        return {"score": 50, "level_label": "Повышенное", "level_emoji": "🟠", "breakdown": {}}

    import services.infra_pressure as ip
    monkeypatch.setattr(ip, "compute_pressure", fake_compute)
    st = _run(fg.status(None, 7))
    assert st["score"] == 50 and st["multiplier"] == 1.6 and st["level"] == "amber"
    assert "explain" in st and st["emoji"] == "🟠"


def test_fail_open_to_one(monkeypatch):
    fg.invalidate(9)

    async def boom(pool, owner_id):
        raise RuntimeError("db down")

    import services.infra_pressure as ip
    monkeypatch.setattr(ip, "compute_pressure", boom)
    assert _run(fg.tempo_multiplier(None, 9)) == 1.0   # не роняем операции


def test_op_worker_scales_base_delay(monkeypatch):
    """op_worker._governed_delay растягивает базовую паузу на множитель губернатора."""
    from services import op_worker

    async def fake_mult(pool, owner_id):
        return 2.5

    # _governor_mult делает `from services import fleet_governor` — патчим модуль.
    monkeypatch.setattr(fg, "tempo_multiplier", fake_mult)
    out = _run(op_worker._governed_delay(None, 1, 10.0))
    assert out == 25.0



def test_explain_uses_russian_decimal_comma():
    """«×1,5», а не «×1.5»: строка уходит прямо в баннер мини-аппа и в бота.

    Владелец не читает по-английски, и десятичная точка в русском тексте —
    такая же чужая деталь, как латинская буква. Тот же класс, что «12.8K»
    вместо «12,8К» в карточках счётчиков.
    """
    assert fg._mult_ru(1.5) == "1,5"
    assert fg._mult_ru(2.0) == "2"          # целое без хвоста «,0»
    for level in ("amber", "red"):
        txt = fg._explain(level, 1.5)
        assert "×1,5" in txt, txt
        assert "1.5" not in txt, txt


def test_governor_bar_prints_no_undefined():
    """Баннер не печатает «×undefined · давление undefined/100».

    Частичный ответ сервера (поле не пришло) давал владельцу именно эту
    строку, а он читает её как поломку, а не как «нет данных» — ровно то же,
    что исправили в num() с «NaN». Честный прочерк вместо сырого undefined.
    """
    import pathlib
    import re
    html = (pathlib.Path(__file__).resolve().parents[1]
            / "mini_app" / "index.html").read_text(encoding="utf-8")
    i = html.index("async function loadGovernorBar(")
    # искать следующее объявление надо ПОСЛЕ заголовка, иначе regex
    # совпадёт с ним же и срез окажется пустым (тест был бы вечно зелёным).
    nxt = re.compile(r"^(?:async )?function \w+\(", re.M).search(html, i + 20)
    body = html[i:nxt.start() if nxt else len(html)]
    assert len(body) > 300, "разбор границ функции сломался"
    assert "×${g.multiplier}" not in body, (
        "множитель печатается сырым полем ответа — при его отсутствии в баннере "
        "окажется «×undefined»")
    assert "${g.score}/100" not in body, "давление печатается сырым полем ответа"
    assert "isFinite(g.multiplier)" in body and "isFinite(g.score)" in body, (
        "нет проверки, что пришло число")
    assert "'—'" in body, "нет честного прочерка на случай отсутствующего поля"
