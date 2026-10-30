"""Удаление спрашивает — и спрашивает по-настоящему.

ЧТО ЗАЩИЩАЕМ. Удаление в мини-аппе необратимо: аккаунт, канал, прокси, шаблон,
контакт, воронку владелец вернуть не может — у сервера нет корзины. Экран
управляется большим пальцем в телефоне, кнопка «🗑» стоит в строке списка
рядом с остальными, и промах по ней стоит данных.

Сейчас всех дверей ровно одна: `askConfirm` (нативный диалог Telegram, а при
его отсутствии `window.confirm`), и 54 функции мини-аппа, посылающие DELETE,
проходят через неё — прямо или через обёртку `confirmDelete`. Правило держит
это число равным нулю: ни одного разрушающего вызова без вопроса.

ПОЧЕМУ ЭТО ПРАВИЛО, А НЕ НАБЛЮДЕНИЕ. Такую дыру не видно ни в прогоне, ни на
экране: кнопка работает, удаление происходит, тост говорит «удалено». Заметит
её только владелец и только когда уже потерял данные. Добавить её ничего не
стоит: новая строка списка пишется копированием соседней, а подтверждение в
соседней стоит СТРОКОЙ ВЫШЕ самого вызова — именно та позиция, которую при
копировании теряют первой.

КАК СЧИТАЕТСЯ ДВЕРЬ. Подтверждение бывает не в самой функции, а у того, кто её
зовёт: `deleteComp` спрашивает сама, а `doDeleteAr` — уже последствие чужого
вопроса. Поэтому функция считается закрытой, если спрашивает сама ИЛИ если
спрашивают ВСЕ, кто её зовёт (любой незакрытый вход — дыра, поэтому «все», а
не «кто-нибудь»).

ЧЕГО Я ЗДЕСЬ НЕ ДЕЛАЮ — и это стоило мне половины разбора. Первая версия
отбирала дверь по механизму: искала `askConfirm(` и `confirm(`. Она объявила
19 функций незакрытыми, и все 19 оказались закрытыми через `confirmDelete` —
обёртку, имени которой мерка не знала. Тот же промах, что уже записан в
CLAUDE.md про отбор храповика: мерка, названная механизмом, видит одну из
реализаций свойства и молчит про остальные. Поэтому дверь ищется СВОЙСТВОМ —
«достижим ли `askConfirm`» — а обёртки вычисляются из исходника, а не
перечисляются руками. И чтобы обёртка не могла стать декоративной, ниже
отдельно проверено, что КАЖДАЯ функция с «confirm» в имени действительно
доходит до `askConfirm`: иначе дверью считалась бы вывеска.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
_FILES = [ROOT / "mini_app" / "index.html"] + sorted(
    (ROOT / "mini_app" / "screens").glob("*.js"))

_FN = re.compile(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")
# Разрушающий вызов: метод DELETE, либо путь, в котором стоит глагол удаления.
# Глагол удаления считается только ЦЕЛЫМ сегментом пути: в
# `/api/miniapp/channels/removed` «removed» — существительное, и это обычный
# GET-список удалённых каналов. Первая версия без границы сегмента объявила его
# разрушающим вызовом — ложная находка в правиле о необратимом действии дороже
# обычной: следующий разбор заносит ручку в исключения, и дыра появляется там,
# где её не было.
_DESTRUCTIVE = re.compile(
    r"""method:\s*['"]DELETE['"]"""
    r"""|api\(\s*[`'"][^`'"]*"""
    r"""/(?:delete|remove|purge|wipe|revoke)(?:[/?'"`]|\$\{|$)""",
    re.I)
_ROOT_DOOR = "askConfirm"


def _bodies(extra: dict[str, str] | None = None) -> dict[str, tuple[str, str]]:
    """{имя функции: (файл, тело со скобками)} по всему исходнику мини-аппа."""
    out: dict[str, tuple[str, str]] = {}
    for f in _FILES:
        src = f.read_text(encoding="utf-8")
        for m in _FN.finditer(src):
            i = src.find("{", m.end() - 1)
            if i < 0:
                continue
            depth = 0
            for j in range(i, len(src)):
                if src[j] == "{":
                    depth += 1
                elif src[j] == "}":
                    depth -= 1
                    if depth == 0:
                        out[m.group(1)] = (f.name, src[i:j + 1])
                        break
    for name, body in (extra or {}).items():
        out[name] = ("<проба>", body)
    return out


def _calls(body: str, name: str) -> bool:
    return re.search(r"\b" + re.escape(name) + r"\s*\(", body) is not None


def _doors(bodies: dict[str, tuple[str, str]]) -> set[str]:
    """Имена, вызов которых = заданный вопрос.

    Корень — `askConfirm`. Обёртка — функция, которая зовёт корень напрямую и
    сама названа подтверждением: `confirmDelete`, `botDeleteConfirm`. Имя в
    условии нужно, чтобы обёрткой не оказалась функция, которая спрашивает про
    ОДНО, а зовущий удаляет другое.
    """
    doors = {_ROOT_DOOR}
    for name, (_f, body) in bodies.items():
        if "onfirm" in name.lower() and _calls(body, _ROOT_DOOR):
            doors.add(name)
    return doors


def _asks(name: str, bodies, doors, seen=()) -> bool:
    if name in seen or name not in bodies:
        return False
    body = bodies[name][1]
    if any(_calls(body, d) for d in doors):
        return True
    seen = seen + (name,)
    callers = [c for c, (_f, b) in bodies.items()
               if c != name and _calls(b, name)]
    # Спросить обязаны ВСЕ входы: один незакрытый — уже дыра.
    return bool(callers) and all(_asks(c, bodies, doors, seen) for c in callers)


def _silent_deletes(extra: dict[str, str] | None = None) -> list[str]:
    bodies = _bodies(extra)
    doors = _doors(bodies)
    out = []
    for name, (fname, body) in bodies.items():
        if not _DESTRUCTIVE.search(body):
            continue
        if not _asks(name, bodies, doors):
            out.append(f"{fname}:{name}")
    return sorted(out)


def test_no_destructive_call_happens_without_a_question():
    silent = _silent_deletes()
    assert silent == [], (
        "разрушающий вызов без подтверждения — владелец теряет данные "
        "промахом по кнопке: " + ", ".join(silent))


def test_the_measure_actually_selects_the_deletes():
    """Пустой отбор дал бы вечно зелёное правило ни о чём."""
    bodies = _bodies()
    destructive = [n for n, (_f, b) in bodies.items() if _DESTRUCTIVE.search(b)]
    assert len(destructive) >= 50, (
        f"отбор разрушающих вызовов обмелел до {len(destructive)} — "
        "скорее всего сломан он, а не исходник")


def test_every_door_really_reaches_the_native_dialog():
    """Обёртка не имеет права быть вывеской.

    Если функция с «confirm» в имени перестанет доходить до `askConfirm`, она
    останется дверью по мерке и перестанет быть дверью по делу: удаление будет
    молчать, а правило — зеленеть.
    """
    bodies = _bodies()
    doors = _doors(bodies)
    for d in sorted(doors - {_ROOT_DOOR}):
        assert _calls(bodies[d][1], _ROOT_DOOR), f"{d} не спрашивает"
    named = {n for n in bodies if "onfirm" in n.lower()}
    decorative = sorted(n for n in named - doors
                        if not _asks(n, bodies, doors))
    assert decorative == [], (
        "функция названа подтверждением, но вопроса не задаёт: "
        + ", ".join(decorative))


def test_the_root_door_is_the_telegram_dialog_with_a_fallback():
    """`askConfirm` обязана ОТВЕЧАТЬ, иначе тихое удаление вернётся через неё.

    `tg.showConfirm` есть не в каждом клиенте, и в прежнем коде отсутствие
    диалога означало `undefined` — то есть «пользователь согласился».
    """
    body = _bodies()[_ROOT_DOOR][1]
    assert "tg.showConfirm" in body
    assert "window.confirm" in body, (
        "без запасного диалога клиент без showConfirm удалял бы молча")


# ── Самопроверки отбора: правило обязано ловить то, о чём написано ──────────

def test_a_fabricated_silent_delete_is_caught():
    bad = _silent_deletes({"deleteEverythingNow": """{
        await api('/api/miniapp/account/'+id, {method:'DELETE'});
        toast('готово');
    }"""})
    assert any("deleteEverythingNow" in b for b in bad), (
        "правило не видит разрушающий вызов без вопроса — оно пустое")


def test_a_listing_of_removed_things_is_not_a_deletion():
    """«removed» в пути списка — существительное, а не глагол.

    На этом правило и поймало само себя: `openRemovedChannels` делает GET
    `/api/miniapp/channels/removed`, ничего не удаляет и подтверждения не
    требует. Проба держит отбор на границе сегмента пути.
    """
    bad = _silent_deletes({"openSomethingRemoved": """{
        const d = await api('/api/miniapp/channels/removed');
        render(d.channels || []);
    }"""})
    assert not any("openSomethingRemoved" in b for b in bad), (
        "список удалённого принят за удаление — правило требует диалог на GET")


def test_a_question_asked_by_the_caller_counts():
    bad = _silent_deletes({
        "doWipeIt": "{ await api('/api/miniapp/x', {method:'DELETE'}); }",
        "wipeIt": "{ if (!await askConfirm('Удалить?')) return; doWipeIt(1); }",
    })
    assert not any("doWipeIt" in b for b in bad), (
        "вопрос у звавшего не зачтён — правило начнёт требовать второй диалог")


def test_a_question_asked_through_a_wrapper_counts():
    bad = _silent_deletes({"deleteViaWrapper": """{
        if (!await confirmDelete('Удалить шаблон', el)) return;
        await api('/api/miniapp/template/'+id, {method:'DELETE'});
    }"""})
    assert not any("deleteViaWrapper" in b for b in bad), (
        "обёртка не зачтена — ровно тот промах, с которого начался разбор")


def test_one_unguarded_entry_is_enough_to_fail():
    """«Все входы», а не «какой-нибудь»: кнопка без вопроса — уже потеря."""
    bad = _silent_deletes({
        "doKill": "{ await api('/api/miniapp/x', {method:'DELETE'}); }",
        "killAsked": "{ if (!await askConfirm('Удалить?')) return; doKill(1); }",
        "killQuietly": "{ doKill(2); }",
    })
    assert any("doKill" in b for b in bad), (
        "незакрытый вход не замечен: достаточно одной кнопки мимо вопроса")


def test_a_decorative_wrapper_is_not_a_door():
    bad = _silent_deletes({
        "confirmNothing": "{ return true; }",
        "deleteWithTheatre": """{
            if (!await confirmNothing()) return;
            await api('/api/miniapp/proxy/'+id, {method:'DELETE'});
        }""",
    })
    assert any("deleteWithTheatre" in b for b in bad), (
        "вывеска зачтена за дверь — удаление молчит, а правило зелёное")
