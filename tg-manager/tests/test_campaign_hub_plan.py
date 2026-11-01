"""Ядро единого модуля рассылок: спек кампании → дочерние операции.

Кампания — одна сущность, а не прыжки по разделам: один спек (контент + общие
настройки + выбор ресурсов) раскладывается на операции существующих движков под
общим запуском. Здесь проверяем ИМЕННО эту раскладку: что включённые ресурсы
дают правильные типы операций и параметры, что выключенные пропускаются, и что
кривой спек отбивается понятной русской ошибкой ДО постановки в очередь.
"""
from __future__ import annotations

import pytest

from services import campaign_hub as ch


def _full_spec() -> dict:
    return {
        "name": "Промо",
        "content": {"text": "Привет!", "buttons": [{"text": "Go", "url": "https://t.me"}]},
        "settings": {"silent": True, "delay_seconds": 5},
        "targets": {
            "channels": {"enabled": True, "channel_ids": [10, 20]},
            "chats": {"enabled": True, "acc_ids": [1, 2]},
            "bots": {"enabled": True, "segment": "all_each"},
            "dm": {"enabled": True, "target_type": "all_bots"},
        },
    }


def test_all_four_kinds_map_to_right_engines():
    ops = ch.plan_campaign(_full_spec())
    by_kind: dict[str, list] = {}
    for op in ops:
        by_kind.setdefault(op["kind"], []).append(op)

    # Каналы → mass_publish (одна операция, явные channel_ids).
    assert [o["op_type"] for o in by_kind["channels"]] == ["mass_publish"]
    assert by_kind["channels"][0]["params"]["channel_ids"] == [10, 20]
    assert by_kind["channels"][0]["params"]["target"] == "channels"

    # Чаты → group_announce ПО ОДНОЙ операции на аккаунт (движок работает одним).
    assert [o["op_type"] for o in by_kind["chats"]] == ["group_announce", "group_announce"]
    assert sorted(o["params"]["acc_id"] for o in by_kind["chats"]) == [1, 2]

    # Боты → network_broadcast (одна операция, сегмент прокинут).
    assert by_kind["bots"][0]["op_type"] == "network_broadcast"
    assert by_kind["bots"][0]["params"]["segment"] == "all_each"

    # ДМ → dm_campaign + спек строки dm_campaigns (создаст слой постановки).
    assert by_kind["dm"][0]["op_type"] == "dm_campaign"
    assert by_kind["dm"][0]["dm_create"]["target_type"] == "all_bots"
    assert by_kind["dm"][0]["dm_create"]["text"] == "Привет!"


def test_common_settings_propagate_to_every_child():
    ops = ch.plan_campaign(_full_spec())
    for op in ops:
        if op["kind"] == "dm":
            continue  # у ДМ параметры собираются из строки кампании
        assert op["params"]["silent"] is True
        assert op["params"]["delay_seconds"] == 5
        assert op["params"]["text"] == "Привет!"
    # Кнопки попадают туда, где движок их понимает (каналы/боты).
    chan = next(o for o in ops if o["kind"] == "channels")
    assert chan["params"]["buttons"] == [{"text": "Go", "url": "https://t.me"}]


def test_disabled_kinds_are_skipped():
    spec = _full_spec()
    spec["targets"]["chats"]["enabled"] = False
    spec["targets"]["bots"]["enabled"] = False
    kinds = {o["kind"] for o in ch.plan_campaign(spec)}
    assert kinds == {"channels", "dm"}


def test_enabled_kinds_order_is_stable():
    assert ch.enabled_kinds(_full_spec()) == ["channels", "chats", "bots", "dm"]


def test_channels_without_ids_means_all_managed():
    # «Во все» — это отсутствие списка (так шлёт экран кампании).
    spec = _full_spec()
    spec["targets"] = {"channels": {"enabled": True}}
    ops = ch.plan_campaign(spec)
    assert len(ops) == 1 and ops[0]["op_type"] == "mass_publish"
    assert "channel_ids" not in ops[0]["params"]


@pytest.mark.parametrize("bad", [[], ["abc"]])
def test_an_empty_or_broken_channel_pick_is_not_all(bad):
    # Раньше `[]` (и список из мусора) значил «во все управляемые каналы».
    spec = _full_spec()
    spec["targets"] = {"channels": {"enabled": True, "channel_ids": bad}}
    with pytest.raises(ch.CampaignSpecError):
        ch.plan_campaign(spec)


def test_empty_content_rejected():
    spec = _full_spec()
    spec["content"]["text"] = "   "
    with pytest.raises(ch.CampaignSpecError, match="текст или медиа"):
        ch.plan_campaign(spec)


def test_no_resource_selected_rejected():
    spec = _full_spec()
    for k in ("channels", "chats", "bots", "dm"):
        spec["targets"][k]["enabled"] = False
    with pytest.raises(ch.CampaignSpecError, match="хотя бы один ресурс"):
        ch.plan_campaign(spec)


def test_chats_without_accounts_rejected():
    spec = {"content": {"text": "x"}, "targets": {"chats": {"enabled": True, "acc_ids": []}}}
    with pytest.raises(ch.CampaignSpecError, match="аккаунт"):
        ch.plan_campaign(spec)


def test_bots_selected_segment_requires_ids():
    spec = {"content": {"text": "x"},
            "targets": {"bots": {"enabled": True, "segment": "selected_bots"}}}
    with pytest.raises(ch.CampaignSpecError, match="выбранным ботам"):
        ch.plan_campaign(spec)


def test_bots_unknown_segment_rejected():
    spec = {"content": {"text": "x"},
            "targets": {"bots": {"enabled": True, "segment": "wat"}}}
    with pytest.raises(ch.CampaignSpecError, match="сегмент"):
        ch.plan_campaign(spec)


def test_dm_unknown_target_rejected():
    spec = {"content": {"text": "x"},
            "targets": {"dm": {"enabled": True, "target_type": "nope"}}}
    with pytest.raises(ch.CampaignSpecError, match="аудитории ДМ"):
        ch.plan_campaign(spec)


def test_dm_import_list_requires_recipients():
    spec = {"content": {"text": "x"},
            "targets": {"dm": {"enabled": True, "target_type": "import_list", "import_list": []}}}
    with pytest.raises(ch.CampaignSpecError, match="получателей ДМ"):
        ch.plan_campaign(spec)


def test_op_types_are_registered_in_op_worker():
    """Каждый тип, на который целится кампания, реально есть в таблице исполнителей.

    Сверяем по ИСХОДНИКУ op_worker (импорт модуля тянет тяжёлые зависимости):
    в `_build_dispatch` должен быть ключ-строка для каждого op_type кампании —
    иначе дочерняя операция уйдёт в очередь и не найдёт исполнителя."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "services" / "op_worker.py").read_text(
        encoding="utf-8")
    i = src.index("def _build_dispatch(")
    j = src.index("def dispatch_table(", i)
    body = src[i:j]
    for op_type in ch.KIND_OP_TYPE.values():
        assert f'"{op_type}":' in body, (
            f"кампания целится в op_type '{op_type}', которого нет в "
            "op_worker._build_dispatch — дочерняя операция осталась бы без исполнителя")
