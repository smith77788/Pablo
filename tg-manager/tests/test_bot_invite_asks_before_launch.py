"""Бот не запускает инвайт по нажатию кнопки настройки.

Инвайт — самая баноопасная операция продукта. В боте кнопка выбора объёма
(«25 / аккаунт») ставила операцию в очередь немедленно: ни экрана «вот что
сейчас будет», ни возможности передумать. Половины настроек, которые есть в
мини-аппе, в боте не было вовсе — включая отключение дедупа, которое итог
самой операции советует («или отключите дедуп»), и отказ от выдачи админки
аккаунтам оператора.
"""
from __future__ import annotations

import ast
import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HANDLER = os.path.join(ROOT, "bot", "handlers", "mass_inviter.py")
API = os.path.join(ROOT, "services", "mini_app_api.py")


@functools.lru_cache(maxsize=1)
def _src() -> str:
    return open(HANDLER, encoding="utf-8").read()


@functools.lru_cache(maxsize=1)
def _funcs() -> dict[str, str]:
    src = _src()
    lines = src.splitlines()
    out: dict[str, str] = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = "\n".join(lines[node.lineno - 1:node.end_lineno])
    return out


def _code(name: str) -> str:
    """Тело функции без строк-комментариев: слово в комментарии ничего не делает."""
    body = _funcs().get(name)
    assert body, f"обработчик {name} не найден"
    return "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))


def test_volume_button_does_not_launch():
    """Кнопка объёма ведёт на обзор, а не в очередь операций."""
    vol = _code("_inv_offer_volume")
    assert 'action="review"' in vol, (
        "кнопки объёма снова ведут прямо на запуск — операция стартует без "
        "единого экрана подтверждения")
    assert 'action="confirm"' not in vol, (
        "с экрана выбора объёма снова можно запустить операцию одним нажатием")
    # Постановка в очередь живёт только в обработчике запуска.
    assert "operation_bus.submit" not in _code("cb_inviter_review"), (
        "экран обзора сам ставит операцию — он обязан только показывать")
    assert "operation_bus.submit" in _code("cb_inviter_confirm"), (
        "запуск потерял постановку операции")


def test_review_shows_what_will_happen():
    review = _code("_inv_offer_review")
    for token in ("group", "acc_count", "total_users", "inv_method",
                  "inv_pace", "inv_vol"):
        assert token in review, f"обзор не показывает {token}"
    assert "🚀 Запустить" in review, "на обзоре нет кнопки запуска"
    assert 'action="confirm"' in review, "кнопка запуска ничего не запускает"
    assert "❌ Отмена" in review, "с обзора нельзя уйти, не запустив"


def test_launch_reads_volume_from_state_not_from_the_button():
    """Кнопка запуска шлёт «go» — объём обязан браться из состояния."""
    conf = _code("cb_inviter_confirm")
    assert 'data.get("inv_vol"' in conf, (
        "объём снова читается из нажатой кнопки: с экрана обзора туда приходит "
        "«go», и любой выбор оператора молча превратился бы в «авто»")


def test_missing_settings_reached_the_bot():
    """Настройки, которые были только в мини-аппе, доехали до бота."""
    conf = _code("cb_inviter_confirm")
    review = _code("_inv_offer_review") + _src()
    pairs = [
        ("use_daughter_groups", "inv_daughter"),
        ("skip_invited", "inv_skip_invited"),
        ("auto_promote", "inv_auto_promote"),
        ("promote_trick", "inv_promote_trick"),
        ("link_fallback", "inv_link_fallback"),
    ]
    missing = [p for p, key in pairs
               if p not in conf or key not in review]
    assert not missing, (
        "настройка есть в мини-аппе, но в боте её снова нет: " + ", ".join(missing))
    # Значения по умолчанию совпадают с поведением исполнителя без флага:
    # в params попадают только отличия, иначе экран обещал бы одно, а
    # операция делала другое.
    for key in ("inv_skip_invited", "inv_auto_promote",
                "inv_promote_trick", "inv_link_fallback"):
        assert re.search(re.escape(key) + r"[\"']?\s*,\s*True\)\s*is False", conf), (
            f"{key} кладётся в params не только при отличии от умолчания")


def test_showcase_never_goes_without_daughter_groups():
    """Исполнитель требует обе настройки — экран не должен обещать иначе."""
    # Проверяем ВЛОЖЕННОСТЬ по дереву, а не порядок слов в тексте: строчкой
    # ниже — это всё ещё «независимо», и текстовая проверка бы это пропустила.
    tree = ast.parse(_funcs()["cb_inviter_confirm"])
    outer = None
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "inv_daughter" in ast.dump(node.test):
            outer = node
            break
    assert outer is not None, "«Мать-Дочка» больше не проверяется перед витриной"
    inside = "use_showcase" in "\n".join(ast.dump(st) for st in outer.body)
    total = sum("use_showcase" in ast.dump(n)
                for n in ast.walk(tree) if isinstance(n, ast.Subscript))
    assert inside, "витрина ставится вне проверки «Мать-Дочка»"
    assert total == 1, (
        "витрина ставится где-то ещё, минуя проверку «Мать-Дочка» — "
        "операция её отвергнет")
    vis = _code("_inv_visible_toggles")
    assert "inv_showcase" in vis and "inv_daughter" in vis, (
        "витрина показывается даже без «Мать-Дочка»")
    tgl = _code("cb_inviter_toggle")
    assert "inv_showcase" in tgl, (
        "выключив «Мать-Дочка», оператор остался бы с включённой витриной")


def test_invite_text_reaches_the_fallback_too():
    """Фолбэк «ссылка в ЛС» включён и для direct/admin — текст нужен и там."""
    conf = _code("cb_inviter_confirm")
    m = re.search(r"if data\.get\(\"inv_link_msg\"\)[^\n]*\n[^\n]*", conf)
    assert m, "текст приглашения больше не пробрасывается"
    assert 'method == "link"' in m.group(0) and "inv_link_fallback" in m.group(0), (
        "текст приглашения снова уходит только для метода «ссылка в ЛС», а "
        "фолбэк для обычного инвайта шлёт текст по умолчанию")
    # Тот же разбор в мини-аппе — чтобы поверхности не разъезжались.
    api = open(API, encoding="utf-8").read()
    assert "link_fallback" in api, "мини-апп потерял этот же флаг"
