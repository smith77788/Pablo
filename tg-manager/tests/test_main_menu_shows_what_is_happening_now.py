"""Главное меню показывает, что идёт прямо сейчас, кнопками — а не числом.

Строка статистики главного экрана называла «🔔 3 алертов», но открыть их
можно было только через «Все инструменты → Мониторинг», а идущие операции —
через «Операции». Теперь, пока есть идущие операции или свежие предупреждения,
они — первые кнопки главного меню: «⏳ Идут операции: 2» и «🔔 Предупреждения
за сутки: 3». Заодно строка статистики говорит по-русски и с верным числом:
«3 операции · 1 аккаунт · 🔔 3 предупреждения», а не «3 операций · 1
аккаунтов · 🔔 3 алертов».
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace


class _Pool:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, *a, **k):
        return self.row


class _State:
    async def get_state(self):
        return None

    async def clear(self):
        pass


def _open_main(monkeypatch, row, uid):
    from bot.handlers import botmother_menu as bm
    from bot.utils import menu_cache
    from services import infra_pressure

    shown: list[tuple[str, object]] = []

    async def fake_edit(cb, text, markup=None):
        shown.append((text, markup))

    async def fake_answer(cb, *a, **k):
        return None

    async def fake_pressure(pool, uid):
        return {}

    monkeypatch.setattr(bm, "_edit", fake_edit)
    monkeypatch.setattr(bm, "safe_answer", fake_answer)
    monkeypatch.setattr(bm, "_MAIN_STATS_WAIT", 2.0)
    monkeypatch.setattr(infra_pressure, "compute_pressure", fake_pressure)
    menu_cache.invalidate(f"u:{uid}:")
    cb = SimpleNamespace(from_user=SimpleNamespace(id=uid))
    asyncio.run(bm.cb_main(cb, None, _Pool(row), _State()))
    # Второе открытие — из кэша: кнопки обязаны остаться.
    asyncio.run(bm.cb_main(cb, None, _Pool(row), _State()))
    return shown


def _buttons(markup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data or "") for row in markup.inline_keyboard for b in row]


def test_running_ops_and_alerts_are_the_first_buttons(monkeypatch):
    row = {"today_ops": 3, "running_ops": 2, "active_accs": 1, "in_cooldown": 0, "new_alerts": 3}
    shown = _open_main(monkeypatch, row, 99188001)
    assert len(shown) == 2
    for text, markup in shown:
        btns = _buttons(markup)
        assert btns[0][0] == "⏳ Идут операции: 2" and btns[0][1].startswith("bm:ops:")
        assert btns[1][0] == "🔔 Предупреждения за сутки: 3" and btns[1][1].startswith("bm:alerts")
        assert "3 операции · 1 аккаунт" in text
        assert "3 предупреждения" in text and "алертов" not in text


def test_quiet_fleet_gets_the_usual_menu(monkeypatch):
    row = {"today_ops": 0, "running_ops": 0, "active_accs": 5, "in_cooldown": 0, "new_alerts": 0}
    shown = _open_main(monkeypatch, row, 99188002)
    texts = [t for t, _ in _buttons(shown[0][1])]
    assert not any(t.startswith(("⏳ Идут", "🔔 Предупреждения")) for t in texts)
    assert "5 аккаунтов" in shown[0][0]
