"""Регрессия (класс «несуществующая колонка», расширение на весь код):

1. topology_nodes (/api/miniapp/topology/nodes, экран графа сети) падал 500:
   - SELECT is_active FROM tg_channels — у tg_channels нет колонки is_active;
   - FROM managed_bots WHERE owner_id=$1 — у managed_bots нет owner_id (реальная
     колонка added_by).
   Позже источник каналов исправлен с пустой tg_channels на managed_channels
   (в tg_channels нет ни одной записи в коде → узлы-каналы были всегда пусты).
2. ecosystem_brain.sync_ecosystem_members фильтровал managed_channels по
   is_active=TRUE — у managed_channels нет такой колонки → запрос 500.

Найдено широким сканом сырых SQL-запросов bot/services против схемы (с учётом
Python-embedded ALTER).
"""
from __future__ import annotations

import inspect
import re

from services import ecosystem_brain, mini_app_api


def _topology_nodes_body() -> str:
    src = inspect.getsource(mini_app_api)
    m = re.search(r"async def topology_nodes\(.*?\n(.*?)async def ", src, re.DOTALL)
    assert m, "topology_nodes handler not found"
    return m.group(1)


def test_topology_managed_bots_uses_added_by():
    body = _topology_nodes_body()
    assert "FROM managed_bots WHERE added_by=$1" in body, (
        "managed_bots скоупится по added_by, не owner_id (такой колонки нет)"
    )
    assert "FROM managed_bots WHERE owner_id" not in body


def test_topology_channels_from_managed_channels_no_is_active():
    # Каналы топологии берутся из managed_channels (туда реально пишут), а НЕ из
    # пустой tg_channels; и без несуществующей колонки is_active. Проверяем КОД,
    # а не комментарии (в них tg_channels упоминается как поясняющий текст).
    raw = _topology_nodes_body()
    code = "\n".join(re.sub(r"#.*$", "", ln) for ln in raw.splitlines())
    assert "FROM managed_channels" in code, (
        "каналы топологии должны читаться из managed_channels (в tg_channels никто "
        "не пишет → узлы всегда пусты)"
    )
    assert "tg_channels" not in code, "tg_channels никем не заполняется — не источник"
    assert "is_active" not in code[:code.index("FROM managed_channels")], (
        "managed_channels не имеет is_active — фильтр/выборка по ней валит запрос"
    )


def test_ecosystem_managed_channels_no_is_active_filter():
    src = inspect.getsource(ecosystem_brain)
    # ни один запрос к managed_channels не должен фильтровать по is_active
    for m in re.finditer(r"FROM managed_channels[\s\S]{0,120}", src):
        seg = m.group(0)
        assert "is_active" not in seg, (
            "managed_channels не имеет колонки is_active — фильтр по ней валит запрос"
        )
