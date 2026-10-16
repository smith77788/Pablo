"""Экран, который не смог загрузить данные, не придумывает их.

Ошибка запроса — это «не знаю», а не «пусто» и не значение по умолчанию.
Пойманный случай: `openBilling()` при упавшем запросе писал в название тарифа
«Бесплатный». Человек, который платит, видел «Бесплатный», пустой срок
действия и список тарифов с предложением купить то, что у него уже есть.
В том же экране, тридцатью строками ниже, состав тарифа обрабатывался
правильно и с объяснением: «Выдумывать состав тарифа нельзя — про него
принимают решение о покупке». Название тарифа — ровно тот же случай.

Вторая проверка — про выход. Экран, который грузится сам (показал крутилку, а
не ждёт нажатия), обязан в ветке ошибки дать способ продолжить: `errHtml` с
повтором, `empty` с действием или собственную кнопку. Пять функций из списка
исключений — это результат нажатия кнопки, которую видно и можно нажать
снова, поэтому отдельный выход им не нужен; всё остальное — тупик.
Соседний `test_miniapp_error_has_exit.py` проверяет сами помощники
(`errHtml`/`empty`), этот — места, которые их обходят.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html, screen_files

#: Ветка ошибки без своего выхода допустима там, где загрузку запускает
#: нажатие видимой кнопки: человек нажимает её ещё раз.
BUTTON_DRIVEN = {
    "checkProxyIsolation": "кнопка «Проверить изоляцию» остаётся на экране",
    "computeCampaignPlan": "кнопка «Рассчитать план» остаётся на экране",
    "gpLoadPresetCities": "кнопка подбора городов остаётся в форме",
    "dmPreview": "предпросмотр ЛС вызывается кнопкой в модалке",
    "previewFactoryNames": "предпросмотр имён вызывается кнопкой в форме",
}


def _sources() -> dict[str, str]:
    out = {"mini_app/index.html": miniapp_html()}
    for p in screen_files():
        out[f"mini_app/screens/{p.name}"] = p.read_text(encoding="utf-8")
    return out


def _block(text: str, open_at: int) -> str:
    """Тело блока от его `{` до парной `}` — по балансу скобок, не по длине."""
    depth = 0
    for j in range(open_at, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at : j + 1]
    return text[open_at:]


def _functions(text: str):
    for m in re.finditer(r"\b(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", text):
        start = m.end() - 1
        body = _block(text, start)
        yield m.group(1), text[: m.start()].count("\n") + 1, body, start + len(body)


def _catches(body: str):
    for m in re.finditer(r"\bcatch\s*\(\s*\w*\s*\)\s*\{", body):
        start = m.end() - 1
        cb = _block(body, start)
        yield cb, body[start + len(cb) : start + len(cb) + 400]


def _billing_catch() -> str:
    html = miniapp_html()
    for name, _line, body, _end in _functions(html):
        if name == "openBilling":
            for cb, _after in _catches(body):
                if "billingPlanName" in cb:
                    return cb
    raise AssertionError("не нашёл ветку ошибки openBilling — разбор функции сломался")


def test_detector_reads_the_functions():
    """Измеритель обязан видеть разбираемый код, иначе пустой список находок
    не значит ничего."""
    html = miniapp_html()
    names = {n for n, _l, _b, _e in _functions(html)}
    assert "openBilling" in names and "loadHome" in names
    assert len(names) > 500, f"функций разобрано всего {len(names)}"
    assert "billingPlanName" in _billing_catch()


def test_billing_does_not_claim_a_plan_it_failed_to_load():
    cb = _billing_catch()
    # комментарий внутри ветки объясняет, почему так делать нельзя, и сам
    # называет тариф — проверяем код, а не объяснение
    code = "\n".join(l for l in cb.splitlines() if not l.lstrip().startswith("//"))
    for lie in ("'Бесплатный'", '"Бесплатный"', "'Платный'", '"Платный"'):
        assert lie not in code, (
            "упавший запрос подписывается конкретным тарифом — платящий человек "
            "увидит чужой тариф и предложение купить то, что у него уже есть")
    assert "не загрузился" in cb.lower() or "не удалось" in cb.lower(), \
        "ветка ошибки должна прямо сказать, что тариф неизвестен"
    assert "openBilling()" in cb, "в ветке ошибки нет повтора запроса"


def test_self_loading_screens_offer_a_way_out():
    dead = []
    for fname, text in _sources().items():
        for name, line, body, _end in _functions(text):
            if "spin-wrap" not in body:      # экран не грузится сам — ждёт нажатия
                continue
            if name in BUTTON_DRIVEN:
                continue
            for cb, after in _catches(body):
                if not ("innerHTML" in cb or re.search(r"\btxt\(", cb)):
                    continue
                if any(tok in cb for tok in ("errHtml", "empty(", "<button", "btn ", "toast(", "<option")):
                    continue
                if re.match(r"\s*finally\s*\{", after):   # состояние вернёт finally
                    continue
                one_line = re.sub(r"\s+", " ", cb)[:90]
                dead.append(f"{fname}:{line} {name}() — {one_line}")
    assert not dead, (
        "экран грузится сам, а из ошибки нет выхода — останется текст ошибки и "
        "ни одной кнопки:\n  " + "\n  ".join(dead))
