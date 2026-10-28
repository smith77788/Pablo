"""mass_report не зовёт несуществующую функцию разведки.

Раньше mass_report делал `await account_manager.get_channel_intel(...)` — такой
функции в account_manager нет (разведка называется strike_map_target). Вызов
мгновенно падал в AttributeError, глотался, а результат (intel) всё равно нигде не
использовался — report_peer_deep_v2 сам делает рекон на каждом аккаунте. Мёртвый
код убран; тест не даёт ему вернуться.
"""
from __future__ import annotations

import inspect

from services import account_manager, strike_engine


def test_mass_report_has_no_get_channel_intel_call():
    src = inspect.getsource(strike_engine.mass_report)
    # допускаем упоминание строки в комментарии, но НЕ вызов
    assert "await _am.get_channel_intel" not in src
    assert "account_manager.get_channel_intel" not in src


def test_account_manager_has_no_get_channel_intel_but_has_recon():
    # разведка существует под своим настоящим именем
    assert hasattr(account_manager, "strike_map_target")
    # а мнимой get_channel_intel — нет (иначе вызов «случайно» ожил бы)
    assert not hasattr(account_manager, "get_channel_intel")
