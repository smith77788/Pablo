"""Уровень 10: аналитик — «что теперь делать», а не «что произошло».

ЧЕГО НЕ ХВАТАЛО. `/invite/analytics` показывает цифры: столько успешных, столько
флудов, вот график за неделю. Пользователь приходит с другим вопросом — и что
мне с этим делать. Цифры сами не подсказывают, что три конкретных аккаунта пора
вывести из ротации, что у половины флота пустой профиль и он поэтому сам себе
режет лимит, что флота не хватит под заявленную аудиторию или что четыре аккаунта
сидят на одном IP.

Разбор ДЕТЕРМИНИРОВАННЫЙ, и это осознанный выбор, а не упрощение: рекомендации
оперируют числами, по которым пользователь принимает решения о живых аккаунтах.
Языковая модель эти числа уверенно переформулирует — и так же уверенно ошибётся,
а проверить их пользователю неоткуда. Здесь каждая рекомендация несёт числа, на
которых построена.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from services import invite_advisor as ia

ROOT = Path(__file__).resolve().parents[1]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Pool:
    """Пул, отвечающий разными наборами строк на разные запросы аналитика."""

    def __init__(self, *, week=None, accounts=None, boom=False):
        self.week = week or []
        self.accounts = accounts or []
        self.boom = boom

    async def fetch(self, q, *a):
        if self.boom:
            raise RuntimeError("db down")
        if "account_daily_stats" in q:
            return self.week
        if "tg_accounts" in q:
            return self.accounts
        return []

    async def fetchrow(self, q, *a):
        return None


def _acc(**over):
    base = {"id": 1, "label": "+79990000001", "first_name": "Иван",
            "username": "ivan", "has_photo": True, "warmup_level": 3}
    base.update(over)
    return base


def _stat(**over):
    base = {"account_id": 1, "label": "+79990000001", "ok": 50, "failed": 5, "floods": 0}
    base.update(over)
    return base


def _advise(pool, **kw):
    return _run(ia.build_advice(pool, 100, **kw))


def _titles(res):
    return " | ".join(a["title"] for a in res["advice"])


def _sev(res, severity):
    return [a for a in res["advice"] if a["severity"] == severity]


# ── рекомендации по фактам ───────────────────────────────────────────────────

def test_flooded_accounts_are_named():
    res = _advise(_Pool(week=[_stat(floods=3), _stat(account_id=2, label="b", floods=1),
                              _stat(account_id=3, label="c")],
                        accounts=[_acc()]))
    danger = _sev(res, "danger")
    assert danger, "аккаунты с флудами обязаны попасть в разбор"
    named = {a["id"] for a in danger[0]["accounts"]}
    assert named == {1, 2}, "названы должны быть именно виновники, а не весь флот"
    assert "4" in danger[0]["detail"], "число флудов должно быть в тексте — его можно проверить"


def test_poor_conversion_blames_audience_not_pace():
    """Отказы на инвайте — это приватность целей, а не скорость. Совет
    «замедлись» тут был бы вредным: он тратит те же лимиты дольше."""
    res = _advise(_Pool(week=[_stat(ok=10, failed=40)], accounts=[_acc()]))
    warn = [a for a in _sev(res, "warn") if "онверси" in a["title"]]
    assert warn, "низкая конверсия обязана разбираться"
    assert "аудитори" in warn[0]["detail"].lower()


def test_small_sample_is_not_judged():
    res = _advise(_Pool(week=[_stat(ok=2, failed=3)], accounts=[_acc()]))
    assert not [a for a in res["advice"] if "онверси" in a["title"]], (
        "пять попыток — не статистика, вывод по ним хуже отсутствия вывода"
    )


def test_clean_week_is_reported_as_good():
    res = _advise(_Pool(week=[_stat(ok=90, failed=5)], accounts=[_acc()]))
    assert _sev(res, "good"), "чистую неделю тоже нужно назвать — это решение не вмешиваться"


def test_empty_profile_is_flagged():
    res = _advise(_Pool(week=[_stat()],
                        accounts=[_acc(), _acc(id=2, first_name="", label="b")]))
    assert "профил" in _titles(res)


def test_missing_avatar_is_flagged():
    res = _advise(_Pool(week=[_stat()], accounts=[_acc(id=2, has_photo=False)]))
    assert "профил" in _titles(res)


def test_unchecked_avatar_is_not_flagged():
    """has_photo IS NULL = «не проверяли». Ругаться на пробел в НАШИХ данных нельзя."""
    res = _advise(_Pool(week=[_stat()], accounts=[_acc(has_photo=None)]))
    assert "профил" not in _titles(res)


def test_cold_accounts_are_flagged():
    res = _advise(_Pool(week=[_stat()], accounts=[_acc(warmup_level=0)]))
    assert "прогрет" in _titles(res)


# ── хватит ли флота ──────────────────────────────────────────────────────────

def test_capacity_shortfall_is_reported(monkeypatch):
    async def _limit(pool, acc_id):
        return {"limit": 10, "used_today": 0, "remaining": 10, "basis": "тест"}
    from services import flood_engine
    monkeypatch.setattr(flood_engine, "recommended_daily_limit", _limit)

    res = _advise(_Pool(week=[_stat()], accounts=[_acc(), _acc(id=2)]), audience_size=100)
    short = [a for a in res["advice"] if "хватит" in a["title"]]
    assert short, "нехватку лимитов надо назвать ДО запуска, а не показать в итоге"
    assert "20" in short[0]["title"], "должен быть реальный остаток, а не абстракция"


def test_capacity_is_not_guessed_without_audience(monkeypatch):
    async def _limit(pool, acc_id):
        return {"limit": 10, "used_today": 0, "remaining": 10, "basis": "тест"}
    from services import flood_engine
    monkeypatch.setattr(flood_engine, "recommended_daily_limit", _limit)

    res = _advise(_Pool(week=[_stat()], accounts=[_acc()]))
    assert not [a for a in res["advice"] if "хватит" in a["title"]], (
        "не спрашивали размер аудитории — не с чем сравнивать"
    )


def test_exhausted_accounts_are_explained(monkeypatch):
    async def _limit(pool, acc_id):
        return {"limit": 10, "used_today": 10, "remaining": 0, "basis": "тест"}
    from services import flood_engine
    monkeypatch.setattr(flood_engine, "recommended_daily_limit", _limit)

    res = _advise(_Pool(week=[_stat()], accounts=[_acc()]))
    exh = [a for a in res["advice"] if "исчерпал" in a["title"]]
    assert exh and exh[0]["severity"] == "info", (
        "исчерпанный лимит — не ошибка, а защита; пугать им нельзя"
    )


# ── изоляция ─────────────────────────────────────────────────────────────────

def test_shared_ip_is_the_top_risk(monkeypatch):
    async def _iso(pool, owner_id, **kw):
        return {"shared_ip_groups": [{"ip": "1.2.3.4", "account_ids": [1, 2, 3], "count": 3}],
                "accounts_without_proxy": []}
    from services import proxy_selector
    monkeypatch.setattr(proxy_selector, "audit_proxy_isolation", _iso)

    res = _advise(_Pool(week=[_stat()], accounts=[_acc()]))
    ip = [a for a in _sev(res, "danger") if "IP" in a["title"]]
    assert ip, "общий IP для инвайта — худший из рисков, он обязан быть в разборе"
    assert "групп" in ip[0]["detail"] or "пачк" in ip[0]["detail"], (
        "объяснить надо именно групповой характер риска"
    )


def test_accounts_without_proxy_are_flagged(monkeypatch):
    async def _iso(pool, owner_id, **kw):
        return {"shared_ip_groups": [], "accounts_without_proxy": [7, 8]}
    from services import proxy_selector
    monkeypatch.setattr(proxy_selector, "audit_proxy_isolation", _iso)

    res = _advise(_Pool(week=[_stat()], accounts=[_acc()]))
    assert "без прокси" in _titles(res)


# ── честность и устойчивость ─────────────────────────────────────────────────

def test_no_data_is_stated_not_faked():
    res = _advise(_Pool())
    assert res["has_data"] is False, "отсутствие истории должно быть видно вызывающему"


def test_empty_advice_is_a_valid_answer():
    res = _advise(_Pool(week=[_stat()], accounts=[_acc()]))
    assert res["has_data"] is True
    assert res["checked"], "что именно проверяли — тоже часть честного ответа"


def test_db_failure_does_not_break_the_screen():
    res = _advise(_Pool(boom=True))
    assert res["advice"] == [] and res["has_data"] is False, (
        "аналитик, роняющий экран, хуже отсутствующего"
    )


def test_no_llm_in_the_numbers_path():
    """Числа считаются по БД. Модель могла бы их уверенно переформулировать —
    и так же уверенно ошибиться, а проверить пользователю неоткуда."""
    src = ROOT.joinpath("services", "invite_advisor.py").read_text(encoding="utf-8")
    for ghost in ("ai_providers", "openai", "anthropic", "ai_claude"):
        assert ghost not in src, f"{ghost} в пути расчёта чисел — недопустимо"


# ── проводка ─────────────────────────────────────────────────────────────────

def test_route_is_registered():
    api = ROOT.joinpath("services", "mini_app_api.py").read_text(encoding="utf-8")
    assert '"/api/miniapp/invite/advice"' in api, "иначе экран стучится в никуда"


def test_card_exposes_factors():
    api = ROOT.joinpath("services", "mini_app_api.py").read_text(encoding="utf-8")
    assert '"limit_factors"' in api, (
        "карточка должна показывать не только ЧТО решено, но и по каким признакам"
    )


def test_ui_renders_advice_and_card():
    html = ROOT.joinpath("mini_app", "index.html").read_text(encoding="utf-8")
    assert 'id="invAdvice"' in html and "loadInviteAdvice" in html, "блок разбора"
    assert 'id="s-invacc"' in html and "openInviteAccount" in html, "экран карточки"
    assert "loadInviteAdvice();" in html, "разбор должен грузиться при открытии экрана"
    # Карточка открывается из списка аккаунтов, иначе до неё не добраться.
    assert re.search(r"openInviteAccount\(\$\{a\.id\}\)", html), (
        "экран без входа — тупик"
    )


# ── почему инвайт не стартует: невидимые блокеры ─────────────────────────────
# Жалоба «всё ещё не инвайтит» упиралась в три причины, о которых экран молчал:
# предохранитель op_worker (все операции владельца на паузе), отсутствие
# аккаунтов с сессией и поголовный карантин. Каждая честно роняла операцию —
# но узнать об этом можно было только из истории операций.

def test_open_circuit_breaker_is_surfaced(monkeypatch):
    from services import op_worker
    async def _open(uid):
        return {"status": "open", "failures": 3, "cooldown_remaining_s": 900}
    monkeypatch.setattr(op_worker, "circuit_breaker_status", _open)
    res = _advise(_Pool(week=[_stat()], accounts=[_acc()]))
    pause = [a for a in _sev(res, "danger") if "пауз" in a["title"].lower()]
    assert pause, (
        "операции стоят на паузе, а экран об этом молчит — снаружи это "
        "неотличимо от «продукт не работает»"
    )
    assert "15" in pause[0]["title"], "остаток паузы должен быть назван в минутах"
    assert "не пропадёт" in pause[0]["detail"], (
        "надо сказать, что запущенное стартует само — иначе пользователь начнёт "
        "перезапускать и продлит паузу"
    )


def test_closed_breaker_is_silent(monkeypatch):
    from services import op_worker
    async def _closed(uid):
        return {"status": "closed", "failures": 0}
    monkeypatch.setattr(op_worker, "circuit_breaker_status", _closed)
    res = _advise(_Pool(week=[_stat()], accounts=[_acc()]))
    assert not [a for a in res["advice"] if "пауз" in a["title"].lower()]


def test_no_usable_accounts_is_the_first_thing_said():
    """Самая частая причина «инвайт не работает»: приглашать физически нечем."""
    res = _advise(_Pool(week=[], accounts=[]))
    none_acc = [a for a in _sev(res, "danger") if "нет аккаунтов" in a["title"].lower()]
    assert none_acc, "отсутствие пригодных аккаунтов обязано называться прямо"
    assert "сесси" in none_acc[0]["detail"], "нужно объяснить, чего именно не хватает"


def test_all_accounts_quarantined_is_a_blocker(monkeypatch):
    from services import infra_memory
    async def _quar(pool, acc_id):
        return True
    monkeypatch.setattr(infra_memory, "is_account_quarantined", _quar)
    res = _advise(_Pool(week=[_stat()], accounts=[_acc(), _acc(id=2)]))
    q = [a for a in _sev(res, "danger") if "риск-пульс" in a["title"]]
    assert q, "если пропустят всех — инвайт не сделает ничего, это надо сказать заранее"


def test_partial_quarantine_is_a_warning_not_a_blocker(monkeypatch):
    from services import infra_memory
    async def _quar(pool, acc_id):
        return int(acc_id) == 1
    monkeypatch.setattr(infra_memory, "is_account_quarantined", _quar)
    res = _advise(_Pool(week=[_stat()], accounts=[_acc(), _acc(id=2)]))
    q = [a for a in _sev(res, "warn") if "риск-пульс" in a["title"]]
    assert q, "частичный карантин снижает ёмкость — предупредить стоит"
    assert "1 из 2" in q[0]["title"], "числа должны быть настоящими"


def test_quarantine_failure_does_not_break_the_advisor(monkeypatch):
    from services import infra_memory
    async def _boom(pool, acc_id):
        raise RuntimeError("нет пульса")
    monkeypatch.setattr(infra_memory, "is_account_quarantined", _boom)
    res = _advise(_Pool(week=[_stat()], accounts=[_acc()]))
    assert isinstance(res["advice"], list), "сбой проверки не имеет права ронять разбор"


def test_db_failure_never_claims_there_are_no_accounts():
    """«Аккаунтов нет» и «мы не смогли их спросить» — разные факты.

    Ранняя версия `_fetch` возвращала [] в обоих случаях, и при сбое БД разбор
    уверенно сообщал «нет аккаунтов, которыми можно приглашать» — врал с видом
    точности ровно там, где пользователь принимает решение.
    """
    res = _advise(_Pool(boom=True))
    assert not [a for a in res["advice"] if "нет аккаунтов" in a["title"].lower()], (
        "при сбое запроса нельзя утверждать, что аккаунтов нет"
    )


def test_empty_result_and_failed_query_are_distinguishable():
    import inspect
    from services import invite_advisor
    src = inspect.getsource(invite_advisor._fetch)
    assert "return None" in src, "сбой запроса обязан отличаться от пустого ответа"
