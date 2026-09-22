"""Пул прокси: экран читал поля, которых эндпоинт не отдавал.

Найдено при разборе «быстрых действий»: `renderProxyPoolList` берёт
`p.account_count`, а `loadProxyPool`/`renderProxyPoolBars` — `p.latency_ms`.
`/api/miniapp/proxies` не возвращал ни того, ни другого, поэтому:

* счётчик аккаунтов на прокси не показывался НИКОГДА;
* «Средняя задержка» всегда была «—», а график задержек всегда рисовал
  «Нет данных о задержке» — при том что `latency_avg_ms` исправно пишет сторож
  прокси (`services/proxy_watchdog.py`).

Молчаливо пустой экран — худший вид поломки: он выглядит как «данных ещё нет».

Заодно стережётся срочность: мёртвый прокси с двенадцатью аккаунтами и мёртвый
запасной без единого выглядели в списке одинаково.
"""
from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _proxies_handler() -> str:
    i = _API.find("    async def proxies(request: web.Request)")
    assert i != -1, "хендлер proxies не найден"
    j = _API.find("    async def add_proxy(", i)
    return _API[i:j if j != -1 else i + 4000]


# ── Эндпоинт отдаёт то, что рисует экран ───────────────────────────────────

def test_endpoint_returns_the_account_count():
    assert "AS acc_count" in _proxies_handler()


def test_endpoint_returns_latency_under_the_key_the_screen_reads():
    """Колонка называется latency_avg_ms, экран читает latency_ms."""
    assert "latency_avg_ms AS latency_ms" in _proxies_handler()


def test_migration_lag_fallback_still_fills_both_fields():
    """Фолбэк без is_backup не должен отдавать строки БЕЗ новых полей — иначе
    экран снова молча опустеет на старой схеме."""
    h = _proxies_handler()
    fb = h[h.find("except Exception:"):]
    assert "acc_count=0" in fb and "latency_ms=None" in fb


def test_no_screen_reads_the_old_account_count_key():
    """Старое имя поля осталось бы мёртвым — эндпоинт его не отдаёт."""
    assert "p.account_count" not in _UI


def test_screen_reads_the_fields_it_is_given():
    assert "p.acc_count" in _UI and "p.latency_ms" in _UI


# ── Срочность видна ────────────────────────────────────────────────────────

def test_dead_proxy_carrying_accounts_is_marked_as_urgent():
    assert "аккаунтов простаивают" in _UI


def test_row_shows_how_many_accounts_sit_on_the_proxy():
    assert re.search(r"acc_count\s*\|\|\s*0", _UI)


# ── Удаление объясняется до тапа, а не отказом после ───────────────────────

def test_delete_warns_before_the_server_refuses():
    """Сервер и так не даст удалить назначенный прокси (409: аккаунты ушли бы
    напрямую и рискуют AUTH_KEY_DUPLICATED) — но объяснять это лучше заранее."""
    i = _UI.find("async function deleteProxy(")
    body = _UI[i:i + 900]
    assert "accN" in body and "Снять прокси" in body


def test_delete_of_a_free_proxy_still_asks_for_confirmation():
    i = _UI.find("async function deleteProxy(")
    body = _UI[i:i + 900]
    assert "askConfirm" in body and "необратимо" in body


def test_both_proxy_lists_pass_the_count_into_delete():
    """Два экрана рисуют корзину; забыть один — вернуть отказ без объяснения."""
    assert _UI.count("deleteProxy(${p.id},${Number(p.acc_count||0)})") == 2


def test_server_side_guard_is_still_in_place():
    """Предупреждение во фронте не заменяет серверный гард.

    Гард переехал в одну дверь на весь продукт (proxy_hygiene.delete_proxy_safely):
    в боте у той же кнопки проверки не было вовсе, пока каждый интерфейс носил
    свою копию.
    """
    assert "delete_proxy_safely" in _API
