"""Ускоренный барьер секретов отвечает так же, как прежний обход.

`scrub_payload` проходит КАЖДЫЙ ответ API. Прежний обход собирал копию
каждого словаря и звал `lower()` на каждом ключе; на мегабайтных ответах это
было больше половины времени ручки. Новый копирует только при первой замене.
Сверяем с эталоном (прежний алгоритм, переписанный здесь) на случайных
вложенных данных: тот же результат и тот же объект, когда менять нечего.
"""
from __future__ import annotations

import random

from services import secret_masking as sm


def _reference(data, depth=0):
    if depth > sm._MAX_SCRUB_DEPTH:
        return data
    if isinstance(data, str):
        return sm._scrub_str(data)
    if isinstance(data, dict):
        changed, out = False, {}
        for key, val in data.items():
            if isinstance(key, str) and key.lower() in sm._SECRET_KEYS:
                new = sm._MASK if val not in (None, "", b"") else val
            elif (isinstance(key, str) and key.lower() in sm._URL_CRED_KEYS
                    and isinstance(val, str) and "@" in val):
                new = sm._URL_CRED_RE.sub(
                    lambda m: f"{m.group(1)}{m.group(2)}:{sm._MASK}@", val)
            else:
                new = _reference(val, depth + 1)
            if new is not val:
                changed = True
            out[key] = new
        return out if changed else data
    if isinstance(data, (list, tuple)):
        items = [_reference(v, depth + 1) for v in data]
        if any(n is not o for n, o in zip(items, data)):
            return type(data)(items) if isinstance(data, tuple) else items
        return data
    return data


_TOK = "123456789:" + "A" * 35
_VALS = [None, 1, 2.5, True, "", b"", "short", "x" * 40, f"текст {_TOK} конец",
         "socks5://u:p@h:1", "http://h/", _TOK, "a:" + "b" * 40]
_KEYS = ["a", "Session_Str", "proxy_url", "PASSWORD", "token", "cf_relay_url",
         "bot_token", "secret", "title"]


def _gen(rnd, d=0):
    r = rnd.random()
    if d > 15 or r < 0.4:
        return rnd.choice(_VALS)
    if r < 0.7:
        return {rnd.choice(_KEYS) + str(rnd.randint(0, 2) if rnd.random() < 0.3 else ""):
                _gen(rnd, d + 1) for _ in range(rnd.randint(0, 4))}
    if r < 0.9:
        return [_gen(rnd, d + 1) for _ in range(rnd.randint(0, 4))]
    return tuple(_gen(rnd, d + 1) for _ in range(rnd.randint(0, 3)))


def test_same_result_as_reference():
    rnd = random.Random(7)
    for _ in range(20000):
        x = _gen(rnd)
        want, got = _reference(x), sm.scrub_payload(x)
        assert got == want, (x, got, want)
        assert (got is x) == (want is x), x


def test_secrets_still_masked():
    out = sm.scrub_payload({"list": [{"session_str": "1abc", "proxy_url": "socks5://u:p@h:1",
                                      "note": f"мой {_TOK}"}]})
    row = out["list"][0]
    assert row["session_str"] == "***"
    assert row["proxy_url"] == "socks5://u:***@h:1"
    assert "A" * 35 not in row["note"]
