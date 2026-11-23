"""Кнопка «🗑 Удалить» сначала спрашивает.

У кампании ЛС, плана и сессии прогрева, ДНК-шаблона, перехватчика ключевых
слов и очереди операций «🗑» удаляла с первого нажатия — рядом с «▶️ Запуск»
и «⏸ Пауза», то есть промах пальцем стоил записи целиком (вместе с журналом
отправок кампании). Так же «🚪 Покинуть» в пространстве.

Правило: обработчик кнопки с разрушительной подписью либо сам показывает
подтверждение, либо кнопка ведёт на шаг `*_ask` / `*confirm*` / `*_yes`.
Блокировка пользователя в админке обратима (разбан) — она в списке
исключений, а не в правиле.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "bot" / "handlers"

_DESTRUCTIVE = re.compile(r"Удалить|Очистить|Сбросить|Отвязать|Отозвать|Покинуть")
_SAFE_ACTION = re.compile(r"(^|_)(ask|confirm\w*|yes|ok|do|go|exec|run)(_|$)")
_ACTS = re.compile(r"DELETE FROM|\bdb\.(delete|remove|clear|reset|revoke)_\w+\(|"
                   r"\b(delete|remove|clear|reset|revoke)_\w+\(")
_ASKS = re.compile(r"Да,|Подтвер|уверены|confirm|_yes|ask_to_confirm", re.I)
_REVERSIBLE = {("AdminUserCb", "ban")}


def _scan(files: dict[str, str]) -> list[str]:
    buttons = defaultdict(list)
    handlers = {}
    for name, src in files.items():
        tree = ast.parse(src)
        for c in ast.walk(tree):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "button":
                kw = {k.arg: k.value for k in c.keywords}
                text, cd = kw.get("text"), kw.get("callback_data")
                if isinstance(text, (ast.Constant, ast.JoinedStr)) and isinstance(cd, ast.Call) \
                        and isinstance(cd.func, ast.Name):
                    for k in cd.keywords:
                        if k.arg == "action" and isinstance(k.value, ast.Constant):
                            buttons[(cd.func.id, k.value.value)].append((ast.unparse(text), f"{name}:{c.lineno}"))
        for fn in ast.walk(tree):
            if isinstance(fn, ast.AsyncFunctionDef):
                for d in fn.decorator_list:
                    for m in re.finditer(r"(\w+Cb)\.filter\(F\.action == '([^']+)'\)", ast.unparse(d)):
                        handlers[(m.group(1), m.group(2))] = ast.get_source_segment(src, fn) or ""
    bad = []
    for key, texts in sorted(buttons.items()):
        if key in _REVERSIBLE or _SAFE_ACTION.search(key[1]):
            continue
        if not any(_DESTRUCTIVE.search(t) for t, _ in texts) or any(re.search(r"Да[,!]", t) for t, _ in texts):
            continue
        body = handlers.get(key)
        if body and _ACTS.search(body) and not _ASKS.search(body):
            bad.append(f"{key[0]}(action={key[1]!r}) — {texts[0][1]}")
    return bad


def test_every_delete_button_asks_first():
    files = {p.name: p.read_text(encoding="utf-8") for p in sorted(HANDLERS.glob("*.py"))}
    bad = _scan(files)
    assert not bad, "удаляет с первого нажатия, без подтверждения:\n" + "\n".join(bad)


def test_probe_sees_an_instant_delete_and_spares_an_ask_step():
    instant = (
        "kb.button(text='🗑 Удалить', callback_data=XCb(action='del', item_id=1))\n"
        "@router.callback_query(XCb.filter(F.action == 'del'))\n"
        "async def h(c, d):\n    await db.delete_item(pool, d.item_id)\n"
    )
    asked = instant.replace("action='del', item_id", "action='del_ask', item_id")
    assert _scan({"a.py": instant}) == ["XCb(action='del') — a.py:1"]
    assert _scan({"a.py": asked}) == []


def test_ask_step_offers_the_original_action_and_a_way_back():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from bot.callbacks import DmCb
    from bot.handlers import dm_campaigns

    cb = MagicMock()
    cb.answer = AsyncMock()
    cb.message.edit_text = AsyncMock()
    asyncio.run(dm_campaigns.cb_dmcb_delete_ask(cb, DmCb(action="delete_ask", campaign_id=5)))
    text = cb.message.edit_text.await_args.args[0]
    rows = cb.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert "Удалить кампанию?" in text and "Отменить это будет нельзя" in text
    assert rows[0][0].callback_data == DmCb(action="delete", campaign_id=5).pack()
    assert rows[1][0].callback_data == DmCb(action="detail", campaign_id=5).pack()
