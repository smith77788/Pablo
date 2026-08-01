"""Регрессия: у Strike в мини-аппе не было нужных настроек — «нету настроек».

Жалоба: у модулей не хватает настроек, которые есть у конкурентов. Движок Strike
(_exec_strike) читает из params `min_restrike_hours` (анти-детект: не бить одну цель
чаще интервала) и `force` (сознательный обход интервала), но strike_launch слал
только target/reason/num_waves/account_ids. Итог: когда цель атакована в последние
Nч, движок возвращал «повтор пропущен», а пользователь НЕ мог ни изменить интервал,
ни форсировать повтор — тупик без единой ручки.

Фикс: strike_launch пробрасывает min_restrike_hours (0..168) и force в params;
UI (Шаг 3) даёт поле интервала и чекбокс форса; strikeLaunch кладёт их в payload.

Тесты падают без фикса: раньше этих ключей не было ни в params, ни в UI.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api, op_worker


def _api_src() -> str:
    return inspect.getsource(mini_app_api)


def _launch_body() -> str:
    src = _api_src()
    m = re.search(r"async def strike_launch\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "strike_launch handler не найден"
    return m.group(1)


def _index_html() -> str:
    p = Path(__file__).resolve().parent.parent / "mini_app" / "index.html"
    return p.read_text(encoding="utf-8")


def test_launch_reads_restrike_and_force_from_body():
    body = _launch_body()
    assert 'body.get("min_restrike_hours"' in body, (
        "strike_launch должен читать min_restrike_hours из тела запроса"
    )
    assert 'body.get("force")' in body, "strike_launch должен читать force из тела"
    # интервал зажат в разумные рамки (0..168ч), 0 = без ограничения
    assert "min(168" in body and "max(0" in body, (
        "min_restrike_hours должен быть ограничен 0..168"
    )


def test_launch_passes_restrike_and_force_into_params():
    body = _launch_body()
    # оба ключа должны попасть в params операции (иначе движок их не увидит)
    assert '"min_restrike_hours": min_restrike_hours' in body, (
        "min_restrike_hours должен уходить в params операции"
    )
    assert '"force": force' in body, "force должен уходить в params операции"


def test_engine_actually_consumes_these_params():
    """Связь «настройка доходит до эффекта»: _exec_strike обязан читать оба ключа —
    иначе ручка в UI была бы «тихой» (крутится, ни на что не влияет)."""
    src = inspect.getsource(op_worker._exec_strike)
    assert 'params.get("min_restrike_hours"' in src, (
        "_exec_strike должен читать min_restrike_hours (иначе настройка мёртвая)"
    )
    assert 'params.get("force")' in src, "_exec_strike должен читать force"


def test_ui_has_restrike_and_force_controls():
    html = _index_html()
    assert 'id="strikeRestrikeH"' in html, "нет поля интервала повтора в UI"
    assert 'id="strikeForce"' in html, "нет чекбокса форса в UI"
    # и strikeLaunch должен класть их в payload
    assert "min_restrike_hours" in html and "_payload.force" in html, (
        "strikeLaunch должен передавать интервал и force в payload"
    )
