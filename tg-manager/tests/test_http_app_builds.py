"""Стартовый смоук: HTTP-приложение действительно собирается.

Зачем.

`payment_webhook.make_app` — второй кусок старта, который можно выполнить в
тесте: обычная синхронная фабрика, ей нужны только объекты пула и бота. Внутри
она регистрирует ~800 маршрутов — мини-апп, REST API, вебхуки платежей и
дочерних ботов.

Ошибка здесь не роняет процесс: сервер поднимается отдельной задачей под
`_web_resilient`, которая ловит исключение и пробует снова каждые 5 секунд. Бот
при этом отвечает как ни в чём не бывало (поллинг живёт своей жизнью), а на
$PORT не слушает никто — Railway отдаёт «Application failed to respond».
Снаружи это выглядит как «бот работает, приложение не работает», и по логу бота
причину не найти.

Так и случилось 2026-09-04: маршрут `/api/miniapp/fleet/warnings` зарегистрировали
на 600 строк РАНЬШЕ, чем определили его хендлер. `setup_routes` — обычная
функция, ссылка на ещё не созданную локальную функцию даёт UnboundLocalError,
и приложение не собиралось целиком. Мини-апп лежал полдня; ни один из 4540
тестов этого не видел, потому что `make_app` никто не вызывал.
"""
from __future__ import annotations

import os
from collections import Counter
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("MANAGER_BOT_TOKEN", "1:test-token")
    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/test")
    from services import payment_webhook

    return payment_webhook.make_app(MagicMock(), MagicMock())


def test_app_builds(app):
    """Главное: фабрика отрабатывает целиком. Именно это и падало."""
    assert app is not None


def test_all_routes_registered(app):
    """Маршруты на месте, а не потерялись на первой же ошибке.

    Порог грубый намеренно: точное число растёт с каждым разделом, проверяем
    сам факт регистрации.
    """
    assert len(list(app.router.routes())) > 500


def test_key_routes_present(app):
    """Точки, без которых продукт не работает снаружи: health (по нему
    платформа решает, жив ли сервис), сам мини-апп и его API."""
    paths = {
        getattr(r.resource, "canonical", str(r.resource))
        for r in app.router.routes()
    }
    for required in ("/health", "/miniapp", "/api/miniapp/fleet/warnings"):
        assert required in paths, f"маршрут {required} не зарегистрирован"


def test_no_shadowed_routes(app):
    """Один и тот же метод+путь зарегистрирован дважды — второй хендлер
    недостижим: aiohttp отдаёт запрос первому совпавшему ресурсу и молчит.

    Статические ресурсы исключены: `add_get("/miniapp")` рядом с
    `add_static("/miniapp")` — не дубль, а нормальная пара «точный путь плюс
    префикс».
    """
    from aiohttp.web_urldispatcher import StaticResource

    keys = [
        (r.method, r.resource.canonical)
        for r in app.router.routes()
        if not isinstance(r.resource, StaticResource)
    ]
    dupes = sorted(k for k, n in Counter(keys).items() if n > 1)
    assert not dupes, f"метод+путь зарегистрированы дважды, второй недостижим: {dupes}"


def test_build_is_repeatable(app):
    """Сборка не должна зависеть от того, первая она в процессе или нет:
    иначе перезапуск сервера под _web_resilient не поднимет его никогда."""
    from services import payment_webhook

    second = payment_webhook.make_app(MagicMock(), MagicMock())
    assert len(list(second.router.routes())) == len(list(app.router.routes()))
