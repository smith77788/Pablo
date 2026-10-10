"""Четвёртый вид паузы жил в памяти воркера, и снять его было нечем.

ЧТО БЫЛО. `account_health` держит процесс-локальный кеш: `health_score` (0..100)
и флаги `suitability` (invite, dm, create, post, join). На бане или спам-блоке
`update_after_failure` обнуляет счётчик и гасит ВСЕ флаги. Последствия идут
дальше базы:

  * риск-пульс считает аккаунт выбывшим при `health_score` ниже порога — в
    списке аккаунтов это «🛑 На паузе»;
  * `account_health.get_sorted_accounts` молча выбрасывает аккаунт из подбора
    под каждое действие, у которого флаг снят.

Ни то, ни другое не видно ни в `tg_accounts`, ни в кулдауне `flood_engine`.
Экран «Снять паузы и карантин» спрашивал про три вида паузы и про этот не
спрашивал: аккаунт, которого держала только память процесса, в списке не
появлялся, и кнопка отвечала «✅ Нет активных кулдаунов — все аккаунты
доступны». Четвёртый способ получить жалобу «кулдаун не сбрасывается».

А сам сброс снимал два флага из пяти (`dm`, `invite`) и `health_score` не
трогал вовсе. То есть даже дойдя до кнопки, владелец получал аккаунт, который
по-прежнему не берут в `create`, `post` и `join`, а пульс по-прежнему светит
«на паузе» — и причину было не видно.

ЧЕГО ЭТОТ ТЕСТ НЕ ТРЕБУЕТ. Сброс не выдумывает хорошую оценку: счётчики
возвращаются к НЕЙТРАЛЬНОМУ значению свежего воркера (100 — «ничего не
знаем»), а настоящую оценку заново считает `load_from_db` из базы. История
ограничений в БД не удаляется никогда — её фильтрует `risk_cleared_at`.
"""
from __future__ import annotations

import asyncio

from services import account_health as ah
from services import account_reset


def _fresh(acc_id: int):
    ah._health_cache.pop(acc_id, None)
    return ah.get_health(acc_id)


# ── Предикат «держит память процесса» ──────────────────────────────────────

def test_an_unknown_account_is_not_held():
    """Свежий воркер ничего не помнит — и не имеет права никого держать."""
    ah._health_cache.pop(901, None)
    assert ah.local_block_reason(901) is None


def test_a_healthy_account_is_not_held():
    _fresh(902)
    assert ah.local_block_reason(902) is None


def test_a_banned_account_is_held_and_says_why():
    _fresh(903)
    ah.update_after_failure(903, is_ban=True)
    why = ah.local_block_reason(903)
    assert why, "аккаунт после бана память процесса держит, а экран не видит"
    assert not any(c.isascii() and c.isalpha() for c in why), why


def test_a_single_switched_off_action_is_enough_to_be_held():
    """Подбор под это действие аккаунт уже не берёт — значит, он на паузе."""
    health = _fresh(904)
    health.suitability["post"] = False
    why = ah.local_block_reason(904)
    assert why and "post" in why, why


def test_the_threshold_is_one_number_for_the_pulse_and_the_screen():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "services" / "infra_memory.py").read_text(encoding="utf-8")
    assert "QUARANTINE_SCORE" in src, (
        "риск-пульс снова судит по своему числу — экран снятия пауз о нём не "
        "узнает")
    assert ah.QUARANTINE_SCORE < ah.RISK_SCORE


# ── Сброс снимает ВСЮ память процесса ──────────────────────────────────────

def test_clearing_returns_every_action_not_just_two():
    _fresh(905)
    ah.update_after_failure(905, is_ban=True)
    ah.clear_local_blocks(905)
    health = ah.get_health(905)
    assert all(health.suitability.values()), health.suitability
    assert ah.local_block_reason(905) is None


def test_clearing_returns_the_score_to_the_neutral_value():
    _fresh(906)
    ah.update_after_failure(906, is_ban=True)
    assert ah.get_health(906).health_score == 0.0
    ah.clear_local_blocks(906)
    assert ah.get_health(906).health_score == 100.0, (
        "пульс продолжит считать аккаунт выбывшим после ручного снятия паузы")


def test_clearing_an_unknown_account_does_not_invent_a_record():
    ah._health_cache.pop(907, None)
    ah.clear_local_blocks(907)
    assert 907 not in ah._health_cache


# ── Экран снятия пауз показывает такой аккаунт ─────────────────────────────

class _Pool:
    def __init__(self, rows):
        self.rows = rows
        self.executed: list[tuple] = []

    async def fetch(self, q, *a):
        if "restriction_events" in q:
            return []
        return [dict(r) for r in self.rows]

    async def execute(self, q, *a):
        self.executed.append((q, a))
        return "UPDATE 1"


def _rows(*ids):
    return [{"id": i, "acc_status": "active", "cd_db": False} for i in ids]


def test_an_account_held_only_by_memory_is_listed_as_paused():
    _fresh(908)
    ah.update_after_failure(908, is_ban=True)
    cooled = asyncio.run(account_reset.cooled_account_ids(_Pool(_rows(908)), 7))
    assert cooled == [908], (
        "экран снова отвечает «нет активных кулдаунов» о том, кого продукт не "
        "берёт")


def test_a_healthy_fleet_is_still_reported_as_free():
    """Самопроверка пробника: на здоровом флоте список обязан быть пустым."""
    for acc_id in (909, 910):
        _fresh(acc_id)
    cooled = asyncio.run(account_reset.cooled_account_ids(_Pool(_rows(909, 910)), 7))
    assert cooled == []


def test_a_dead_account_is_not_offered_for_release():
    """Снятие риска забаненного не воскрешает — кнопка не должна его обещать."""
    _fresh(911)
    ah.update_after_failure(911, is_ban=True)
    rows = [{"id": 911, "acc_status": "banned", "cd_db": False}]
    cooled = asyncio.run(account_reset.cooled_account_ids(_Pool(rows), 7))
    assert cooled == []


def test_the_release_actually_frees_the_account():
    """Путь целиком: аккаунт в списке → сброс → в списке больше нет."""
    _fresh(912)
    ah.update_after_failure(912, is_ban=True)
    pool = _Pool(_rows(912))
    assert asyncio.run(account_reset.cooled_account_ids(pool, 7)) == [912]
    assert asyncio.run(account_reset.reset_account(pool, 912, 7)) is True
    assert asyncio.run(account_reset.cooled_account_ids(pool, 7)) == []


# ── Экран снятия пауз называет причину, а не угадывает её ───────────────────

def test_the_release_screen_does_not_blame_restrictions_for_everything():
    """Строка «карантин по недавним ограничениям» — только для карантина.

    Аккаунт без окна паузы экран подписывал этой строкой всегда, в том числе
    когда ограничений у него нет вовсе и держит его память воркера. Та же ложь,
    что была здесь с «ещё скоро» — время, которого не существует.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "bot" / "handlers" / "health_dashboard.py").read_text(encoding="utf-8")
    menu = src[src.index('F.action == "reset_cooldown_menu"'):]
    menu = menu[:menu.index("@router.callback_query", 10)]
    assert "quarantined_accounts" in menu, (
        "экран не спрашивает, кто под карантином, и подписывает им всех")
    assert "local_block_reason" in menu, (
        "причину из памяти воркера экран не показывает")
    # Последнее вхождение — сама подпись; первое живёт в комментарии над
    # запросом карантина.
    where_quar = menu.rindex("карантин по недавним")
    assert "in quarantined" in menu[:where_quar], (
        "подпись про ограничения стоит не под проверкой карантина")
