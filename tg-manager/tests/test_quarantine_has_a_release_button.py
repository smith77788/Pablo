"""Карантин риск-пульса владелец обязан уметь снять — и видеть, что он есть.

ЧТО БЫЛО. Пауза аккаунта бывает трёх видов: окно `cooldown_until` в базе,
кулдаун `flood_engine` в памяти процесса и карантин риск-пульса по
`restriction_events` (его читает гейт ВСЕХ операций `is_account_quarantined`).
Экран «Сбросить кулдауны» и массовый сброс брали список у
`account_reset.cooled_account_ids`, а тот считал только первые два вида.

У карантинного аккаунта окна кулдауна нет вовсе, поэтому в списке он не
появлялся никогда. Приборный щиток считал его в «Карантин», операции обходили
стороной, а единственная кнопка, которая его освобождает (она ставит
`risk_cleared_at`), отвечала владельцу «✅ Нет активных кулдаунов — все
аккаунты доступны». Это ровно та жалоба «кулдаун не сбрасывается», ради
которой заведён `account_reset`, только у неё не было даже кнопки: снять
карантин из бота было нечем, помогал лишь Mini App.

ЧТО ТЕПЕРЬ. Общий список считает и карантин. Мёртвые по статусу аккаунты в
него не идут: снятие риска не превращает `banned` обратно в рабочий аккаунт, а
кнопка, которая ничего не меняет, — это тот же тупик с другой стороны.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from services import account_reset

ROOT = Path(__file__).resolve().parents[1]


class _FleetPool:
    """Пул, различающий запрос списка аккаунтов и запрос карантина.

    Разделение по SQL, а не по порядку вызовов: порядок — деталь реализации,
    и тест, завязанный на него, ломается от любой перестановки.
    """

    def __init__(self, accounts: list[tuple[int, bool, str]],
                 quarantined: set[int]):
        self.accounts = accounts
        self.quarantined = quarantined
        self.asked_about: list[list[int]] = []
        self.executed: list[tuple] = []

    async def fetch(self, sql, *args):
        if "restriction_events" in sql:
            ids = list(args[0])
            self.asked_about.append(ids)
            return [{"account_id": i} for i in ids if i in self.quarantined]
        return [{"id": i, "cd_db": cd, "acc_status": st}
                for i, cd, st in self.accounts]

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE %d" % len(args[1]) if len(args) > 1 else "UPDATE 1"


def test_quarantined_account_appears_in_the_release_list():
    """Падало: карантин без окна кулдауна не попадал в список никогда."""
    pool = _FleetPool([(1, False, "active"), (2, False, "active")], {2})
    cooled = asyncio.run(account_reset.cooled_account_ids(pool, 99))
    assert 2 in cooled, (
        "аккаунт в карантине операции не берут — владельцу нечем его освободить")
    assert 1 not in cooled, "свободному аккаунту в списке не место"


def test_cooldown_and_quarantine_are_one_list():
    pool = _FleetPool([(1, True, "active"), (2, False, "active"),
                       (3, False, "active")], {2})
    assert sorted(asyncio.run(account_reset.cooled_account_ids(pool, 99))) == [1, 2]


def test_dead_account_is_not_offered_for_release():
    """Снятие риска не воскрешает забаненный аккаунт — кнопке там не место."""
    pool = _FleetPool([(1, False, "banned"), (2, False, "spamblock"),
                       (3, False, "active")], {1, 2, 3})
    cooled = asyncio.run(account_reset.cooled_account_ids(pool, 99))
    assert cooled == [3], (
        "кнопка, которая ничего не меняет, — тот же тупик, что и её отсутствие")
    assert pool.asked_about == [[3]], (
        "про мёртвые аккаунты карантин спрашивать незачем")


def test_quarantine_is_asked_in_one_query_for_the_whole_fleet():
    """На 200 аккаунтах экран не имеет права сделать 200 round-trip."""
    pool = _FleetPool([(i, False, "active") for i in range(1, 201)], {7, 42})
    asyncio.run(account_reset.cooled_account_ids(pool, 99))
    assert len(pool.asked_about) == 1
    assert len(pool.asked_about[0]) == 200


def test_bulk_release_covers_the_quarantined_accounts():
    """Массовый сброс бьёт ровно по тому набору, что владелец видит."""
    pool = _FleetPool([(1, True, "active"), (2, False, "active")], {2})
    asyncio.run(account_reset.reset_all_cooled(pool, 99))
    sql, args = pool.executed[0]
    assert "risk_cleared_at = NOW()" in sql
    assert sorted(args[1]) == [1, 2]


def test_list_survives_a_broken_quarantine_door():
    """Обогащение не критично: кулдауны обязаны показаться и без него."""

    class _Broken(_FleetPool):
        async def fetch(self, sql, *args):
            if "restriction_events" in sql:
                raise RuntimeError("карантин недоступен")
            return await super().fetch(sql, *args)

    pool = _Broken([(1, True, "active"), (2, False, "active")], {2})
    assert asyncio.run(account_reset.cooled_account_ids(pool, 99)) == [1]


# ── Экран бота ─────────────────────────────────────────────────────────────

def _menu_source() -> str:
    dash = (ROOT / "bot" / "handlers" / "health_dashboard.py").read_text(
        encoding="utf-8")
    m = re.search(r"async def cb_reset_cooldown_menu\(.*?\n(?=@router)", dash, re.S)
    assert m, "экран сброса в боте не найден — тест устарел"
    return m.group(0)


def test_screen_does_not_invent_a_countdown_for_quarantine():
    """Падало: у карантина окна нет, а экран писал «ещё скоро»."""
    src = _menu_source()
    assert "карантин" in src.lower(), (
        "экран обязан называть карантин своим именем, а не придумывать время")
    assert '"скоро"' not in src, (
        "«скоро» — время, которого у карантина не существует")


def test_screen_promises_only_what_the_button_does():
    """Заголовок и кнопка говорят про паузы И карантин, раз снимают оба."""
    src = _menu_source()
    assert "Снять паузы и карантин" in src
    assert "Нет активных кулдаунов" not in src, (
        "старый текст обещал, что всё доступно, пока карантин держал аккаунты")
