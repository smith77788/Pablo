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
