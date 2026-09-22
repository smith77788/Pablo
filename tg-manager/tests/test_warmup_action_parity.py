"""Прогрев не имеет права рапортовать о действиях, которых не совершал.

Здесь закрывается конкретный найденный баг и весь его класс.

Что было
--------
Диспетчеров действий было ДВА: один в пути планов (`_run_daily_warmup_impl`),
второй в пути сессий (`_run_warmup_session_impl`), оба написаны руками
цепочкой elif. Они разъехались: в путь сессий не доехали view_profile,
open_chat, join_channel, search, dm_bot, story_view и check_notifications —
семь действий из двадцати. И заканчивалась цепочка так:

    else:
        success = True

То есть неизвестное действие считалось ВЫПОЛНЕННЫМ. На первых днях прогрева,
где из семи объявленных действий путь сессий умел четыре, это означало: почти
половина «прогрева» была записью в журнал без единого запроса в Telegram.
Владелец видел растущий счётчик и считал, что аккаунт греется.

Что проверяется
---------------
1. Каждое действие из расписания зарегистрировано и имеет исполнителя.
2. Ни одна ветка прогрева не считает неизвестное действие успехом.
3. У каждого действия есть подпись во всех трёх местах, где она показывается.
4. Диспетчер ровно один — обе ветки зовут perform_action().
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import account_warmer as aw  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _scheduled_actions() -> set[str]:
    out: set[str] = set()
    for actions in aw._WARMUP_SCHEDULE.values():
        out |= set(actions)
    return out


# ── 1. реестр покрывает всё, что может быть выбрано ──────────────────────────


def test_every_scheduled_action_is_registered():
    missing = _scheduled_actions() - set(aw.registered_actions())
    assert not missing, f"действия без описания в реестре: {sorted(missing)}"


def test_every_registered_action_has_an_executor():
    handlers = aw._handler_table()
    missing = set(aw.registered_actions()) - set(handlers)
    assert not missing, f"действия без исполнителя: {sorted(missing)}"
    for name, fn in handlers.items():
        assert callable(fn), f"{name}: исполнитель не вызываем"


def test_profile_weights_do_not_invent_actions():
    """Вес у действия, которого нет в реестре, — опечатка: он молча не сработает."""
    known = set(aw.registered_actions())
    for profile, weights in aw._PROFILE_WEIGHTS.items():
        unknown = set(weights) - known
        assert not unknown, f"профиль {profile} ссылается на несуществующие: {unknown}"


def test_safe_actions_are_real_actions():
    unknown = set(aw._WARM_SAFE_ACTIONS) - set(aw.registered_actions())
    assert not unknown, f"в «безопасных» действиях мусор: {unknown}"


# ── 2. молчаливого успеха больше нет ─────────────────────────────────────────


def test_unknown_action_is_a_failure_not_a_success():
    """Именно это и было сломано: `else: success = True`."""
    import asyncio

    ok, _target = asyncio.run(
        aw.perform_action("не существует", client=None, target="@x")
    )
    assert ok is False


def test_no_branch_marks_an_action_done_without_doing_it():
    """Текстовый ратчет на весь класс: в модуле прогрева не должно быть НИ ОДНОГО
    присваивания успеха константой.

    Обе прежние ветки делали ровно это — одна голым `success = True`, вторая с
    паузой перед ним, «чтобы было похоже на работу». Успех теперь приходит
    только из возвращаемого значения исполнителя.
    """
    src = _read("services/account_warmer.py")
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )
    bad = re.findall(r"^\s*success\s*=\s*True\s*$", code, re.M)
    assert not bad, "прогрев снова засчитывает действие успехом, не выполнив его"


def test_both_paths_use_the_single_dispatcher():
    src = _read("services/account_warmer.py")
    assert src.count("await perform_action(") >= 2, (
        "один из путей прогрева снова исполняет действия сам — диспетчеры разъедутся"
    )


def test_every_action_runs_under_a_timeout():
    """Зависший вызов съедал весь дневной бюджет аккаунта: в пути сессий
    таймаутов не было вовсе."""
    for name, spec in aw._ACTION_SPECS.items():
        assert spec.timeout > 0, f"{name}: действие без таймаута"
    assert "asyncio.wait_for(" in _read("services/account_warmer.py")


# ── 3. подписи: журнал не должен показывать сырые ключи ──────────────────────


def test_labels_exist_for_every_action():
    for name in aw.registered_actions():
        assert aw.action_label(name) != name, f"{name}: нет подписи для журнала"
        assert aw.action_progress(name) != name, f"{name}: нет строки прогресса"


def test_miniapp_knows_every_action():
    ui = _read("mini_app/index.html")
    block = ui.split("const WLOG_ACTION = {", 1)[1].split("};", 1)[0]
    missing = [n for n in aw.registered_actions() if f"{n}:" not in block]
    assert not missing, f"мини-апп покажет сырые ключи: {missing}"


def test_bot_log_reads_labels_from_the_registry():
    """Четвёртой копии подписей быть не должно."""
    src = _read("bot/handlers/account_warmup.py")
    assert "account_warmer.action_label(" in src, (
        "бот снова держит собственную копию подписей — она отстанет"
    )


# ── 4. безопасность набора ───────────────────────────────────────────────────


def test_level_zero_actions_never_write_to_telegram():
    """Свежему/больному аккаунту разрешено только то, что ничего не пишет.

    Это и есть смысл расширения набора: разнообразие добавлено читающими
    действиями, а не ослаблением защиты."""
    writing = [a for a in aw._WARM_SAFE_ACTIONS if aw._ACTION_SPECS[a].writes]
    assert not writing, f"в наборе для свежего аккаунта есть пишущие: {writing}"


def test_fresh_account_gets_a_varied_repertoire():
    """До этой доработки свежему аккаунту оставалось ровно три действия —
    presence, диалоги, чтение канала — и он повторял их день за днём."""
    day1 = aw._progressive_actions(
        aw._get_actions_for_day(1), trust_score=1.0, age_days=1, warmup_day=0
    )
    assert len(day1) >= 10, f"репертуар свежего аккаунта всё ещё узкий: {day1}"


def test_writing_actions_stay_gated_for_fresh_accounts():
    """Обратная сторона: расширение набора не должно открыть пишущее раньше срока."""
    day1 = aw._progressive_actions(
        aw._get_actions_for_day(30), trust_score=1.0, age_days=1, warmup_day=0
    )
    writing = [a for a in day1 if aw._ACTION_SPECS[a].writes]
    assert not writing, f"свежему аккаунту разрешили писать: {writing}"


def test_reading_actions_are_available_from_the_first_day():
    day1 = set(aw._get_actions_for_day(1))
    assert {"deep_scroll", "view_posts", "open_media"} <= day1


# ── 6. объём: читающее идёт СВЕРХ дневного бюджета ───────────────────────────
#
# Без этой развилки «шире репертуар» означало бы ровно те же 5–12 действий в
# день, просто выбранных из более длинного списка: флот смотрел бы не больше
# каналов и постов, а ровно столько же.


def test_reading_happens_on_top_of_the_daily_budget():
    assert aw._reading_bonus(20, 10) > 0


def test_reading_volume_grows_with_warmup():
    assert aw._reading_bonus(0, 10) < aw._reading_bonus(5, 10) <= aw._reading_bonus(30, 10)


def test_reading_volume_has_a_ceiling():
    """«Бесплатно» не значит «сколько угодно»: аккаунт, читающий по двести
    постов в сутки, тоже не похож на человека."""
    assert aw._reading_bonus(999, 10_000) <= 16


def test_reading_bonus_survives_garbage():
    assert aw._reading_bonus(None, None) >= 2
    assert aw._reading_bonus("день", "десять") >= 2


def test_bonus_steps_can_never_be_a_writing_action():
    """Иначе добавка объёма стала бы добавкой риска — ровно тем, чего нельзя."""
    for day in (1, 5, 10, 30):
        pool = aw._reading_only(aw._get_actions_for_day(day))
        assert pool, f"день {day}: сверхбюджетных действий нет вовсе"
        assert not [a for a in pool if aw._ACTION_SPECS[a].writes]


def test_both_paths_grant_the_reading_bonus():
    src = _read("services/account_warmer.py")
    assert src.count("_reading_bonus(") >= 3, (
        "один из путей прогрева не получает читающую добавку"
    )
    assert src.count("_reading_only(") >= 3


def test_writing_budget_is_untouched():
    """Регрессия: добавка объёма не должна была тронуть бюджет риска."""
    assert aw._actions_for_day_count(1, 10) == max(2, 10 // 4)
    assert aw._actions_for_day_count(30, 10) == 10


# ── 5. TL-имена ──────────────────────────────────────────────────────────────


def test_tl_request_names_are_real():
    """conftest подменяет telethon предельно снисходительной заглушкой: опечатка
    в имени TL-класса проходит ВСЕ юнит-тесты и падает только в проде. Поэтому
    там, где настоящий telethon доступен (прод-образ, машина разработчика),
    имена проверяются по нему."""
    try:
        import telethon  # noqa: F401
        from telethon.tl import functions as tlf
    except Exception:
        pytest.skip("настоящий telethon недоступен — проверять не по чему")
    if getattr(tlf, "__file__", None) is None:
        pytest.skip("telethon подменён заглушкой")
    src = _read("services/warmup_actions.py")
    for mod, names in re.findall(
        r"from telethon\.tl\.functions\.(\w+) import \(?\s*([\w,\s]+?)\)?\n\n", src
    ):
        module = getattr(tlf, mod)
        for name in [n.strip().rstrip(",") for n in names.split(",") if n.strip()]:
            assert hasattr(module, name), f"telethon.tl.functions.{mod}.{name} не существует"
