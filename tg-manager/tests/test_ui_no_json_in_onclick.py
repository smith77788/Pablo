"""Храповик: данные не подставляются в атрибут onclick.

`onclick='fn(${JSON.stringify(x)})'` выглядит безопасно, но безопасен не бывает
никогда: `JSON.stringify` экранирует двойные кавычки и НЕ трогает апостроф, а
свои двойные добавляет всегда. Дальше всё решает, в какие кавычки обёрнут сам
атрибут — и обе развилки ломаются:

* атрибут в ДВОЙНЫХ → его закрывают собственные кавычки JSON, и разметка рвётся
  на КАЖДОЙ строке, при любых данных;
* атрибут в ОДИНАРНЫХ → его закрывает первый же апостроф в данных: «don't»,
  «О'Брайен», сохранённый вызов `openAccountChat(5,'123')`.

Так были сломаны (проверено подстановкой в node, а не рассуждением):

* список сообществ — `openCommunityNode(${n.id}, ${JSON.stringify(esc(title))},
  ...)` в двойных кавычках: НИ ОДНА строка не открывалась, всегда;
* кнопка «Скопировать текст» в наборе жалоб — апостроф в шаблоне убивал кнопку;
* чипы частых действий — сохранённая строка вызова почти всегда со своими
  апострофами;
* две кнопки подсказок Пульса — действие там объект `{kind, param}`, и `param`
  бывает строкой.

Приём замены везде один и тот же, он уже принят в рассылках и воронках: в
атрибут уходит id или индекс, значение берётся из кэша.
"""
from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_FILES = [_ROOT / "mini_app" / "index.html"] + sorted(
    (_ROOT / "mini_app" / "screens").glob("*.js"))


def _sources() -> list[tuple[str, str]]:
    return [(p.name, p.read_text(encoding="utf-8")) for p in _FILES]


# Атрибут-обработчик, в теле которого зовут JSON.stringify: onclick, onchange,
# oninput и прочие on*. Ищем именно внутри значения атрибута, а не где угодно.
_ATTR_JSON = re.compile(
    r"""on[a-z]+\s*=\s*(['"])(?:(?!\1).)*?JSON\.stringify""",
    re.IGNORECASE | re.DOTALL)


def test_no_data_is_serialised_into_an_event_attribute():
    hits: list[str] = []
    for name, src in _sources():
        for m in _ATTR_JSON.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            hits.append(f"{name}:{line}  {' '.join(m.group(0).split())[:90]}")
    assert not hits, (
        "данные подставлены в атрибут-обработчик через JSON.stringify — "
        "апостроф в данных (или собственные кавычки JSON) порвут разметку. "
        "Передавайте id/индекс, значение берите из кэша:\n  " + "\n  ".join(hits)
    )


# ── Проверка самого измерителя ─────────────────────────────────────────────

def test_probe_sees_both_broken_forms():
    """Зелёный храповик, не умеющий краснеть, ничего не защищает."""
    double = """<div onclick="openNode(${n.id}, ${JSON.stringify(t)})">x</div>"""
    single = """<b onclick='copyText(${JSON.stringify(full)})'>c</b>"""
    assert _ATTR_JSON.search(double), "не увидел атрибут в двойных кавычках"
    assert _ATTR_JSON.search(single), "не увидел атрибут в одинарных кавычках"


def test_probe_does_not_slander_json_outside_attributes():
    """JSON.stringify в теле запроса — норма, его трогать нельзя."""
    ok = "await api('/x', {method:'POST', body: JSON.stringify(payload)});"
    assert not _ATTR_JSON.search(ok)


def test_probe_does_not_slander_a_plain_handler():
    ok = """<div onclick="openCommunityNode(${n.id})">x</div>"""
    assert not _ATTR_JSON.search(ok)


# ── Замены сделаны именно тем приёмом, а не переносом проблемы ─────────────

def _index_html() -> str:
    return (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_community_rows_pass_only_an_id():
    src = _index_html()
    assert 'onclick="openCommunityNode(${n.id})"' in src
    assert "CUR_CNODES" in src, "нужен кэш, из которого берутся название и chat_id"


def test_takedown_copy_button_passes_an_index():
    src = _index_html()
    assert "copyComplaintText(${i})" in src
    assert "_TAKEDOWN_TEXTS" in src


def test_frequent_chips_pass_an_index():
    src = _index_html()
    assert "replayFrequent(${i})" in src
    assert "_FREQ_ITEMS" in src


def test_pulse_buttons_pass_a_key():
    src = _index_html()
    assert "runPulseActionById(" in src and "_PULSE_ACTIONS" in src


def test_every_new_handler_actually_exists():
    """Замена не должна превратить рабочую кнопку в мёртвый тап."""
    src = _index_html()
    for fn in ("copyComplaintText", "replayFrequent", "runPulseActionById",
               "openCommunityNode"):
        assert re.search(rf"(async\s+)?function\s+{fn}\s*\(", src), fn
