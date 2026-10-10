"""Регресс: TON_API_KEY/TON_RATE откатывались на дефолт после каждого рестарта,
а экран настроек оплаты после рестарта врал про статус кошельков.

Первопричина №1. `msg_payment_setting_value` (админ меняет настройку оплаты)
пишет TON_WALLET/TRON_WALLET/TON_API_KEY/TON_RATE в platform_settings И в
`os.environ[key]` — но ТОЛЬКО для текущего процесса. `main.py` при старте
восстанавливал из БД в `os.environ`/`_PAY_OVERRIDES` только TRON_WALLET и
TON_WALLET — TON_API_KEY и TON_RATE не восстанавливались никак. После любого
рестарта/редеплоя `_get_ton_rate()` (`os.getenv("TON_RATE","3.0")`) и
`payment_checker._TON_API_KEY()` тихо возвращались к дефолту 3.0 / пустому
ключу — платежи считались по устаревшему курсу без единого предупреждения.

Первопричина №2. `_payment_settings_kb()`/`cb_pay_edit()` читали кошельки
через голый `os.getenv(key, "")`, а не через `_pay_cfg()` (который проверяет
`_PAY_OVERRIDES` — куда main.py кладёт кошельки, восстановленные из БД).
После рестарта `_payment_settings_text()` (использует `_ton_wallet()` →
`_pay_cfg`) показывала «✅ активен», а кнопка списка настроек — «❌ не задан»
для ТОГО ЖЕ кошелька на ОДНОМ И ТОМ ЖЕ экране.

main() — точка входа, слишком большая и с сетевыми побочными эффектами,
чтобы вызывать её в тесте (см. test_no_duplicate_router_include.py — тот же
довод для другого бага в main()). Разбор исходника + прямой вызов чистых
функций подтверждает и баг, и фикс.
"""
from __future__ import annotations

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _main_restore_block() -> str:
    src = _read("main.py")
    i = src.index("Платёжные кошельки из БД")
    j = src.index("\n    # Self-heal", i)
    return src[i:j]


# ── main.py: TON_API_KEY/TON_RATE обязаны восстанавливаться при старте ──────

def test_startup_restores_ton_api_key_and_rate_from_db():
    block = _main_restore_block()
    assert "pay_ton_api_key" in block, "TON_API_KEY не восстанавливается из БД при старте"
    assert "pay_ton_rate" in block, "TON_RATE не восстанавливается из БД при старте"
    assert 'os.environ["TON_API_KEY"]' in block or "os.environ[_env_name]" in block


def test_startup_still_restores_wallets_too():
    """Не сломать то, что уже работало."""
    block = _main_restore_block()
    assert "pay_tron_wallet" in block and "pay_ton_wallet" in block


# ── поведенческая проверка: то, что main.py ДОЛЖЕН делать, реально работает ──

@pytest.fixture(autouse=True)
def _clean_env():
    saved = {k: os.environ.get(k) for k in ("TON_API_KEY", "TON_RATE")}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_ton_rate_reflects_a_restored_env_value():
    """То же самое присваивание, что делает main.py при старте (os.environ[...] =
    значение из platform_settings) — должно сразу подхватываться потребителями."""
    from bot.handlers import subscription as sub

    os.environ["TON_RATE"] = "7.25"
    assert sub._get_ton_rate() == 7.25


def test_ton_api_key_reflects_a_restored_env_value():
    from services import payment_checker

    os.environ["TON_API_KEY"] = "restored-from-db-key"
    assert payment_checker._TON_API_KEY() == "restored-from-db-key"


# ── экран настроек: не должен противоречить сам себе ────────────────────────

def test_payment_settings_kb_uses_pay_cfg_not_bare_getenv():
    src = _read("bot/handlers/subscription.py")
    i = src.index("def _payment_settings_kb")
    j = src.index("\ndef ", i + 10)
    body = src[i:j]
    assert "_pay_cfg(key)" in body
    assert "os.getenv(key" not in body, (
        "кнопка обязана видеть кошелёк, восстановленный в _PAY_OVERRIDES, "
        "а не только тот, что лежит прямо в os.environ"
    )


def test_pay_edit_uses_pay_cfg_not_bare_getenv():
    src = _read("bot/handlers/subscription.py")
    i = src.index("async def cb_pay_edit")
    j = src.index("\n@router", i + 10)
    body = src[i:j]
    assert "_pay_cfg(key)" in body
    assert "os.getenv(key" not in body


def test_payment_settings_screen_agrees_with_itself_after_restart():
    """Сквозной сценарий: main.py восстановил кошелёк ТОЛЬКО в _PAY_OVERRIDES
    (не в os.environ — так уже устроено для кошельков). Заголовок экрана и
    кнопка списка обязаны показывать ОДИНАКОВЫЙ статус."""
    from bot.handlers import subscription as sub

    saved_env = os.environ.pop("TON_WALLET", None)
    saved_override = dict(sub._PAY_OVERRIDES)
    try:
        sub._PAY_OVERRIDES.clear()
        sub.set_pay_config({"TON_WALLET": "UQD_restored_after_restart_abc123"})

        text = sub._payment_settings_text()
        assert "✅" in text.split("TON кошелёк:")[1].split("\n")[0]

        kb = sub._payment_settings_kb()
        button_texts = [btn.text for row in kb.inline_keyboard for btn in row]
        wallet_btn = next((t for t in button_texts if "TON кошелёк" in t), None)
        assert wallet_btn is not None
        assert "не задан" not in wallet_btn, (
            f"заголовок и кнопка списка расходятся: text says ok, button says {wallet_btn!r}"
        )
    finally:
        sub._PAY_OVERRIDES.clear()
        sub._PAY_OVERRIDES.update(saved_override)
        if saved_env is not None:
            os.environ["TON_WALLET"] = saved_env
