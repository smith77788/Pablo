"""Приостановленная DM-кампания возобновляется из списка (не только удаляется).

Тот же класс, что прогрев: paused-состояние без «возобновить». Список DM-кампаний
показывал ▶ только для draft, а paused — лишь 🗑. При этом launch безопасно
возобновляет (dm_engine пропускает уже обработанных из dm_campaign_log — без повторной
рассылки), а бэкенд launch отвергает только running. Фикс: ▶ Возобновить для paused.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api, dm_engine

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_paused_campaign_has_resume_button():
    # в рендере списка кампаний paused получает кнопку возобновления
    assert "c.status==='paused'?" in HTML
    assert "launchDm(${c.id},'paused')" in HTML
    assert "Возобновить" in HTML


def test_launch_backend_allows_non_running():
    src = inspect.getsource(mini_app_api)
    m = re.search(r"async def dm_campaign_launch\(request.*?\n(.*?)\n    async def dm_campaign_delete", src, re.DOTALL)
    assert m, "dm_campaign_launch не найден"
    body = m.group(1)
    # отвергает только running (значит paused/draft — можно)
    assert 'row["status"] == "running"' in body and "уже выполняется" in body


def test_run_campaign_is_resume_safe():
    # dm_engine пропускает уже обработанных → возобновление не шлёт повторно
    src = inspect.getsource(dm_engine)
    assert "dm_campaign_log" in src and "status IN ('sent'" in src
    assert "not in sent_ids" in src
