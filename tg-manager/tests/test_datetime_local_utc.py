"""Регресс+гейт: время из <input datetime-local> уходит на бэкенд в UTC.

Класс 10 (таймзона). Цепочка обязана быть:
    datetime-local (ЛОКАЛЬНОЕ) → localToUtcIso()/new Date().toISOString() (UTC)
    → бэкенд TIMESTAMPTZ → показ обратно через toLocalInput().

Что было сломано (сознательно отложенный хвост, теперь добит):
  * `bs-datetime` — предзаполнение делалось `now.toISOString().slice(0,16)`, т.е.
    UTC клали в поле, которое ПОКАЗЫВАЕТ локальное → дефолт «через час» съезжал
    на величину смещения (для UTC+3 — на 3 часа в ПРОШЛОЕ).
  * `submitBcSchedule` слал сырую локальную строку в `scheduled_at`, а
    `operation_queue.scheduled_for` — TIMESTAMPTZ, сравниваемый с `now()` →
    рассылка уходила со сдвигом на часовой пояс пользователя.
  * `saveCrmReminder` слал сырую локальную строку, а `contact_crm.next_reminder_at`
    — TIMESTAMPTZ, сравниваемый с `NOW()` для «предстоящие/просроченные».
"""
from __future__ import annotations

import re
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "mini_app" / "index.html"


def _html() -> str:
    return INDEX.read_text(encoding="utf-8")


def _fn(name: str) -> str:
    html = _html()
    m = re.search(r"(?:async\s+)?function " + re.escape(name) + r"\s*\([^)]*\)\s*\{", html)
    assert m, f"{name} не найдена"
    i, depth = m.end() - 1, 0
    while i < len(html):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[m.start(): i + 1]
        i += 1
    raise AssertionError(f"тело {name} не закрыто")


def test_broadcast_schedule_sends_utc():
    src = _fn("submitBcSchedule")
    assert "localToUtcIso(datetime)" in src, (
        "scheduled_at обязан уходить в UTC: scheduled_for — TIMESTAMPTZ vs now()"
    )
    assert not re.search(r"scheduled_at\s*:\s*datetime\b", src), (
        "сырая локальная строка сдвинет рассылку на часовой пояс пользователя"
    )


def test_broadcast_schedule_prefill_is_local():
    # предзаполнение поля — в ЛОКАЛЬНОМ времени, иначе дефолт съезжает
    # Проверяем сами присваивания в поле, а не срез фиксированной длины вокруг
    # его id: окно сдвигается вместе с кодом, и отрицательная проверка ниже
    # («UTC сюда не кладём») молча выключилась бы, ничего не сообщив.
    html = _html()
    assigns = re.findall(
        r"document\.getElementById\('bs-datetime'\)\.value\s*=\s*([^;\n]+)", html)
    assert assigns, "значение поля bs-datetime нигде не задаётся"
    for expr in assigns:
        assert "toLocalInput(" in expr, (
            f"дефолт datetime-local должен ставиться toLocalInput, а не {expr.strip()!r}")
        assert "toISOString" not in expr, (
            "toISOString() кладёт UTC в поле, показывающее локальное время: "
            f"{expr.strip()!r}")


def test_crm_reminder_sends_utc():
    src = _fn("saveCrmReminder")
    assert "localToUtcIso(remindAt)" in src, (
        "next_reminder_at — TIMESTAMPTZ vs NOW(): время обязано уходить в UTC"
    )
    assert not re.search(r"remind_at\s*:\s*remindAt\b", src), "сырая локальная строка"


def test_no_datetime_local_field_sent_raw():
    """Гейт на весь класс: значение каждого datetime-local поля перед отправкой
    проходит конверсию в UTC (localToUtcIso или new Date(...).toISOString())."""
    html = _html()
    ids = set(re.findall(r'type="datetime-local"[^>]*id="([\w-]+)"', html))
    ids |= set(re.findall(r'id="([\w-]+)"[^>]*type="datetime-local"', html))
    assert ids, "поля datetime-local не найдены — гейт деградировал"

    offenders = []
    for fid in sorted(ids):
        for m in re.finditer(r"getElementById\(\s*['\"]" + re.escape(fid) + r"['\"]\s*\)\s*\??\.value", html):
            window = html[m.start(): m.start() + 700]
            # присваивание значения полю (предзаполнение) — не отправка
            if re.match(r".*\.value\s*=", window[:120], re.DOTALL):
                continue
            if "localToUtcIso" in window or "toISOString()" in window:
                continue
            # переменная может конвертироваться ниже — ищем её имя
            var = re.search(r"(?:const|let|var)\s+(\w+)\s*=\s*document\.getElementById", html[max(0, m.start() - 80): m.end()])
            if var and re.search(r"localToUtcIso\(\s*" + re.escape(var.group(1)) + r"\s*\)|new Date\(\s*" + re.escape(var.group(1)), html[m.start(): m.start() + 2500]):
                continue
            offenders.append(fid)
    assert not offenders, (
        "значение datetime-local уходит без конверсии в UTC (сдвиг на часовой "
        f"пояс пользователя): {sorted(set(offenders))}"
    )
