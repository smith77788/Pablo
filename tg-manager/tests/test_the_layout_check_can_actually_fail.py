"""Проверка вёрстки обязана уметь падать.

ЧТО СЛУЧИЛОСЬ. `deploy/scripts/render_miniapp.mjs` — единственный способ в этом
окружении увидеть мини-апп глазами, и он печатал строку `h-overflow: OK`.
Сравнивал он `document.documentElement.scrollWidth` с `window.innerWidth`, а в
этом приложении `html,body{overflow:hidden}` и `.screen{overflow-x:hidden}` —
значит scrollWidth НЕ МОЖЕТ превысить innerWidth никогда, ни на одном экране,
ни при какой вёрстке. Строка печаталась всегда и означала ровно ничего. Проверено
прямо: в показанный экран подставлялся элемент шириной 900px при окне 390 — старая
мерка его не видела.

Беда к тому же другой формы, чем мерили. Слишком широкое содержимое здесь не
прокручивается, а ОБРЕЗАЕТСЯ: владелец не видит правую часть кнопки или числа, и
горизонтальной полосы, по которой это можно было бы заметить, нет. Поэтому
честная мерка смотрит правый край самих элементов внутри показанного экрана, а
не ширину документа, и не считает за обрезку то, что лежит в прокручиваемой
полосе чипов (без этого исключения мерка давала шесть ложных находок).

ЧТО ДЕРЖИТ ЭТА ПРОВЕРКА. Браузер здесь не запускается: обход 114 экранов идёт
отдельным инструментом и занимает минуты. Но два свойства самого инструмента
проверяются статически и дёшево — потому что именно они и подвели:
1) решение об обрезке не принимается по scrollWidth;
2) инструмент на каждом запуске печатает САМОПРОВЕРКУ — подставляет заведомо
   широкий элемент и обязан его найти. Без этой строки «обрезки нет» снова
   станет ничем не подкреплённым обещанием.
3) список экранов строится из исходника и не обмелел.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
_HARNESS = ROOT / "deploy" / "scripts" / "render_miniapp.mjs"
_OPENERS = ROOT / "deploy" / "scripts" / "miniapp_screen_openers.py"


def _src() -> str:
    return _HARNESS.read_text(encoding="utf-8")


def test_the_verdict_is_not_taken_from_scroll_width():
    """Сравнение scrollWidth с innerWidth в этом приложении тождественно ложно."""
    src = _src()
    verdicts = re.findall(
        r"documentElement\.scrollWidth\s*>\s*window\.innerWidth", src)
    # Одно упоминание разрешено — в самопроверке, где ПОКАЗЫВАЕТСЯ, что прежняя
    # мерка заведомо обрезанный элемент не видит. Решение по нему не принимается.
    assert len(verdicts) <= 1, (
        "решение об обрезке снова принимается по scrollWidth — в этом "
        "приложении это сравнение тождественно ложно")
    for line in src.split("\n"):
        if "documentElement.scrollWidth" not in line:
            continue
        bare = line.strip()
        # Пояснения в комментариях упоминают прежнюю мерку по делу — она тут и
        # разобрана; проверяется только исполняемый код.
        if bare.startswith(("*", "//", "/*", "#")):
            continue
        assert "old" in line, (
            "scrollWidth используется не как показ слепоты прежней мерки: "
            + bare[:90])


def test_the_measure_looks_at_the_elements_right_edge():
    src = _src()
    assert "getBoundingClientRect" in src and "r.right > W" in src, (
        "мерка больше не смотрит правый край элементов — а обрезку видно "
        "только по нему")
    assert "overflowX" in src, (
        "исключение прокручиваемой полосы чипов пропало: вернутся шесть "
        "ложных находок")


def test_every_run_prints_its_own_self_check():
    src = _src()
    assert "самопроверка мерки" in src, (
        "инструмент перестал печатать самопроверку — «обрезки нет» снова "
        "станет обещанием, а не проверкой")
    assert "МЕРКА СЛЕПА" in src, (
        "ослепшая мерка больше не говорит об этом вслух")


def test_the_screen_list_is_built_from_the_source_and_did_not_shrink():
    out = subprocess.run([sys.executable, str(_OPENERS)], capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    names = [n for n in out.stdout.split("\n") if n.strip()]
    assert len(names) >= 100, (
        f"список экранов обмелел до {len(names)} — скорее всего сломан разбор, "
        "а не приложение; прежний режим проверял пять вкладок, и возврат к "
        "этому числу надо замечать")
    assert len(set(names)) == len(names), "в списке повторы"
    assert "openSettings" in names and "openDashboard" in names


def test_the_sweep_mode_exists_and_reports_its_findings():
    src = _src()
    assert "sweep" in src and "экранов проверено" in src
    assert "process.exitCode = 1" in src, (
        "обход с находками завершается успехом — находки останутся "
        "незамеченными в любом автоматическом запуске")
