"""Ядро, шаг 3: связка «аудитория → операция» (не острова).

Раньше после парсинга аудитории был тупик: результат можно было только
ЭКСПОРТировать в файл и вручную перенести. Прямой кнопки «пустить эту аудиторию в
рассылку» не было; композер DM хоть и умел target_type=parsed_audience, но всегда
слал target_id=null → рассылка шла по ВСЕЙ аудитории парсера, а не по выбранному
запуску (хотя backend это поддерживал).

Фикс: на завершённом запуске парсера — кнопка «📨 В рассылку» → композер
предвыбирается на parsed_audience с parse_run_id этого запуска; submit шлёт
target_id=run; backend и считает, и ОТПРАВЛЯЕТ по этому parse_run_id (счётчик =
факт).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _index() -> str:
    return (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_parser_run_has_send_action():
    html = _index()
    # на завершённом запуске — кнопка «в рассылку» → startCampaignFromParse(run.id)
    assert "startCampaignFromParse(${r.id})" in html
    assert "📨 В рассылку" in html


def test_bridge_preselects_run_and_submit_sends_target_id():
    html = _index()
    m = re.search(r"function startCampaignFromParse\(runId\)\s*\{(.*?)\n\}", html, re.DOTALL)
    assert m, "startCampaignFromParse не найдена"
    body = m.group(1)
    assert "openCmpModal()" in body, "должна открыть композер"
    assert "CMP_PARSE_RUN = runId" in body, "должна запомнить выбранный запуск"
    assert "'parsed_audience'" in body, "должна предвыбрать target_type=parsed_audience"
    # submit шлёт target_id=parse_run_id именно для parsed_audience
    assert "target_type==='parsed_audience' && CMP_PARSE_RUN" in html
    assert "payload.target_id = CMP_PARSE_RUN" in html
    # openCmpModal сбрасывает выбор (чтобы обычная рассылка не унаследовала run)
    m2 = re.search(r"function openCmpModal\(\)\s*\{(.*?)\n\}", html, re.DOTALL)
    assert m2 and "CMP_PARSE_RUN = null" in m2.group(1)


def test_backend_send_filters_by_parse_run_id():
    """Ключ для консистентности счётчик=факт: движок отправки фильтрует получателей
    по parse_run_id, а не шлёт всем."""
    eng = _read("services/dm_engine.py")
    # именно ветка резолва получателей (не pace-словарь с тем же ключом)
    i = eng.index('target_type == "parsed_audience"')
    seg = eng[i:i + 1500]
    assert "parse_run_id=$2" in seg, "отправка должна фильтровать по parse_run_id"


def test_backend_count_matches_target_run():
    """Счётчик total_targets для parsed_audience тоже считает по parse_run_id."""
    api = _read("services/mini_app_api.py")
    assert ("FROM parsed_audiences WHERE owner_id=$1 AND parse_run_id=$2" in api), (
        "total_targets для выбранного запуска должен считаться по parse_run_id"
    )
