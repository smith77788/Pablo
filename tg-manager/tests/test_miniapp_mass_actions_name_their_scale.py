"""Регрессия: подтверждения необратимых массовых действий не называли масштаб.

Что было сломано. Четыре действия спрашивали согласие, ни разу не назвав числа
адресатов — масштаб приходил только в тосте ПОСЛЕ запуска, когда посты уже
ушли в Telegram, а сообщения живым людям:

* «Опубликовать во ВСЕ каналы этого аккаунта?» — сколько их, неизвестно;
* «Разослать объявление во ВСЕ группы этого аккаунта?» — сообщения реальным
  людям, число групп человек узнавал постфактум;
* «Разослать сообщение всем пользователям?» — рассылка по всей платформе;
* «Удалить БЕЗВОЗВРАТНО все контакты под фильтр? Сначала лучше сделать
  предпросмотр» — предпросмотр был СОВЕТОМ, а не частью действия: удалить
  безвозвратно можно было, ни разу не увидев, сколько контактов попадёт.

Число адресатов считается теми же запросами, которыми действие выбирает себе
цели: иначе подтверждение обещало бы один набор, а ушло бы в другой.
"""
from __future__ import annotations

import inspect
import re

import pytest

from services import mini_app_api
from tests.miniapp_source import source_of


def _api_src() -> str:
    return inspect.getsource(mini_app_api)


def _fn(name: str) -> str:
    src = source_of(name)
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\([^)]*\)\s*\{", src, re.M)
    assert m, f"функция {name} не найдена"
    i = m.end() - 1
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    pytest.fail(f"не закрылось тело {name}")


def _handler(name: str) -> str:
    """Тело python-хендлера: от его def до следующего def того же уровня."""
    src = _api_src()
    start = src.index(f"    async def {name}(")
    nxt = src.find("\n    async def ", start + 1)
    return src[start:nxt if nxt != -1 else len(src)]


# ── Бэкенд: есть чем назвать масштаб ─────────────────────────────────────────

def test_mass_targets_endpoint_exists():
    src = _api_src()
    assert "async def mass_targets(" in src, (
        "нечем узнать масштаб массового действия до его запуска")
    assert 'add_get("/api/miniapp/mass_targets"' in src, "эндпойнт не зарегистрирован"


@pytest.mark.parametrize("kind, counting", [
    # Запрос счёта обязан совпадать с тем, которым действие выбирает цели.
    ("bulk_post_chans",
     "FROM managed_channels WHERE owner_id=$1 AND acc_id=$2"),
    ("group_announce",
     "AND type IN ('megagroup','supergroup','group','chat')"),
    ("admin_broadcast",
     "FROM platform_users "),
])
def test_each_kind_counts_what_the_action_targets(kind, counting):
    body = _handler("mass_targets")
    assert f'kind == "{kind}"' in body, f"масштаб {kind} не считается"
    assert counting in body, (
        f"счёт для {kind} идёт не тем запросом, которым действие выбирает цели")


def test_unknown_kind_is_rejected():
    body = _handler("mass_targets")
    assert "Неизвестное массовое действие" in body, (
        "мусорный op должен получить честный отказ, а не чужой счёт")


def test_platform_wide_broadcast_count_is_admin_only():
    body = _handler("mass_targets")
    ab = body[body.index('kind == "admin_broadcast"'):]
    assert "_is_admin(uid)" in ab.split("else:")[0], (
        "число пользователей платформы — не для всех подряд")


# ── Экран: масштаб назван до нажатия ─────────────────────────────────────────

@pytest.mark.parametrize("fn, op", [
    ("submitBulkPostChans", "bulk_post_chans"),
    ("submitGroupAnnounce", "group_announce"),
    ("submitAdminBroadcast", "admin_broadcast"),
])
def test_confirmation_names_the_scale(fn, op):
    body = _fn(fn)
    assert f"massTargets('{op}'" in body, (
        f"{fn} не спрашивает масштаб перед подтверждением")
    assert "massScale(" in body, (
        f"подтверждение в {fn} не называет число адресатов")
    # Порядок важен: сначала узнать масштаб, потом спрашивать согласие.
    assert body.index("massTargets(") < body.index("askConfirm("), (
        f"{fn} спрашивает согласие раньше, чем узнаёт масштаб")


def test_unknown_scale_is_said_out_loud_not_hidden():
    """Если число узнать не удалось, подтверждение обязано это сказать."""
    body = _fn("massScale")
    assert "узнать их число не удалось" in body, (
        "неизвестный масштаб нельзя выдавать за известный или замалчивать")


def test_empty_target_set_stops_the_action():
    for fn in ("submitBulkPostChans", "submitGroupAnnounce", "submitAdminBroadcast"):
        body = _fn(fn)
        assert ".total === 0" in body, (
            f"{fn} запускает массовое действие в пустоту вместо честного отказа")


def test_irreversible_contact_delete_counts_before_asking():
    body = _fn("uchClDelete")
    assert "uch/filter/preview" in body, (
        "предпросмотр был советом, а не частью действия: удалить безвозвратно "
        "можно было, ни разу не увидев, сколько контактов попадёт")
    assert body.index("uch/filter/preview") < body.index("askConfirm("), (
        "согласие спрашивается раньше, чем становится известно число контактов")
    assert "plural(uchN" in body, "подтверждение не называет число контактов"
    assert "uchN === 0" in body, (
        "удаление по фильтру, под который никто не попадает, должно "
        "останавливаться, а не рапортовать «Удалено: 0»")


# ── Действия по всему флоту: потолок операции тоже часть масштаба ────────────

@pytest.mark.parametrize("kind, counting", [
    # Отбор обязан совпадать с тем, которым действие берёт аккаунты.
    ("join_all",
     "FROM tg_accounts WHERE owner_id=$1 AND is_active "),
    # «Все активные» и «все активные БЕЗ плана» — разные наборы.
    ("warmup_bulk", "FROM account_warmup_plans wp"),
])
def test_fleet_action_counts_its_own_selection(kind, counting):
    body = _handler("mass_targets")
    assert f'kind == "{kind}"' in body, f"масштаб {kind} не считается"
    assert counting in body, (
        f"счёт для {kind} идёт не тем отбором, которым действие берёт аккаунты")


def test_fleet_actions_report_their_cap():
    """У обеих операций бэкенд берёт лишь первые N подходящих."""
    body = _handler("mass_targets")
    assert '"cap": 200' in body and '"cap": 500' in body, (
        "потолок не доезжает до фронта: согласие на «всеми аккаунтами», когда "
        "дойдёт только до двухсот, — согласие не на то, что произойдёт")


def test_cap_is_passed_through_to_the_dialog():
    body = _fn("massTargets")
    assert "cap" in body, "massTargets теряет потолок по дороге"
    scale = _fn("massScaleCapped")
    assert "t.cap" in scale and "t.total > t.cap" in scale, (
        "строка масштаба не различает «их N» и «подходящих N, берутся первые M»")


@pytest.mark.parametrize("fn, op", [
    ("joinAllToGroup", "join_all"),
    ("bulkWarmup", "warmup_bulk"),
])
def test_fleet_confirmation_names_the_scale(fn, op):
    body = _fn(fn)
    assert f"massTargets('{op}')" in body, f"{fn} не спрашивает масштаб"
    assert "massScaleCapped(" in body, (
        f"подтверждение в {fn} не называет ни числа аккаунтов, ни потолка")
    assert body.index("massTargets(") < body.index("askConfirm("), (
        f"{fn} спрашивает согласие раньше, чем узнаёт масштаб")
    assert ".total === 0" in body, (
        f"{fn} запускает операцию в пустоту вместо честного отказа")
