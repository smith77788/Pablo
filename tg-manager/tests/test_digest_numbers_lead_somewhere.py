"""Пульс-отчёт: каждое число ведёт туда, где с ним можно что-то сделать.

Экран был сводкой цифр, по которой нельзя нажать: «Мёртвых 4», «Ошибок за 24ч 7»
— и дальше человек искал эти строки по приложению руками. Плюс в отчёт утекал
английский (уровень губернатора `green`) и сырой тип операции (`mass_invite`).

Тест держит контракт: у каждой метрики отчёта есть стабильный `id`, у каждого id
— экран в `DIG_GO`, и этот экран реально существует.
"""
from __future__ import annotations

import os
import re

from services.organism.digest import compose_digest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()


def _snap(**over):
    base = {
        "fleet": {"accounts": 10, "active": 8, "dead": 2, "bans_24h": 1,
                  "pressure": 30, "governor_mult": 0.8, "governor_level": "orange"},
        "ops": {"running": 1, "pending": 2, "failed_24h": 7,
                "last_failed": {"op_type": "mass_invite", "reason": "PEER_FLOOD"}},
        "graph": {"contacts": 100, "hot_leads": 5, "intents_24h": 3},
        "growth": {"channels": 4, "growth_ops_7d": 2},
        "bots": {"total": 3, "active": 3, "community_nodes": 1, "community_empty": 2},
        "seo": {"scored": 4, "weak": 3},
        "vault": {"health": "ok", "stale_days": 0, "waiting_reply": 6},
    }
    base.update(over)
    return base


def _all_stats():
    d = compose_digest(_snap(), suggestions=[])
    return [(sec["key"], st) for sec in d["sections"] for st in sec["stats"]]


def _dig_go() -> dict:
    """Карта id → экран из мини-аппа."""
    m = re.search(r"const DIG_GO = \{(.*?)\n\};", HTML, re.S)
    assert m, "в мини-аппе нет карты DIG_GO"
    out = {}
    for key, fn in re.findall(r"(\w+):\s*\{fn:\s*(\"[^\"]+\"|'[^']+')", m.group(1)):
        out[key] = fn.strip("\"'")
    assert out, "карта DIG_GO разобралась пустой — изменился формат"
    return out


def test_every_number_has_stable_id():
    for key, st in _all_stats():
        assert st.get("id"), f"метрика «{st['label']}» в разделе {key} без id — нажать некуда"


def test_every_number_has_a_screen_behind_it():
    go = _dig_go()
    missing = [(k, st["id"]) for k, st in _all_stats() if st["id"] not in go]
    assert not missing, f"числа без экрана: {missing}"


def test_targets_really_exist_in_mini_app():
    """Переход не должен вести в несуществующую функцию."""
    js = HTML
    for fid, call in _dig_go().items():
        name = call.split("(")[0]
        assert re.search(r"(async )?function %s\s*\(" % re.escape(name), js), \
            f"{fid} ведёт в {name}() — такой функции в мини-аппе нет"


def test_stat_row_is_tappable():
    """Строка с известным id рисуется как переход, а не как мёртвая цифра."""
    m = re.search(r"function _digStat\(st\) \{(.*?)\n\}", HTML, re.S)
    assert m, "нет рендера строки отчёта _digStat"
    body = m.group(1)
    assert "DIG_GO[st.id]" in body
    assert 'onclick="${g.fn}"' in body
    assert 'class="chev"' in body          # видно, что это переход
    # Сам экран рисует строки через этот рендер, а не собственной вставкой.
    dig = re.search(r"async function openDigest\(\) \{(.*?)\n\}\n", HTML, re.S).group(1)
    assert "map(_digStat)" in dig


def test_governor_level_is_russian():
    """В отчёт уходил сырой green/orange/red — владелец читает по-русски."""
    d = compose_digest(_snap(), suggestions=[])
    note = next(s for s in d["sections"] if s["key"] == "fleet")["note"]
    assert "orange" not in note and "green" not in note and "red" not in note
    assert "умеренно" in note


def test_last_failure_is_structured_and_translated():
    """Тип операции переводится в мини-аппе через opRu, а не уходит сырым."""
    d = compose_digest(_snap(), suggestions=[])
    risks = next(s for s in d["sections"] if s["key"] == "risks")
    assert risks["fail"] == {"op_type": "mass_invite", "reason": "PEER_FLOOD"}
    assert "mass_invite" not in (risks.get("note") or "")
    dig = re.search(r"async function openDigest\(\) \{(.*?)\n\}\n", HTML, re.S).group(1)
    assert "opRu(f.op_type)" in dig


def test_tracked_metrics_are_actually_shown():
    """Отчёт считал тренды по метрикам, которых не показывал ни одной строкой."""
    from services.organism.digest import _TREND_METRICS
    shown = {st["id"] for _, st in _all_stats()}
    unseen = set(_TREND_METRICS) - shown
    assert unseen <= {"retained"}, f"метрики считаются для трендов, но не видны: {unseen}"
    assert {"seo_weak", "waiting_reply"} <= shown


def test_ops_filter_jump_exists():
    """Число ошибок ведёт в очередь сразу на нужном срезе."""
    m = re.search(r"function opsGo\(status\) \{(.*?)\n\}", HTML, re.S)
    assert m, "нет перехода в очередь операций на срезе"
    body = m.group(1)
    assert "OPS_FILTER = status" in body
    assert "push('s-ops')" in body
    assert "loadOps()" in body


def test_digest_without_recommendations_is_not_a_dead_end():
    dig = re.search(r"async function openDigest\(\) \{(.*?)\n\}\n", HTML, re.S).group(1)
    tail = dig[dig.index("recs.length"):]
    assert "Что дальше" in tail
    assert "openOps()" in tail
