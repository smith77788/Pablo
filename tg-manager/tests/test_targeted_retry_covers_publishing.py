"""«Повторить упавшие» работает для публикации, а не только для трёх типов.

ЧТО БЫЛО. Точечный повтор упавших ЦЕЛЕЙ объявлен в реестре операций и
держится на одном условии: operation_log.target должен однозначно обращаться в
элемент списка целей из params. Условие честное — угадывать нельзя, повтор ушёл
бы не по тем целям. Но объявлено оно было только у трёх типов из 77, а самые
ходовые операции постинга это условие выполняли и без объявления:

  • массовая публикация и быстрый пост пишут target = id канала, одинаково в
    успехе и в ошибке, а params несут channel_ids;
  • публикация в каналы аккаунта пишет target = id канала из того же
    channel_ids;
  • клонирование контента пишет target = ссылку на канал-приёмник, как она
    пришла в target_refs.

Без объявления у владельца оставался только повтор ЦЕЛИКОМ: лимиты аккаунтов
тратятся на каналы, которые пост уже получили, а кнопка «повторить упавшие» на
экране операции просто не показывалась.

ЧТО ТЕПЕРЬ. Четыре типа объявили retry_targets, и кнопка появляется у них сама
— она смотрит на реестр. Мини-апп перестал собирать список упавших каналов
своим запросом и берёт его у общего сборщика.
"""
from __future__ import annotations

import ast
import inspect

import pytest


PUBLISHING = {
    "mass_publish": ("channel_ids", "int"),
    "quick_post": ("channel_ids", "int"),
    "bulk_post_chans": ("channel_ids", "int"),
    "content_clone": ("target_refs", "str"),
}


def _func_body(module, name: str) -> str:
    src = inspect.getsource(module)
    tree = ast.parse(src)
    lines = src.split("\n")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"функция {name} не найдена в {module.__name__}")


@pytest.mark.parametrize("op_type", sorted(PUBLISHING))
def test_publishing_offers_targeted_retry(op_type):
    from services import operation_bus

    assert operation_bus.supports_retry_failed(op_type), (
        f"у операции «{op_type}» нет повтора упавших целей — владелец может "
        f"только перезапустить её целиком и послать пост второй раз в каналы, "
        f"которые его уже получили"
    )


@pytest.mark.parametrize("op_type", sorted(PUBLISHING))
def test_declaration_matches_what_the_executor_writes(op_type):
    """Объявление обязано совпадать с реальным полем params и форматом target."""
    from services import operation_bus

    meta = operation_bus.retry_targets_meta(op_type)
    param, kind = PUBLISHING[op_type]

    assert meta["param"] == param
    assert meta.get("kind") == kind
    assert not meta.get("prefix"), "исполнитель пишет цель без префикса"
    assert not meta.get("per_account"), (
        "в цель бьёт ОДИН аккаунт: пометив тип как per_account, повтор послал "
        "бы второй пост в канал, который уже опубликован"
    )


@pytest.mark.parametrize("op_type", sorted(PUBLISHING))
def test_target_written_by_the_executor_is_parsed_back(op_type):
    from services import operation_bus

    meta = operation_bus.retry_targets_meta(op_type)
    raw = "@target_channel" if meta["kind"] == "str" else "-1001234567890"

    parsed = operation_bus.parse_log_target(raw, meta)

    assert parsed is not None, "цель из журнала не разбирается обратно"
    assert str(parsed) == raw.lstrip() if meta["kind"] == "str" else parsed == -1001234567890


def test_a_channel_that_succeeded_later_is_not_repeated():
    """Упал на первой попытке, опубликован на второй — повторять нечего."""
    import asyncio

    from services import operation_bus

    class _Pool:
        async def fetch(self, query, *args):
            return [
                {"target": "-100111", "status": "error"},
                {"target": "-100111", "status": "ok"},
                {"target": "-100222", "status": "error"},
            ]

    failed = asyncio.run(
        operation_bus.collect_failed_targets(_Pool(), 41, "mass_publish"))

    assert failed == [-100222], (
        "канал, который в итоге получил пост, попал в повтор — он получит "
        "второй такой же"
    )


def test_miniapp_publish_retry_uses_the_shared_collector():
    from services import mini_app_api

    body = _func_body(mini_app_api, "retry_operation")

    assert "collect_failed_targets(" in body, (
        "мини-апп собирает список упавших каналов своим запросом: он берёт все "
        "строки со status='error' и не смотрит, не закрылась ли цель успехом "
        "позже — канал, опубликованный со второй попытки, получит второй пост"
    )
    assert "DISTINCT target FROM operation_log" not in body, (
        "в повторе остался собственный запрос за целями мимо "
        "operation_bus.collect_failed_targets"
    )
