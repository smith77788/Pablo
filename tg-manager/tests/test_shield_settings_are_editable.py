"""Настройки «Щита аккаунтов» можно менять, а не только смотреть.

Экран показывал четыре строки — порог риска, порог бана, авто-паузу и
уведомления — красивыми значениями и ни одна не нажималась. Причина глубже
вёрстки: у таблицы `shield_configs` не было писателя со стороны мини-аппа
вообще. Прочитать настройки умел `get_shield_config`, записать — никто; из
бота переключались только две галочки. То есть «0.7» на экране был не
настройкой, а надписью.

Здесь проверяется вся цепочка: функция сохранения в сервисе (пишет только то,
что передали, и зажимает значения в разумные границы), маршрут
`POST /api/miniapp/shield/config` с проверкой входа, и экран, где каждая
строка настройки открывается, число «Аккаунтов» ведёт в список, а строка
истории — в карточку аккаунта.
"""
from __future__ import annotations

import asyncio
import functools
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "mini_app", "index.html")
API = os.path.join(ROOT, "services", "mini_app_api.py")


@functools.lru_cache(maxsize=1)
def _html() -> str:
    with open(HTML, encoding="utf-8") as f:
        return f.read()


@functools.lru_cache(maxsize=1)
def _api() -> str:
    with open(API, encoding="utf-8") as f:
        return f.read()


def _js_func(name: str) -> str:
    h = _html()
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", h)
    assert m, f"функция {name} в мини-аппе не найдена"
    depth = 0
    for j in range(m.end() - 1, len(h)):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                return h[m.start():j + 1]
    raise AssertionError(f"не удалось найти конец функции {name}")


# ─── сервис ───────────────────────────────────────────────────────────────────


class _Pool:
    """Запоминает, что писали, и отдаёт это обратно как строку конфига."""

    def __init__(self, row=None):
        self.row = row or {
            "risk_threshold": 0.7, "ban_prob_threshold": 0.5,
            "auto_pause": True, "notify_admin": True, "cool_duration_hours": 24,
        }
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((" ".join(sql.split()), args))
        return "INSERT 0 1"

    async def fetchrow(self, sql, *args):
        return dict(self.row)


def test_writer_exists_at_all():
    """Антивакуумность: без функции сохранения остальные проверки пусты."""
    from services import account_shield
    assert hasattr(account_shield, "save_shield_config"), (
        "у shield_configs нет писателя — настройки экрана остаются надписью"
    )


def test_only_passed_fields_are_written():
    """Частичное сохранение не должно затирать соседние настройки дефолтами."""
    from services.account_shield import save_shield_config
    pool = _Pool()
    asyncio.run(save_shield_config(pool, 7, risk_threshold=0.9))
    assert len(pool.calls) == 1
    sql, args = pool.calls[0]
    assert "risk_threshold" in sql
    for other in ("ban_prob_threshold", "auto_pause", "notify_admin", "cool_duration_hours"):
        assert other not in sql, f"{other} переписан, хотя его не передавали"
    assert args == (7, 0.9)
    assert "ON CONFLICT (owner_id) DO UPDATE" in sql, "второе сохранение упадёт на дубле"


def test_values_are_clamped():
    from services.account_shield import save_shield_config
    pool = _Pool()
    asyncio.run(save_shield_config(pool, 7, cool_duration_hours=999, risk_threshold=5.0))
    args = pool.calls[0][1]
    assert 999 not in args, "пауза на 999 часов — это месяц простоя аккаунта"
    assert 168 in args
    assert 5.0 not in args and 1.0 in args


def test_nothing_to_save_is_not_an_insert():
    """Пустой патч не должен создавать строку из одних дефолтов."""
    from services.account_shield import save_shield_config
    pool = _Pool()
    cfg = asyncio.run(save_shield_config(pool, 7))
    assert pool.calls == []
    assert cfg.risk_threshold == 0.7


def test_booleans_survive_being_false():
    """Выключить галочку — тоже сохранение; False не должен считаться «не передали»."""
    from services.account_shield import save_shield_config
    pool = _Pool()
    asyncio.run(save_shield_config(pool, 7, auto_pause=False))
    assert pool.calls, "выключение авто-паузы не дошло до базы"
    assert pool.calls[0][1] == (7, False)


# ─── маршрут ──────────────────────────────────────────────────────────────────


def test_route_is_registered_and_guarded():
    src = _api()
    assert 'add_post("/api/miniapp/shield/config", shield_config_save)' in src, (
        "маршрута сохранения настроек щита нет"
    )
    m = re.search(r"async def shield_config_save\(request.*?\n    async def ", src, re.S)
    body = m.group(0) if m else ""
    assert body, "хендлер shield_config_save не найден"
    assert "_get_uid(request)" in body and "401" in body, "сохранение без проверки входа"
    assert "validate_integer" in body, "часы паузы не проверяются"
    assert "0.0 <= v <= 1.0" in body, "порог не ограничен диапазоном 0..1"


def test_history_rows_carry_the_account():
    """Строка истории без account_id никуда вести не может."""
    src = _api()
    m = re.search(r"async def shield_summary\(request.*?\n    async def ", src, re.S)
    body = m.group(0)
    assert "a.id AS account_id" in body, "история щита не отдаёт аккаунт"
    assert '"account_id": r["account_id"]' in body
    assert "history_total" in body, "потолок в 20 записей нечем показать честно"
    assert "cool_duration_hours" in body, "длительность паузы не отдаётся на экран"


# ─── экран ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field", [
    "risk_threshold", "ban_prob_threshold", "cool_duration_hours",
    "auto_pause", "notify_admin",
])
def test_every_config_row_is_clickable(field):
    body = _js_func("openShield")
    assert f"'{field}'" in body, f"строка {field} ничего не делает при нажатии"


def test_rows_lead_to_save():
    body = _js_func("openShield")
    assert "shieldPick(" in body and "shieldToggle(" in body
    save = _js_func("shieldSave")
    assert "/api/miniapp/shield/config" in save, "выбор никуда не сохраняется"
    assert "method:'POST'" in save.replace('"', "'")


def test_numbers_lead_somewhere():
    body = _js_func("openShield")
    assert "healthGoAccounts(" in body, "число аккаунтов остаётся числом"
    assert "openAccount(" in body, "строка истории не открывает аккаунт"


def test_cap_is_admitted():
    """Сервер отдаёт последние 20 — список не выдаёт себя за полный."""
    body = _js_func("openShield")
    assert "history_total" in body and "Показаны последние" in body


def test_error_has_a_way_out():
    assert "errHtml(e.message, 'openShield()')" in _js_func("openShield")


def test_screen_speaks_russian():
    """Владелец не читает по-английски: пороги должны быть объяснены словами."""
    body = _js_func("openShield")
    assert "risk_score" not in body, "на экран вылезло имя колонки из базы"
    for step in ("SHIELD_RISK_STEPS", "SHIELD_BAN_STEPS", "SHIELD_HOURS"):
        assert step in _html(), f"нет вариантов выбора {step}"
    assert "Осторожный" in _html() and "по умолчанию" in _html()


def test_history_words_are_the_ones_the_shield_actually_writes():
    """Словарь переводил действия, которых щит не пишет.

    `account_shield._decide` кладёт в `shield_actions` ровно ok/warn/cool/pause,
    а перевод был заготовлен для auto_pause/ban_detect/rotate/cooldown/restore.
    Совпадений ноль, поэтому в историю падало сырое английское «pause».
    """
    import inspect
    from services import account_shield

    src = inspect.getsource(account_shield)
    written = set(re.findall(r'action = "(\w+)"', src))
    assert written, "не нашли, какие действия пишет щит — проверка потеряла смысл"
    m = re.search(r"const SHIELD_ACTIONS = \{(.*?)\};", _html(), re.S)
    assert m, "словаря действий щита нет"
    known = set(re.findall(r"(\w+)\s*:", m.group(1)))
    assert written <= known, f"без перевода остались: {sorted(written - known)}"
