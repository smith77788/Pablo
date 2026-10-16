"""Зашифрованный proxy_url не уходит в подключение и не показывается человеку.

`user_proxies.proxy_url` хранится зашифрованным (token_vault, AES-256-GCM).
Значит у каждого потребителя ровно две обязанности, и обе молча нарушались.

ПОДКЛЮЧЕНИЕ. `ProxyConnector.from_url("ENC:…")` бросает, а вызывающий ловит
исключение и записывает «прокси недоступен». Поэтому:

* `services/proxy_watchdog.check_once` — фоновый обход — объявлял мёртвым
  КАЖДЫЙ живой прокси, писал это в базу и слал владельцу письмо «прокси
  умер». Выглядело как «все прокси разом сдохли»;
* «Проверить все» в боте делала то же самое вручную, а рядом стоит порог
  `_DEAD_THRESHOLD = 3` — три нажатия гасили весь пул владельца;
* определение гео и `auto_select_proxy` не работали по той же причине.

ПОКАЗ ЧЕЛОВЕКУ. Запасной подписью прокси была нарезка `proxy_url[:30]` прямо
из базы, то есть кусок шифротекста: в списке стояли неразличимые «ENC:8f2a…».
А расшифрованный адрес показывать целиком тоже нельзя — в нём логин и пароль.
Правильный ответ один: `proxy_hygiene.proxy_display`.

Тест смотрит исходники: расшифровку не проверить, не подняв настоящий прокси,
а вот «сырой столбец ушёл в функцию подключения» видно прямо в коде.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re

import pytest

from services.proxy_hygiene import mask_proxy_url, proxy_display, proxy_plain_url
from services.token_vault import encrypt_token

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Функции, которые реально подключаются к прокси или разбирают его адрес.
_CONNECTORS = {
    "check_proxy_alive", "_check_proxy_alive", "probe_proxy", "test_proxy",
    "_detect_proxy_geo", "extract_ip_from_proxy",
}

# Где расшифровка уже делается ВНУТРИ самой функции — там вызывающий не обязан.
_DECRYPTS_INSIDE = {"probe_proxy", "extract_ip_from_proxy"}

_RAW_COLUMN = re.compile(r'''\[["']proxy_url["']\]|\.get\(["']proxy_url["']\)''')


def _py_files():
    for sub in ("services", "bot"):
        for dirpath, _dirs, files in os.walk(ROOT / sub):
            if "__pycache__" in dirpath:
                continue
            for f in files:
                if f.endswith(".py"):
                    yield pathlib.Path(dirpath) / f


def _raw_column_calls():
    """Вызовы функций подключения, куда уходит СЫРОЙ столбец proxy_url."""
    out = []
    for path in _py_files():
        src = path.read_text(encoding="utf-8", errors="ignore")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in _CONNECTORS or name in _DECRYPTS_INSIDE:
                continue
            for arg in node.args:
                seg = ast.get_source_segment(src, arg) or ""
                if _RAW_COLUMN.search(seg) and "decrypt" not in seg and "plain" not in seg:
                    rel = path.relative_to(ROOT).as_posix()
                    out.append(f"{rel}:{node.lineno} — {name}({seg.strip()[:60]})")
    return out


def _raw_column_labels():
    """Подписи вида `label or row["proxy_url"][:30]` — показ шифротекста."""
    out = []
    pat = re.compile(r'''\[["']label["']\]\s*or\s*\w+\[["']proxy_url["']\]''')
    for path in _py_files():
        src = path.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(src.splitlines(), 1):
            if pat.search(line):
                out.append(f"{path.relative_to(ROOT).as_posix()}:{i} — {line.strip()[:70]}")
    return out


def test_detector_sees_the_connector_calls():
    """Сначала проверяем сам пробник: он обязан находить вызовы вообще.

    Детектор, который ничего не находит, — это зелёный тест ни о чём.
    """
    found = 0
    for path in _py_files():
        src = path.read_text(encoding="utf-8", errors="ignore")
        found += sum(src.count(name + "(") for name in _CONNECTORS)
    assert found > 5, "пробник не видит вызовов функций подключения — он сломан"


def test_no_raw_proxy_url_into_connectors():
    bad = _raw_column_calls()
    assert not bad, (
        "сырой (зашифрованный) proxy_url уходит в подключение — прокси будет "
        "считаться мёртвым, хотя он жив:\n  " + "\n  ".join(bad)
        + "\n\nПропустите значение через proxy_hygiene.proxy_plain_url."
    )


def test_no_ciphertext_shown_as_proxy_label():
    bad = _raw_column_labels()
    assert not bad, (
        "подписью прокси стоит сырой столбец — человек увидит «ENC:…», а после "
        "расшифровки увидел бы логин и пароль:\n  " + "\n  ".join(bad)
        + "\n\nИспользуйте proxy_hygiene.proxy_display(proxy_url, label)."
    )


# ── Поведение самих помощников ────────────────────────────────────────────────

def test_plain_url_decrypts():
    enc = encrypt_token("socks5://user:pass@1.2.3.4:1080")
    assert enc.startswith("ENC:")
    assert proxy_plain_url(enc) == "socks5://user:pass@1.2.3.4:1080"


def test_plain_url_passes_legacy_plaintext_through():
    """Старые незашифрованные строки читаются как есть — иначе сломаются они."""
    assert proxy_plain_url("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"


def test_plain_url_handles_empty():
    assert proxy_plain_url(None) == ""
    assert proxy_plain_url("") == ""


def test_display_prefers_label():
    enc = encrypt_token("socks5://user:pass@1.2.3.4:1080")
    assert proxy_display(enc, "Германия-1") == "Германия-1"


def test_display_hides_credentials():
    """Без ярлыка показываем адрес, но логин и пароль — никогда."""
    enc = encrypt_token("socks5://user:s3cret@1.2.3.4:1080")
    shown = proxy_display(enc)
    assert "1.2.3.4" in shown, "адрес нужен, чтобы отличать прокси друг от друга"
    assert "s3cret" not in shown and "user" not in shown
    assert "ENC:" not in shown


def test_display_never_leaks_ciphertext():
    enc = encrypt_token("socks5://1.2.3.4:1080")
    assert proxy_display(enc) == "socks5://1.2.3.4:1080"


def test_display_of_empty_is_readable():
    assert proxy_display(None) == "без названия"


def test_mask_still_used_on_plaintext_only():
    """Маска рассчитана на открытый адрес: на шифротексте она бессильна.

    Именно поэтому маскировать надо ПОСЛЕ расшифровки — иначе в письме
    владельцу оставалась строка «ENC:…».
    """
    enc = encrypt_token("socks5://user:pass@1.2.3.4:1080")
    assert mask_proxy_url(enc) == enc, "маска не умеет и не должна уметь шифротекст"
    assert mask_proxy_url(proxy_plain_url(enc)) == "socks5://***@1.2.3.4:1080"


def test_auto_select_proxy_is_owner_scoped():
    """Запасной прокси берётся только из пула того же владельца."""
    import inspect

    from services import account_manager

    src = inspect.getsource(account_manager.auto_select_proxy)
    selects = [ln for ln in src.splitlines() if "FROM user_proxies" in ln or "user_proxies" in ln]
    assert selects, "запрос к user_proxies исчез — тест устарел"
    assert "owner_id" in src, (
        "запасной прокси выбирается по всей таблице: аккаунт одного владельца "
        "уедет через чужой платный прокси"
    )
