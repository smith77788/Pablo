"""Отказ Telegram при входе по номеру приходил владельцу как тупик.

Жалоба владельца: «не отправляются коды повторно». На экране — красная полоса
«Не удалось повторно отправить код», и всё. Из этого текста не следует НИЧЕГО:
подождать? начать заново? номер уже не годится? Настоящую причину знал только
лог сервера, куда владелец не заглядывает, а автоповтор останавливался молча.

Причина у отказа своя у каждого случая и Telegram её называет:
SEND_CODE_UNAVAILABLE — кончились способы доставки на этот номер (именно это и
происходит, когда автоповтор долбит код), AUTH_RESTART — начать вход заново,
PHONE_NUMBER_BANNED — номером не войти вообще. Каждый случай требует РАЗНОГО
действия, а общий текст делал их неразличимыми.

Поэтому проверка здесь требует двух вещей сразу: обработчик обязан отдавать
перевод, а перевод обязан быть русским и непустым. Английская формулировка
telethon наружу не идёт — владелец не читает по-английски (CLAUDE.md).
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _handler(src: str, name: str) -> str:
    """Тело обработчика: от объявления до следующего — не окном длины.

    Окно фиксированной длины однажды перестаёт доставать до искомого просто
    потому, что код сдвинулся, и проверка молча зеленеет.
    """
    i = src.index("async def " + name + "(")
    nxt = re.search(r"\n    async def \w+\(", src[i:])
    return src[i:i + nxt.start()] if nxt else src[i:]


# Поддельные ошибки telethon: настоящие классы в тестах недоступны (telethon
# застаблен), а сопоставление идёт по ИМЕНИ класса — значит проверяемо без него.
class SendCodeUnavailableError(Exception):
    message = "SEND_CODE_UNAVAILABLE"


class AuthRestartError(Exception):
    message = "AUTH_RESTART"


class PhoneNumberBannedError(Exception):
    message = "PHONE_NUMBER_BANNED"


class FloodWaitError(Exception):
    seconds = 87
    message = "FLOOD_WAIT_87"


class RPCError(Exception):
    message = "A wild unknown error (caused by ResendCodeRequest)"


def _ru(exc) -> str:
    from services import account_manager as am
    return am.login_error_ru(exc)


def test_probe_the_mapper_is_actually_consulted():
    """Если бы перевод возвращал пустоту, проверки ниже были бы бессмысленны."""
    assert _ru(SendCodeUnavailableError()).strip()
    assert _ru(RPCError()).strip()


def test_each_refusal_gets_its_own_answer():
    """Три разных отказа — три разных действия, а был один текст на всех."""
    texts = {
        _ru(SendCodeUnavailableError()),
        _ru(AuthRestartError()),
        _ru(PhoneNumberBannedError()),
    }
    assert len(texts) == 3, f"отказы снова неразличимы: {texts}"


def test_exhausted_delivery_is_named_as_such():
    """Главный случай владельца: автоповтор сжёг способы доставки."""
    t = _ru(SendCodeUnavailableError())
    assert "способ" in t.lower(), t
    assert "начните вход заново" in t.lower() or "подождите" in t.lower(), t


def test_flood_wait_says_how_long():
    assert "87" in _ru(FloodWaitError()), _ru(FloodWaitError())


def test_unknown_refusal_still_names_the_code():
    """Неизвестный отказ — тоже не тупик: виден код, с ним можно разбираться."""
    t = _ru(RPCError())
    assert "RPCError" in t or "ResendCodeRequest" in t, t
    assert "начните вход заново" in t.lower() or "позже" in t.lower(), t


def test_no_english_prose_reaches_the_owner():
    """Владелец не читает по-английски — это правило проекта, не вкусовщина."""
    from services import account_manager as am

    for key, text in am.LOGIN_ERROR_RU.items():
        # Заглавные идентификаторы (TG_API_ID) допустимы, английские СЛОВА — нет.
        words = re.findall(r"\b[a-z]{3,}\b", text)
        assert not words, f"{key}: английские слова в тексте владельцу: {words}"
        assert re.search(r"[а-яА-Я]", text), f"{key}: текст не на русском"


def test_handlers_do_not_answer_with_one_text_for_everything():
    api = _read("services/mini_app_accounts.py")
    for name in ("account_add_phone_start", "account_add_phone_resend"):
        body = _handler(api, name)
        assert "_login_err(" in body, (
            f"{name} снова отвечает общим текстом — владелец не узнает, "
            f"что ответил Telegram")


def test_probe_sees_a_handler_that_swallows_the_reason():
    """Измеритель обязан отличать починенный обработчик от прежнего."""
    bad = '''
    async def account_add_phone_resend(request):
        try:
            pass
        except Exception:
            return _err("Не удалось повторно отправить код", 400)

    async def other(request):
        pass
'''
    body = _handler(bad, "account_add_phone_resend")
    assert "_login_err(" not in body and "async def other" not in body


def test_screen_no_longer_promises_an_unlimited_pace():
    """Подсказка на экране утверждала, что потолок — только FloodWait.

    Из-за неё владелец и включил автоповтор в 5 запросов/мин: настоящая стена
    (кончились способы доставки на номер) в тексте не упоминалась вовсе.
    """
    html = _read("mini_app/index.html")
    i = html.index('id="accPhoneResendAdmin"')
    block = html[i:html.index("</div>", html.index("accPhoneRpm", i))]
    assert "Реально темп ограничит только FloodWait" not in block, (
        "экран снова обещает, что мешает только пауза")
    assert "способ" in block.lower(), (
        "о том, что способы доставки кончаются, на экране снова не сказано")
