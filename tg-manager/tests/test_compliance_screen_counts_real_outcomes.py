"""Комплаенс показывал нули при полном журнале — и ни одна строка не открывалась.

Экран «✅ Комплаенс» (и такой же отчёт в боте) считал исходы `success`, `ban` и
`flood_wait`. В `compliance_audit` их **не пишет никто**: исполнитель операций
кладёт туда статус операции (`done` / `partial` / `failed` / `cancelled`),
проверка контента — `blocked`, запуск жалоб — `abuse_report`. Поэтому
«Успешных», «Рисковых» и «Успешность (30 дн.)» были нулями всегда, при любом
журнале, а подписанный отчёт для аудита выдавал «Успешных: 0 (0.0%)».

Отдельно: в таблице у каждой записи есть `op_id`, но запрос экрана его даже не
выбирал. Запись «Массовый инвайт · failed · 2 часа назад» некуда было открыть,
хотя операция, о которой она говорит, лежит рядом в очереди.

Поэтому словарь исходов здесь — ОДИН, в движке, и проверяется вычислением:
берём значения, которые продукт реально пишет (константы `op_status` плюс
литералы из мест вызова `record`), и требуем, чтобы каждое было отнесено к
группе и имело русскую подпись. Новый исход без подписи — красный тест, а не
английское слово на экране у владельца, который английского не знает.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _written_outcomes() -> set[str]:
    """Исходы, которые продукт кладёт в аудит.

    Статусы операции приходят переменной (`status`), поэтому берём их из
    `op_status` целиком; остальные — литералами из мест вызова `record`.
    """
    from services import op_status

    out = {op_status.DONE, op_status.PARTIAL, op_status.FAILED,
           op_status.CANCELLED}
    src = "\n".join(_read(os.path.join("services", f))
                    for f in os.listdir(os.path.join(ROOT, "services"))
                    if f.endswith(".py"))
    for m in re.finditer(r"compliance_engine\.record\((.{0,400}?)\)\n", src, re.S):
        call = m.group(1)
        kw = re.search(r'outcome\s*=\s*["\']([\w:]+)["\']', call)
        if kw:
            out.add(kw.group(1))
            continue
        # позиционный пятый аргумент: pool, user, account, op_type, outcome
        pos = re.match(r"\s*[^,]+,\s*[^,]+,\s*[^,]+,\s*[^,]+,\s*[\"']([\w:]+)[\"']",
                       call.replace("\n", " "))
        if pos:
            out.add(pos.group(1))
    return out


def test_probe_sees_the_outcomes_the_product_writes():
    """Пустой список сделал бы проверку ниже вечно зелёной."""
    written = _written_outcomes()
    assert "done" in written and "failed" in written, written
    assert "blocked" in written, "исход проверки контента перестал находиться"
    assert "abuse_report" in written, "правовое основание жалобы не находится"


def test_every_written_outcome_is_classified_and_named_in_russian():
    from services import compliance_engine as ce

    for outcome in sorted(_written_outcomes()):
        group = ce.classify_outcome(outcome)
        assert group != "unknown", (
            f"исход {outcome!r} продукт пишет, а словарь движка его не знает — "
            f"на экране он попадёт в «ничьи» и ни в один счётчик")
        ru = ce.outcome_ru(outcome)
        assert ru and not re.search(r"[A-Za-z]", ru), (
            f"исход {outcome!r} показывается владельцу как {ru!r} — "
            f"владелец не читает по-английски")


def test_success_rate_counts_what_the_worker_actually_writes():
    """Главная поломка: восемь выполненных операций давали ноль успешных."""
    from services import compliance_engine as ce

    s = ce.summarize({"done": 8, "failed": 2})
    assert s["ok"] == 8, s
    assert s["risk"] == 2, s
    assert s["success_rate"] == 80.0, s


def test_legal_basis_record_does_not_spoil_the_rate():
    """`abuse_report` — не исход операции, а запись основания: он нейтрален."""
    from services import compliance_engine as ce

    s = ce.summarize({"done": 1, "abuse_report": 9})
    assert s["neutral"] == 9, s
    assert s["success_rate"] == 100.0, s


def test_partial_is_neither_success_nor_silence():
    from services import compliance_engine as ce

    s = ce.summarize({"done": 1, "partial": 1})
    assert s["partial"] == 1 and s["ok"] == 1
    assert s["success_rate"] == 50.0, "частичная операция не полный успех"


def test_overview_asks_the_engine_instead_of_inventing_a_vocabulary():
    api = _read("services/mini_app_api.py")
    i = api.index("async def compliance_overview")
    # Границы — по следующему хендлеру: в окне фиксированной длины «искомого
    # нет» однажды станет правдой просто потому, что код сдвинулся.
    window = api[i:api.index("async def compliance_export", i)]
    assert "outcome='success'" not in window, (
        "обзор снова считает исход, которого никто не пишет")
    assert "'ban','flood_wait'" not in window.replace(" ", ""), (
        "обзор снова считает риск по словарю, которого нет в журнале")
    assert "compliance_engine" in window, "обзор обязан брать словарь из движка"


def test_records_carry_the_way_to_the_operation():
    eng = _read("services/compliance_engine.py")
    fn = eng[eng.index("async def get_recent"):]
    fn = fn[:fn.index("\nasync def ", 10)] if "\nasync def " in fn[10:] else fn
    assert "op_id" in fn, "запись без op_id открыть некуда"
    assert "operation_queue" in fn, (
        "экран обязан знать, жива ли ещё операция: иначе тап ведёт в ошибку")


def test_screen_opens_the_operation_and_lets_filter_by_outcome():
    html = _read("mini_app/index.html")
    i = html.index("async function openCompliance")
    # Границы по соседней функции, а не по длине: срез фиксированной длины рвёт
    # экран пополам и начинает лгать при первой же правке.
    body = html[i:html.index("async function exportCompliance", i)]
    assert "openOpDetail(" in body, "строка аудита никуда не ведёт"
    assert "complianceFilter(" in body or "compGo(" in body, (
        "по исходу нельзя отфильтровать — «Рисковых 12» снова ни во что не ведёт")
    assert "moreBtn(" in body, "видно только первые записи, остальные не достать"


def test_front_knows_a_russian_name_for_every_outcome_the_engine_classifies():
    from services import compliance_engine as ce

    html = _read("mini_app/index.html")
    i = html.index("function outRu(")
    table = html[i:i + 700]
    for outcome in sorted(ce.OUTCOME_RU):
        assert outcome in table, (
            f"{outcome} движок уже знает, а экран покажет его по-английски")


def test_export_report_is_russian():
    eng = _read("services/compliance_engine.py")
    fn = eng[eng.index("async def export_text"):]
    assert "COMPLIANCE REPORT" not in fn, (
        "шапка выгружаемого отчёта по-английски, а владелец его читает")
