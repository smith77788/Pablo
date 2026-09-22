"""Название операции владелец читает — значит оно по-русски.

Владелец не понимает английский; это записано первой строкой CLAUDE.md. При
этом подписи операций уходили в очередь по-английски и всплывали ровно там, где
он на них смотрит: в списке операций, в панели «⏳ Выполняется», в баннере
завершения и в шапке деталей. Он видел «Mass Invite → @chat», «Growth Agent: …»,
«Ad Intel scan @channel», «Network Broadcast: …», «Clone: @a → @b»,
«Self-promo: …», «Strike: …» — то есть название своей же основной операции не
мог прочитать.

Отдельно «Quick Post в {N} каналов»: форма «каналов» не согласуется с числами
вроде 1 и 2, а ставить ради подписи склонятор незачем — «каналов: N» верно при
любом N.

ЭТОТ ТЕСТ не даёт классу вернуться: подпись операции, в которой есть латиница и
нет ни одной кириллической буквы, — это подпись, которую владелец не прочитает.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Имена собственные и НАЗВАНИЯ ФУНКЦИЙ продукта. Названия переводить нельзя
#: поодиночке: «Growth Agent», «Self Promo» и «Strike» стоят так же в меню бота,
#: на плитках и в заголовках экранов, и перевод одной только подписи операции
#: дал бы одной функции два имени — хуже, чем одно английское. Переименование
#: функции целиком — решение владельца, а не побочный эффект правки подписи.
ALLOWED_PREFIX = ("Growth Agent", "Self Promo", "Strike")
ALLOWED = {
    "Google / Gmail",
    "Microsoft / Outlook",
}

# Ловим только форму `label=…` — ту, которой подпись передают в очередь.
# Ключ `"label": …` в проверку НЕ берём намеренно: так называют поля в десятках
# внутренних словарей («security», «ncmec», «username», коды регионов), и гейт
# на них выдал бы список, в котором настоящая находка тонет. Детектор, который
# шумит, выключают — а этот должен остаться рабочим.
_LABEL = re.compile(r"""(?<![,\w])label\s*=\s*(f?"[^"\n]{3,200}"|f?'[^'\n]{3,200}')""")


def _text_of(literal: str) -> str:
    """Литерал без кавычек, префикса f и подстановок {...}."""
    body = literal.lstrip("f")[1:-1]
    return re.sub(r"\{[^{}]*\}", "", body)


def offenders() -> list[str]:
    bad: list[str] = []
    for path in sorted((ROOT / "services").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        for m in _LABEL.finditer(src):
            literal = m.group(1)
            # `op_type, params, label = "key", {...}, "Подпись"` — слева кортеж,
            # и сразу за `=` стоит НЕ подпись, а первый элемент. Пропускаем
            # только ЭТУ форму, а не любую строку с запятой: иначе из проверки
            # выпадёт `submit(..., label=f"...")`, то есть почти все подписи.
            line = src[src.rfind("\n", 0, m.start()) + 1: m.start()]
            if re.match(r"^\s*\w+\s*(,\s*\w+\s*)+,\s*$", line):
                continue
            text = _text_of(literal)
            if text.strip() in ALLOWED:
                continue
            if text.lstrip().startswith(ALLOWED_PREFIX):
                continue
            letters = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", text)
            if len(letters) < 3:
                continue
            if re.search(r"[А-Яа-яЁё]", letters):
                continue
            ln = src[:m.start()].count("\n") + 1
            bad.append(f"services/{path.name}:{ln} → {literal[:70]}")
    return bad


def test_no_english_operation_labels():
    bad = offenders()
    assert not bad, (
        "подпись операции без единой русской буквы — владелец её не прочитает:\n  "
        + "\n  ".join(bad)
        + "\n\nПодписи видны в списке операций, в панели «Выполняется» и в "
          "баннере завершения. Имя собственное — в ALLOWED, с обоснованием."
    )


def test_quick_post_label_agrees_with_any_number():
    src = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    assert 'f"Quick Post в {len(channel_ids)} каналов"' not in src
    assert "каналов: {len(channel_ids)}" in src, (
        "подпись снова склоняет «каналов» по числу, которое заранее неизвестно")
