"""Потолок: число наборов, прибитых к пути `services/mini_app_api.py`.

API-слой мини-аппа пилится на `services/mini_app_*.py`. Проверка, которая
читает монолит ПО ПУТИ, после выноса ручки краснеет не потому, что
функциональность пропала, а потому, что она переехала — и каждый вынос стоит
отдельного красного прогона в CI. За одну сессию так упало четыре раза:
боты, инвайты с каналами, операции, дашборд.

Хуже другого рода случай: не покраснеть. Храповик
`test_miniapp_path_param_scope_ratchet` — защита от самого дорогого бага
мультиарендного продукта — читал один файл и к моменту этой проверки видел
113 ручек из 172. Треть поверхности была вне защиты, и узналось это не находкой,
а тем, что собственная самопроверка храповика не дотянула до порога.

Поэтому: новые наборы берут источник из `tests.miniapp_source` (`api_source`,
`api_function`, `api_routes`, `api_files`), а число старых обязано только
убывать. Потолок — не список имён: он честно сторожит ровно рост, и его
достаточно, чтобы долг не увеличивался. Уменьшили — опустите число здесь же,
тем же коммитом.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# Снято 15.10.2026, после выноса дашборда: 55 наборов. Только вниз.
CEILING = 55

PIN = re.compile(r'["\']services[/\\]mini_app_api\.py["\']')


def _pinned() -> list[str]:
    out = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == "miniapp_source.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if PIN.search(text):
            out.append(path.name)
    return out


def test_the_detector_still_finds_the_pins():
    """Самопроверка: пустой список — это сломанный измеритель, а не чистота."""
    assert _pinned(), "ни одного набора с путём к монолиту — проверка измеряет не то"


def test_the_number_of_pinned_test_files_only_shrinks():
    pinned = _pinned()
    assert len(pinned) <= CEILING, (
        f"наборов, прибитых к пути монолита, стало {len(pinned)} вместо "
        f"{CEILING}. Новая проверка про API-слой обязана брать источник из "
        f"tests.miniapp_source (api_source / api_function / api_routes), иначе "
        f"она ослепнет при первом же выносе ручки.\n  "
        + "\n  ".join(pinned[-12:]))
    if len(pinned) < CEILING:
        raise AssertionError(
            f"долг уменьшился до {len(pinned)} — опустите CEILING в этом файле "
            f"тем же коммитом, иначе потолок перестанет сторожить рост")
