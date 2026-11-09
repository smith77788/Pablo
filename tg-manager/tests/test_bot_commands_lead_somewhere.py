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
