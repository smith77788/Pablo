"""Ошибка в форме не должна выключать пользователю ВСЕ операции на 30 минут.

ЖАЛОБА ПОЛЬЗОВАТЕЛЯ: «всё ещё не инвайтит».

ЧТО БЫЛО СЛОМАНО. `op_worker` держит предохранитель: после
`_CIRCUIT_BREAKER_THRESHOLD` (3) неудачных операций подряд цепь открывается и
ВСЕ операции владельца на `_CIRCUIT_BREAKER_COOLDOWN` (30 минут) начинают
молча откладываться — задача выходит, операция возвращается в `pending` со
`scheduled_for = now + остаток`.

Беда в том, ЧТО считалось неудачей. Исполнители возвращают `status='failed'` и на
чисто конфигурационных отказах: «⚠️ Аудитория пуста», «⚠️ Не указана группа»,
«⚠️ Нет активных аккаунтов». Таких возвратов в `op_worker` десятки. То есть три
подряд неверно заполненные формы — например запуск инвайта, когда парсер ещё не
собирал аудиторию, — открывали цепь, и дальше не запускалось НИЧЕГО. Снаружи это
ровно «всё ещё не инвайтит»: операция уходит в «ожидает» и не стартует, без
единого объяснения.

Признак конфигурационного отказа выбран так, чтобы не размечать вручную десятки
мест: исполнитель не тронул НИ ОДНОЙ цели (ok=0 и failed=0) и не бросил
исключение — значит он отказал ДО работы, а не сломался в ней. Исключения
учитываются отдельной веткой и предохранителя не теряют.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _record_site() -> str:
    m = re.search(r"# Circuit breaker: считаем.*?_circuit_breaker_record\(owner_id, _final_status == \"done\"\)",
                  WORKER, re.DOTALL)
    assert m, "точка учёта результата в предохранителе не найдена"
    return m.group(0)


def test_config_refusal_is_recognised():
    site = _record_site()
    assert "_config_refusal" in site, "конфигурационный отказ должен отличаться от сбоя"
    assert "_ok == 0 and _failed == 0" in site, (
        "признак — исполнитель не тронул ни одной цели, то есть отказал ДО работы"
    )


def test_config_refusal_does_not_open_the_circuit():
    site = _record_site()
    body = site.split("_config_refusal")[-1]
    assert "else:" in body and "_circuit_breaker_record" in body, (
        "учёт в предохранителе обязан быть в ветке ИНАЧЕ — при отказе до работы "
        "его трогать нельзя"
    )
    idx_if = site.index("if _config_refusal")
    idx_rec = site.index("_circuit_breaker_record(owner_id")
    assert idx_rec > idx_if, "запись в предохранитель должна идти после проверки"


def test_real_execution_failure_still_counts():
    """Операция, которая ПЫТАЛАСЬ работать и провалилась, обязана считаться —
    иначе предохранитель перестанет защищать от настоящих сбоев."""
    site = _record_site()
    assert '_final_status == "failed" and _ok == 0 and _failed == 0' in site, (
        "провал с попытками (failed > 0) не должен попадать под исключение"
    )


def test_exception_path_untouched():
    """Исключение исполнителя — безусловный сбой, отдельной веткой."""
    m = re.search(r"except Exception.*?await _circuit_breaker_record\(owner_id, False\)",
                  WORKER, re.DOTALL)
    assert m, "путь исключения обязан по-прежнему открывать цепь"


def test_refusal_is_logged_for_diagnosis():
    site = _record_site()
    assert "log.info" in site and "отказ до начала работы" in site, (
        "решение не трогать предохранитель должно быть видно в логах"
    )


def test_breaker_thresholds_are_what_the_docstring_claims():
    """Если пороги поменяют, объяснение выше устареет молча."""
    from services import op_worker
    assert op_worker._CIRCUIT_BREAKER_THRESHOLD == 3
    assert op_worker._CIRCUIT_BREAKER_COOLDOWN == 1800


def test_many_executors_return_config_failures():
    """Предпосылка фикса: таких возвратов действительно десятки, поэтому
    размечать их по одному — не вариант."""
    n = len(re.findall(r'return \{"status": "failed", "summary": "⚠️', WORKER))
    assert n >= 20, f"ожидались десятки конфигурационных отказов, найдено {n}"
