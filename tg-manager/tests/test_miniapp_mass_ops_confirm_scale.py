"""Массовая операция называет свой масштаб до запуска.

Часть экранов это уже делала: рассылка в ЛС считает получателей и срок, пост
во все каналы аккаунта показывает выборку целей, инвайт называет темп, цели и
аккаунты. Четыре операции того же веса запускались молча — по нажатию кнопки,
без единой цифры:

* массовая установка профиля (и пароля двухфакторной защиты) на N аккаунтов;
* ИИ-комментинг живыми аккаунтами в чужих каналах;
* накрутка просмотров/реакций/подписчиков.

Все четыре выполняются живыми аккаунтами, видны посторонним и не
отменяются. Отдельный случай — пароль 2FA: он ставится НА аккаунты, и если
потеряется, аккаунты потеряются вместе с ним; это единственное действие
здесь, которое не исправить повтором, поэтому у него своё предупреждение.

Проверка не требует подтверждения от всего подряд: список закрытый и
перечислен поимённо.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_source

SRC = miniapp_source()

# Операция -> что обязано прозвучать в вопросе.
MUST_ASK = {
    "submitProfileSetter": ["_setterScale", "askConfirm"],
    # Единая публикация спрашивает так же: 22.10.2026 «Быстрый пост» сначала
    # удалили как её дубль, потом вернули — а она в этом списке не значилась
    # вовсе, хотя публикует в те же каналы тем же движком.
    "sendMassPublish": ["pickedIds.length", "askConfirm"],
    "submitAiComment": ["list.length", "acc_count", "askConfirm"],
    "submitBoost": ["_bScale", "askConfirm"],
}


def _fn_body(name: str) -> str:
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\(", SRC, re.M)
    assert m, f"функция {name} не найдена"
    p = SRC.index("(", m.end() - 1)
    depth = 0
    for j in range(p, len(SRC)):
        if SRC[j] == "(":
            depth += 1
        elif SRC[j] == ")":
            depth -= 1
            if depth == 0:
                p = j
                break
    i = SRC.index("{", p)
    depth = 0
    for j in range(i, len(SRC)):
        if SRC[j] == "{":
            depth += 1
        elif SRC[j] == "}":
            depth -= 1
            if depth == 0:
                return SRC[i:j + 1]
    raise AssertionError(f"не закрылось тело {name}")


def test_each_operation_asks_before_acting():
    for fn, needles in MUST_ASK.items():
        body = _fn_body(fn)
        for n in needles:
            assert n in body, f"{fn}: в вопросе нет «{n}»"


def test_the_question_comes_before_the_request():
    """Вопрос после запроса — это уже не вопрос."""
    for fn in MUST_ASK:
        body = _fn_body(fn)
        ask = body.index("askConfirm")
        # действие — POST-запрос этой операции
        m = re.search(r"await api\('/api/miniapp/[\w/]+',\s*\{method:'POST'", body)
        assert m, f"{fn}: не нашли запускающий запрос"
        assert ask < m.start(), f"{fn}: спрашивает уже после запуска"


def test_refusal_stops_the_operation():
    """`await askConfirm(...)` без проверки результата запустил бы операцию и
    на «Отмена» — это худший вид подтверждения: выглядит как защита, ею не
    являясь."""
    for fn in MUST_ASK:
        body = _fn_body(fn)
        m = re.search(r"if\s*\(\s*!\s*\(?\s*await askConfirm\(", body)
        assert m, f"{fn}: результат вопроса не проверяется — «Отмена» ничего не остановит"
        assert "return" in body[m.start():m.start() + 800], f"{fn}: отказ ничего не останавливает"


def test_two_factor_password_warns_about_losing_the_accounts():
    body = _fn_body("submitProfileSetter")
    assert "SETTER_OP==='2fa'" in body, "предупреждение не привязано к операции 2FA"
    i = body.index("_setterWarn")
    seg = body[i:i + 400]
    assert "потеряете доступ" in seg.lower(), "не сказано, чем грозит потеря пароля"
    # и только к ней: смена имени таким текстом пугать не должна
    assert "'\\n\\n⚠️" in body or '"\\n\\n⚠️' in body or "\n\n⚠️" in body


def _confirm_arg(body: str) -> str:
    """Аргумент askConfirm по балансу скобок: внутри есть свои `(`."""
    i = body.index("askConfirm(") + len("askConfirm")
    depth = 0
    for j in range(i, len(body)):
        if body[j] == "(":
            depth += 1
        elif body[j] == ")":
            depth -= 1
            if depth == 0:
                return body[i + 1:j]
    raise AssertionError("не закрылся аргумент askConfirm")


def test_scale_is_worded_by_plural_helper():
    """«40 аккаунтов» против «41 аккаунт» — вручную это пишут с ошибкой.

    Считаем внутри самого вопроса: каждому числу нужна своя согласованная
    форма, иначе «3 каналах» или «1 аккаунтов» проедут незамеченными.
    Форма может стоять и в переменной, собранной выше (_setterScale, _bScale) —
    такие подстановки тоже считаем.
    """
    for fn, extra in (("submitProfileSetter", ["_setterScale"]),
                      ("sendMassPublish", []),
                      ("submitAiComment", []),
                      ("submitBoost", ["_bScale"])):
        body = _fn_body(fn)
        # Текст вопроса + определения переменных, которые в него подставляются:
        # форма может стоять и там (_setterScale, _bScale).
        text = _confirm_arg(body)
        for e in extra:
            k = body.index(e + " =") if (e + " =") in body else body.index(e)
            text += body[k:k + 300]
        numbers = text.count("num(")
        forms = text.count("plural(")
        assert numbers, f"{fn}: в вопросе не оказалось ни одного числа"
        assert forms >= numbers, (
            f"{fn}: чисел {numbers}, согласованных форм {forms} — «3 каналах» проедет незамеченным")


def test_post_repeat_is_quoted_from_the_list_not_recomputed():
    """Пересчёт минут давал «каждые 1 дн.»; подпись списка читается как речь.

    Урок найден на «Быстром посте» и переехал вместе с повтором на единую
    публикацию, когда эти два экрана свели в один (22.10.2026). Проверка
    привязана к экрану, который публикует, а не к тому, где урок нашли:
    на этом уже обжигались — экран удалили, и проверка осталась ни о чём.
    """
    for fn in ("sendMassPublish",):
        body = _fn_body(fn)
        assert "options[e.selectedIndex].textContent" in body, (
            f"{fn}: подпись срока или повтора снова не читается из самого списка")
        assert "humanDur(" not in body, f"{fn}: срок снова пересчитывается из минут"


def test_already_careful_operations_stay_careful():
    """Соседние операции задавали вопрос с масштабом и раньше — если он
    пропадёт, это тот же дефект, что чинил этот тест."""
    for fn in ("submitDmAdhoc", "submitBulkPostChans", "submitMassInvite"):
        body = _fn_body(fn)
        assert re.search(r"if\s*\(\s*!\s*\(?\s*await askConfirm\(", body), (
            f"{fn}: вопрос перестал останавливать запуск")
    assert "massTargets(" in _fn_body("submitBulkPostChans"), (
        "пост во все каналы перестал называть цели")
