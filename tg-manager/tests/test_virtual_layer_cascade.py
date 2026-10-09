"""Virtual Layer: каскад аудитории в проде.

Каскад свёртки (много «горячих» контактов → аудитория горячая) написан в ядре
и проверен в test_virtual_layer_core. Здесь — что он реально катится:
recompute_cascade пишет состояние уровня аудитории и эмитит событие только на
смене вердикта, а организм зовёт его редким проходом (не на каждом тике).
"""
from __future__ import annotations

import asyncio
import pathlib

from services import virtual_layer as V
from services.organism import spine

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = (ROOT / "services" / "organism" / "runner.py").read_text(encoding="utf-8")
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
UI = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


class _Pool:
    """Заглушка: дети-контакты + одно состояние аудитории.

    Выборку детей каскад делает АГРЕГАТОМ (строка на «значение + просрочено
    ли», а не на человека: иначе запрос растёт вместе с аудиторией). Заглушка
    поэтому группирует переданные значения так же, как это сделала бы база —
    тест остаётся написанным в терминах детей.
    """
    def __init__(self, child_values, audience=None, expired_values=()):
        self._groups: dict[tuple, int] = {}
        for v in child_values:
            self._groups[(v, False)] = self._groups.get((v, False), 0) + 1
        for v in expired_values:
            self._groups[(v, True)] = self._groups.get((v, True), 0) + 1
        self.audience = audience          # текущее состояние NETWORK/audience
        self.writes = []
        self.rows_served = 0

    async def fetch(self, q, *a):
        if "entity_type=$2 AND state_key=$3" in q and "COUNT(*)" in q:
            rows = [{"value": v, "expired": exp, "c": c}
                    for (v, exp), c in self._groups.items()]
            self.rows_served += len(rows)
            return rows
        return []

    async def fetchrow(self, q, *a):
        if "FROM virtual_states" in q:
            return {"value": self.audience, "confidence": 0.8,
                    "source": "cascade", "expires_at": None} if self.audience else None
        return None

    async def execute(self, q, *a):
        if "INSERT INTO virtual_states" in q:
            self.audience = a[4]
            self.writes.append(a[4])


def setup_function(_):
    spine._clear()


def test_cascade_writes_hot_audience_and_emits_once():
    seen = []
    spine.on("network_became_hot", lambda o, k, p, pool: seen.append(k) or None)
    pool = _Pool(["ready"] * 30 + ["new"] * 70)
    v = asyncio.run(V.recompute_cascade(pool, 7, V.NETWORK, "audience", V.USER,
                                        min_count=20, min_share=0.15))
    assert v == "hot" and pool.audience == "hot"
    assert seen == ["network_became_hot"]


def test_cascade_no_change_is_quiet():
    """Уже «горячая» — повтор не пишет и не шумит."""
    seen = []
    spine.on("*", lambda o, k, p, pool: seen.append(k) or None)
    pool = _Pool(["ready"] * 30 + ["new"] * 70, audience="hot")
    v = asyncio.run(V.recompute_cascade(pool, 7, V.NETWORK, "audience", V.USER,
                                        min_count=20, min_share=0.15))
    assert v == "hot" and pool.writes == [] and seen == []


def test_cascade_below_threshold_returns_none():
    pool = _Pool(["ready"] * 2 + ["new"] * 98)
    assert asyncio.run(V.recompute_cascade(
        pool, 7, V.NETWORK, "audience", V.USER,
        min_count=20, min_share=0.15)) is None


# ── Проводка ───────────────────────────────────────────────────────────────

def test_cascade_is_wired_into_the_organism():
    assert "recompute_cascade" in RUNNER
    assert 'V.NETWORK, "audience"' in RUNNER or '_vl.NETWORK, "audience"' in RUNNER


def test_cascade_runs_on_the_rare_pass_not_every_tick():
    """Каскад привязан к тому же редкому проходу, что прунинг/распад — не
    гонять GROUP BY по всем владельцам каждую минуту."""
    assert "_do_cascade" in RUNNER


def test_audience_temperature_surfaced_to_overview_and_screen():
    assert '"audience": ov.get("audience")' in API
    assert "d.audience" in UI


def test_cascade_does_not_read_a_row_per_person():
    """Строка на человека на каждом сердцебиении — счёт, растущий с аудиторией.

    С подписчиками бота в слое детей у владельца столько, сколько у него
    людей, а каскаду нужны только числа: сколько всего и сколько горячих.
    """
    pool = _Pool(["ready"] * 5000 + ["new"] * 5000)
    v = asyncio.run(V.recompute_cascade(pool, 7, V.NETWORK, "audience", V.USER,
                                        min_count=20, min_share=0.15))
    assert v == "hot"
    assert pool.rows_served <= 4, (
        f"каскад прочитал {pool.rows_served} строк на 10000 детей — "
        "запрос снова не агрегирующий")


def test_an_expired_rung_does_not_heat_the_audience():
    """Просроченный «готов» — уже не «готов»: один рунг вниз, и он не горячий."""
    pool = _Pool([], expired_values=["ready"] * 30)
    assert asyncio.run(V.recompute_cascade(
        pool, 7, V.NETWORK, "audience", V.USER,
        min_count=20, min_share=0.15)) is None
    # А свежие ровно те же тридцать — греют.
    pool2 = _Pool(["ready"] * 30)
    assert asyncio.run(V.recompute_cascade(
        pool2, 7, V.NETWORK, "audience", V.USER,
        min_count=20, min_share=0.15)) == "hot"
