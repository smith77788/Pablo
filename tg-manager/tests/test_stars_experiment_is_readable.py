"""Тест цены в звёздах: видно, можно ли уже верить результату.

Экран «⭐ Звёзды» показывал строку вида «A:⭐50 CR12% · B:⭐100 CR8%» и кнопку
паузы. Два процента без третьего числа — достоверности — не говорят ничего:
при двадцати показах разница в четыре процента это шум, при тысяче — деньги.
Сам расчёт (хи-квадрат) в продукте есть, его делает stars_optimizer раз в
шесть часов, но показывался он только в Telegram-боте.

Теперь строка открывает карточку теста: сколько раз цена показана, сколько раз
купили, сколько принесла, и прямым текстом — хватает ли данных, случайна ли
разница, можно ли ставить цену основной. Итог подводится кнопкой, тест можно
удалить.

Отдельно держим порог показов: он один и тот же у движка и у экрана. Разойдясь,
экран обещал бы вывод раньше, чем движок его сделает.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
OPT = open(os.path.join(ROOT, "services", "stars_optimizer.py"), encoding="utf-8").read()
SNAPSHOT = open(os.path.join(ROOT, "tests", "miniapp_routes_snapshot.txt"),
                encoding="utf-8").read()


def _fn(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена"
    i = HTML.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:j + 1]
    raise AssertionError(name)


def _handler(name: str) -> str:
    i = API.index(f"async def {name}(request")
    j = API.index("\n    async def ", i + 10)
    return API[i:j]


def test_routes_exist():
    for r in ("GET /api/miniapp/stars/experiment/{exp_id}",
              "POST /api/miniapp/stars/experiment/{exp_id}/evaluate",
              "DELETE /api/miniapp/stars/experiment/{exp_id}"):
        assert r in SNAPSHOT, f"нет маршрута: {r}"


def test_detail_returns_the_verdict_not_just_counters():
    h = _handler("stars_experiment_detail")
    for key in ("p_value", "enough_data", "confident", "need_a", "need_b",
                "min_impressions"):
        assert f'"{key}"' in h, f"в ответе нет поля {key} — верить проценту нельзя"
    assert "_chi_square_p" in h, "достоверность не считается"


def test_detail_is_read_only():
    """Открыть карточку — не значит закрыть тест: UPDATE только по кнопке."""
    h = _handler("stars_experiment_detail")
    assert "evaluate_experiment" not in h, (
        "просмотр карточки сохраняет вывод — тест закроется сам собой")


def test_every_handler_is_scoped_to_the_owner():
    i = API.index("async def _own_stars_exp")
    assert "owner_id=$2" in API[i:i + 400], "выборка теста не скоупится по владельцу"
    for name in ("stars_experiment_detail", "stars_experiment_evaluate"):
        assert "_own_stars_exp(uid, exp_id)" in _handler(name), (
            f"{name} не проверяет владельца — чужой тест по номеру")
    d = _handler("stars_experiment_delete")
    assert "owner_id=$2" in d, "удаление не скоупится по владельцу"


def test_threshold_is_shared_with_the_engine():
    assert "MIN_IMPRESSIONS_FOR_WINNER = 100" in OPT
    assert re.search(r"imp_a >= MIN_IMPRESSIONS_FOR_WINNER", OPT), (
        "движок считает порог своим числом — разъедется с экраном")
    assert "MIN_IMPRESSIONS_FOR_WINNER as _STARS_MIN_IMPRESSIONS" in API
    h = _handler("stars_experiment_detail")
    # Порог упоминается в карточке четыре раза (хватает ли данных и сколько не
    # хватает по каждой цене) — и каждый раз именем из движка, не числом.
    code = "\n".join(ln for ln in h.splitlines()
                     if not ln.lstrip().startswith("#") and '"""' not in ln
                     and "·" not in ln)
    assert code.count("_STARS_MIN_IMPRESSIONS") >= 4, (
        "порог вшит в экран числом вместо импорта из движка")


def test_list_row_opens_the_experiment():
    f = _fn("openStars")
    assert "openStarsExp(" in f, "строка теста ни на что не нажимается"


def test_screen_says_whether_the_result_can_be_trusted():
    f = _fn("openStarsExp")
    for phrase in ("Данных пока мало", "не случайна", "вровень"):
        assert phrase in f, f"экран не говорит «{phrase}» — вывод непонятен"
    assert "need_a" in f and "need_b" in f, "не сказано, сколько показов не хватает"


def test_screen_has_no_bare_abbreviations():
    """«CR12%» владельцу ничего не говорит — нужны слова."""
    f = _fn("openStars")
    assert "CR${" not in f and "CR$" not in f, "вернулась аббревиатура CR"
    assert "покупают" in f, "процент покупок не назван по-русски"


def test_destructive_actions_ask_first():
    assert "askConfirm(" in _fn("evaluateStarsExp"), (
        "итог подводится без подтверждения — тест закроется случайным тапом")
    assert "confirmDelete(" in _fn("deleteStarsExp"), "удаление без подтверждения"


def test_post_bodies_and_methods_are_correct():
    assert "method:'POST'" in _fn("evaluateStarsExp")
    assert "method:'DELETE'" in _fn("deleteStarsExp")


def test_content_type_labels_are_defined():
    assert "const STARS_CTYPE_RU" in HTML, (
        "подписи типа контента не объявлены — экран упадёт на ReferenceError")
    for key in ("message:", "media:", "subscription:", "gift:"):
        i = HTML.index("const STARS_CTYPE_RU")
        assert key in HTML[i:i + 300], f"нет подписи для {key}"


def test_empty_state_offers_the_action():
    f = _fn("openStars")
    i = f.index("Тестов цены ещё не было")
    assert "openStarsExpModal()" in f[i:i + 400], (
        "пустой экран не предлагает создать тест")
