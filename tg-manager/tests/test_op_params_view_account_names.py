"""Карточка операции показывает ИМЕНА аккаунтов, а не голые номера.

Жалоба владельца: в результатах операции аккаунты пронумерованы (#123 / 1,2,3)
вместо имён. describe() — чистая функция без БД, поэтому имена резолвит хендлер
(operation_status) и передаёт картой id→имя. Эти тесты сторожат формат.
"""
from __future__ import annotations

from services.op_params_view import describe


def test_account_ids_show_names_when_map_given():
    names = {1: "Иван", 2: "@petya", 3: "+79990001122"}
    out = {r["label"]: r["value"] for r in
           describe("mass_report", {"account_ids": [1, 2, 3]}, names)}
    val = out["Аккаунты"]
    assert "Иван" in val and "@petya" in val and "+79990001122" in val
    assert "#1" not in val  # номер заменён именем


def test_single_account_shows_name():
    out = {r["label"]: r["value"] for r in
           describe("bulk_create_channels", {"acc_id": 7}, {7: "Мария"})}
    assert out["Аккаунт"] == "Мария"


def test_unresolved_ids_fall_back_to_hash_marker():
    # Без карты (или если имя не нашли) — «#id», а не голое число: так видно, что
    # это идентификатор, а формат совпадает с одиночным «Аккаунт».
    out = {r["label"]: r["value"] for r in
           describe("mass_report", {"account_ids": [5, 6]})}
    assert out["Аккаунты"] == "2: #5, #6"
    out2 = {r["label"]: r["value"] for r in
            describe("mass_report", {"account_ids": [5, 6]}, {5: "Пять"})}
    # Частичный резолв: известное — именем, неизвестное — #id.
    assert out2["Аккаунты"] == "2: Пять, #6"


def test_backward_compatible_without_map():
    # Старая сигнатура describe(op_type, params) всё ещё работает.
    out = describe("strike", {"account_ids": [1]})
    assert out and out[0]["value"] == "1: #1"
