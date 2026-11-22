"""«🔔 Алерты» в боте называют событие по-русски, а не кодом из базы."""
import re

from bot.handlers.botmother_menu import _alert_ru


def test_known_and_composed_types():
    assert _alert_ru("account_flood_risk") == "Аккаунт часто ловит флуд-лимиты"
    assert _alert_ru("drift_detected") == "Канал изменили вне Infragram"
    assert _alert_ru("channel_search_hidden") == "Канал скрыт из поиска"
    assert _alert_ru("bot_geoban") == "Бот заблокирован в части стран"
    assert _alert_ru("group_unreachable") == "Группа недоступна снаружи"
    assert _alert_ru("group_search_hidden") == "Группа скрыта из поиска"


def test_unknown_type_is_not_a_code():
    for code in ("something_new", "", None, "x"):
        assert not re.search(r"[A-Za-z]", _alert_ru(code))


def test_every_composed_type_is_covered():
    from services import entity_restriction_check as erc
    for kind in ("channel", "group", "bot"):
        for st in (erc.STATUS_RESTRICTED, erc.STATUS_HIDDEN, erc.STATUS_UNREACHABLE):
            label = _alert_ru(erc.event_type_for(kind, st))
            assert label != "Событие безопасности", (kind, st)


def test_operation_names_are_russian_in_bot_screens():
    from pathlib import Path
    from bot.handlers.mass_ops import _op_name, _ost_label
    assert _op_name("mass_invite") == "Массовый инвайт участников в группы"
    assert _op_name("totally_unknown") == "Операция"
    assert _ost_label("failed") == "завершилась с ошибкой"
    root = Path(__file__).resolve().parents[1] / "bot/handlers"
    for name in ("botmother_menu.py", "channel_ops.py", "mass_ops.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert 'escape(op["op_type"])' not in src, name
    assert "<code>{op['op_type']}</code>" not in (root / "approval_flow.py").read_text(encoding="utf-8")
    assert "[{op['status']}]" not in (root / "mass_ops.py").read_text(encoding="utf-8")
