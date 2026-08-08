"""Контент → публикация: spintax НА КАНАЛ в массовой публикации (anti-detection).

Раньше mass_publish слал ОДИН и тот же текст во все каналы — палевная сигнатура
координации (Telegram это ловит). dm_engine уже спинтил per-recipient, а публикатор
— нет. Фикс: в исполнителе _exec_mass_publish на каждый канал раскрываем spintax
(expand_spintax), без spintax — текст как есть (no-op).
"""
from __future__ import annotations

import inspect

from services import op_worker
from services.dm_engine import expand_spintax


def test_expand_spintax_varies_choice():
    # инструмент реально даёт вариацию (каждый вызов — один из вариантов)
    outs = {expand_spintax("{A|B|C}") for _ in range(50)}
    assert outs <= {"A", "B", "C"} and len(outs) >= 2, "spintax должен варьировать выбор"
    # без spintax — текст без изменений
    assert expand_spintax("обычный текст") == "обычный текст"


def test_mass_publish_applies_spintax_per_channel():
    src = inspect.getsource(op_worker._exec_mass_publish)
    assert "expand_spintax" in src, "публикатор должен применять spintax"
    # свой вариант текста НА КАНАЛ (внутри цикла по каналам)
    assert "_ch_text = _expand_spintax(mp_text)" in src
    # отправляется именно вариант, а не сырой mp_text
    i_loop = src.index("for idx, target_entry")
    after = src[i_loop:]
    # оба вызова post_to_channel в цикле используют _ch_text
    assert after.count("_ch_text") >= 2, "оба send-пути должны слать _ch_text"


def test_ui_hints_spintax_in_mass_publish():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")
    # у поля публикации есть подсказка про Spintax
    i = html.index('id="mpText"')
    seg = html[i:i + 400]
    assert "Spintax" in seg or "spintax" in seg.lower()
