"""Сторож застоя не должен прерывать прогон за соблюдение правил Telegram.

Два предела судят по одному и тому же признаку — движению `done_items`:

  * `_OP_STALL_MIN` (env `OP_STALL_MIN`, по умолчанию 90 мин, минимум 10) —
    «операция не двигается, прогон прерываем»;
  * `_FLOOD_INLINE_MAX_S` (env `OP_FLOOD_INLINE_MAX_SEC`, по умолчанию 15 мин,
    максимум 6 часов) — сколько прогону РАЗРЕШЕНО стоять на месте, выдерживая
    назначенную Telegram паузу (`bounded_flood_sleep`).

Диапазоны env пересекаются: `OP_STALL_MIN=10` вместе с
`OP_FLOOD_INLINE_MAX_SEC=3600` — оба значения внутри задокументированных
пределов — давали операцию, которую сторож прерывает за то, что она выдерживает
паузу. Снаружи это «операция сама падает на флуде», причём тем чаще, чем честнее
продукт выжидает паузу.

Побеждает сторож: владелец, поставивший узкое окно, хочет быстро узнавать о
зависших прогонах, и расширять окно за него неправильно. Ограничивается
разрешённая пауза — а длинную паузу и не надо пересиживать занятым слотом, для
этого есть `_defer_op_for_flood`: операция уходит в очередь и продолжается сама,
когда пауза истечёт. Ждать меньше, чем просил Telegram, при этом никто не
начинает — меняется только то, чем занят воркер, пока пауза идёт.
"""
from __future__ import annotations

import inspect

import pytest

from services import op_worker


# Крайние точки задокументированных диапазонов env: (пауза, сек ; окно, мин).
_ENV_CORNERS = [
    (15 * 60, 90),        # значения по умолчанию
    (15 * 60, 10),        # пауза по умолчанию против минимального окна
    (3600, 10),           # часовая пауза против минимального окна
    (6 * 3600, 10),       # максимальная пауза против минимального окна
    (30, 24 * 60),        # минимальная пауза против максимального окна
    (6 * 3600, 24 * 60),  # максимальная пауза против максимального окна
]


@pytest.mark.parametrize("pause_s,stall_min", _ENV_CORNERS)
def test_the_allowed_pause_always_fits_inside_the_stall_window(pause_s, stall_min):
    cap = op_worker._inline_pause_cap(pause_s, stall_min)
    assert cap + op_worker._STALL_PAUSE_GRACE_S <= stall_min * 60 or cap == 30, (
        f"пауза {cap}с не укладывается в окно застоя {stall_min} мин — сторож "
        f"прервёт операцию, которая соблюдает правила платформы")
    assert cap <= pause_s, "настройку владельца нельзя увеличивать"
    assert cap >= 30, "обнулить паузу внутри прогона нельзя — см. докстринг"


def test_the_default_configuration_is_left_alone():
    """90 мин окна и 15 мин паузы согласованы и так — трогать нечего."""
    assert op_worker._inline_pause_cap(15 * 60, 90) == 15 * 60


def test_the_live_configuration_is_consistent():
    assert (op_worker._FLOOD_INLINE_MAX_S + op_worker._STALL_PAUSE_GRACE_S
            <= op_worker._OP_STALL_MIN * 60), (
        "фактические значения разъехались: прогон, выдерживающий паузу, будет "
        "снят сторожем застоя")


def test_the_constant_goes_through_the_cap():
    src = inspect.getsource(op_worker)
    assert "_FLOOD_INLINE_MAX_S = _inline_pause_cap(" in src, (
        "ограничение объявлено, но к самой настройке не применено")


def test_the_stall_guard_keeps_its_own_window():
    """Окно сторожа — настройка владельца, её не расширяем: иначе зависший
    прогон держал бы слот дольше, чем владелец разрешил."""
    src = inspect.getsource(op_worker._run_with_stall_guard)
    assert "_OP_STALL_MIN * 60" in src
    assert "_FLOOD_INLINE_MAX_S" not in src


def test_a_long_pause_still_goes_to_the_queue_instead_of_the_slot():
    """Ограничение паузы работает только вместе с отсрочкой операции."""
    src = inspect.getsource(op_worker)
    assert "_defer_op_for_flood" in src
    assert "_FLOOD_INLINE_MAX_S" in inspect.getsource(op_worker._exec_mass_publish), (
        "исполнитель обязан сам решать: пересидеть короткую паузу или отложиться")


def test_the_grace_is_small_and_explicit():
    """Запас нужен на шаг опроса сторожа и джиттер паузы, а не вместо окна."""
    assert 60 <= op_worker._STALL_PAUSE_GRACE_S <= 15 * 60
