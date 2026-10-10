"""Срез аккаунтов, которого сервер не знает, молча превращался во ВЕСЬ флот.

`_accounts_where` понимает срез `proxy_down` — аккаунты, простаивающие из-за
мёртвого прокси, — и на экране есть плитка «Прокси мёртв», которая его ставит.
А оба хендлера сверяли пришедший срез со своим рукописным списком, в котором
`proxy_down` не было, и при несовпадении молча подставляли `all`.

Два последствия. Первое: плитка показывала «5», а открывала все 500 аккаунтов.
Второе и опасное: «применить ко всему срезу» в этом состоянии ставило массовую
операцию по ВСЕМУ флоту вместо тех пяти — владелец видел пять, подтверждал
пять, уходило пятьсот.

Поэтому список срезов теперь один (`ACCOUNT_FILTERS`), а проверка ниже
вычисляется из самого билдера: добавили ветку в `_accounts_where` и забыли
внести срез в список — тест красный, а не операция по всему флоту.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src() -> str:
    with open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8") as f:
        return f.read()


def _builder(src: str) -> str:
    i = src.index("def _accounts_where(")
    return src[i:src.index("\n    async def ", i)]


def _branches(src: str) -> set[str]:
    """Срезы, которые билдер действительно умеет разбирать."""
    return set(re.findall(r'flt == "(\w+)"', _builder(src)))


def _declared(src: str) -> set[str]:
    m = re.search(r"ACCOUNT_FILTERS\s*=\s*\(([^)]*)\)", src)
    assert m, "общего списка срезов нет — он снова рукописный в каждом хендлере"
    return set(re.findall(r'"(\w+)"', m.group(1)))


def test_probe_sees_the_branches():
    """Пустой список веток сделал бы проверку ниже вечно зелёной."""
    found = _branches(_src())
    assert "proxy_down" in found and "banned" in found, found


def test_every_slice_the_builder_knows_is_accepted():
    src = _src()
    missing = _branches(src) - _declared(src)
    assert not missing, (
        f"срезы {sorted(missing)} билдер умеет, а хендлеры их не принимают — "
        f"такой срез молча подменяется на «все аккаунты», и массовая операция "
        f"по нему уходит по всему флоту")


def test_no_handler_keeps_its_own_hand_written_list():
    src = _src()
    stray = re.findall(r'flt not in \(([^)]*)\)', src)
    for lst in stray:
        assert "ACCOUNT_FILTERS" in lst or not re.search(r'"\w+"', lst), (
            f"хендлер сверяется со своим списком срезов ({lst.strip()[:60]}…) — "
            f"он уже разошёлся с билдером и подменял срез на «все»")


def test_both_entry_points_use_the_shared_list():
    src = _src()
    assert src.count("ACCOUNT_FILTERS") >= 3, (
        "общий список объявлен, но им пользуется не каждый вход: список и "
        "масс-действие обязаны принимать одно и то же")
