"""Регресс-тесты Last Seen в парсере (services.parser.status_to_days).

Telethon-статусы имитируем лёгкими заглушками — сеть/Telethon не нужны.
"""

from datetime import datetime, timedelta, timezone

from services.parser import status_to_days


class _Status:
    """Заглушка Telethon UserStatus* — важен только тип (имя класса)."""


def _mk(name, **attrs):
    cls = type(name, (_Status,), {})
    obj = cls()
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


def test_none_and_empty_are_unknown():
    assert status_to_days(None) is None
    assert status_to_days(_mk("UserStatusEmpty")) is None


def test_online_and_buckets():
    assert status_to_days(_mk("UserStatusOnline")) == 0
    assert status_to_days(_mk("UserStatusRecently")) == 2
    assert status_to_days(_mk("UserStatusLastWeek")) == 7
    assert status_to_days(_mk("UserStatusLastMonth")) == 30


def test_offline_exact_days():
    was = datetime.now(timezone.utc) - timedelta(days=5, hours=1)
    assert status_to_days(_mk("UserStatusOffline", was_online=was)) == 5


def test_offline_naive_datetime_ok():
    # naive datetime (без tzinfo) не должен ронять — трактуем как UTC
    was = datetime.utcnow() - timedelta(days=3)
    assert status_to_days(_mk("UserStatusOffline", was_online=was)) == 3


def test_offline_without_date_is_unknown():
    assert status_to_days(_mk("UserStatusOffline")) is None
