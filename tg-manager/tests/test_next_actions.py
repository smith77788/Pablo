"""Регресс-тесты движка Copilot «Что делать дальше» (services/next_actions).

Тестируем чистую функцию build_suggestions на состоянии владельца — без БД.
"""

from services.next_actions import build_suggestions


def _base(**over):
    st = {
        "acc_active": 3,
        "acc_low_trust": 0,
        "acc_no_proxy": 0,
        "proxies_total": 3,
        "proxies_dead": 0,
        "ops_failed_24h": 0,
        "ops_pending": 0,
        "bots": 1,
        "subscribers": 0,
        "dm_running": 0,
        "funnels": 1,
        "auto_rules": 1,
        "channels": 0,
        "ecosystems": 0,
        "parsed_recent": 0,
        "recent_ops": [],
    }
    st.update(over)
    return st


def _ids(sugs):
    return [s["id"] for s in sugs]


def test_empty_account_suggests_add_first_account_top():
    sugs = build_suggestions(_base(acc_active=0, bots=0, funnels=0, auto_rules=0))
    assert sugs[0]["id"] == "add_first_account"
    assert sugs[0]["priority"] == 100
    assert sugs[0]["nav"] == "accounts"


def test_failed_ops_surface_high():
    sugs = build_suggestions(_base(ops_failed_24h=5))
    ids = _ids(sugs)
    assert "review_failed_ops" in ids
    top = sugs[0]
    assert top["id"] == "review_failed_ops"
    assert "5" in top["title"]
    assert top["fn"] == "openOps"


def test_parsed_audience_drives_invite_next_step():
    # Свежая аудитория есть, инвайтов ещё не было → инвайт в приоритете.
    sugs = build_suggestions(_base(parsed_recent=120))
    inv = next(s for s in sugs if s["id"] == "invite_parsed_audience")
    assert inv["priority"] == 90
    assert inv["fn"] == "openMassInvite"


def test_parsed_audience_deprioritised_after_invite():
    sugs = build_suggestions(
        _base(parsed_recent=120, recent_ops=[{"op_type": "mass_invite", "status": "done"}])
    )
    inv = next(s for s in sugs if s["id"] == "invite_parsed_audience")
    assert inv["priority"] == 60


def test_no_proxy_and_dead_proxy_flags():
    sugs = build_suggestions(_base(acc_no_proxy=2, proxies_dead=1))
    ids = _ids(sugs)
    assert "assign_proxies" in ids
    assert "replace_dead_proxies" in ids


def test_low_trust_suggests_warmup():
    sugs = build_suggestions(_base(acc_low_trust=4))
    w = next(s for s in sugs if s["id"] == "warmup_cold_accounts")
    assert "4" in w["title"]
    assert w["fn"] == "openWarmup"


def test_subscribers_without_campaign_suggests_broadcast():
    sugs = build_suggestions(_base(subscribers=500, dm_running=0))
    assert "reach_subscribers" in _ids(sugs)


def test_subscribers_with_running_campaign_no_broadcast_suggestion():
    sugs = build_suggestions(_base(subscribers=500, dm_running=1))
    assert "reach_subscribers" not in _ids(sugs)


def test_bots_without_funnel_and_rules():
    sugs = build_suggestions(_base(funnels=0, auto_rules=0))
    ids = _ids(sugs)
    assert "setup_funnel" in ids
    assert "setup_autoresponder" in ids


def test_channels_without_ecosystem():
    sugs = build_suggestions(_base(channels=3, ecosystems=0))
    e = next(s for s in sugs if s["id"] == "build_ecosystem")
    assert "3" in e["title"]


def test_single_channel_no_ecosystem_suggestion():
    sugs = build_suggestions(_base(channels=1, ecosystems=0))
    assert "build_ecosystem" not in _ids(sugs)


def test_accounts_but_empty_suggests_collect_audience():
    sugs = build_suggestions(
        _base(acc_active=2, bots=0, subscribers=0, parsed_recent=0, funnels=0, auto_rules=0)
    )
    assert "collect_audience" in _ids(sugs)


def test_suggestions_sorted_by_priority_desc():
    sugs = build_suggestions(
        _base(ops_failed_24h=1, acc_no_proxy=1, acc_low_trust=1, funnels=0)
    )
    prios = [s["priority"] for s in sugs]
    assert prios == sorted(prios, reverse=True)


def test_no_duplicate_ids():
    sugs = build_suggestions(_base(acc_no_proxy=1, acc_low_trust=1, ops_failed_24h=1))
    ids = _ids(sugs)
    assert len(ids) == len(set(ids))


def test_every_suggestion_has_a_destination():
    # Каждая подсказка обязана вести в раздел: либо nav (вкладка), либо fn.
    sugs = build_suggestions(
        _base(acc_active=0, bots=0, funnels=0, auto_rules=0, ops_failed_24h=2,
              acc_no_proxy=1, proxies_dead=1, channels=3, parsed_recent=5)
    )
    for s in sugs:
        assert s.get("nav") or s.get("fn"), f"suggestion {s['id']} has no destination"
        assert s.get("title") and s.get("cta")


def test_healthy_account_quiet():
    # Всё настроено и работает — не спамим подсказками (в идеале пусто/мало).
    sugs = build_suggestions(_base(subscribers=100, dm_running=2))
    assert "review_failed_ops" not in _ids(sugs)
    assert "reach_subscribers" not in _ids(sugs)


class _FakePool:
    """Мини-заглушка asyncpg.Pool для интеграционной проверки цепочки
    _gather_state → build_suggestions (без реальной БД)."""

    def __init__(self, vals: dict, ops: list):
        self._vals = vals
        self._ops = ops

    async def fetchval(self, q, *a):
        for frag, v in self._vals.items():
            if frag in q:
                return v
        return 0

    async def fetch(self, q, *a):
        return self._ops


async def test_compute_next_actions_end_to_end():
    from services.next_actions import compute_next_actions

    pool = _FakePool(
        {
            "trust_score": 2,
            "proxy_id IS NULL": 1,
            "is_active=true": 5,
            "is_alive=false": 1,
            "user_proxies": 4,
            "failed": 3,
            "pending": 2,
            "COUNT(*) FROM managed_bots": 2,
            "bot_users": 300,
            "dm_campaigns": 0,
            "funnels": 0,
            "automation_rules": 0,
            "managed_channels": 3,
            "ecosystems": 0,
            "parsed_audiences": 50,
        },
        [{"op_type": "parse_audience", "status": "done"}],
    )
    actions = await compute_next_actions(pool, 123, limit=5)
    assert 1 <= len(actions) <= 5
    ids = [a["id"] for a in actions]
    assert len(ids) == len(set(ids))
    assert actions == sorted(actions, key=lambda z: -z["priority"])
    for a in actions:
        assert a.get("nav") or a.get("fn")
    # Упавшие операции — самый высокий приоритет в этом состоянии.
    assert actions[0]["id"] == "review_failed_ops"


def test_relog_expired_sessions_suggested():
    """Критично (связано с AuthKeyUnregistered при синке контактов): аккаунты с
    acc_status='session_expired' должны давать высокоприоритетную подсказку релога."""
    from services.next_actions import build_suggestions
    r = build_suggestions({"acc_active": 5, "acc_expired": 3, "recent_ops": []})
    relog = [x for x in r if x["id"] == "relog_expired"]
    assert relog, "нет подсказки релога при session_expired"
    assert relog[0]["priority"] == 94 and relog[0]["nav"] == "accounts"
    assert "3" in relog[0]["title"]
    # нет истёкших → нет подсказки
    r2 = build_suggestions({"acc_active": 5, "acc_expired": 0, "recent_ops": []})
    assert not [x for x in r2 if x["id"] == "relog_expired"]


def test_warmup_after_register_context_chain():
    # Недавно регистрировали аккаунты, trust ещё не просел → контекст-прогрев.
    sugs = build_suggestions(
        _base(acc_low_trust=0, recent_ops=[{"op_type": "auto_register", "status": "done"}])
    )
    ids = _ids(sugs)
    assert "warmup_after_register" in ids
    # не дублируем обычный warmup
    assert "warmup_cold_accounts" not in ids


def test_warmup_after_register_suppressed_when_low_trust():
    # Если trust уже просел — показываем обычный warmup, не контекстный (без дубля).
    sugs = build_suggestions(
        _base(acc_low_trust=3, recent_ops=[{"op_type": "auto_register", "status": "done"}])
    )
    ids = _ids(sugs)
    assert "warmup_cold_accounts" in ids
    assert "warmup_after_register" not in ids


def test_engage_after_invite_context_chain():
    sugs = build_suggestions(
        _base(recent_ops=[{"op_type": "mass_invite", "status": "done"}])
    )
    assert "engage_after_invite" in _ids(sugs)


def test_engage_after_invite_not_duplicating_reach_subscribers():
    # Когда уже есть reach_subscribers (боты+подписчики без кампании) — контекст
    # engage не добавляем, чтобы не было двух карточек про рассылку.
    sugs = build_suggestions(
        _base(subscribers=300, dm_running=0,
              recent_ops=[{"op_type": "mass_invite", "status": "done"}])
    )
    ids = _ids(sugs)
    assert "reach_subscribers" in ids
    assert "engage_after_invite" not in ids


def test_fallback_health_for_active_but_quiet_user():
    # Всё настроено, срочного нет → активный пользователь всё равно видит шаг.
    sugs = build_suggestions(_base())  # accounts=3, funnels=1, rules=1, subs=0
    assert "check_account_health" in _ids(sugs)


def test_fallback_health_not_shown_for_empty_account():
    sugs = build_suggestions(_base(acc_active=0, bots=0, funnels=0, auto_rules=0))
    ids = _ids(sugs)
    assert "check_account_health" not in ids
    assert "add_first_account" in ids


def test_fallback_health_suppressed_when_other_suggestion_exists():
    sugs = build_suggestions(_base(ops_failed_24h=2))
    ids = _ids(sugs)
    assert "review_failed_ops" in ids
    assert "check_account_health" not in ids  # fallback только когда иначе пусто


def test_apply_labels_present_on_actionable_suggestions():
    # Подсказки с безопасным действием в один клик несут поле apply.
    failed = build_suggestions(_base(ops_failed_24h=2))
    assert next(x for x in failed if x["id"] == "review_failed_ops").get("apply")
    dead = build_suggestions(_base(proxies_dead=1))
    assert next(x for x in dead if x["id"] == "replace_dead_proxies").get("apply")
    warm = build_suggestions(_base(acc_low_trust=2))
    assert next(x for x in warm if x["id"] == "warmup_cold_accounts").get("apply")
    health = build_suggestions(_base())
    assert next(x for x in health if x["id"] == "check_account_health").get("apply")


def test_apply_absent_on_navigation_only_suggestions():
    # Подсказки, требующие ручного ввода, НЕ имеют apply (нельзя применить в клик).
    empty = build_suggestions(_base(acc_active=0, bots=0, funnels=0, auto_rules=0))
    assert "apply" not in next(x for x in empty if x["id"] == "add_first_account")
    inv = build_suggestions(_base(parsed_recent=50))
    assert "apply" not in next(x for x in inv if x["id"] == "invite_parsed_audience")


def test_build_ecosystem_has_apply():
    sugs = build_suggestions(_base(channels=3, ecosystems=0))
    e = next(x for x in sugs if x["id"] == "build_ecosystem")
    assert e.get("apply") == "Создать из каналов"
    # assign_proxies применяется через вылизанный apply_rotation → есть apply
    p = build_suggestions(_base(acc_no_proxy=2))
    assert next(x for x in p if x["id"] == "assign_proxies").get("apply") == "Назначить прокси"
