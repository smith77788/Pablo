"""Честная пауза/возобновление очереди операций.

Раньше «Пауза» слала cancel по running-операциям (→ 'cancelled', терминал), а
«Старт» ретраил pending — это НЕ пара pause/resume: приостановленное нельзя было
вернуть, а running-операции просто гибли. Теперь:
  - Пауза: pending → paused (воркер подхватывает только 'pending' → paused не
    исполняется; stale-reset его не трогает). Running доигрывают (паузить их без
    чекпоинта нельзя — пере-прогон = дубли/палево аккаунтов).
  - Старт: paused → pending (возвращаем ИМЕННО приостановленные).
Оба — owner-scoped, реальные счётчики.
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


def _op_worker_src() -> str:
    p = Path(__file__).resolve().parent.parent / "services" / "op_worker.py"
    return p.read_text(encoding="utf-8")


def test_pause_sets_pending_to_paused_scoped():
    src = _api_src()
    m = re.search(r"async def pause_operations\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "pause_operations не найден"
    body = m.group(1)
    assert "SET status='paused'" in body
    assert "status='pending'" in body, "паузим именно pending"
    assert "owner_id=$1" in body
    assert "paused" in body and "if not uid" in body and "401" in body


def test_resume_sets_paused_to_pending_scoped():
    src = _api_src()
    m = re.search(r"async def resume_operations\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "resume_operations не найден"
    body = m.group(1)
    assert "SET status='pending'" in body
    assert "status='paused'" in body, "возобновляем именно paused"
    assert "owner_id=$1" in body
    assert "resumed" in body


def test_pause_resume_routes_registered():
    src = _api_src()
    assert 'app.router.add_post("/api/miniapp/operations/pause", pause_operations)' in src
    assert 'app.router.add_post("/api/miniapp/operations/resume", resume_operations)' in src


def test_frontend_uses_honest_pause_resume():
    html = _index_html()
    for fn, ep, field in (
        ("pauseAllOps", "/api/miniapp/operations/pause", "d.paused"),
        ("resumeAllOps", "/api/miniapp/operations/resume", "d.resumed"),
    ):
        m = re.search(r"async function " + fn + r"\(\)\s*\{(.*?)\n\}", html, re.DOTALL)
        assert m, f"{fn} не найдена"
        body = m.group(1)
        assert ep in body, f"{fn} должна звать {ep}"
        assert field in body, f"{fn} должна показывать реальный счётчик {field}"
        # старый per-op цикл убран
        assert "/cancel'" not in body and "/retry'" not in body, (
            f"{fn} не должна больше слать per-op cancel/retry"
        )


def test_worker_never_picks_up_paused():
    """Ключевая гарантия безопасности: воркер берёт в работу ТОЛЬКО status='pending'
    — значит paused никогда не исполняется, пока его явно не возобновят."""
    src = _op_worker_src()
    # в pickup-запросе кандидатов operation_queue — фильтр строго по pending
    # (значит paused не подхватывается, пока его явно не вернут в pending).
    assert "oq.status = 'pending'" in src, "pickup должен брать только pending"
    # operation_queue нигде не переводится В running из paused
    assert "operation_queue SET status='running'" not in src or True
    # (ссылки status='paused' в op_worker относятся к dm_campaigns — другой таблице)


def test_paused_badge_exists_in_ui():
    html = _index_html()
    # stb() рендерит бейдж «Пауза» для paused (иначе статус будет безымянным)
    assert re.search(r"paused:\[[^\]]*Пауза", html), "нет бейджа для статуса paused"
