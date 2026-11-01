"""Хук, выдающий платный доступ, не должен работать без проверки подписи.

POST /webhook/payment активировал подписку по user_id из тела запроса. Проверка
подписи стояла под условием «если задан WEBHOOK_SECRET», а переменная нигде в
деплое не задаётся — значит любой, кто знает адрес, мог выдать себе платный
тариф одним запросом.

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


def test_universal_payment_hook_is_gone(monkeypatch):
    """Универсальный денежный хук снят (D29 карты дефектов).

    Его не звал ни один провайдер. Закрытым он был только пока не задан
    WEBHOOK_SECRET; с секретом тело без поля `plan` выдавало месяц подписки за
    любую сумму от 90 % цены, а срок из тела не сверялся со списком сроков.
    Живой путь оплаты — payment_checker (транзакция в блокчейне).
    """
    pw = _module(monkeypatch, WEBHOOK_SECRET="s3cr3t")
    src = open(pw.__file__, encoding="utf-8").read()
    assert '"/webhook/payment"' not in src, "универсальный денежный хук вернулся"
    for name in ("payment_webhook", "_activate_subscription", "_PLAN_MAP"):
        assert not hasattr(pw, name) and f"def {name}" not in src, name


def test_no_route_in_the_app_grants_a_subscription(monkeypatch):
    """Источник правды — собранное приложение, а не текст файла."""
    pw = _module(monkeypatch, WEBHOOK_SECRET="s3cr3t")

    class _P:
        pass

    app = pw.make_app(_P(), None)
    paths = {r.resource.canonical for r in app.router.routes() if r.resource is not None}
    assert "/webhook/payment" not in paths
    assert "/webhook/cryptopay" not in paths


def test_dead_cryptopay_door_is_gone(monkeypatch):
    """Вход CryptoPay снят: счетов CryptoPay продукт не выставляет.

    Он ждал токен в заголовке, а CryptoPay подписывает тело HMAC, то есть
    настоящее уведомление не принял бы никогда, — оставалась только денежная
    дверь, через которую при утечке токена выдаётся подписка.
    """
    pw = _module(monkeypatch)
    src = open(pw.__file__, encoding="utf-8").read()
    assert 'add_post("/webhook/cryptopay"' not in src, (
        "мёртвый вход CryptoPay вернулся: счетов CryptoPay в коде нет")
    assert "def cryptopay_webhook" not in src
