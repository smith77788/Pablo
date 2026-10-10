"""Гейт: каждое действие нуджа «Пульса» (мозг организма) разворачивается во
фронте — иначе тап по кнопке нуджа молча ничего не открывает.

brain.build_suggestions отдаёт подсказки с action={"kind": ...}. Фронт
разворачивает kind в один тап через runPulseAction() и подписывает кнопку через
pulseActionLabel(). Легко добавить новый kind в мозг и забыть ветку во фронте —
кнопка покажет «Открыть» и по тапу выдаст toast «Открываю…», не открыв ничего.
Гейт ловит расхождение статически.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAIN = (ROOT / "services" / "organism" / "brain.py").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _brain_kinds() -> set[str]:
    return set(re.findall(r'"kind":\s*"([a-z_]+)"', BRAIN))


def _slice(fn_name: str) -> str:
    i = HTML.find(f"function {fn_name}(")
    assert i != -1, f"{fn_name} не найдена во фронте"
    # тело до следующего объявления функции верхнего уровня
    j = HTML.find("\nfunction ", i + 1)
    return HTML[i: j if j != -1 else i + 1500]


def test_every_brain_kind_handled_in_runpulseaction():
    kinds = _brain_kinds()
    assert kinds, "не нашли ни одного kind в brain.py — сломался парсер"
    body = _slice("runPulseAction")
    for k in sorted(kinds):
        assert re.search(rf"k\s*===?\s*'{k}'", body), (
            f"kind '{k}' из мозга не обрабатывается в runPulseAction — мёртвый тап"
        )


def test_every_brain_kind_has_label():
    kinds = _brain_kinds()
    body = _slice("pulseActionLabel")
    for k in sorted(kinds):
        assert re.search(rf"\b{k}\s*:", body), (
            f"kind '{k}' не имеет метки в pulseActionLabel — кнопка будет «Открыть»"
        )
