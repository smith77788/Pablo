"""A/B рассылка: варианты текста как первоклассная сущность.

Раньше рассылка слала один текст (spintax давал вариативность против сигнатуры,
но не измерял, ЧТО заходит). A/B — про измерение: аудитория делится на равные
группы, каждой уходит свой вариант, а дальше сравниваем доставку/конверсию и
выбираем победителя со статистической строгостью (двухпропорциональный z-тест —
как в behavioral_engine для авто-заключения экспериментов).

Чистые функции — тестируются без БД и Telegram.
"""
from __future__ import annotations

import math

MAX_VARIANTS = 4
_Z95 = 1.96  # порог значимости (95%)


def clean_variants(variants) -> list[str]:
    """Нормализовать список вариантов: обрезать, убрать пустые, не более MAX."""
    out: list[str] = []
    for v in variants or []:
        s = (v or "").strip()
        if s:
            out.append(s[:4096])
        if len(out) >= MAX_VARIANTS:
            break
    return out


def split_audience(refs: list, n: int) -> list[list]:
    """Разбить получателей на n сбалансированных групп (по одной на вариант).

    Детерминировано (round-robin по порядку) — группы различаются не более чем
    на одного получателя, поэтому доли честные.
    """
    n = max(1, int(n))
    groups: list[list] = [[] for _ in range(n)]
    for i, r in enumerate(refs or []):
        groups[i % n].append(r)
    return groups


def _two_prop_z(n_a: int, c_a: int, n_b: int, c_b: int) -> float:
    """Z-статистика разницы конверсий двух вариантов. 0.0 при нехватке данных."""
    if n_a <= 0 or n_b <= 0:
        return 0.0
    p_a, p_b = c_a / n_a, c_b / n_b
    p = (c_a + c_b) / (n_a + n_b)
    denom = math.sqrt(p * (1 - p) * (1 / n_a + 1 / n_b))
    if denom == 0:
        return 0.0
    return (p_a - p_b) / denom


def plan_winner_followup(variants: list[dict], winner_label: str) -> dict:
    """Follow-up по победителю: кому дослать ПОБЕДИВШИЙ текст.

    variants: [{"label": str, "text": str, "recipients": [ref, ...]}, ...] —
    по одному на A/B-вариант (получатели каждой группы + её текст).

    Логика: получатели проигравших групп «потрачены» на худшую копию —
    дошлём им текст победителя. Тех, кто уже в группе победителя, НЕ трогаем
    (у них и так лучший вариант — повтор был бы спамом). Дедуп, стабильный
    порядок.

    Возвращает {"winner_text": str|None, "targets": [ref, ...],
    "loser_variants": int, "reason": str|None}. Если победитель не найден или
    его текст пуст — targets пуст и reason объясняет почему.
    """
    winner_label = (winner_label or "").strip()
    if not winner_label:
        return {"winner_text": None, "targets": [], "loser_variants": 0,
                "reason": "победитель не определён"}
    winner_text = None
    winner_recips: set = set()
    for v in variants or []:
        if (v.get("label") or "").strip() == winner_label:
            winner_text = (v.get("text") or "").strip() or None
            for r in v.get("recipients") or []:
                if r:
                    winner_recips.add(r)
    if not winner_text:
        return {"winner_text": None, "targets": [], "loser_variants": 0,
                "reason": "у победителя нет текста для рассылки"}
    seen: set = set(winner_recips)
    targets: list = []
    loser_variants = 0
    for v in variants or []:
        if (v.get("label") or "").strip() == winner_label:
            continue
        loser_variants += 1
        for r in v.get("recipients") or []:
            if r and r not in seen:
                seen.add(r)
                targets.append(r)
    reason = None if targets else "нет получателей проигравших вариантов"
    return {"winner_text": winner_text, "targets": targets,
            "loser_variants": loser_variants, "reason": reason}


def pick_winner(stats: list[dict], min_sample: int = 30) -> dict:
    """Определить победителя A/B по конверсии.

    stats: [{"variant": i, "sent": n, "converted": c}, ...].
    Возвращает {"winner": idx|None, "confident": bool, "rates": {i: rate},
    "leader": idx|None}. confident=True — только при достаточной выборке у обоих
    лидеров И значимом отрыве (|z| ≥ 1.96). Иначе leader — просто текущий лидер
    по доле (без статуверенности).
    """
    rows = [s for s in (stats or []) if int(s.get("sent") or 0) > 0]
    rates = {int(s["variant"]): (int(s["converted"] or 0) / int(s["sent"]))
             for s in rows}
    if not rows:
        return {"winner": None, "confident": False, "rates": {}, "leader": None}
    rows.sort(key=lambda s: rates[int(s["variant"])], reverse=True)
    leader = int(rows[0]["variant"])
    if len(rows) == 1:
        return {"winner": None, "confident": False, "rates": rates, "leader": leader}
    a, b = rows[0], rows[1]
    n_a, c_a = int(a["sent"]), int(a["converted"] or 0)
    n_b, c_b = int(b["sent"]), int(b["converted"] or 0)
    z = _two_prop_z(n_a, c_a, n_b, c_b)
    confident = (n_a >= min_sample and n_b >= min_sample and abs(z) >= _Z95)
    return {
        "winner": leader if confident else None,
        "confident": confident,
        "rates": rates,
        "leader": leader,
        "z": round(z, 3),
    }
