"""Экран называется «Перевод подарков», а передать с него было нельзя.

Под экраном лежит целая подсистема: планы передачи, позиции, отчёты, проверка
источника оплаты, тип операции `gift_transfer` в реестре шины и движок
`services/gift_transfer.py`. Дойти до неё можно было только через бота —
мини-апп показывал два числа и список подарков без единого действия.

Отдельно здесь закрыты две вещи, найденные по дороге:
* проверка плана отвечала владельцу по-английски («No gifts selected for
  transfer»), а бот показывал этот текст как есть;
* поиск сохранённого получателя в боте шёл по `WHERE id=$1` без владельца —
  callback_data приходит от пользователя, и подстановка чужого id отдавала
  чужого получателя.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _handler(src: str, name: str) -> str:
    i = src.index(f"\n    async def {name}(request")
    nxt = src.find("\n    async def ", i + 10)
    return src[i:nxt if nxt > 0 else len(src)]


def test_transfer_route_exists_and_goes_through_the_bus():
    api = _read("services/mini_app_api.py")
    assert '"/api/miniapp/gifts/transfer"' in api, "передавать подарки по-прежнему нечем"
    fn = _handler(api, "gift_transfer_submit")
    assert "gift_transfer" in fn and "submit(" in fn, (
        "операция ставится мимо шины — в обход тарифа, предохранителя и дедупа")


def _selection_helper() -> str:
    api = _read("services/mini_app_api.py")
    i = api.index("async def _gift_selection(")
    return api[i:api.index("\n    async def ", i + 10)]


def test_transfer_takes_only_my_own_gifts():
    """Скоуп живёт в общем помощнике — проверяем и его, и что им пользуются."""
    api = _read("services/mini_app_api.py")
    for name in ("gift_transfer_submit", "gift_transfer_preview"):
        assert "_gift_selection(" in _handler(api, name), (
            f"{name} выбирает подарки мимо общего помощника — скоуп по "
            f"владельцу разъедется с ним")
    assert "owner_id=$1" in _selection_helper().replace(" ", ""), (
        "подарки для передачи выбираются без скоупа по владельцу — подстановка "
        "чужого id отдала бы чужой подарок")


def test_preview_tells_the_cost_before_anything_leaves():
    """Передача необратима: сколько уйдёт и во сколько обойдётся — до нажатия."""
    api = _read("services/mini_app_api.py")
    assert '"/api/miniapp/gifts/transfer/preview"' in api, "предпросмотра нет"
    fn = _handler(api, "gift_transfer_preview")
    assert "stars" in fn, "предпросмотр молчит о стоимости"
    assert "create_plan" not in fn, (
        "предпросмотр создаёт план: брошенные предпросмотры осядут в базе")


def test_saved_recipients_are_reachable_from_the_app():
    api = _read("services/mini_app_api.py")
    assert '"/api/miniapp/gifts/recipients"' in api, (
        "сохранённые получатели есть в базе и в боте, но не в приложении")


def test_inventory_returns_what_the_transfer_needs():
    api = _read("services/mini_app_api.py")
    fn = _handler(api, "gift_inventory")
    for col in ("gi.account_id", "gi.gift_id"):
        assert col in fn, f"без {col} позицию плана передачи не собрать"


def test_validation_answers_the_owner_in_russian():
    src = _read("services/gift_transfer.py")
    fn = src[src.index("async def validate_plan"):]
    fn = fn[:fn.index("    async def _check_payment_source")]
    said = re.findall(r'(?:errors|warnings)\.append\(\s*\n?\s*f?"([^"]+)"', fn)
    assert said, "не нашлось ни одного сообщения проверки — проба сломана"
    for msg in said:
        # Подстановки f-строки — имена переменных, а не текст для владельца.
        msg = re.sub(r"\{[^}]*\}", "", msg)
        assert not re.search(r"[A-Za-z]{3,}", msg), (
            f"проверка плана отвечает владельцу по-английски: {msg!r}")


def test_bot_recipient_lookup_is_owner_scoped():
    src = _read("bot/handlers/gift_transfer.py")
    for m in re.finditer(r"FROM gift_recipients WHERE ([^\"']+)", src):
        assert "owner_id" in m.group(1), (
            "получатель ищется без владельца: callback_data приходит от "
            "пользователя, и чужой id вернул бы чужого получателя")


def test_screen_lets_pick_gifts_and_name_the_recipient():
    html = _read("mini_app/index.html")
    i = html.index("async function openGifts")
    body = html[i:html.index("async function scanGifts", i)]
    assert "giftToggle(" in body, "подарок нельзя выбрать"
    assert "giftsTransferStart(" in html, "некуда перейти к передаче"
    # Непередаваемый подарок выбирать нельзя: операция всё равно его пропустит.
    assert "is_transferable" in body


def test_confirmation_names_what_goes_and_to_whom():
    html = _read("mini_app/index.html")
    i = html.index("async function giftsTransferSubmit")
    body = html[i:i + 2000]
    assert "askConfirm(" in body, "необратимая передача уходит без подтверждения"
    assert "RECIP" in body or "recipient" in body, (
        "в подтверждении не названо, кому уходят подарки")
