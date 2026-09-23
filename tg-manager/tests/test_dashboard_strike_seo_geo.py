"""Проход по 5 направлениям (Strike, SEO, Dashboard, Гео, паритет бот↔mini-app)
как единый организм: пульс/сигналы связывают модули.

- Strike: reflex пульса (fail-open) бережёт флагнутые аккаунты от добивания.
- Dashboard: приборный щиток — реальные vitals SEO + гео (owner-scoped, fail-soft).
- Паритет: бот /dashboard показывает те же SEO/гео/карантин, что и mini-app.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_strike_respects_quarantine_fail_open():
    """mass_report не бросает в бой карантинные аккаунты, но fail-open: пустой
    фильтр не обнуляет операцию (лучше рискнуть, чем no-op)."""
    se = _read("services/strike_engine.py")
    seg = se[se.index("viable_accounts = preflight_accounts"):]
    seg = seg[:2000]
    assert "is_account_quarantined" in seg
    assert "if _healthy:" in seg  # пустой фильтр → оставляем исходный список
    assert "except Exception:" in seg  # fail-open


def test_dashboard_has_seo_and_geo_vitals():
    """Дашборд — приборный щиток: реальные SEO (tracked_keywords) + гео
    (global_presence_plans), owner-scoped, fail-soft."""
    api = _read("services/mini_app_api.py")
    assert "async def _seo_vitals" in api and "async def _geo_vitals" in api
    assert "FROM tracked_keywords WHERE owner_id=$1" in api
    assert "FROM global_presence_plans WHERE owner_id=$1" in api
    # реально инъектятся в ответ дашборда
    assert '"seo": await _seo_vitals(uid)' in api
    assert '"geo": await _geo_vitals(uid)' in api
    # Чипы `dk-seo`/`dk-geo` жили во втором «Дашборде метрик» — недостижимом
    # экране, удалённом вместе с его кодом. SEO и гео показывает единый дашборд.
    from tests.miniapp_source import miniapp_source

    ui = miniapp_source()
    assert "d.seo" in ui and "d.geo" in ui, "ответ дашборда читают не полностью"
    assert "seo.tracked_keywords" in ui, "SEO-ключи не выведены на экран"
    assert "geo.plans" in ui, "гео-планы не выведены на экран"


def test_strike_records_outcome_to_memory():
    """Обучение организма: исход страйка пишется в infra_memory (action='strike'),
    чтобы отбор аккаунтов учился. Запись fail-soft."""
    se = _read("services/strike_engine.py")
    start = se.index("async def _strike_one")
    seg = se[start:start + 2200]
    assert "record_account_op" in seg and '"strike"' in seg
    assert "peer_reported" in seg
    assert "except Exception:" in seg  # обучение не должно ронять страйк


def test_strike_orders_by_learned_memory():
    """Замыкание цикла обучения: mass_report сортирует аккаунты по memory-score
    action='strike' (лучшие — вперёд). Стабильно/fail-open (score=0.5 по умолч.)."""
    se = _read("services/strike_engine.py")
    # реордер идёт ПОСЛЕ карантин-фильтра, до разбивки по волнам
    seg = se[se.index("viable_accounts = preflight_accounts"):]
    seg = seg[:3500]
    assert "get_account_score" in seg and '"strike"' in seg
    assert "viable_accounts.sort" in seg


def test_boost_migrated_to_bus():
    """Волна S/1A: boost.py больше не делает прямой INSERT в очередь — только
    через operation_bus.submit(label=…)."""
    b = _read("bot/handlers/boost.py")
    # нет прямой вставки в коде (в комментарии — не считается)
    code_lines = [ln for ln in b.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("INSERT INTO operation_queue" in ln for ln in code_lines)
    assert "operation_bus.submit(" in b and "label=label" in b


def test_seo_surfaces_pending_suggestions():
    """SEO decision-фаза видна: непринятые авто-подсказки реоптимизации в дашборде
    (mini-app) и в боте."""
    api = _read("services/mini_app_api.py")
    assert "pending_suggestions" in api
    assert "FROM bot_seo_suggestions" in api and "applied_at IS NULL" in api
    from tests.miniapp_source import miniapp_source

    # см. комментарий выше: подсказки показывает единый дашборд, а не удалённый
    # второй «Дашборд метрик».
    ui = miniapp_source()
    assert "seo.pending_suggestions" in ui, "непринятые подсказки нигде не видны"
    md = _read("bot/handlers/metrics_dashboard.py")
    assert "bot_seo_suggestions" in md and "applied_at IS NULL" in md


def test_geo_presence_no_dead_buttons():
    """Гео-модуль: каждая кнопка GeoPresenceCb(action=…) имеет обработчик
    F.action == … (трасса кнопка→хендлер, регресс от мёртвых кнопок)."""
    import re
    gp = _read("bot/handlers/global_presence.py")
    used = set(re.findall(r'GeoPresenceCb\(action="([a-z_]+)"', gp))
    handled = set(re.findall(r'F\.action == "([a-z_]+)"', gp))
    missing = used - handled
    assert not missing, f"мёртвые гео-кнопки без обработчика: {sorted(missing)}"


def test_bot_dashboard_parity_seo_geo_pulse():
    """Bot-паритет: /dashboard в боте показывает те же SEO/гео/карантин, что mini-app."""
    md = _read("bot/handlers/metrics_dashboard.py")
    assert "tracked_keywords" in md and "global_presence_plans" in md
    assert "get_account_health" in md and "quarantine" in md
    # источники owner-scoped, обёрнуты в fail-soft
    assert "WHERE owner_id=$1" in md
