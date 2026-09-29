"""Командный центр: каждое число — переход, а не просто цифра.

Экран был ровно тем, на что жаловался владелец: большое красивое полотно из
колец, баров и столбиков, где не нажимается ничего. Аккаунтов 1234 — и всё,
дальше некуда; прокси живых 121 — и всё; операций за неделю столько-то — и
всё. Данные есть, а следующего шага нет.

Теперь строка легенды открывает список аккаунтов под этим статусом, бар
прокси — пул, здоровье — экран здоровья, столбики операций — очередь. Там,
где среза нет (статус без фильтра), строка остаётся строкой и кнопкой не
притворяется: ведущая «в никуда» кнопка хуже её отсутствия.

Заодно: 'ok' пишут в acc_status наравне с 'active', и в легенде вылезало
сырое английское «ok» — а после перевода две строки «Активные» подряд.
И подпись кольца обещала «всего», хотя запрос считает только is_active=TRUE.
"""
from __future__ import annotations

import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CC = os.path.join(ROOT, "mini_app", "screens", "cmdcenter.js")
HTML = os.path.join(ROOT, "mini_app", "index.html")
API = os.path.join(ROOT, "services", "mini_app_api.py")


@functools.lru_cache(maxsize=1)
def _cc() -> str:
    with open(CC, encoding="utf-8") as f:
        return f.read()


@functools.lru_cache(maxsize=1)
def _html() -> str:
    with open(HTML, encoding="utf-8") as f:
        return f.read()


def _js_func(src: str, name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    assert m, f"функция {name} не найдена"
    depth = 0
    for j in range(m.end() - 1, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f"не удалось найти конец функции {name}")


def test_screen_still_shows_those_numbers():
    """Антивакуумность: если карточки убрали, проверки ниже пусты."""
    body = _js_func(_cc(), "_ccRender")
    for card in ("Аккаунты по статусам", "Прокси-пул", "Здоровье сетки", "Операции за 7 дней"):
        assert card in body, f"карточка «{card}» пропала"


def test_account_statuses_lead_to_the_list():
    body = _js_func(_cc(), "_ccRender")
    assert "healthGoAccounts(" in body, "число аккаунтов никуда не ведёт"
    assert "role=\"button\"" in body


def test_proxies_health_and_ops_lead_somewhere():
    body = _js_func(_cc(), "_ccRender")
    for fn in ("openProxies()", "openHealth()", "openOps()"):
        assert fn in body, f"с командного центра нет перехода через {fn}"


def test_a_status_without_a_slice_is_not_a_fake_button():
    """Кнопка, ведущая «во все аккаунты» вместо своего среза, врёт."""
    assert re.search(r"limited:.*go: null", _cc()), (
        "«Ограничены» ведёт куда попало — среза под этот статус нет"
    )
    bar = _js_func(_cc(), "_ccBar")
    assert "onclick ?" in bar, "бар без перехода всё равно выглядит нажимаемым"


def test_every_status_the_db_writes_is_translated():
    """Словарь не знал 'ok', и в легенду падало сырое английское слово."""
    with open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8") as f:
        api = f.read()
    written = set(re.findall(r"acc_status\s*=\s*'([a-z_]+)'", api))
    m = re.search(r"const CC_STATUS = \{(.*?)\n\};", _cc(), re.S)
    assert m, "словаря статусов нет"
    known = set(re.findall(r"^\s{2}(\w+):", m.group(1), re.M))
    assert "ok" in known, "'ok' пишут в acc_status, а перевода нет"
    assert written <= known, f"без перевода остались: {sorted(written - known)}"


def test_identical_labels_are_merged():
    """'ok' и 'active' — один статус: две строки «Активные» подряд бессмысленны."""
    body = _js_func(_cc(), "_ccRender")
    assert "merged" in body, "одинаковые подписи не сводятся в одну строку"
    assert "m.label === st.label" in body


def test_donut_caption_matches_what_is_counted():
    """Запрос считает только is_active=TRUE — «всего» обещало больше."""
    body = _js_func(_cc(), "_ccRender")
    assert "'всего'" not in body, "подпись кольца считает не то, что обещает"
    assert "в работе" in body
    assert "Отключённые аккаунты" in body, "умолчание о том, кого нет в кольце"


def test_stage_slice_reaches_the_account_list():
    """«Прогрев» — не фильтр здоровья, а CRM-этап: без него вёл бы во «все»."""
    go = _js_func(_html(), "healthGoAccounts")
    assert "ACC_STAGE_FILTER" in go, "второй срез не доезжает до списка"
    assert re.search(r"warming:.*go: \['all', 'warming'\]", _cc())
