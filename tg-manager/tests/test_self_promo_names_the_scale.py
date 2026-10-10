"""Самопиар: перед рассылкой сказано, скольким людям она уйдёт.

Рассылка в личные сообщения необратима и банооопасна: отозвать её нельзя, а
каждое сообщение от бота человеку, который его не ждал, — повод для жалобы.
Подтверждение при этом спрашивало «Запустить рассылку самопиара по
подписчикам ваших ботов?» и не называло ни одного числа: ни скольким людям,
ни с каких ботов. Размер аудитории продукт считает сам, но уже ПОСЛЕ запуска,
внутри исполнителя.

Второе: операция ставилась с total_items=1 независимо от того, уходит она
троим или тысяче. До старта очередь показывала «1 из 1», и только дойдя до
операции, воркер исправлял число.

Главное, что держит этот тест, — совпадение отборов. Число в подтверждении и
список, по которому исполнитель реально шлёт, должны строиться одним условием;
разойдясь, подтверждение будет честно называть неправильное число.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
WORKER = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
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


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


def test_route_exists():
    assert "GET /api/miniapp/self_promo/audience" in SNAPSHOT


def _counted_sql() -> str:
    """Склеить SQL из строковых литералов константы в mini_app_api."""
    i = API.index("_SELF_PROMO_AUDIENCE_WHERE = (")
    j = API.index("\n    )", i)
    parts = re.findall(r'"([^"]*)"', API[i:j])
    assert parts, "константу отбора больше не собрать из литералов"
    return _norm("".join(parts))


def _sent_sql() -> str:
    m = re.search(
        r"FROM bot_users bu\s+JOIN managed_bots mb.*?AND mb\.token IS NOT NULL",
        WORKER, re.S)
    assert m, "исполнитель больше не отбирает получателей этим запросом"
    return _norm(m.group(0))


def test_detector_sees_both_queries():
    """Самопроверка измерителя: оба запроса найдены и непусты."""
    assert _counted_sql().startswith("from bot_users bu"), _counted_sql()
    assert _sent_sql().startswith("from bot_users bu"), _sent_sql()


def test_counted_audience_matches_what_the_executor_sends_to():
    """Отбор получателей — слово в слово тот же, что у исполнителя."""
    counted, sent = _counted_sql(), _sent_sql()
    assert counted == sent, (
        "подсчёт и отправка отбирают по разным условиям — подтверждение "
        "назовёт неправильное число:\n  счёт:     " + counted +
        "\n  отправка: " + sent)


def test_cap_matches_the_executor_limit():
    i = API.index("_SELF_PROMO_CAP = ")
    cap = int(re.search(r"_SELF_PROMO_CAP = (\d+)", API[i:i + 60]).group(1))
    m = re.search(r"AND mb\.token IS NOT NULL\s+ORDER BY bu\.user_id\s+LIMIT (\d+)",
                  WORKER)
    assert m, "у исполнителя пропал потолок рассылки"
    assert cap == int(m.group(1)), (
        f"экран обещает потолок {cap}, исполнитель шлёт по {m.group(1)}")


def test_confirmation_names_the_number():
    f = _fn("launchSelfPromo")
    assert "self_promo/audience" in f, "размер аудитории не спрашивается"
    assert "aud.will_send" in f, "в подтверждении нет числа получателей"
    assert "Отозвать рассылку нельзя" in f, (
        "не сказано, что рассылку не отозвать")
    assert "aud.cap" in f, "не сказано про потолок за один запуск"


def test_confirmation_survives_a_failed_count():
    """Не смогли посчитать — спрашиваем честно, а не молча шлём."""
    f = _fn("launchSelfPromo")
    assert "Не удалось посчитать" in f, (
        "при сбое подсчёта подтверждение промолчит про размер")
    assert f.count("askConfirm(") == 1, "подтверждение можно обойти"


def test_queue_is_not_told_one():
    h = _handler("self_promo_launch")
    assert "total_items=1," not in h, (
        "операция снова ставится как «1 из 1» независимо от размера")
    assert 'total_items=max(1, int(aud["will_send"]))' in h


def test_label_is_russian():
    h = _handler("self_promo_launch")
    assert "Self Promo:" not in h, "подпись операции вернулась к английской"
    assert "Самопиар:" in h


def test_full_template_text_is_readable():
    f = _fn("openSelfPromo")
    assert "showSelfPromoText(" in f, "полный текст шаблона прочитать негде"
    assert "slice(0,90)+'…'" in f, (
        "многоточие снова дописывается к любому тексту, даже короткому")
    assert "showInfo(" in _fn("showSelfPromoText"), (
        "показ текста спрашивает «Отмена/ОК» там, где отменять нечего")


def test_no_english_abbreviation():
    f = _fn("openSelfPromo")
    assert "CTA:" not in f, "вернулась английская подпись CTA"


def test_states_have_a_way_out():
    f = _fn("openSelfPromo")
    assert "errHtml(errRu(e), 'openSelfPromo()')" in f, "ошибка — тупик"
    assert "openSelfPromoCreateModal()" in f, (
        "пустой экран не предлагает создать шаблон")
