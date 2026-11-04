"""Текст обрезается ДО экранирования, а не после.

`html.escape(v)[:200]` режет посреди «&amp;» / «&lt;»: Telegram отвечает
«can't parse entities» и не принимает сообщение целиком. Подписчик, ответивший
строкой из «&» или «<», так глушил карточку заявки в чат отдела продаж —
заявка терялась. Падает без фикса.
"""
from __future__ import annotations

import re
from pathlib import Path

from services import workflow_runtime as rt

ROOT = Path(__file__).resolve().parents[1]
_BROKEN = re.compile(r"&(?!(?:amp|lt|gt|quot|#x27);)")
_CUT_AFTER = re.compile(r"escape\([^()]*(?:\([^()]*\))?[^()]*\)\[:\d+\]")


def test_lead_card_has_no_half_entities():
    for junk in ("&" * 300, "<" * 300, "a" + "&" * 250):
        card = rt._lead_card("Заявка", {"first_name": "x"}, 1, {"answer": junk})
        assert not _BROKEN.search(card), card[-40:]


def test_no_code_cuts_escaped_text():
    bad = []
    for base in ("services", "bot"):
        for p in (ROOT / base).rglob("*.py"):
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if _CUT_AFTER.search(line):
                    bad.append(f"{p.relative_to(ROOT)}:{i}")
    assert not bad, "обрезать до escape(), не после: " + ", ".join(bad)
