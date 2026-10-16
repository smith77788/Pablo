"""Copilot: папки, связки и Хранилище перестали быть немыми модулями.

До этого движок подсказок знал про аккаунты, прокси, ботов, каналы и операции,
но три модуля не был представлен ни одной подсказкой. Пользователь не узнавал
о сломанном артефакте (папка без ссылки), о нарисованной, но не развёрнутой
связке и о том, что архив Хранилища тихо перестал пополняться.

Отдельно здесь стережётся сдержанность: «подключите Хранилище» всем подряд не
показываем — это требует Telegram Business и превращается в вечный баннер,
который нельзя убрать (кнопка «отложить» прячет подсказку лишь на сутки).
"""
from __future__ import annotations

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
        "channels_silent": 0,
        "ecosystems": 0,
        "parsed_recent": 0,
        "recent_ops": [],
        "folders_total": 1,
        "folders_no_link": 0,
        "nets_total": 1,
        "nets_empty": 0,
        "net_nodes_pending": 0,
        "vault_conn": 0,
        "vault_conn_off": 0,
        "vault_rules": 0,
        "vault_intents_7d": 0,
    }
    st.update(over)
    return st


def _ids(st):
    return [x["id"] for x in build_suggestions(st)]


def _one(st, sid):
    return next(x for x in build_suggestions(st) if x["id"] == sid)


# ── Папки ──────────────────────────────────────────────────────────────────

def test_folder_without_link_is_surfaced():
    """Папка есть, раздавать нечего — артефакт сломан, а экран молчал."""
    a = _one(_base(folders_no_link=2), "fix_folder_link")
    assert "2" in a["title"] and a["fn"] == "openFolders"


def test_folder_being_created_right_now_is_not_reported():
    """Окно в 10 минут отсекается запросом — в состоянии таких папок нет."""
    assert "fix_folder_link" not in _ids(_base(folders_no_link=0))


def test_channels_without_a_shared_folder_get_the_offer():
    a = _one(_base(channels=4, folders_total=0), "create_shared_folder")
    assert "4" in a["title"]


def test_single_channel_does_not_need_a_shared_folder():
    """Папка из одного канала не экономит ни одного приглашения."""
    assert "create_shared_folder" not in _ids(_base(channels=1, folders_total=0))


def test_existing_folder_stops_the_offer():
    assert "create_shared_folder" not in _ids(_base(channels=4, folders_total=1))


# ── Связки ─────────────────────────────────────────────────────────────────

def test_pending_nodes_ask_to_deploy():
    a = _one(_base(net_nodes_pending=5), "deploy_network")
    assert "5" in a["title"] and a["fn"] == "openNetworkBuilder"


def test_empty_network_asks_for_nodes():
    assert "fill_empty_network" in _ids(_base(nets_empty=1))


def test_bot_and_channel_without_a_network_get_the_offer():
    assert "build_first_network" in _ids(_base(channels=2, bots=1, nets_total=0))


def test_existing_network_stops_the_first_network_offer():
    assert "build_first_network" not in _ids(_base(channels=2, bots=1, nets_total=1))


def test_channel_without_bots_is_not_offered_a_network():
    """Связывать нечего с чем — предложение было бы пустым."""
    assert "build_first_network" not in _ids(_base(channels=2, bots=0, nets_total=0))


# ── Хранилище ──────────────────────────────────────────────────────────────

def test_disabled_vault_connection_is_loud():
    """Тихая потеря данных: архив не пополняется, а экран об этом не говорил."""
    a = _one(_base(vault_conn_off=1), "vault_reconnect")
    assert a["priority"] >= 70 and a["fn"] == "openVault"


def test_working_connection_alongside_a_disabled_one_is_not_an_alarm():
    assert "vault_reconnect" not in _ids(_base(vault_conn=1, vault_conn_off=1))


def test_intent_hits_are_treated_as_hot_contacts():
    a = _one(_base(vault_intents_7d=7), "review_intent_hits")
    assert "7" in a["title"] and a["priority"] >= 75


def test_connected_vault_without_rules_offers_the_sensor():
    assert "setup_intent_rules" in _ids(_base(vault_conn=1, vault_rules=0))


def test_vault_with_rules_is_left_alone():
    assert "setup_intent_rules" not in _ids(_base(vault_conn=1, vault_rules=3))


def test_vault_is_never_advertised_to_someone_who_has_not_connected_it():
    """Хранилище требует Telegram Business — вечный баннер «подключите» стал бы
    несмываемым: «отложить» прячет подсказку лишь на сутки."""
    ids = _ids(_base(vault_conn=0, vault_conn_off=0))
    assert not [i for i in ids if i.startswith("vault_") or i == "setup_intent_rules"]


# ── Общие контракты движка ─────────────────────────────────────────────────

def test_new_suggestions_all_have_a_destination_and_text():
    st = _base(folders_no_link=1, channels=3, folders_total=0, nets_empty=1,
               net_nodes_pending=2, nets_total=0, bots=1,
               vault_conn_off=1, vault_intents_7d=3)
    for a in build_suggestions(st):
        assert a.get("nav") or a.get("fn"), f"{a['id']} без адреса перехода"
        assert a.get("title") and a.get("cta") and a.get("reason")


def test_new_suggestions_do_not_break_priority_order():
    st = _base(folders_no_link=1, net_nodes_pending=2, vault_intents_7d=3,
               ops_failed_24h=1)
    prios = [a["priority"] for a in build_suggestions(st)]
    assert prios == sorted(prios, reverse=True)


def test_missing_state_keys_do_not_crash_the_engine():
    """Старая БД без этих таблиц отдаёт пустое состояние — движок обязан жить."""
    assert build_suggestions({"acc_active": 2}) is not None


def test_quiet_owner_stays_quiet():
    """Ничего не сломано — новые модули не начинают шуметь."""
    ids = _ids(_base())
    assert "fix_folder_link" not in ids
    assert "deploy_network" not in ids
    assert "vault_reconnect" not in ids
