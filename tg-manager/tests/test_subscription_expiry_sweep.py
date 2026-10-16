"""Регресс: истёкшие подписки самолечатся в обоих хранилищах.

get_plan дата-aware (`WHERE expires_at > now()`), поэтому доступ снимается
вовремя. Но денормализованная копия — platform_users.current_plan и флаг
subscriptions.is_active — после истечения остаются «paid»/true навсегда и
подтекают в админ-CSV/сегментацию/фолбэки чтения. Часовой свип
`expire_stale_subscriptions` возвращает их в согласие; идемпотентен (только
реально истёкшие строки).
"""
from __future__ import annotations

import inspect
import re

from services import scheduler


def test_sweep_function_exists_and_touches_both_stores():
    src = inspect.getsource(scheduler.expire_stale_subscriptions)
    # деактивирует истёкшие подписки строго по дате
    assert re.search(
        r"UPDATE subscriptions SET is_active=false\s*\"?\s*\n?\s*\"?WHERE is_active=true AND expires_at <= now\(\)",
        src,
    ), "свип должен гасить только истёкшие subscriptions"
    # возвращает platform_users на free строго по дате
    assert "current_plan='free'" in src and "plan_expires_at IS NOT NULL AND plan_expires_at <= now()" in src, (
        "свип должен сбрасывать platform_users только для истёкших строк"
    )


def test_sweep_only_touches_expired_rows():
    """Гейт идемпотентности: НИ один UPDATE в свипе не должен затрагивать
    свежие/активные строки — у каждого обязателен предикат `<= now()`."""
    src = inspect.getsource(scheduler.expire_stale_subscriptions)
    updates = re.findall(r"UPDATE \w+ SET.*?(?=\"\s*\)|\Z)", src, re.DOTALL)
    assert updates, "не найдено UPDATE в свипе"
    for u in updates:
        assert "<= now()" in u, f"UPDATE без границы истечения (заденет живые подписки): {u[:80]}"


def test_sweep_wired_into_scheduler_hourly():
    run_src = inspect.getsource(scheduler.run)
    # вызывается в том же часовом такте, что и AB-свип
    assert "expire_stale_subscriptions(pool)" in run_src, "свип не подключён к циклу планировщика"
    m = re.search(r"if _ab_sweep_cycle >= 60:(.*?)await asyncio\.sleep", run_src, re.DOTALL)
    assert m and "expire_stale_subscriptions" in m.group(1), (
        "свип должен идти в часовом такте (внутри `if _ab_sweep_cycle >= 60`)"
    )
