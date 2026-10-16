"""У «убрать бота» в мини-аппе одно действие и правдивая подпись.

РАЗРЫВ. На экране ботов жили ДВА разных на вид действия: 🗑 «Убрать» на
карточке и в шапке бота, и «⏸ Выключить» рядом с ним. Обе кнопки писали один и
тот же `is_active`: `DELETE /api/miniapp/bot/{id}` делает
`UPDATE managed_bots SET is_active=FALSE`, `PUT …/toggle` — его же наоборот.
То есть корзина ничего не удаляла, а две кнопки в двух сантиметрах друг от
друга рассказывали про одно действие две разные истории. На уже выключенном
боте корзина не делала вообще ничего видимого: «удаляла» второй раз то, что
уже убрано.

Подписи расходились с делом и дальше. Тост говорил «🗑 Бот деактивирован»,
бейдж на карточке — «Выкл», а `bot_health.describe` — «Бот выключен вами»,
хотя во второй путь в это состояние бота уводит опрос после мёртвого токена,
и владелец там ни при чём.

Цена не косметическая. Убранный бот остаётся в списке мини-аппа (и это
правильно: его данные целы и его возвращают), но со словом «удалён» владелец
ждёт, что бот исчезнет, жмёт ещё раз — и делает вывод, что продукт сломан.
После перехода бота на мягкое удаление тот же флаг стал прятать бота из списка
в самом боте, так что цена ошибки выросла: «выключил» и «убрал» — теперь одно и
то же состояние во всём продукте.

ЭТОТ ТЕСТ держит инвариант: действие одно, оба направления у него есть,
и ни одна подпись не обещает удаления, которого не происходит.
"""
from __future__ import annotations

import re

from services import bot_health
from tests.miniapp_source import miniapp_html


def _fn(src: str, name: str) -> str:
    m = re.search(r"(?:async )?function " + re.escape(name) + r"\s*\(", src)
    assert m, f"функция {name} не найдена"
    j = src.index("{", m.start())
    depth, k, n = 0, j, len(src)
    while k < n:
        c = src[k]
        if c in "'\"`":
            q, k = c, k + 1
            while k < n and src[k] != q:
                k += 2 if src[k] == "\\" else 1
        elif c == "/" and k + 1 < n and src[k + 1] == "/":
            nl = src.find("\n", k)
            if nl == -1:
                break
            k = nl
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
        k += 1
    raise AssertionError(f"не нашли конец {name}")


def test_one_control_not_two_for_the_same_write():
    """Экран бота не предлагает две кнопки на одно и то же действие."""
    html = miniapp_html()
    assert "function botPowerConfirm" in html
    # старой пары больше нет ни под каким из её прежних имён
    assert "removeBotConfirm" not in html, "осталось второе имя для того же действия"
    assert "toggleBotActive" not in html, (
        "осталась вторая кнопка того же действия (или её осиротевший код)")


def test_the_single_control_works_both_ways():
    body = _fn(miniapp_html(), "botPowerConfirm")
    assert "method:'DELETE'" in body.replace(" ", ""), "нет пути «убрать»"
    assert "/toggle" in body, "нет пути «вернуть»"
    # возврат в работу — не разрушающее действие, подтверждения не требует
    ask = body.index("askConfirm")
    assert "if (isActive)" in body[:ask], (
        "подтверждение спрашивают и при возврате бота в работу — лишний шаг "
        "на действии, которое ничего не теряет")


def test_no_wording_promises_a_deletion_that_never_happens():
    body = _fn(miniapp_html(), "botPowerConfirm")
    # в подтверждении и тостах — то, что правда происходит
    assert "сохранятся" in body or "сохранены" in body, (
        "подтверждение не говорит, что подписчики и воронки остаются")
    assert "Вернуть в работу" in body, "не сказано, как вернуть бота"
    for lie in ("удалён", "удалена", "Удалить бота", "деактивирован"):
        assert lie not in body, f"подпись обещает «{lie}» — этого не происходит"


def test_off_badge_names_the_state_instead_of_shrugging():
    body = _fn(miniapp_html(), "renderBots")
    assert "'⏸ Убран из работы'" in body, "бейдж выключенного бота ничего не объясняет"
    assert "'Выкл'" not in body, "бейдж бота — обрубок «Выкл», а не состояние"


def test_health_does_not_blame_the_owner_for_a_dead_token():
    """Во второе «выключено» бота уводит опрос — владелец там ни при чём."""
    d = bot_health.describe({"is_active": False})
    assert d["state"] == "off"
    assert "вами" not in d["hint"], (
        "подпись утверждает, что бота выключил владелец, хотя сюда же попадает "
        "бот, отключённый после мёртвого токена")
    # состояние обратимо, и об этом сказано
    assert "верните" in d["hint"].lower() or "вернуть" in d["hint"].lower()
    assert "сохранен" in d["hint"].lower(), "не сказано, что данные целы"


def test_active_bot_is_unaffected():
    assert bot_health.describe({"is_active": True, "fail_streak": 0})["state"] == "ok"
