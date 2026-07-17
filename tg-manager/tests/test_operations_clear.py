"""Регрессия по скриншоту: «Очистить» пишет «Очищено 30», но не удаляет ничего;
кнопка «Создать» в шапке кривая (схлопнутый flex-item).

БАГ 1 (очистка-ничего): фронт слал cancel по каждой done-операции, а
cancel_operation бьёт ТОЛЬКО по status IN ('pending','running') → для завершённых
это 404/no-op. Тост показывал число ВЫБРАННЫХ, а не удалённых. Реального DELETE не
было вовсе (cancel лишь ставит status='cancelled'). Фикс: эндпойнт
/operations/clear делает DELETE терминальных (done/failed/cancelled), фронт
показывает реальный d.deleted.

БАГ 2 (кривая кнопка): header-кнопки «+ Создать» имели inline `flex:0;min-width:0`.
В шапках, где hdr-meta = display:flex (Диспетчер задач, Боты, дашборд), кнопка —
flex-item, и flex:0 (basis 0%) + min-width:0 схлопывали её фон в «кружок», а
nowrap-текст вытекал наружу. Фикс: flex:0 0 auto (basis auto = ширина контента).
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api


def _api_src() -> str:
    return inspect.getsource(mini_app_api)


def _index_html() -> str:
    p = Path(__file__).resolve().parent.parent / "mini_app" / "index.html"
    return p.read_text(encoding="utf-8")


def test_clear_operations_endpoint_deletes_terminal_scoped():
    src = _api_src()
    m = re.search(r"async def clear_operations\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "clear_operations handler не найден"
    body = m.group(1)
    assert "DELETE FROM operation_queue" in body, "должен реально удалять, а не cancel"
    assert "owner_id=$1" in body, "скоуп по владельцу обязателен"
    assert "'done'" in body and "'failed'" in body and "'cancelled'" in body, (
        "удаляем терминальные состояния"
    )
    # активные операции не трогаем: в SQL-условии их нет
    assert "status IN ('done','failed','cancelled')" in body, (
        "DELETE только терминальных; pending/running не в условии"
    )
    assert "deleted" in body, "возвращаем реальное число удалённых"
    assert "if not uid" in body and "401" in body


def test_clear_operations_route_registered():
    src = _api_src()
    assert 'app.router.add_post("/api/miniapp/operations/clear", clear_operations)' in src


def test_frontend_clear_uses_real_endpoint():
    html = _index_html()
    m = re.search(r"async function clearDoneOps\(\)\s*\{(.*?)\n\}", html, re.DOTALL)
    assert m, "clearDoneOps не найдена"
    body = m.group(1)
    assert "/api/miniapp/operations/clear" in body, "должен звать реальный DELETE-эндпойнт"
    assert "d.deleted" in body, "тост должен показывать реальное число удалённых"
    # не должен больше слать cancel по каждой done-операции
    assert "/cancel'" not in body and "'/cancel'" not in body, (
        "старый cancel-цикл (ничего не удалял) должен быть убран"
    )


def test_bulk_ops_report_real_success_count():
    """Тот же класс, что «Очистить»: массовые действия показывают РЕАЛЬНОЕ число
    (счётчик от бэка), а не число выбранных — иначе тост врёт при частичных сбоях.
    pause/resume ушли на честные bulk-эндпойнты (d.paused/d.resumed)."""
    html = _index_html()
    for fn, field in (("pauseAllOps", "d.paused"), ("resumeAllOps", "d.resumed")):
        m = re.search(r"async function " + fn + r"\(\)\s*\{(.*?)\n\}", html, re.DOTALL)
        assert m, f"{fn} не найдена"
        body = m.group(1)
        assert field in body, f"{fn} должна показывать реальный счётчик {field}"
        # не должно быть тоста с чистым ops.length как «успешно N»
        assert not re.search(r"toast\('[^']*'\+ops\.length\+'[^']*(приостановлено|возобновлено|удалено)",
                             body, re.IGNORECASE), (
            f"{fn} не должна выдавать число выбранных за число успешных"
        )


def test_header_buttons_do_not_collapse():
    """Ни одна кнопка не должна использовать схлопывающий flex:0;min-width:0 —
    в display:flex-шапках это ломает вид («кружок» вместо кнопки)."""
    html = _index_html()
    # связка flex:0 (basis 0%) + min-width:0 в ОДНОМ style = коллапс во flex-шапке
    # (любой порядок атрибутов). flex:0 0 auto — безопасно, его не ловим.
    bad = re.findall(r'style="[^"]*(?:\bflex:0;[^"]*min-width:0|min-width:0[^"]*\bflex:0;)[^"]*"', html)
    assert not bad, f"осталась схлопывающая кнопка(и): {len(bad)} — {bad[:3]}"
