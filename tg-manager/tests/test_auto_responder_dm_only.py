"""Дочерний бот не отвечает и не копит подписчиков в группах.

Тот же класс, что баг гейта подписки. auto_responder держит chat_id как id
пользователя во ВСЁМ пользовательском пути (учёт нового юзера, подписка на
воронку, add_bot_user, авто-ответы, /start, релей входящих оператору). Это
верно только в личке, где chat.id == user.id. В группе chat.id — это id группы:
дочерний бот, попавший в чат при инвайте Мать-Дочка, отвечал бы прямо в группу
(спам, выгоняющий только что приглашённых) и засорял бы базу подписчиков
id-ами групп.
"""
from __future__ import annotations

from services.auto_responder import _is_dm_update


def _upd(chat_type):
    return {"message": {"chat": {"id": 123, "type": chat_type},
                        "from": {"id": 555}, "text": "привет"}}


def test_private_chat_is_processed():
    assert _is_dm_update(_upd("private")) is True


def test_group_and_supergroup_and_channel_are_skipped():
    for ctype in ("group", "supergroup", "channel"):
        assert _is_dm_update(_upd(ctype)) is False, ctype


def test_missing_type_defaults_to_dm():
    """Telegram всегда шлёт type; отсутствие не должно ломать штатный путь лички."""
    assert _is_dm_update({"message": {"chat": {"id": 1}, "text": "x"}}) is True


def test_update_without_message_is_not_dm():
    assert _is_dm_update({}) is False
    assert _is_dm_update({"callback_query": {"id": "1"}}) is False


def test_guard_is_wired_before_processing():
    """Отсечка должна стоять ДО извлечения chat_id/обработки, иначе группа
    успеет попасть в воронки/авто-ответы."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "services" / "auto_responder.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_process_bot")
    seg = ast.get_source_segment(src.read_text(encoding="utf-8"), fn)
    assert "_is_dm_update(upd)" in seg
    # guard раньше, чем разбор текста на /start и подписки
    assert seg.index("_is_dm_update(upd)") < seg.index("is_start")
