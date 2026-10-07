"""Сбой API на экране операции не маскируется под отсутствие данных."""
from __future__ import annotations

from pathlib import Path


def _detail_source() -> str:
    html = (
        Path(__file__).resolve().parents[1] / "mini_app" / "index.html"
    ).read_text(encoding="utf-8")
    start = html.index("async function openOpDetail")
    return html[start:html.index("\n// ── Лог операции", start)]


def test_operation_request_error_is_not_reported_as_not_found():
    source = _detail_source()
    assert "api('/api/miniapp/operation/'+opId).catch(()=>null)" not in source
    assert "const o = await api('/api/miniapp/operation/'+opId)" in source
    assert "catch(e) { txt('opdetBody', errHtml(errRu(e)))" in source


def test_journal_error_is_visible_and_retryable():
    source = _detail_source()
    assert ".catch(()=>({logs:[]}))" not in source
    assert "Не удалось загрузить журнал операции" in source
    assert "Повторить загрузку журнала" in source
