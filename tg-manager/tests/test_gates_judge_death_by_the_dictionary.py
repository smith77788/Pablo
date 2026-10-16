"""Гейты на питоне судили о смерти аккаунта своими множествами.

SQL-условия выборки свели в одну дверь (`account_status.sql_not_dead`), и
сразу стало видно, что та же дыра осталась на питоне: условие стояло не в
запросе, а в коде, множеством из трёх-пяти значений. Статусы `deleted` и
`frozen` добавили в словарь, массовая дверь перестала брать такие аккаунты —
а эти проверки продолжали считать их рабочими.

Чем это стоило дороже всего:

  * `op_worker._INVITE_UNSAFE_STATUS` — отсев перед МАССОВЫМ ИНВАЙТОМ, самой
    баноопасной операцией продукта. Аккаунт со статусом `deleted` или `frozen`
    шёл инвайтить;
  * `trust_engine` опускал доверие только забаненным и деактивированным:
    удалённый и замороженный аккаунт оставался в верху рейтинга доверия, то
    есть первым кандидатом там, где продукт берёт «самых надёжных»;
  * `warmup_status.can_resume` разрешал возобновить прогрев на
    удалённом и замороженном аккаунте — гнать по нему действия ровно там, где
    это только добивает;
  * `account_warmer` расходился сам с собой: дневной цикл отозванную сессию
    пропускал, сессионный — брал и жёг коннекты вслепую;
  * `strike_engine.preflight_accounts`, `invite_overflow.reserve_options`,
    `infra_analytics`, `intelligence_engine` — то же в своих воротах;
  * `tg_cloud.mark_dead_for_banned_storekeepers` помечал потерянные реплики
    только по двум статусам: кусок на деактивированном аккаунте числился
    живой копией, и файл считал себя избыточным, не будучи им.

Отдельно проверяем, что сведение не стало ОСЛАБЛЕНИЕМ: шире безвозвратных
статусов хранителя реплик брать нельзя, иначе heal объявит невосстановимым
файл, данные которого на месте.
"""
from __future__ import annotations

import inspect
import re

from services import account_status as acc
from services import (
    account_health,
    account_warmer,
    invite_overflow,
    strike_engine,
    tg_cloud,
    trust_engine,
    warmup_status,
)

EFFECTIVE_ONLY = acc.EFFECTIVE_DEAD_STATUSES - acc.DEAD_STATUSES


# ── Словарь: разбиение полное, без пересечений, с русскими подписями ────────

def test_the_dictionary_is_partitioned_by_what_to_do():
    assert (acc.LOST_STATUSES | acc.RESTRICTED_STATUSES
            | acc.SESSION_STATUSES) == acc.DEAD_STATUSES
    groups = (acc.LOST_STATUSES, acc.RESTRICTED_STATUSES, acc.SESSION_STATUSES)
    for i, left in enumerate(groups):
        for right in groups[i + 1:]:
            assert not (left & right), sorted(left & right)


def test_every_status_has_a_russian_label():
    """Код статуса не имеет права попасть владельцу на экран как есть."""
    for status in sorted(acc.EFFECTIVE_DEAD_STATUSES):
        label = acc.ru_label(status)
        assert label != status, f"нет русской подписи для {status}"
        assert not re.search(r"[A-Za-z]", label.replace("Telegram", "")), label


def test_effective_set_is_a_superset_of_the_dictionary():
    assert acc.DEAD_STATUSES < acc.EFFECTIVE_DEAD_STATUSES
    assert EFFECTIVE_ONLY == {"archived", "no_session"}


def test_status_list_for_sql_refuses_a_status_outside_the_dictionary():
    """Список для SQL подставляется в текст запроса — опечатку надо ловить."""
    import pytest
    with pytest.raises(ValueError):
        acc.sql_status_list({"активный"})


# ── Инвайт: самая баноопасная операция ──────────────────────────────────────

def test_invite_rejects_every_effectively_dead_status():
    from services import op_worker

    assert op_worker._INVITE_UNSAFE_STATUS == acc.EFFECTIVE_DEAD_STATUSES
    for status in ("deleted", "frozen"):
        assert status in op_worker._INVITE_UNSAFE_STATUS, status


class _StatusPool:
    """Пул, отдающий статусы аккаунтов — только то, что нужно отсеву."""

    def __init__(self, statuses: dict[int, str]):
        self.statuses = statuses

    async def fetch(self, sql, *args):
        return [{"id": i, "acc_status": s} for i, s in self.statuses.items()]


async def test_invite_filter_drops_a_deleted_account():
    from services import op_worker

    pool = _StatusPool({1: "active", 2: "deleted", 3: "frozen"})
    kept = await op_worker._filter_unready_for_invite(
        pool, 1, [{"id": 1}, {"id": 2}, {"id": 3}])
    assert [a["id"] for a in kept] == [1]


async def test_invite_filter_stays_fail_open_when_all_are_dead():
    """Отсев не имеет права обнулить операцию целиком — это прежний контракт."""
    from services import op_worker

    pool = _StatusPool({1: "banned", 2: "deleted"})
    kept = await op_worker._filter_unready_for_invite(pool, 1, [{"id": 1}, {"id": 2}])
    assert len(kept) == 2


# ── Доверие ────────────────────────────────────────────────────────────────

def test_trust_zeroes_the_lost_and_caps_the_restricted():
    src = inspect.getsource(trust_engine._recalculate_scores)
    assert "sql_status_list" in src, "доверие снова судит по своему списку"
    assert "LOST_STATUSES" in src and "RESTRICTED_STATUSES" in src
    # Набор обнуления — ровно безвозвратные: спам-блок лечится, обнулять его
    # доверие насовсем было бы потерей рабочего аккаунта.
    assert "'deleted'" in acc.sql_status_list(acc.LOST_STATUSES)
    assert "spamblock" not in acc.sql_status_list(acc.LOST_STATUSES)


def test_restricted_trust_cap_covers_the_frozen_and_the_expired():
    capped = acc.sql_status_list(acc.RESTRICTED_STATUSES | acc.SESSION_STATUSES)
    for status in ("frozen", "session_expired", "spamblock"):
        assert f"'{status}'" in capped, status


# ── Прогрев ────────────────────────────────────────────────────────────────

def test_warmup_cannot_be_resumed_on_any_dead_status():
    for status in sorted(acc.DEAD_STATUSES):
        ok, why = warmup_status.can_resume(None, status, True)
        assert ok is False, f"прогрев разрешён на статусе {status}"
        assert why, status
        assert not re.search(r"[A-Za-z]", why.replace("Telegram", "")), why


def test_warmup_is_still_allowed_on_a_healthy_account():
    """Самопроверка: сведение не запретило прогрев вообще."""
    for status in ("active", "warming", None):
        ok, _ = warmup_status.can_resume(None, status, True)
        assert ok is True, status


def test_both_warmup_cycles_use_the_same_dictionary():
    daily = inspect.getsource(account_warmer._run_daily_warmup_impl)
    session = inspect.getsource(account_warmer._run_warmup_session_impl)
    assert "_acc_status.is_dead(" in daily
    assert "_acc_status.is_dead(" in session


# ── Остальные ворота ───────────────────────────────────────────────────────

def test_strike_never_takes_a_dead_account():
    src = inspect.getsource(strike_engine.preflight_accounts)
    assert "_acc_status.is_dead(" in src, "страйк судит по своему списку"


def test_strike_preflight_drops_an_expired_session():
    accounts = [{"id": 1, "is_active": True, "acc_status": "active"},
                {"id": 2, "is_active": True, "acc_status": "session_expired"},
                {"id": 3, "is_active": True, "acc_status": "frozen"}]
    kept = {a["id"] for a in strike_engine.preflight_accounts(accounts)}
    assert kept == {1}, kept


def test_channel_admin_is_called_unusable_on_any_dead_status():
    src = inspect.getsource(invite_overflow.reserve_options)
    assert "_acc_status.is_dead(" in src


def test_account_sorter_cuts_the_dead_in_the_query_not_after_the_limit():
    src = inspect.getsource(account_health.get_sorted_accounts)
    assert "_ACC_NOT_DEAD" in src, "мёртвые снова отсеиваются после LIMIT"
    assert "LIMIT $2" in src
    assert src.index("_ACC_NOT_DEAD") < src.index("LIMIT $2"), (
        "условие обязано стоять в WHERE, иначе LIMIT срезает живых")
    assert "is_effectively_dead" in src, "эффективный статус всё ещё проверяем"


def test_recommendation_engine_judges_by_the_effective_set():
    from services import intelligence_engine

    src = inspect.getsource(intelligence_engine._analyze_accounts_impl)
    assert "_acc_status.is_effectively_dead(" in src


# ── Облако: сведение не имеет права стать ослаблением ───────────────────────

def test_lost_keeper_marks_its_replicas_dead():
    src = inspect.getsource(tg_cloud.mark_dead_for_banned_storekeepers)
    assert "LOST_STATUSES" in src, "хранитель реплик судится по своему списку"
    listed = acc.sql_status_list(acc.LOST_STATUSES)
    assert "'deactivated'" in listed


def test_a_recoverable_keeper_is_not_marked_dead():
    """Шире безвозвратных брать нельзя: помеченная реплика для сборки не
    существует, а спам-блок и заморозка снимаются — данные ещё читаются."""
    listed = acc.sql_status_list(acc.LOST_STATUSES)
    for status in ("spamblock", "frozen", "session_expired"):
        assert f"'{status}'" not in listed, status
