"""Гейт: единая дверь выбора аккаунта НЕ отдаёт аккаунты под спам-блоком.

По аудиту (проактивная реабилитация): аккаунт в 'spamblock' ограничен Telegram и
не должен толкаться в массовые операции — иначе ограничение усугубляется вплоть до
бана. flood_engine сам ВЫСТАВЛЯЕТ spamblock, но при ВЫБОРЕ его надо исключать.
Оба селектора двери (bulk select_all_active и одиночный get_best_account) обязаны
фильтровать spamblock наряду с banned/deactivated/session_expired.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# Проверка искала в исходнике дословную строку
# `NOT IN ('banned', 'deactivated', 'session_expired', 'spamblock')`. Пока список
# был вписан в каждую дверь руками, это работало; потом набор свели в один
# словарь `account_status.DEAD_STATUSES`, двери стали брать список оттуда — и
# дословной строки не стало ни в одной. Проверка покраснела на ВЕРНОЙ правке, а
# заодно перестала бы замечать настоящее: словарь пополнился `deleted` и
# `frozen`, и дверь с вписанным вручную списком тихо пускала такие аккаунты.
#
# Поэтому сверяем не текст, а то, что обе двери берут набор из словаря.


def _takes_the_list_from_the_vocabulary(src: str) -> bool:
    return "sql_dead_list()" in src


def test_the_vocabulary_calls_spamblock_dead():
    """Сам словарь обязан считать spamblock непригодным для работы."""
    from services.account_status import DEAD_STATUSES

    assert "spamblock" in DEAD_STATUSES, (
        "spamblock выпал из словаря мёртвых статусов — ограниченный Telegram "
        "аккаунт снова пойдёт в работу и доведёт дело до бана")


def test_bulk_door_excludes_spamblock():
    src = (ROOT / "services" / "resource_selector.py").read_text(encoding="utf-8")
    assert _takes_the_list_from_the_vocabulary(src), (
        "select_all_active не берёт список мёртвых статусов из словаря — "
        "своя копия разъедется со словарём, как уже было")


def test_single_door_excludes_spamblock():
    src = (ROOT / "services" / "flood_engine.py").read_text(encoding="utf-8")
    assert _takes_the_list_from_the_vocabulary(src), (
        "get_best_account не берёт список мёртвых статусов из словаря — "
        "одиночные операции будут работать по более слабым правилам")
