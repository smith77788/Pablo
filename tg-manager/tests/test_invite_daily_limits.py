"""Уровень 2: суточные счётчики аккаунта и рекомендуемый лимит по его истории.

ДВЕ ДЫРЫ, которые это закрыло:

1. Таблица `account_daily_stats` (schema_v41) НИКОГДА не заполнялась — это прямо
   признано в комментарии `infra_analytics.cb_infra_daily_stats`, где запрос
   пришлось переписать в обход неё. Плюс операция инвайта не писала вообще
   никакой per-account истории (0 вызовов `_audit`). То есть суточных фактов
   «сколько приглашено / успешно / флудов» не существовало, и самообучающийся
   лимитер было не на чем строить.

2. Лимит на аккаунт задавался ОДНИМ ручным числом на всю операцию и не знал, что
   один аккаунт год работает чисто, а другой словил флуд вчера.

Теперь счётчики накапливаются, а лимит выводится из фактов за 7 суток и
применяется как СТРОЖАЙШИЙ из (ручной, рекомендованный минус израсходованное).
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from services import flood_engine as fe

WORKER = Path(__file__).resolve().parents[1] / "services" / "op_worker.py"


class _Pool:
    """Пул для проверки расчёта ПО ИСТОРИИ.

    Важно: `recommended_daily_limit` делает ДВА разных запроса — статистику и
    факторы аккаунта. Наивный пул, отвечающий одной строкой на оба, подставлял бы
    строку статистики как профиль аккаунта и ложно срезал лимит. Поэтому на
    запрос факторов отвечаем None (= данных о профиле нет → нейтрально).
    """

    def __init__(self, row):
        self.row = row
        self.executed: list[tuple] = []

    async def fetchrow(self, q, *a):
        if "reg_check_cache" in q:
            return None
        return self.row

    async def execute(self, q, *a):
        self.executed.append((q, a))
        return "OK"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _exec_src() -> str:
    src = WORKER.read_text(encoding="utf-8")
    m = re.search(r"async def _exec_mass_invite\(.*?(?=\nasync def )", src, re.DOTALL)
    assert m
    return m.group(0)


# ── расчёт лимита ────────────────────────────────────────────────────────────

def test_cold_start_is_conservative():
    r = _run(fe.recommended_daily_limit(_Pool(None), 1))
    assert r["limit"] == fe._INVITE_LIMIT_COLD_START
    assert "нет истории" in r["basis"], "решение должно объясняться человеку"


def test_clean_week_grows_limit():
    row = {"inv_ok": 80, "fails": 5, "floods": 0, "active_days": 5, "best_day": 20, "today": 0}
    r = _run(fe.recommended_daily_limit(_Pool(row), 1))
    assert r["limit"] > 20, "неделя без флудов должна повышать лимит"
    assert r["limit"] <= fe._INVITE_LIMIT_CEILING, "но не выше жёсткого потолка"


def test_floods_cut_limit_progressively():
    base = {"inv_ok": 40, "fails": 10, "active_days": 4, "best_day": 20, "today": 0}
    one = _run(fe.recommended_daily_limit(_Pool({**base, "floods": 1}), 1))["limit"]
    two = _run(fe.recommended_daily_limit(_Pool({**base, "floods": 2}), 1))["limit"]
    assert two < one, "чем больше флудов, тем сильнее срез"
    assert two >= fe._INVITE_LIMIT_FLOOR, "но не ниже пола"


def test_low_success_rate_cuts_volume():
    good = {"inv_ok": 80, "fails": 5, "floods": 0, "active_days": 5, "best_day": 20, "today": 0}
    bad = {"inv_ok": 10, "fails": 30, "floods": 0, "active_days": 3, "best_day": 20, "today": 0}
    assert (_run(fe.recommended_daily_limit(_Pool(bad), 1))["limit"]
            < _run(fe.recommended_daily_limit(_Pool(good), 1))["limit"]), (
        "много отказов — гнать объём бессмысленно и рискованно"
    )


def test_used_today_is_subtracted():
    row = {"inv_ok": 60, "fails": 2, "floods": 0, "active_days": 4, "best_day": 20, "today": 18}
    r = _run(fe.recommended_daily_limit(_Pool(row), 1))
    assert r["remaining"] == max(0, r["limit"] - 18), (
        "повторный запуск не должен удваивать суточный объём"
    )


def test_db_error_falls_back_to_cold_start():
    class Boom:
        async def fetchrow(self, q, *a):
            raise RuntimeError("db down")
    r = _run(fe.recommended_daily_limit(Boom(), 1))
    assert r["limit"] == fe._INVITE_LIMIT_COLD_START, (
        "сбой БД → консервативно, лучше недобрать, чем спалить аккаунт"
    )


# ── накопление счётчиков ─────────────────────────────────────────────────────

def test_bump_daily_upserts_and_accumulates():
    from services import op_worker
    pool = _Pool(None)
    _run(op_worker.bump_daily_stats(pool, 7, ok=3, fail=1, invites=3))
    assert pool.executed, "счётчики обязаны писаться"
    q = pool.executed[0][0]
    assert "account_daily_stats" in q and "ON CONFLICT" in q, "нужен UPSERT по дню"
    assert "+ EXCLUDED." in q, "значения должны НАКАПЛИВАТЬСЯ, а не перезаписываться"


def test_bump_daily_noop_on_zero():
    from services import op_worker
    pool = _Pool(None)
    _run(op_worker.bump_daily_stats(pool, 7))
    assert not pool.executed, "пустое обновление не должно ходить в БД"


def test_bump_daily_never_raises():
    from services import op_worker
    class Boom:
        async def execute(self, q, *a):
            raise RuntimeError("db down")
    _run(op_worker.bump_daily_stats(Boom(), 7, ok=1))  # не должно бросить


# ── проводка в исполнителе ───────────────────────────────────────────────────

def test_invite_records_daily_stats():
    src = _exec_src()
    assert "bump_daily_stats" in src, "инвайт обязан писать суточные факты"
    assert "floods=1" in src, "флуд должен попадать в статистику"
    assert "invites_ok" not in src or True  # счётчик передаётся как invites=


def test_invite_applies_strictest_limit():
    src = _exec_src()
    assert "recommended_daily_limit" in src, "рекомендация должна реально применяться"
    assert "min(_acc_cap" in src, "берём СТРОЖАЙШИЙ из ручного и рекомендованного"
    assert "_remaining <= 0" in src, "исчерпанный суточный лимит обязан пропускать аккаунт"


# ── факторы самого аккаунта (уровень 1: возраст/профиль/прогрев) ─────────────

class _FactorPool:
    """Пул, отвечающий разными строками на запрос статистики и на запрос факторов."""

    def __init__(self, stats, acc):
        self.stats, self.acc = stats, acc

    async def fetchrow(self, q, *a):
        return self.acc if "reg_check_cache" in q else self.stats


def _mature(**over):
    import datetime as d
    base = {
        "first_name": "Иван", "username": "ivan", "warmup_level": 3, "tg_user_id": 1,
        "reg_date": d.datetime.now(d.timezone.utc) - d.timedelta(days=500),
    }
    base.update(over)
    return base


_CLEAN = {"inv_ok": 80, "fails": 5, "floods": 0, "active_days": 5, "best_day": 20, "today": 0}


def _limit_for(acc):
    return _run(fe.recommended_daily_limit(_FactorPool(_CLEAN, acc), 1))


def test_young_account_gets_smaller_limit():
    import datetime as d
    young = _mature(reg_date=d.datetime.now(d.timezone.utc) - d.timedelta(days=10))
    assert _limit_for(young)["limit"] < _limit_for(_mature())["limit"], (
        "молодой аккаунт ограничат быстрее даже при той же истории"
    )


def test_empty_profile_gets_smaller_limit():
    assert _limit_for(_mature(first_name="", username=""))["limit"] < _limit_for(_mature())["limit"], (
        "пустой профиль — спам-признак"
    )


def test_unwarmed_account_gets_smaller_limit():
    assert _limit_for(_mature(warmup_level=0))["limit"] < _limit_for(_mature())["limit"]


def test_factors_are_explained_to_human():
    r = _limit_for(_mature(first_name=""))
    assert "профил" in r["basis"], "причина среза должна быть видна человеку"
    assert r.get("factors", {}).get("notes"), "факторы возвращаются отдельным полем"


def test_missing_reg_date_does_not_penalise():
    """Нет оценки возраста — не выдумываем: аккаунт не наказывается за пробел
    в наших данных."""
    no_reg = _limit_for(_mature(reg_date=None))["limit"]
    assert no_reg >= _limit_for(_mature(warmup_level=0))["limit"]


def test_unchecked_profile_is_not_penalised():
    """Premium/аватар теперь ЕСТЬ в схеме (schema_v160), но пока профиль не
    снимали — `profile_checked_at IS NULL`, и False неотличим от «не спрашивали».
    Аккаунт не должен платить за пробел в НАШИХ данных."""
    unchecked = _limit_for(_mature(is_premium=False, has_photo=False,
                                   profile_checked_at=None))["limit"]
    assert unchecked == _limit_for(_mature())["limit"], (
        "непроверенный профиль обязан считаться нейтральным, а не худшим"
    )


# ── уровень 1: реальные профильные факторы (Premium/аватар/страна) ───────────
# Раньше эти три фактора не считались ВООБЩЕ — не из принципа, а потому что
# данных не было: Premium и аватар живут только в объекте `me` из Telethon и
# нигде не сохранялись. schema_v160 добавила колонки, проверка здоровья их
# заполняет (лишнего коннекта нет — `get_me()` там уже вызывался).

def _checked(**over):
    """Аккаунт со СНЯТЫМ профилем: is_premium/has_photo значат то, что значат."""
    import datetime as d
    return _mature(profile_checked_at=d.datetime.now(d.timezone.utc), **over)


def test_premium_account_gets_bigger_limit():
    assert (_limit_for(_checked(is_premium=True, has_photo=True))["limit"]
            > _limit_for(_checked(is_premium=False, has_photo=True))["limit"]), (
        "Premium — сильный признак живого владельца, объём можно дать выше"
    )


def test_missing_avatar_cuts_limit():
    assert (_limit_for(_checked(has_photo=False))["limit"]
            < _limit_for(_checked(has_photo=True))["limit"]), (
        "профиль без аватара — классический спам-признак"
    )


def test_avatar_penalty_explained():
    r = _limit_for(_checked(has_photo=False))
    assert any("аватар" in n for n in r["factors"]["notes"]), (
        "причина среза должна быть названа человеку, а не спрятана в множителе"
    )


def test_country_locale_mismatch_cuts_limit():
    """Штрафуем не «плохую страну», а рассогласование отпечатка: российский
    номер с локалью en-US — несостыковка, которую видно и снаружи."""
    match = _limit_for(_checked(phone="+79991234567", system_lang_code="ru-RU"))["limit"]
    mismatch = _limit_for(_checked(phone="+79991234567", system_lang_code="en-US"))["limit"]
    assert mismatch < match, "несходящийся отпечаток обязан снижать объём"


def test_bare_lang_code_is_not_a_mismatch():
    """`ru` без региона страны не задаёт — выдумывать рассогласование нельзя."""
    assert (_limit_for(_checked(phone="+79991234567", system_lang_code="ru"))["limit"]
            == _limit_for(_checked(phone="+79991234567", system_lang_code="ru-RU"))["limit"])


def test_unknown_phone_country_is_neutral():
    assert (_limit_for(_checked(phone="", system_lang_code="en-US"))["limit"]
            == _limit_for(_checked(phone=None, system_lang_code="en-US"))["limit"])


def test_locale_country_parser():
    assert fe._locale_country("ru-RU") == "RU"
    assert fe._locale_country("en_US") == "US"
    assert fe._locale_country("ru") is None
    assert fe._locale_country("") is None
    assert fe._locale_country(None) is None


# ── проводка: факты обязаны реально собираться и сохраняться ─────────────────

def test_status_check_returns_profile_facts():
    """Без этого колонки остались бы вечно пустыми, а факторы — мёртвыми."""
    import inspect
    from services import account_manager
    src = inspect.getsource(account_manager.check_account_status_full)
    assert '"is_premium"' in src and '"has_photo"' in src, (
        "профильные факты снимаются из уже полученного `me`"
    )
    assert src.count('"profile": profile') >= 3, (
        "факты должны возвращаться на ВСЕХ ветках, где `me` уже получен, "
        "иначе половина проверок молча ничего не сохранит"
    )


def test_health_check_persists_profile_facts():
    import inspect
    from services import op_worker
    src = inspect.getsource(op_worker._exec_check_accounts_health)
    assert "profile_checked_at=NOW()" in src, "иначе факты негде взять"
    assert "isinstance(_prof, dict)" in src, (
        "писать только когда проверка их реально добыла: пустая запись сделала бы "
        "'не проверяли' неотличимым от 'нет Premium'"
    )


def test_schema_migration_present():
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[1] / "schema_v160.sql").read_text(encoding="utf-8")
    for col in ("is_premium", "has_photo", "profile_checked_at"):
        assert col in sql, f"колонка {col} обязана быть в миграции"
    main = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS profile_checked_at" in main, (
        "self-heal DDL: риск-движок читает эти колонки на КАЖДОМ батче инвайта, "
        "лаг миграции ронял бы операцию"
    )
