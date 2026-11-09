"""Команда бота ведёт в раздел, а не пересказывает путь по меню.

Обход команд 08.11.2026: /ranking и /referral отвечали «Откройте Infragram OS
и перейдите в /menu → …» (у /ranking путь ещё и разошёлся с меню), /promo —
«ПОДПИСКА. Оформите: /subscription» без слова о том, что даёт раздел, а
/report_abuse — «Сначала подключите почтовый ящик» без объяснения зачем.
Кнопка «Трекер позиций» в платформе продвижения вела в RankCb без bot_id и
получала «Бот не найден».
"""
from __future__ import annotations

import pathlib

H = pathlib.Path(__file__).resolve().parents[1] / "bot" / "handlers"


def _src(name: str) -> str:
    return (H / name).read_text(encoding="utf-8")


def test_no_command_tells_the_user_to_navigate_menus():
    for f in sorted(H.glob("*.py")):
        assert "Откройте Infragram OS и перейдите" not in f.read_text(encoding="utf-8"), f.name


def test_ranking_and_referral_buttons_open_the_section():
    assert "VisCb(action=\"dashboard\")" in _src("ranking.py").split("async def cmd_ranking")[1][:900]
    assert "RefCb(action=\"menu\")" in _src("referral.py").split("async def cmd_referral")[1][:900]


def test_promo_lock_explains_value_and_offers_plans():
    src = _src("promo_platform.py")
    assert "Оформите: /subscription\"" not in src
    assert "locked_text(\"Платформа продвижения ботов\"" in src
    from bot.utils.subscription import locked_text
    assert "💡" in locked_text("Платформа продвижения ботов", "pro")


def test_position_tracker_entry_needs_no_bot_id():
    assert "RankCb(action=\"menu\"))" not in _src("promo_platform.py")


def test_no_bare_subscription_lock_anywhere():
    """Голый замок «— 💎 ПОДПИСКА. Оформите: /subscription» не говорит ни что
    даёт раздел, ни какой тариф и за сколько. Обход нашёл его в 21 месте —
    теперь везде общий `locked_text` с тарифом и ценой."""
    import re
    bare = re.compile(r"ПОДПИСКА</b>\\n\\nОформите")
    hits = [f.name for f in sorted(H.glob("*.py")) if bare.search(f.read_text(encoding="utf-8"))]
    assert not hits, hits


def test_server_setup_instructions_are_for_admins_only(monkeypatch):
    """Клиент не видит «добавьте в Railway» и «задайте TG_API_ID»: выполнить
    это он не может, а читается это как поломка сервиса."""
    from bot.handlers import accounts, admin
    monkeypatch.setattr(admin, "is_admin", lambda uid: uid == 1)
    assert "TG_API_ID" not in accounts._api_missing_text(2)
    assert "TG_API_ID" not in accounts._api_missing_text(None)
    assert "TG_API_ID" in accounts._api_missing_text(1)
    src = _src("start.py")
    app = src[src.index("async def cmd_app"):]
    assert app.index("is_admin(message.from_user.id)") < app.index("Добавьте в Railway")


def test_command_menu_is_short_and_has_no_twins():
    """Дефект 22: в меню команд было 16 пунктов, /menu дублировал /start,
    /tasks — операции, а «Spintax» висел по-английски."""
    import re
    main = (H.parents[1] / "main.py").read_text(encoding="utf-8")
    block = main.split("_commands = (", 1)[1].split("_set_boot_phase(\"bot-commands\")", 1)[0]
    cmds = re.findall(r'command="(\w+)", description="([^"]+)"', block)
    names = [c for c, _ in cmds]
    assert len(names) <= 13, names
    assert len(set(names)) == len(names), names
    assert not ({"menu", "start"} <= set(names)), "/menu и /start в меню — одно и то же"
    assert not ({"tasks", "ops"} <= set(names)), "/tasks и /ops в меню — про одно и то же"
    for c, d in cmds:
        assert not re.search(r"[A-Za-z]{4,}", d.replace("Infragram", "")), (c, d)


def test_tasks_empty_state_does_not_claim_everything_is_done():
    """Пустой /tasks говорил «Все фоновые операции завершены», хотя очередь
    он не видит, и сыпал словами op_worker / bulk join/leave."""
    from bot.handlers.active_tasks import _build_text_and_kb
    text, kb = _build_text_and_kb(991799)
    assert "завершены" not in text and "op_worker" not in text and "bulk" not in text, text
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("Очередь" in t for t in labels), labels


def test_ops_and_report_share_one_empty_text():
    src = _src("channel_ops.py")
    assert src.count("_NO_OPS_YET") >= 3
    assert "Операций ещё не было" not in src
