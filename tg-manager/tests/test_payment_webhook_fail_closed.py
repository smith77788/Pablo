"""Хук, выдающий платный доступ, не должен работать без проверки подписи.

POST /webhook/payment активирует подписку по user_id из тела запроса. Проверка
подписи стояла под условием «если задан WEBHOOK_SECRET», а переменная нигде в
деплое не задаётся — значит любой, кто знает адрес, мог выдать себе платный
тариф одним запросом. То же у /webhook/cryptopay без CRYPTOPAY_TOKEN.

Правильное поведение для денежного эндпоинта — отказ, а не пропуск: нет
секрета, нет и приёма. Живой путь оплаты у продукта другой (проверка платежа
в блокчейне, services/payment_checker.py), поэтому закрытый хук ничего не
ломает, а открытый раздаёт тарифы бесплатно.
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import importlib

import pytest


def _func_src(module, name: str) -> str:
    """Тело функции по ГРАНИЦАМ из AST, а не по окну фиксированной длины.

    Окно промахивается, как только код сдвинулся, и отрицательная проверка
    внутри него молча становится правдой — защита выключается сама собой.
    Обработчики вложены в make_app, поэтому обходим всё дерево.
    """
    src = open(module.__file__, encoding="utf-8").read()
    lines = src.split("\n")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"функция {name} не найдена")


def _module(monkeypatch, **env):
    for key in ("WEBHOOK_SECRET", "CRYPTOPAY_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import services.payment_webhook as pw
    return importlib.reload(pw)


def test_signature_check_does_not_pass_without_a_secret(monkeypatch):
    pw = _module(monkeypatch)
    assert pw._verify_signature(b'{"user_id":1}', "") is False
    assert pw._verify_signature(b'{"user_id":1}', "deadbeef") is False


def test_signature_check_accepts_only_the_right_signature(monkeypatch):
    pw = _module(monkeypatch, WEBHOOK_SECRET="s3cr3t")
    body = b'{"user_id":1,"plan":"pro_1m"}'
    good = hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
    assert pw._verify_signature(body, good) is True
    assert pw._verify_signature(body, "sha256=" + good) is True
    assert pw._verify_signature(body, good[:-1] + "0") is False
    assert pw._verify_signature(body + b" ", good) is False


def test_webhook_is_closed_when_no_secret_is_configured(monkeypatch):
    """Источник правды — тело обработчика: он обязан отказывать без секрета."""
    pw = _module(monkeypatch)
    body = _func_src(pw, "payment_webhook")
    assert "_WEBHOOK_SECRET and not _verify_signature" not in body, (
        "проверка подписи снова стоит под условием «если секрет задан» — "
        "без переменной хук опять раздаёт тарифы кому угодно")
    assert "_webhook_closed(" in body, "нет секрета — должен быть явный отказ"


def test_cryptopay_is_closed_without_its_token(monkeypatch):
    pw = _module(monkeypatch)
    body = _func_src(pw, "cryptopay_webhook")
    assert "_webhook_closed(" in body, (
        "без CRYPTOPAY_TOKEN хук обязан отказывать, а не принимать любой запрос")
    assert "compare_digest" in body, (
        "токен надо сравнивать за постоянное время, а не оператором !=")


def test_closed_response_names_the_missing_variable(monkeypatch):
    """Отказ должен объяснять, что настроить, иначе он выглядит поломкой."""
    pw = _module(monkeypatch)
    resp = pw._webhook_closed("WEBHOOK_SECRET")
    assert resp.status == 503
    assert "WEBHOOK_SECRET" in resp.text
