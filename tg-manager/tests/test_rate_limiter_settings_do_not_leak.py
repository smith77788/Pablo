"""Настройки лимитера, изменённые одним тестом, не достаются следующему.

Пороги services/security читаются из окружения при импорте, и тесты лимитера
меняют их через monkeypatch.setenv + importlib.reload. monkeypatch возвращает
окружение, но не модуль: потолок «5 запросов в минуту» из
test_auth_endpoints_rate_limit.py оставался в модуле до конца процесса, и
тест персоны, поднимающий настоящий сервер мини-аппа, получал 429 — только
в полном прогоне части CI, в одиночку он зелёный.

Тесты в этом файле идут парой и по порядку (часть CI делится по файлам,
поэтому пара не разъезжается): первый портит настройки так же, как тесты
лимитера, второй проверяет, что до него это не дошло. Без восстановления в
фикстуре tests/conftest.py::_restore_security_limits второй красный.
"""
from __future__ import annotations

import asyncio
import importlib


class _Req:
    path = "/api/miniapp/dashboard"
    headers: dict = {}

    @property
    def transport(self):
        class _T:
            def get_extra_info(self, _name):
                return ("203.0.113.9", 12345)
        return _T()


def test_a_limiter_test_squeezes_the_limits(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_MAX_REQUESTS", "2")
    monkeypatch.setenv("RATE_LIMIT_AUTH_MAX", "1")
    import services.security as s
    s = importlib.reload(s)
    assert s._RATE_LIMIT_MAX_REQUESTS == 2


def test_the_next_test_gets_the_normal_limits_back():
    import services.security as s
    assert s._RATE_LIMIT_MAX_REQUESTS >= 120, (
        "потолок анонимных запросов остался от прошлого теста: "
        f"{s._RATE_LIMIT_MAX_REQUESTS}")
    assert s._RATE_LIMIT_AUTH_MAX >= 30

    async def go():
        return [await s.check_rate_limit_for_path(_Req()) for _ in range(20)]
    assert all(asyncio.run(go())), "обычные 20 запросов подряд упёрлись в 429"
