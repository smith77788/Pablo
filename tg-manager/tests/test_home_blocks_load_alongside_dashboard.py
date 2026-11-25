"""Нижние блоки главной грузятся разом с показателями, а не после них.

`loadHome` звал чеклист (`/onboarding`), пульс (`/organism/pulse`) и итоги
(`/summary`) только ПОСЛЕ ответа `/dashboard`, хотя от него они не зависят:
главная ждала два похода к серверу подряд, и нижние блоки появлялись на целый
ответ позже. Каждый из трёх грузчиков сам ловит свои ошибки, поэтому запуск
до `await` ничего не меняет в поведении при сбое.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_home() -> str:
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    body = html[html.index("async function loadHome() {"):]
    return body[:body.index("\nfunction homeLoadState(")]


def test_independent_blocks_start_before_the_dashboard_answer():
    body = _load_home()
    wait = body.index("await api('/api/miniapp/dashboard')")
    for call in ("loadOnboarding()", "loadOrganismPulse()", "loadSummary()"):
        assert call in body, f"{call} пропал с главной"
        assert body.index(call) < wait, f"{call} снова ждёт ответа /dashboard"


def test_blocks_are_started_once_per_open():
    body = _load_home()
    for call in ("loadOnboarding()", "loadOrganismPulse()", "loadSummary()"):
        assert body.count(call) == 1, f"{call} зовётся дважды за одно открытие"
