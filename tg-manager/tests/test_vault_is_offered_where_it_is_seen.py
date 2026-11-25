"""Хранилище предлагается на видном месте, а не только в копайлоте.

РАЗРЫВ. Хранилище диалогов — главное умение продукта (бот сохраняет личную
переписку, включая удалённое собеседником). Предлагал его ровно один путь:
подсказка копайлота `connect_vault` с приоритетом 72. Экран показывает ПЯТЬ
подсказок, а подсказок с приоритетом выше 72 в движке больше десяти, поэтому у
владельца с живым флотом (аккаунты без прокси, недоведённые операции, горячие
контакты) предложение не попадало в выдачу НИ РАЗУ. Владелец так и сказал:
«не вижу, чтобы пользователю предлагалось подключить хранилище».

В боте было то же самое: кнопка «🗄 Подключить Хранилище диалогов» жила только
в чеклисте быстрого старта, то есть исчезала сразу после активации, а в главном
меню её не было вообще.

Правило, которое держит этот тест: у предложения есть МЕСТО, не зависящее от
очереди подсказок (полоса на главном экране мини-аппа), и постоянная кнопка в
главном меню бота. Полоса видна только тем, у кого не подключено, и уходит
сама после подключения — иначе это баннер, который учат не замечать.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
PROMO = (ROOT / "mini_app" / "screens" / "vault_promo.js").read_text(encoding="utf-8")
KB = (ROOT / "bot" / "keyboards.py").read_text(encoding="utf-8")


def test_the_home_screen_has_a_place_for_the_offer():
    assert 'id="vaultPromo"' in HTML, "на главном экране нет места для предложения"
    assert "vaultPromoHome()" in HTML, (
        "предложение никто не наполняет: место есть, а вызова нет")
    assert '<script src="screens/vault_promo.js"></script>' in HTML, (
        "файл экрана не подключён — функции не будет, нажатие бросит ошибку")


def test_the_offer_is_hidden_from_those_who_already_connected():
    """Баннер тому, у кого всё работает, — это шум, который учат не замечать."""
    assert "st.connected" in PROMO, "предложение не смотрит на подключение"
    m = re.search(r"if \(!st \|\| st\.connected[^\n]*\) \{[^\n]*return", PROMO)
    assert m, "подключённый владелец всё равно увидит предложение"


def test_an_unknown_status_shows_nothing():
    """Сбой ответа не повод звать подключать того, у кого уже подключено."""
    assert re.search(r"catch \(_\) \{ return; \}", PROMO), (
        "при сбое статуса предложение показывается вслепую")


def test_later_hides_it_for_a_while_and_not_forever():
    assert "VAULT_PROMO_SNOOZE_DAYS" in PROMO
    m = re.search(r"VAULT_PROMO_SNOOZE_DAYS\s*=\s*(\d+)", PROMO)
    assert m and 1 <= int(m.group(1)) <= 30, (
        "«Позже» обязано прятать предложение на срок, а не навсегда: "
        "подключение занимает минуту, и вернуться к нему стоит")


def test_the_button_leads_to_the_connect_screen():
    assert "openVaultConnect()" in PROMO, "кнопка никуда не ведёт"
    assert "function openVaultConnect" in (
        ROOT / "mini_app" / "screens" / "vault_connect.js").read_text(encoding="utf-8")


def test_the_bot_main_menu_keeps_a_permanent_button():
    assert "Хранилище диалогов" in KB, (
        "в главном меню бота снова нет Хранилища — оно остаётся видно только "
        "новичку в чеклисте быстрого старта")
    assert re.search(r'VaultCb\(a="how"\)', KB), "кнопка меню ведёт не на экран хранилища"


def test_the_menu_layout_counts_the_new_button():
    """adjust() раскладывает ровно столько кнопок, сколько их есть."""
    body = re.search(r"def main_menu\([\s\S]*?return kb\.as_markup\(\)", KB).group(0)
    buttons = len(re.findall(r"kb\.button\(", body))
    adjust = re.search(r"kb\.adjust\(([^)]*)\)", body).group(1)
    # Админская кнопка добавляется условно — считаем обе раскладки.
    slots = sum(int(x.strip().split()[0]) for x in adjust.split(",")
                if x.strip() and x.strip()[0].isdigit())
    assert slots >= buttons - 1, (
        f"кнопок {buttons}, а раскладка описывает {slots}: лишние съезжают в "
        "последний ряд по одной")
