"""Срабатывания механизмов надёжности обязаны быть видны снаружи, а не в логах.

Очередь обзавелась тремя способами прекратить операцию помимо обычного провала:
потолок прогона (операция прервана по времени), бюджет живучести (операция
остановлена как ядовитая после N падений воркера) и отсрочка по длинной
флуд-паузе. Все три писали ТОЛЬКО в лог.

Снаружи это выглядит одинаково: операция «не доехала». Понять, прервана она по
времени, отложена платформой или остановлена как ядовитая, можно было лишь
чтением логов постфактум — то есть узнать о деградации получалось из жалобы
владельца, а не с графика. Ровно та причина, по которой в воркере уже есть
`infragram_operations_total` и `infragram_operation_seconds`.

Метрика не должна ронять работу: счётчик — вспомогательная вещь, его сбой не
повод терять операцию.
"""
from __future__ import annotations

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Счётчики, которые обязаны существовать и быть описаны в реестре метрик.
_EXPECTED = (
    "infragram_op_timeouts_total",
    "infragram_op_revives_total",
    "infragram_op_poisoned_total",
    "infragram_op_flood_defers_total",
    "infragram_flood_sleep_truncated_total",
)


# Модули, которые пишут счётчики надёжности своим `_reliability_metric`.
_EMITTERS = ("services/op_worker.py", "services/dm_engine.py",
             "services/broadcaster.py")


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _fn(src: str, name: str) -> str:
    start = src.index(f"def {name}")
    m = re.search(r"\n(?:async )?def ", src[start + 10:])
    return src[start:start + 10 + m.start()] if m else src[start:]


def _emitted_counters() -> set[str]:
    """Имена, которые исполнитель РЕАЛЬНО пишет через _reliability_metric.

    Список в `_EXPECTED` рукописный, и потому неполный: `infragram_op_stalls_total`
    писался в код и не был описан в реестре — на графике это строка без подписи,
    и смысл её знал бы только тот, кто полез в исходник. Поэтому проверяем не
    список, а код.
    """
    # Файлов больше одного: защитные записи есть и вне исполнителя (движок ЛС
    # метит цель ДО отправки своим `_reliability_metric`), а привязка разбора к
    # одному файлу делала проверку описаний ложно-зелёной для всех остальных.
    src = "".join(_read(rel) for rel in _EMITTERS)
    return set(re.findall(r'_reliability_metric\(\s*"([a-z0-9_]+)"', src))


# ── Счётчики, которые пишутся ВНЕ трёх выбранных файлов ─────────────────────
#
# Разбор выше читает три файла из `_EMITTERS`. Это тот самый слепой отбор, на
# котором уже горели другие переписи: счётчик, написанный в четвёртом файле, не
# проверяется ни на описание, ни на попадание в набор тревог владельцу — и
# молчит об этом. Нашлось на живом примере: детектор «очередь не разбирается»
# (`operation_bus.queue_frozen`) пишет свой счётчик ослепшей защиты прямым
# `metrics.inc`, и ни один из трёх файлов его не видит.
#
# Поэтому ниже — разбор по всему рабочему дереву, и он намеренно проверяет
# только ОДНО направление: всё написанное обязано быть описано. Обратное
# («всё описанное где-то пишется») проверять нельзя: имена счётчиков уходят в
# общие двери через переменные (`_retrying_execute(..., metric="...")`), и
# перепись по литералам объявила бы живые счётчики мёртвыми.

_RUNTIME_DIRS = ("bot", "services", "database", "codex_bridge")
_EMIT_FORMS = (
    r'\b(?:_m|metrics|_metrics)\.inc\(\s*"(infragram_[a-z0-9_]+)"',
    r'_reliability_metric\(\s*"(infragram_[a-z0-9_]+)"',
    r'metric\s*=\s*"(infragram_[a-z0-9_]+)"',
)


def _counters_everywhere() -> set[str]:
    out: set[str] = set()
    for d in _RUNTIME_DIRS:
        base = os.path.join(ROOT, d)
        for dirpath, _dirs, files in os.walk(base):
            if "__pycache__" in dirpath:
                continue
            for f in files:
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(dirpath, f), encoding="utf-8",
                          errors="ignore") as fh:
                    src = fh.read()
                for form in _EMIT_FORMS:
                    out |= set(re.findall(form, src))
    return out


def _alarm_list() -> dict[str, str]:
    """`op_worker._PROTECTIVE_FAILURE_COUNTERS` без импорта модуля.

    Импортировать `op_worker` здесь нельзя — он тянет конфиг с обязательными
    переменными среды, и проверка стала бы зависеть от окружения. Поэтому
    читаем сам литерал разбором: границы берёт ast, а не окно по символам.
    """
    tree = ast.parse(_read("services/op_worker.py"))
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        if not any(isinstance(t, ast.Name)
                   and t.id == "_PROTECTIVE_FAILURE_COUNTERS" for t in targets):
            continue
        assert isinstance(node.value, ast.Dict)
        out = {}
        for k, v in zip(node.value.keys, node.value.values):
            if isinstance(k, ast.Constant):
                out[k.value] = ast.literal_eval(v) if isinstance(
                    v, (ast.Constant, ast.BinOp)) else ""
        return out
    raise AssertionError("набор тревог владельцу не найден в op_worker")


def test_the_tree_wide_census_sees_counters_outside_the_three_files():
    """Самопроверка: разбор обязан находить то, чего не видят `_EMITTERS`."""
    everywhere = _counters_everywhere()
    assert len(everywhere) > len(_emitted_counters()), (
        "разбор по дереву не нашёл больше, чем разбор по трём файлам — "
        "значит он сломан или отбор файлов перестал быть узким")
    assert "infragram_queue_frozen_check_failures_total" in everywhere, (
        "счётчик ослепшего детектора остановки написан вне трёх файлов и "
        "разбором не найден")


def test_every_counter_anywhere_is_documented():
    """Описание нужно КАЖДОМУ счётчику, а не тем, что в выбранных файлах."""
    from services.metrics import _HELP

    undocumented = sorted(n for n in _counters_everywhere() if n not in _HELP)
    assert not undocumented, (
        f"счётчики без описания в реестре метрик: {undocumented}. "
        "На графике это строка, смысл которой знает только тот, кто полез в "
        "исходник")


def test_every_failure_counter_reaches_the_owner():
    """`*_failures_total` — это «защита перестала работать», и владелец узнаёт.

    Счётчики живут в памяти процесса, логи владелец не читает; единственный
    способ, которым до него доходит «защита молча отключилась», — сторож,
    который перебирает `_PROTECTIVE_FAILURE_COUNTERS`. Счётчик, не попавший в
    этот набор, растёт в тишине.
    """
    alarm = _alarm_list()
    orphans = sorted(n for n in _counters_everywhere()
                     if n.endswith("_failures_total") and n not in alarm)
    assert not orphans, (
        f"счётчики сбоя защиты вне набора тревог владельцу: {orphans}")
    for name, text in alarm.items():
        assert re.search(r"[а-яА-Я]", text or ""), (
            f"{name}: текст тревоги читает владелец, он должен быть на русском")


def test_every_counter_is_documented_in_the_registry():
    """Метрика без описания — строка, которую некому прочитать на графике."""
    from services.metrics import _HELP

    for name in sorted(set(_EXPECTED) | _emitted_counters()):
        assert name in _HELP, f"{name} не описан в реестре метрик"
        assert re.search(r"[а-яА-Я]", _HELP[name]), (
            f"{name}: описание читает владелец, оно должно быть на русском"
        )


def test_the_detector_sees_the_counters_at_all():
    """Пустой разбор сделал бы проверку выше вечно зелёной."""
    found = _emitted_counters()
    assert len(found) >= 5, f"найдено счётчиков: {sorted(found)} — разбор сломан"
    assert "infragram_op_revives_total" in found


def test_every_counter_is_actually_emitted():
    ow = _read("services/op_worker.py")
    for name in _EXPECTED:
        assert f'"{name}"' in ow, f"{name} объявлен, но нигде не пишется"


def test_metric_helper_never_breaks_the_operation():
    """Сбой счётчика не повод терять операцию — он вспомогателен."""
    code = _fn(_read("services/op_worker.py"), "_reliability_metric")
    assert "try:" in code and "except Exception:" in code
    assert "pass" in code


def test_counters_count_operations_not_watchdog_ticks():
    """Один тик сторожа поднимает несколько операций.

    Инкремент на единицу показал бы «1» там, где операций было десять, и
    масштаб проблемы на графике бы потерялся.
    """
    ow = _read("services/op_worker.py")
    for call, counter in (
        ("infragram_op_revives_total", "count"),
        ("infragram_op_poisoned_total", "poisoned_n"),
    ):
        for m in re.finditer(rf'_reliability_metric\("{call}",\s*([^,)]+)', ow):
            assert m.group(1).strip() == counter, (
                f"{call} обязан инкрементироваться числом операций ({counter}), "
                f"а не {m.group(1).strip()}"
            )


def test_timeout_metric_is_labelled_by_op_type():
    """Без типа операции счётчик не отвечает на главный вопрос: ЧТО зависает."""
    ow = _read("services/op_worker.py")
    m = re.search(r'_reliability_metric\(\s*"infragram_op_timeouts_total",\s*op_type=op_type\s*\)', ow)
    assert m, "таймаут обязан быть размечен типом операции"


def test_revive_and_poison_metrics_say_where_they_fired():
    """Сброс на старте и сторож зависших — разные симптомы, путать их нельзя."""
    ow = _read("services/op_worker.py")
    for source in ("startup", "watchdog"):
        assert f'source="{source}"' in ow, f"нет разметки source={source}"


def test_stuck_alert_text_matches_actual_behaviour():
    """Алерт обещал безусловный авто-сброс — после бюджета живучести это неправда.

    Владелец читает этот текст и принимает по нему решение: ждать или вмешаться.
    Устаревшее обещание здесь хуже отсутствия текста.
    """
    body = _fn(_read("services/op_worker.py"), "_watchdog_alerts")
    # Ищем именно ту строку, которая уходит владельцу, по её смыслу, а не по
    # дословному началу: формулировку переписывали (бот больше не говорит
    # владельцу «pending»), и привязка к литералу делала проверку ложно-зелёной.
    # Границы — один вызов `.append(...)`, то есть одно сообщение.
    tail = ""
    for call in ast.walk(ast.parse(body.strip())):
        if not isinstance(call, ast.Call):
            continue
        if not (isinstance(call.func, ast.Attribute) and call.func.attr == "append"):
            continue
        seg = ast.dump(call)
        if "помечается ошибкой" in seg:
            tail = seg
            break
    assert tail, (
        "в тексте алерта больше нет обещания про предел воскрешений: владелец "
        "читает его и решает, ждать или вмешаться")
    assert "_MAX_REVIVES" in tail, (
        "текст обязан называть предел воскрешений, а не обещать вечный авто-сброс"
    )


def test_metrics_module_accepts_the_value_argument():
    """Поведение проверяем напрямую: подпись inc() могла разойтись с вызовом."""
    from services import metrics

    metrics.reset()
    metrics.inc("infragram_op_revives_total", {"source": "startup"}, 7)
    snap = metrics.snapshot()
    assert any(
        "infragram_op_revives_total" in str(k) and v == 7
        for k, v in snap.get("counters", snap).items()
    ), snap
    metrics.reset()
