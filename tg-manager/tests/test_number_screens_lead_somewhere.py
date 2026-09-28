"""С экрана-витрины чисел должен быть выход к самим объектам.

Жалоба владельца (28.09.2026): «слишком много экранов, которые только
отображают какие-то числа или количество… и в них всё не кликабельно, ничего
не настраивается, не выбирается».

Первые два разобранных экрана — здоровье аккаунтов и статистика контактов.
Было: «17 забанено» и «Premium 1 200» — и всё, посмотреть КАКИЕ именно
нельзя. Стало: число ведёт в список под этим срезом, строка флуд-события — в
карточку аккаунта, строка «по аккаунтам» — в контакты этого аккаунта.

Притворяться нечем: срезы, которых сервер не умеет (низкий траст, флуд,
прогрев, «с телефоном»), остались обычными числами без стрелки.
"""
from __future__ import annotations

import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "mini_app", "index.html")
API = os.path.join(ROOT, "services", "mini_app_api.py")


@functools.lru_cache(maxsize=1)
def _html() -> str:
    with open(HTML, encoding="utf-8") as f:
        return f.read()


def _js_func(name: str) -> str:
    h = _html()
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", h)
    assert m, f"функция {name} в мини-аппе не найдена"
    depth = 0
    for j in range(m.end() - 1, len(h)):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                return h[m.start():j + 1]
    raise AssertionError(f"не удалось найти конец функции {name}")


def test_health_numbers_open_the_accounts():
    body = _js_func("openHealth")
    assert "healthGoAccounts" in body, "числа на экране здоровья никуда не ведут"
    for f in ("'all'", "'active'", "'banned'", "'cooldown'"):
        assert f in body, f"нет перехода по срезу {f}"
    go = _js_func("healthGoAccounts")
    assert "ACC_FILTER" in go and "reloadAccounts" in go, "переход не применяет срез"
    assert "goTab('accounts')" in go


def test_health_does_not_fake_filters_it_cannot_do():
    """Сервер знает all/active/cooldown/banned/spamblock/dead — и только их."""
    src = open(API, encoding="utf-8").read()
    assert '("all", "active", "cooldown", "banned", "spamblock", "dead")' in src, (
        "набор срезов на сервере изменился — проверку ниже надо пересмотреть")
    body = _js_func("openHealth")
    for fake in ("'low_trust'", "'flood'", "'warmup'"):
        assert f"healthGoAccounts({fake})" not in body, (
            f"экран обещает срез {fake}, которого сервер не умеет")


def test_flood_event_opens_the_account():
    body = _js_func("openHealth")
    assert "openAccount(" in body, "со строки флуд-события нельзя попасть в аккаунт"
    src = open(API, encoding="utf-8").read()
    assert "ta.id AS account_id" in src, "сервер не отдаёт id аккаунта в событии"


def test_flood_list_admits_its_cap():
    body = _js_func("openHealth")
    assert "events_total" in body, "«последние 10» выдаются за всю историю"
    src = open(API, encoding="utf-8").read()
    assert '"events_total"' in src
    assert "Последние флуд-события (7 дней)" not in _html(), (
        "заголовок обещает неделю, а список приходит за всё время")


def test_contact_stats_rows_lead_to_contacts():
    body = _js_func("openContactStats")
    assert "csGoContacts" in body, "числа статистики контактов никуда не ведут"
    assert "'premium'" in body and "'favorite'" in body
    assert "openContactGroups" in body, "из «Групп» нельзя попасть в группы"
    assert "csGoAccountContacts" in body, "нельзя посмотреть контакты одного аккаунта"


def test_contacts_can_be_filtered_by_source_account():
    body = _js_func("loadContacts")
    assert "account_id" in body, "список контактов не умеет срез по аккаунту-источнику"
    banner = _js_func("cgRenderBanner")
    assert "CONTACT_ACCOUNT" in banner, "срез по аккаунту не виден в полосе фильтра"


def test_tappable_number_looks_tappable():
    h = _html()
    assert ".kpi-card.tap-card" in h, "нажимаемое число ничем не отличается от обычного"
    body = _js_func("openHealth")
    assert "${lbl} ›" in body, "у нажимаемого числа нет стрелки в подписи"
