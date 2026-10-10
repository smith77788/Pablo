"""Массовый пост в каналы: подтверждение + ОДНА операция (паритет с mass_publish).

Публикация в N выбранных каналов необратима — как mass_publish/invite, должна идти
через осознанное подтверждение. Правки метаданных (title/about/username) обратимы —
без подтверждения.

Пост во все выбранные каналы — ОДНА операция mass_publish, а не по одной на канал:
дробление плодило десятки «Массовая публикация в канал» в диспетчере, у каждой свой
предохранитель и пейсинг, общий разнос (в т.ч. анти-детект ссылок) ломался, прогресс
и отчёт дробились. mass_publish спинтит текст на каждый канал — spintax-паритет
сохраняется.
"""
from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_mass_post_confirms_before_publish():
    m = re.search(r"async function submitChMass\(\)\s*\{(.*?)\n\}", HTML, re.DOTALL)
    assert m, "submitChMass не найден"
    body = m.group(1)
    # подтверждение ТОЛЬКО для необратимого post, до отправки
    i_conf = body.find("askConfirm")
    i_api = body.find("/api/miniapp/channels/mass")
    assert i_conf != -1 and "CHM_OP==='post'" in body, "нет подтверждения для массового поста"
    assert i_conf < i_api, "подтверждение должно быть ДО отправки"
    assert re.search(r"if\s*\(CHM_OP==='post'\s*&&\s*!await askConfirm", body)


def test_metadata_edits_not_gated_by_confirm():
    # title/about/username обратимы → не требуют подтверждения (только post)
    m = re.search(r"async function submitChMass\(\)\s*\{(.*?)\n\}", HTML, re.DOTALL)
    body = m.group(1)
    # единственный askConfirm — под условием post
    assert body.count("askConfirm") == 1


def _channels_mass_src() -> str:
    """Исходник хендлера channels_mass по ТОЧНЫМ границам функции (ast), а не по
    срезу фиксированной длины: иначе отрицательная проверка ниже промахнулась бы
    окном при сдвиге кода и выключилась молча (храповик про это)."""
    import ast
    import inspect
    from services import mini_app_api

    src = inspect.getsource(mini_app_api)
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "channels_mass"):
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError("channels_mass не найдена")


def test_mass_post_is_a_single_operation():
    # channels_mass(post) ставит ОДНУ mass_publish на все каналы, не по одной на
    # канал: иначе десятки операций в диспетчере и дроблёный пейсинг/отчёт.
    body = _channels_mass_src()
    assert '"mass_publish"' in body, "пост не идёт одной mass_publish"
    assert '"channel_ids": ch_ids' in body, "mass_publish без списка каналов"
    # После объединения в одну операцию дробящий bulk_post_to_channel исчезает из
    # хендлера совсем: edit → bulk_chan_exec, promote → promote_all_admins,
    # post → mass_publish. Его возврат = регресс к операции-на-канал.
    assert '"bulk_post_to_channel"' not in body, \
        "пост снова дробится на отдельные bulk_post_to_channel"
