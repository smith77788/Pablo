"""Валидация ссылки на чат на входе, а не на дне операции.

ЧТО БЫЛО СЛОМАНО. Проверки формы не было вовсе: `normalize_telegram_join_ref`
для ЛЮБОГО текста возвращает ("public", текст), поэтому опечатка вроде «мой чат»
уезжала в очередь как @мой чат. Дальше операция клеймила аккаунты, подключалась
ими и падала на резолве — прогон и суточные лимиты аккаунтов сгорали на том, что
видно на входе за миллисекунду.
"""
from __future__ import annotations

import pytest

from services.mass_inviter_engine import validate_group_ref as v


@pytest.mark.parametrize("ref", [
    "@mychat", "mychat", "MyChat_2024",
    "https://t.me/mychat", "t.me/mychat", "https://t.me/mychat/1520",
    "https://t.me/+AbCdEfGh12", "t.me/joinchat/AbCdEfGh12", "+AbCdEfGh12",
    "-1001234567890", "123456789",
])
def test_accepts_real_chat_references(ref):
    ok, why = v(ref)
    assert ok, f"{ref!r} — валидная ссылка, но отклонена: {why}"


@pytest.mark.parametrize("ref,hint", [
    ("", "Укажите"),
    ("   ", "Укажите"),
    ("мой чат", "название чата"),
    ("Наш Клуб", "название чата"),
    ("ab", "Не похоже на чат"),
    ("my-chat", "Не похоже на чат"),
    ("https://t.me/+ab", "обрезанной"),
])
def test_rejects_garbage_with_actionable_reason(ref, hint):
    ok, why = v(ref)
    assert not ok, f"{ref!r} не должно приниматься"
    assert hint.lower() in why.lower(), f"причина отказа не объясняет, что делать: {why}"


def test_rejects_overlong_input():
    ok, why = v("@" + "a" * 600)
    assert not ok and "длинная" in why.lower()


def test_reason_is_never_empty_when_rejected():
    for bad in ("", "x", "мой чат", "!!!", "http://example.com/x"):
        ok, why = v(bad)
        if not ok:
            assert why.strip(), f"отказ без причины для {bad!r}"


# ── Валидация подключена на всех входах ──────────────────────────────────────

def _read(rel):
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")


def test_bot_validates_raw_input_not_normalized():
    """Прежняя проверка `if not group` после parse_group_ref не срабатывала
    никогда: parse_group_ref для любого текста возвращает «@текст»."""
    src = _read("bot/handlers/mass_inviter.py")
    assert src.count("validate_group_ref(message.text") == 2, (
        "обе точки ввода группы (пре-флайт и запуск) должны проверять сырой ввод"
    )
    assert 'if not group:\n        await message.answer("⚠️ Не удалось распознать группу' not in src, (
        "мёртвая проверка должна быть убрана, а не оставлена рядом с рабочей"
    )


def test_miniapp_validates_before_queueing():
    src = _read("services/mini_app_api.py")
    i = src.index("async def mass_inviter_submit")
    seg = src[i:src.index("    async def ", i + 30)]
    assert "validate_group_ref" in seg, (
        "мини-апп обязан отказать на входе, а не ставить негодную операцию в очередь"
    )
    assert seg.index("validate_group_ref") < seg.index("_obus.submit"), (
        "валидация должна идти ДО постановки операции"
    )


def test_executor_has_last_line_of_defence():
    """Операция приходит не только из мини-аппа: бот, цепочка продолжения, API."""
    src = _read("services/op_worker.py")
    i = src.index("async def _exec_mass_invite")
    seg = src[i:src.index("\nasync def _exec_", i + 1)]
    assert "validate_group_ref" in seg
    assert seg.index("validate_group_ref") < seg.index("_claim_available_accounts"), (
        "отказ должен случиться ДО клейма аккаунтов и подключения ими"
    )


# ── Мягкий режим для очереди ─────────────────────────────────────────────────
# Исполнитель не вправе придираться так же, как поле ввода: операция уже стоит в
# очереди, могла быть создана прежней версией или внешним API, и отказ по спорной
# форме превратил бы рабочий прогон в проваленный.

@pytest.mark.parametrize("ref", ["@g", "ab", "my-chat", "1234", "@x_1"])
def test_lenient_mode_lets_queued_operations_run(ref):
    ok, _ = v(ref, strict=False)
    assert ok, f"мягкий режим не должен валить операцию из очереди на {ref!r}"


@pytest.mark.parametrize("ref", ["мой чат", "Наш Клуб", "two words", ""])
def test_lenient_mode_still_stops_a_typed_chat_name(ref):
    """Введённое НАЗВАНИЕ чата не станет ссылкой ни при каких условиях —
    такую операцию надо остановить до клейма аккаунтов."""
    ok, why = v(ref, strict=False)
    assert not ok and why.strip()


def test_strict_mode_is_stricter_than_lenient():
    for ref in ("@g", "ab", "my-chat"):
        assert v(ref, strict=True)[0] is False
        assert v(ref, strict=False)[0] is True
