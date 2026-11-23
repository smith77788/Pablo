"""Тяжёлые разборы экранов идут в потоке, а не в цикле событий.

В одном процессе с мини-аппом живут бот и исполнитель операций. Разбор по
всей базе владельца — чистый Python — останавливал цикл событий целиком: на
стенде (30 000 контактов/разговоров/диалогов) замер задержки цикла во время
запроса: дубли контактов 744 мс, воронка CRM 385 мс, входящие 305 мс,
когорты 286 мс, диалоги Хранилища 197 мс. Всё это время бот не отвечал
никому, а операции стояли. После выноса в поток — 60–117 мс (остаток —
разбор ответа базы и сериализация).

Проверяем, что каждая чистая часть выполняется НЕ в главном потоке (и
сборка выгрузок контактов тоже).
"""
from __future__ import annotations

import asyncio
import threading


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Pool:
    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return None


def _spy(monkeypatch, module, name, result):
    seen = {}
    real = getattr(module, name)

    def _wrapped(*a, **kw):
        seen["thread"] = threading.current_thread()
        out = real(*a, **kw)
        return out if result is None else result

    monkeypatch.setattr(module, name, _wrapped)
    return seen


def _off_main(seen):
    assert "thread" in seen, "чистая часть не вызвалась вовсе"
    assert seen["thread"] is not threading.main_thread(), (
        "разбор идёт в цикле событий и останавливает весь процесс")


def test_duplicates(monkeypatch):
    from services.contacts_hub import trust_engine as te
    seen = _spy(monkeypatch, te, "_find_duplicates", None)
    _run(te.detect_smart_duplicates(_Pool(), 1))
    _off_main(seen)


def test_inbox(monkeypatch):
    from services import unified_inbox as ui
    seen = _spy(monkeypatch, ui, "merge_threads", None)
    _run(ui.list_threads(_Pool(), 1))
    _off_main(seen)


def test_vault_dialogs(monkeypatch):
    from services import vault_dialogs as vd
    seen = _spy(monkeypatch, vd, "_assemble", None)
    _run(vd.dialogs(_Pool(), 1))
    _off_main(seen)


def test_cohorts(monkeypatch):
    from services import cohorts, owner_currency

    class _Conv:
        def convert(self, amount, cur):
            return amount

    async def _load(pool, owner_id, fetch=None):
        return _Conv(), {"currency": "RUB"}

    monkeypatch.setattr(owner_currency, "load", _load)
    seen = _spy(monkeypatch, cohorts, "build", None)
    _run(cohorts.report(_Pool(), 1))
    _off_main(seen)


def test_crm_funnel(monkeypatch):
    from services import crm_funnel_analytics as fa
    from services import mini_app_deals_board

    async def _cur(pool, owner_id):
        return "RUB"

    monkeypatch.setattr(mini_app_deals_board, "owner_main_currency", _cur)
    seen = _spy(monkeypatch, fa, "analyze", None)
    _run(fa.report(_Pool(), 1))
    _off_main(seen)


def test_contact_exports(monkeypatch):
    """Выгрузка контактов: на 30 000 контактов CSV держал цикл 1025 мс, JSON
    (41 МБ) — секунды; после выноса в поток 127 и 199 мс."""
    from services.contacts_hub import export_engine as ex
    for fmt in ("csv", "vcf", "json"):
        seen = _spy(monkeypatch, ex, f"_{fmt}_text", None)
        _run(getattr(ex, f"export_{fmt}")(_Pool(), 1))
        _off_main(seen)


def test_contact_trust_recalc(monkeypatch):
    """Пересчёт доверия контактов: 30 000 контактов держали цикл 168 мс, в потоке — 61."""
    from services.contacts_hub import trust_engine as te

    class _P(_Pool):
        async def fetch(self, q, *a):
            if "FROM unified_contacts" in q and "contact_sources" not in q:
                return [{"id": "x", "phones": "[]"}]
            return []

        async def execute(self, q, *a):
            return "UPDATE 1"

    seen = _spy(monkeypatch, te, "compute_trust_score", None)
    _run(te.update_trust_scores(_P(), 1))
    _off_main(seen)


def test_smart_tags(monkeypatch):
    """Умные метки: 30 000 контактов × 1000 правил держали цикл 6,4 с, в потоке — 125 мс."""
    from services.contacts_hub import smart_tags_engine as st
    seen = _spy(monkeypatch, st, "_match_all", None)
    _run(st.apply_smart_tags(_Pool(), 1))
    _off_main(seen)
