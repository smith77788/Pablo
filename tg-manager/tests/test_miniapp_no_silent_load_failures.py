"""Упавшая загрузка не должна выглядеть как «у вас ничего нет».

РАЗРЫВ. По мини-аппу было раскидано двенадцать мест вида

    try { const d = await api('/api/miniapp/proxies'); sel.innerHTML = …; }
    catch(_) {}

Снаружи провал выглядел не как провал. Выпадашка прокси оставалась с одним
пунктом «Авто» — ровно как у владельца, у которого прокси нет. Список
кросспостинга оставался пустым — ровно как при отсутствии правил. Полоса
статистики контактов — пустой. Человек делает вывод «у меня этого нет» и
действует по нему: заводит аккаунты напрямую вместо своих прокси, заново
создаёт правило, которое уже есть.

Два места были хуже простой пустоты.

* `purgeDeadAccounts` при упавшем запросе оставлял `cnt = 0` и печатал
  «Мёртвых аккаунтов нет 👍» — приложение УТВЕРЖДАЛО факт, который не смогло
  проверить.
* Модалка «Уведомления ловца»: тумблеры висят на `onchange="saveVaultSettings()"`,
  а сохранение пишет ОБА значения разом. Упавшее чтение оставляло оба в
  «выключено»; касание одного тумблера выключало на сервере и второй — тот,
  который там был включён. Провал чтения превращался в неверную запись, молча.

ЭТОТ ТЕСТ запрещает классу вернуться: у catch вокруг `api(` обязан быть след,
который увидит человек. Исключения перечислены поимённо и объяснены — молчание
допустимо только там, где у кода есть настоящий запасной путь.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html, miniapp_source, screen_files

# Молчаливый catch допустим, только когда за ним идёт РЕАЛЬНЫЙ запасной путь и
# человек всё равно получает рабочий экран. Каждый пункт — с обоснованием.
ALLOWED_SILENT = {
    # Ссылка-приглашение: если её не отдали, код тут же собирает пригодный для
    # инвайта идентификатор вида -100… . Поле не остаётся пустым, операция не
    # встаёт, сообщать человеку не о чем.
    "/invite_link",
}


def _blocks(src: str):
    """Пары (тело try, тело catch) — по РЕАЛЬНЫМ скобкам, без окон длины."""
    out = []
    for m in re.finditer(r"\btry\s*\{", src):
        try_body, end = _braced(src, m.end() - 1)
        tail = src[end:end + 200]
        cm = re.match(r"\s*catch\s*\([^)]*\)\s*\{", tail)
        if not cm:
            continue
        catch_body, _ = _braced(src, end + cm.end() - 1)
        out.append((try_body, catch_body, src[:m.start()].count("\n") + 1))
    return out


def _braced(src: str, open_idx: int):
    """Тело от `{` до парной `}`; строки и `//`-комментарии пропускаются."""
    depth, k, n = 0, open_idx, len(src)
    while k < n:
        c = src[k]
        if c in "'\"`":
            q, k = c, k + 1
            while k < n and src[k] != q:
                k += 2 if src[k] == "\\" else 1
        elif c == "/" and k + 1 < n and src[k + 1] == "/":
            nl = src.find("\n", k)
            if nl == -1:
                break
            k = nl
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx:k + 1], k + 1
        k += 1
    return src[open_idx:], n


def offenders() -> list[str]:
    bad: list[str] = []
    sources = [("mini_app/index.html", miniapp_html())]
    sources += [(f"mini_app/screens/{p.name}", p.read_text(encoding="utf-8"))
                for p in screen_files()]
    for name, src in sources:
        for try_body, catch_body, line in _blocks(src):
            if "api(" not in try_body:
                continue
            if any(a in try_body for a in ALLOWED_SILENT):
                continue
            # Правило нарочно узкое: ПУСТОЙ catch — единственная форма, о
            # которой можно судить без разбора смысла. Он не может оставить
            # следа в принципе. Непустой обработчик хоть что-то делает
            # (_accErr, updatePlan, скрыть необязательный блок) — судить о нём
            # регулярками значит плодить ложные срабатывания, а детектор,
            # который шумит, чинят выключением.
            if catch_body.strip("{} \n\t\r"):
                continue
            bad.append(f"{name}:{line}")
    return bad


def test_failed_load_always_leaves_a_trace():
    bad = offenders()
    assert not bad, (
        "пустой catch вокруг api() — экран выглядит как «у вас ничего нет»:\n  "
        + "\n  ".join(bad)
        + "\n\nВ catch нужен видимый след: errHtml(...) с повтором для блока, "
          "пункт «⚠️ … не загрузились» для выпадашки или toast(...). "
          "Настоящий запасной путь — в ALLOWED_SILENT, с объяснением."
    )


def test_purge_dead_never_claims_a_fact_it_failed_to_check():
    """«Мёртвых аккаунтов нет» — вывод, а не заглушка на случай ошибки."""
    src = miniapp_source()
    i = src.index("async function purgeDeadAccounts")
    body, _ = _braced(src, src.index("{", i))
    catch = body[body.index("} catch"):]
    ok = catch[:catch.index("if (!cnt)")]
    assert "return" in ok and "toast" in ok, (
        "упавший запрос доходит до проверки `if (!cnt)` и печатает «Мёртвых "
        "аккаунтов нет 👍» — непроверенное выдаётся за проверенное")


def test_vault_toggles_refuse_to_save_what_they_never_read():
    """Тумблеры «ловца» не пишут на сервер, пока не прочитали оттуда."""
    src = miniapp_source()
    i = src.index("async function openVaultSettings")
    open_body, _ = _braced(src, src.index("{", i))
    assert "disabled = true" in open_body.replace("  ", " "), (
        "тумблеры не блокируются на время загрузки — касание до ответа сервера "
        "запишет оба значения из пустого состояния")
    assert "dataset.loaded" in open_body, "нет отметки «состояние получено»"

    j = src.index("async function saveVaultSettings")
    save_body, _ = _braced(src, src.index("{", j))
    guard = save_body[:save_body.index("api(")]
    assert "dataset.loaded" in guard and "return" in guard, (
        "сохранение не проверяет, что состояние успело загрузиться: упавшее "
        "чтение превратится в запись, выключающую то, что было включено")
