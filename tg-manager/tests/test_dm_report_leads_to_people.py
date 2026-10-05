"""Отчёт по рассылке в ЛС: за каждым числом видно людей, а не только счёт.

Экран отвечал «заблокировали: 54» и «✅ 120 ❌ 54» — и не отвечал на
единственный вопрос, ради которого отчёт открывают: КТО именно не получил.
Строки сводки не нажимались, аккаунт с ошибками никуда не вёл, а в «Последних
отправках» вместо человека стояло «id 783215441» — хотя имя лежит и в CRM, и
в спарсенной аудитории, по тому же tg_user_id.

Отдельно проверяется, что внешний текст (имя, причина недоставки из ошибки
Telegram) не уезжает в атрибут onclick: там он не текст, а код.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()


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
    m = re.search(r"\n    async def " + re.escape(name) + r"\(request", API)
    assert m, f"хендлер {name} не найден"
    nxt = API.find("\n    async def ", m.end())
    return API[m.start():nxt if nxt > 0 else len(API)]


def test_detector_sees_the_screen():
    """Самопроверка измерителя: экран и его загрузчик на месте."""
    assert '<div class="screen" id="s-dmreport">' in HTML
    assert '<div class="screen" id="s-dmrlog">' in HTML
    assert len(_fn("openDmReport")) > 500


def test_recent_sends_show_a_name_not_a_bare_id():
    """«id 783215441» владельцу ничего не говорит."""
    who = _fn("dmrWho")
    assert "r.name" in who and "r.username" in who, who
    assert "Пользователь" in who, who
    rep = _fn("openDmReport")
    assert "id ${esc(String(r.tg_user_id))}" not in rep, (
        "в «Последних отправках» снова голый id"
    )


def test_backend_resolves_the_name_for_recent():
    """Имя берётся из CRM и спарсенной аудитории по tg_user_id владельца."""
    h = _handler("dm_campaign_report")
    assert "crm_contacts" in h, "имя из CRM не ищется"
    assert "parsed_audiences" in h, "имя из спарсенной аудитории не ищется"
    # Скоуп по владельцу обязателен: иначе имя подтянется из чужой базы.
    assert h.count("owner_id=$2") >= 2, h[-900:]
    assert "AS name" in h and "AS username" in h


def test_recent_shows_when_it_happened():
    """Без времени строка «не дошло» не отличает вчера от минуты назад."""
    assert "ago(r.sent_at)" in _fn("dmrRow")


def test_summary_rows_lead_to_the_people_behind_the_number():
    rep = _fn("openDmReport")
    assert "dmrGoStatus(" in rep, "строка исхода никуда не ведёт"
    assert "dmrGoReason(" in rep, "причина недоставки никуда не ведёт"
    assert "openAccount(" in rep, "аккаунт с ошибками никуда не ведёт"
    for name in ("dmrGoStatus", "dmrGoReason", "openDmLog", "openAccount"):
        _fn(name)  # все цели существуют


def test_external_text_never_goes_into_onclick():
    """Имя и причина — внешний текст; в атрибуте onclick это код."""
    rep = _fn("openDmReport")
    for bad in ("onclick=\"openDmLog('${", "${esc(e.reason)})", "JSON.stringify(String(e.reason)"):
        assert bad not in rep, f"внешний текст в onclick: {bad}"
    # Через onclick едет только индекс.
    assert re.search(r'onclick="dmrGoReason\(\$\{i\}\)"', rep), rep


def test_log_route_is_registered_and_scoped_to_owner():
    assert '"/api/miniapp/dm_campaign/{campaign_id}/log", dm_campaign_log_list' in API
    h = _handler("dm_campaign_log_list")
    assert "FROM dm_campaigns WHERE id=$1 AND owner_id=$2" in h, "IDOR: чужая кампания"
    assert 'return _err("Не найдено", 404)' in h


def test_log_filters_are_parameters_not_string_glue():
    """Фильтр из запроса не склеивается в SQL."""
    h = _handler("dm_campaign_log_list")
    assert "l.status=$" in h and "IS NULL OR" in h, h
    assert "l.status='" not in h, "значение фильтра склеено в текст запроса"
    assert "error_msg='" not in h, "причина склеена в текст запроса"


def test_empty_reason_filter_matches_the_label_shown():
    """Сводка подписывает пустую причину «без описания» — фильтр ловит её же."""
    h = _handler("dm_campaign_log_list")
    assert "'без описания'" in h, h
    rep = _handler("dm_campaign_report")
    assert "'без описания'" in rep, "подпись в сводке изменилась — фильтр разойдётся"


def test_recipient_list_can_be_taken_out_of_the_screen():
    """Список мёртвых контактов нужен в работе, а не для чтения."""
    assert "dmrCopyIds(" in _fn("openDmLog")
    assert "copyToClipboard" in _fn("dmrCopyIds")
    assert "moreBtn(" in _fn("openDmLog"), "список молча обрывается на сотне"


def test_report_says_how_much_is_left():
    """«Кампания прошла целиком или встала на трети» — по экрану было не понять."""
    rep = _fn("openDmReport")
    assert "total_targets" in rep and "Осталось" in rep, rep[:400]


def test_telegram_reasons_are_translated():
    """USER_PRIVACY_RESTRICTED владелец не читает — а это главная причина."""
    assert "const DM_ERR_RU" in HTML
    body = HTML[HTML.index("const DM_ERR_RU"):HTML.index("function dmErrRu")]
    for key in ("USER_PRIVACY_RESTRICTED", "PEER_FLOOD", "USER_IS_BLOCKED",
                "USER_DEACTIVATED", "AUTH_KEY_UNREGISTERED", "FLOOD_WAIT"):
        assert key + ":" in body, f"не переведена причина {key}"
    # Перевод не выдумывается: неизвестная причина остаётся как есть.
    f = _fn("dmErrRu")
    assert "|| s" in f, f
    rep = _fn("openDmReport")
    assert "dmErrRu(e.reason)" in rep, "сводка причин всё ещё на английском"
    assert "dmErrRu(err)" in _fn("dmrRow"), "строка получателя всё ещё на английском"


def test_raw_reason_is_kept_for_support():
    """Переведя причину, нельзя потерять исходную строку Telegram."""
    rep = _fn("openDmReport")
    assert "esc(e.reason)" in rep, "исходная строка Telegram пропала с экрана"


def test_filter_by_reason_sends_the_raw_string_not_the_translation():
    """Фильтр ищет в базе по тому, что там записано, — по английской строке."""
    f = _fn("dmrGoReason")
    assert "String(e.reason)" in f and "dmErrRu" not in f, f


def test_undelivered_card_filters_undelivered():
    """Карточка «Не дошло» открывала всех подряд, включая доставленных."""
    rep = _fn("openDmReport")
    assert "failed:true" in rep, rep[:600]
    h = _handler("dm_campaign_log_list")
    assert "l.status<>'sent'" in h, "фильтр недоставленных не доехал до запроса"
    assert 'request.query.get("failed")' in h
