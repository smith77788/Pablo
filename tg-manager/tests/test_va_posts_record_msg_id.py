"""Публикации виртуального администратора сохраняют msg_id сообщения.

Без msg_id пост нельзя открыть в канале (переход к сообщению) и нельзя
спросить у Telegram его просмотры (schema_v228). post_to_channel возвращает
{"msg_id": msg.id}, и ОБА пути публикации обязаны передать его в
content_memory.record_published:

  - mass_publish (одиночная публикация в канал) — передавал;
  - bulk_post_chans (публикация во все каналы аккаунта) — НЕ передавал, и
    массовые посты теряли ссылку на сообщение.

Проверяем по исходнику обоих вызовов (как в test_flood_op_impact): заглушка
пула не докажет, что в запись дошёл именно msg_id результата публикации.
"""
from __future__ import annotations

import inspect
import re

from services import op_worker

_MARKER = "content_memory.record_published("


def _record_published_calls() -> list[str]:
    """Аргументы каждого вызова record_published, со СБАЛАНСИРОВАННЫМИ скобками.

    Простой regex `(.*?)\\)` рвётся на вложенных скобках (`str(dialog["id"])`),
    поэтому читаем до закрывающей скобки с учётом вложенности.
    """
    src = inspect.getsource(op_worker)
    out: list[str] = []
    idx = 0
    while True:
        i = src.find(_MARKER, idx)
        if i < 0:
            break
        j = i + len(_MARKER)
        depth = 1
        while j < len(src) and depth:
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
            j += 1
        out.append(src[i + len(_MARKER):j - 1])
        idx = j
    return out


def test_both_publish_paths_pass_msg_id():
    calls = _record_published_calls()
    assert calls, "в op_worker нет вызовов record_published — тест устарел?"
    without = [c for c in calls if "msg_id" not in c]
    assert not without, (
        "вызов record_published без msg_id — пост потеряет ссылку на сообщение "
        "в канале и просмотры:\n" + "\n---\n".join(c.strip() for c in without))


def test_msg_id_comes_from_publish_result():
    """msg_id берётся из результата публикации, а не выдуман."""
    calls = _record_published_calls()
    # Хотя бы один путь берёт msg_id из результата post_to_channel.
    assert any(re.search(r"(result|last_result)\b.*msg_id|msg_id.*(result|last_result)\b",
                         c, re.DOTALL) or ".get(\"msg_id\")" in c or ".get('msg_id')" in c
               for c in calls), (
        "ни один вызов не берёт msg_id из результата публикации")
