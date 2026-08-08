"""Массовый пост в каналы: подтверждение + spintax-подсказка (паритет с mass_publish).

Публикация в N выбранных каналов необратима — как mass_publish/invite, должна идти
через осознанное подтверждение. Правки метаданных (title/about/username) обратимы —
без подтверждения. Плюс каждый канал получает свой spintax-вариант (post → отдельный
bulk_post_to_channel на канал, а тот спинтит text_to_post).
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


def test_mass_post_backend_spintax_per_channel():
    # channels_mass(post) шлёт отдельный bulk_post_to_channel на канал → свой вариант
    from services import mini_app_api
    import inspect
    src = inspect.getsource(mini_app_api)
    i = src.index("async def channels_mass")
    seg = src[i:i + 4000]
    assert 'submit(pool, uid, "bulk_post_to_channel"' in seg
    assert '"text_to_post": text' in seg
