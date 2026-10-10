"""Зашифрованный токен бота не уходит в Telegram.

`managed_bots.token` хранится зашифрованным (token_vault). В базе для чтения
есть `db.fetch_bots` / `db.fetchrow_bot` / `db.get_bot` — они расшифровывают.
Но часть кода читает колонку обычным `pool.fetch`, и тогда в Telegram уходил
шифротекст: адрес получался вида «/botENC:8f2a…/getMe», Telegram отвечал 401,
а снаружи это выглядело как сломанная функция, а не как ошибка шифрования.

Так не работали, среди прочего: список и установка команд бота, сведения о
вебхуке и его снятие, мультигео, регистрация вебхуков при старте (весь режим
webhook у управляемых ботов) и рассылка self-promo.

Починено в самой последней точке, через которую проходят все: `bot_api.api_url`
расшифровывает токен при сборке адреса. Оставшиеся места, которые собирают
адрес или создают aiogram-бота сами, расшифровывают у себя.

Тест держит эту дверь закрытой: и поведением api_url, и разбором исходников —
вызов, который снова начнёт собирать адрес мимо неё, будет виден.
"""
from __future__ import annotations

import ast
import inspect
import os
import pathlib
import re

from services import bot_api
from services.token_vault import encrypt_token

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOKEN = "123456789:AAHrealtokenlooking-value_0123456789ab"


# ── Поведение единственной двери ──────────────────────────────────────────────

def test_api_url_decrypts_token():
    enc = encrypt_token(TOKEN)
    assert enc.startswith("ENC:")
    url = bot_api.api_url(enc, "getMe")
    assert url == f"https://api.telegram.org/bot{TOKEN}/getMe"
    assert "ENC:" not in url


def test_api_url_passes_plaintext_through():
    """Уже расшифрованный токен обязан проходить без изменений.

    Половина вызывающих читает бота через db.fetch_bots и передаёт открытый
    токен. Если бы api_url портила его, починка одного сломала бы другое.
    """
    assert bot_api.api_url(TOKEN, "getMe") == f"https://api.telegram.org/bot{TOKEN}/getMe"


def test_api_url_survives_empty_token():
    assert bot_api.api_url("", "getMe").endswith("/bot/getMe")
    assert bot_api.api_url(None, "getMe").endswith("/bot/getMe")


def test_call_goes_through_the_door():
    """_call не должен собирать адрес сам — иначе дверь снова мимо."""
    src = inspect.getsource(bot_api._call)
    assert "api_url(" in src
    assert "TG.format(" not in src, "адрес собирается в обход api_url"


def test_no_direct_tg_format_left_in_bot_api():
    src = (ROOT / "services" / "bot_api.py").read_text(encoding="utf-8")
    uses = [ln.strip() for ln in src.splitlines()
            if "TG.format(" in ln and "return TG.format" not in ln]
    assert not uses, f"TG.format вызывается мимо api_url: {uses}"


# ── Разбор исходников: кто читает колонку сырым запросом ──────────────────────

_DECRYPTING_READERS = ("fetch_bots", "fetchrow_bot", "get_bot", "_dec_bot_rows")
_RAW_FETCH = re.compile(r"\b(?:pool|conn)\.(?:fetch|fetchrow|fetchval)\s*\(")
_TOKEN_SELECT = re.compile(r"(?is)managed_bots")
_HAND_ROLLED = re.compile(r"api\.telegram\.org/(?:file/)?bot\{|Bot\(token=")


def _functions():
    """Обходит все def/async def в services/bot/database и отдаёт их тело.

    Тело берём НЕ через ast.get_source_segment(src, n): та функция заново
    разбивает src на строки (_splitlines_no_ff) на КАЖДЫЙ вызов, то есть на
    КАЖДУЮ функцию файла — а mini_app_api.py вырос до ~22К строк / ~700
    функций. Это O(функций × размера_файла): на живом прогоне именно здесь
    тест разросся с секунд до нескольких минут и тянул за собой весь
    сьют. Разбиваем на строки ОДИН раз на файл и режем по lineno/end_lineno
    напрямую — регэксп-проверкам ниже точность до колонки на первой/последней
    строке не нужна, важно только само тело функции.
    """
    for sub in ("services", "bot", "database"):
        for dirpath, _dirs, files in os.walk(ROOT / sub):
            if "__pycache__" in dirpath:
                continue
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = pathlib.Path(dirpath) / f
                src = path.read_text(encoding="utf-8", errors="ignore")
                try:
                    tree = ast.parse(src)
                except SyntaxError:
                    continue
                lines = src.splitlines()
                for n in ast.walk(tree):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        end = getattr(n, "end_lineno", None) or n.lineno
                        body = "\n".join(lines[n.lineno - 1:end])
                        yield path.relative_to(ROOT).as_posix(), n.lineno, n.name, body


def test_detector_sees_token_readers():
    """Пробник обязан находить читателей колонки — иначе тест ни о чём."""
    seen = sum(1 for _p, _l, _n, body in _functions()
               if _RAW_FETCH.search(body) and _TOKEN_SELECT.search(body))
    assert seen > 5, "пробник не видит запросов к managed_bots — он сломан"


def test_raw_token_never_reaches_telegram_directly():
    """Сырой читатель колонки не имеет права сам собирать адрес или бота."""
    bad = []
    for rel, lineno, name, body in _functions():
        if not (_RAW_FETCH.search(body) and _TOKEN_SELECT.search(body)):
            continue
        if not _HAND_ROLLED.search(body):
            continue  # ходит через bot_api — там дверь уже закрыта
        if any(r in body for r in _DECRYPTING_READERS):
            continue
        if "decrypt_token" in body or "_dt(" in body or "_dt_tok(" in body:
            continue
        bad.append(f"{rel}:{lineno} {name}()")
    assert not bad, (
        "токен читается сырым запросом и уходит в Telegram без расшифровки — "
        "Telegram ответит 401, а функция будет выглядеть сломанной:\n  "
        + "\n  ".join(bad)
        + "\n\nЧитайте через db.fetch_bots/db.fetchrow_bot или расшифруйте явно."
    )


def test_webhook_registration_decrypts():
    """Регистрация вебхука собирает адрес сама — значит расшифровывает сама."""
    from services import managed_bot_webhooks as m

    for fn in (m.register_webhook, m.unregister_webhook):
        src = inspect.getsource(fn)
        assert "decrypt_token" in src, (
            f"{fn.__name__} собирает адрес Telegram из сырого токена"
        )


def test_webhook_secret_is_derived_from_one_form_of_token():
    """Секрет вебхука считается от расшифрованного токена в обе стороны.

    Иначе снятие вебхука не найдёт свою же запись в карте секретов.
    """
    from services import managed_bot_webhooks as m

    reg = inspect.getsource(m.register_webhook)
    unreg = inspect.getsource(m.unregister_webhook)
    for src in (reg, unreg):
        i_dec = src.index("decrypt_token(bot_token")
        i_sec = src.index("_make_secret(bot_token)")
        assert i_dec < i_sec, "секрет считается раньше расшифровки"
