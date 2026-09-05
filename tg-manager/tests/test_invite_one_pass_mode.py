"""Инвайт: режим «один проход» — не гадать суточный лимит, а идти до потолка.

«Продумай лучше»: главный блокатор одного прохода был не суточный лимит, а мёртвые
аккаунты (AUTH_KEY) — это уже починено. Остаточный источник дробления «на завтра» —
КОНСЕРВАТИВНЫЙ предсказанный суточный лимит (_acc_budget всегда клампит к
recommended_daily_limit и возвращает 0, когда предсказанный остаток исчерпан).

Конкуренты не гадают: гонят до РЕАЛЬНЫХ сигналов (FloodWait/PeerFlood) и ротируют.
У нас эти сигналы + стоп-кран флота (flood_storm) уже есть. Режим one_pass заменяет
гадание на реакцию по факту: budget = потолок ёмкости (_INVITE_LIMIT_CEILING), без
клампа к предсказанию; защиту держат живые флуды + circuit breaker. Осознанный
опт-ин с явным предупреждением о риске.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
# Мини-апп больше не один файл: экраны вынесены в mini_app/screens/*.js.
# Источник берём целиком, иначе вынос экрана роняет проверку, хотя
# функциональность на месте.
from tests.miniapp_source import miniapp_source

HTML = miniapp_source()


def _exec_body() -> str:
    m = re.search(r"async def _exec_mass_invite\(.*?(?=\nasync def )", WORKER, re.DOTALL)
    assert m
    return m.group(0)


def test_reads_one_pass_param():
    body = _exec_body()
    assert '_one_pass = bool(params.get("one_pass"))' in body


def test_budget_bypasses_prediction_but_keeps_ceiling():
    body = _exec_body()
    # в режиме one_pass не клампим к recommended, но потолок _INVITE_LIMIT_CEILING держим
    seg = body[body.index("_one_pass"):]
    seg = seg[seg.index("_acc_budget"):]  # тело _acc_budget
    assert "_INVITE_LIMIT_CEILING" in seg, "потолок ёмкости обязан оставаться (защита)"
    assert "min(int(_cap), _ceil)" in seg, "нельзя выше потолка даже в one_pass"
    # и это до обычного расчёта recommended_daily_limit
    i_one = seg.index("if _one_pass:")
    i_rec = seg.index("recommended_daily_limit")
    assert i_one < i_rec, "one_pass-ветка должна обходить предсказанный лимит"


def test_real_signals_still_guard():
    """Защита не снимается: стоп-кран флота и per-account cooldown остаются."""
    body = _exec_body()
    assert "flood_storm" in body, "circuit breaker флота обязан работать и в one_pass"
    assert "_rest_invite_account" in body, "per-account cooldown на флуде остаётся"


def test_submit_and_preflight_carry_one_pass():
    assert 'params["one_pass"] = True' in API, "submit должен пробрасывать one_pass"
    # preflight считает ёмкость по потолку в режиме one_pass
    m = re.search(r"async def invite_preflight\(.*?\n(.*?)\n    async def ", API, re.DOTALL)
    pf = m.group(1)
    assert "one_pass" in pf and "_INVITE_LIMIT_CEILING" in pf, (
        "preflight в режиме one_pass должен оценивать ёмкость по потолку"
    )


def test_ui_toggle_with_risk_warning():
    assert 'id="massInviteOnePass"' in HTML, "нет тумблера «один проход»"
    assert "body.one_pass = true" in HTML, "submitMassInvite должен слать one_pass"
    assert "Выше риск бана" in HTML or "выше риск бана" in HTML, (
        "режим должен нести явное предупреждение о риске"
    )
