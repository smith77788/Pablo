"""Подтверждения мини-аппа должны подтверждать, а «назад» — возвращать.

Два разрыва, которые это закрывает.

1. Диалог, который ничего не решал. Удаление авто-ответа было написано так:

       tg.showConfirm?.('Удалить авто-ответ?', ok => {...}) ?? await doDeleteAr(id)

   `showConfirm` в telegram-web-app.js ничего не возвращает, поэтому правая
   часть `??` выполнялась ВСЕГДА: авто-ответ исчезал в момент касания 🗑, ещё до
   ответа пользователя, а нажатое «ОК» отправляло удаление вторым запросом.
   Нажатая «Отмена» не отменяла ничего.

   Рядом жила вторая форма того же: `window.confirm` во встроенном браузере
   Telegram часто блокируется и возвращает false — кнопка просто не срабатывала.
   Ровно об это уже споткнулись однажды, отсюда и появился `askConfirm`;
   вынесенные экраны (`screens/*.js`) про него не знали.

2. Один и тот же экран в стеке дважды. Экраны — синглтоны по id, и где экран
   перерисовывают повторным вызовом своей же open-функции (⟳ на «Защите от
   накрутки», сохранение в редакторе менеджера продаж), `push` клал его в стек
   ещё раз. «Назад» снимал верхний и показывал ТОТ ЖЕ экран: выйти получалось
   только перебором, по нажатию на каждое сделанное действие.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_source, screen_files


def _braced(src: str, anchor: str) -> str:
    """Кусок от `anchor` до закрывающей его фигурной скобки — по РЕАЛЬНОЙ границе.

    Срез фиксированной длины (`src[i:i + 1400]`) держится ровно до следующей
    правки: код подрос — окно промахнулось — проверка внутри него замолчала.
    Считаем скобки, пропуская строки и `//`-комментарии, поэтому границей
    служит сама структура, а не подобранное когда-то число.
    """
    i = src.index(anchor)
    j = src.index("{", i)
    depth, k, n = 0, j, len(src)
    while k < n:
        c = src[k]
        if c in "'\"`":
            q, k = c, k + 1
            while k < n and src[k] != q:
                k += 2 if src[k] == "\\" else 1
        elif c == "/" and k + 1 < n and src[k + 1] == "/":
            k = src.find("\n", k)
            if k == -1:
                break
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise AssertionError(f"не нашли закрывающую скобку для {anchor!r}")



def test_no_confirm_swallowed_by_nullish_coalescing():
    """`showConfirm(...) ?? действие` — действие уходит всегда, мимо ответа."""
    src = miniapp_source()
    assert "showConfirm?.(" not in src, (
        "showConfirm ничего не возвращает: `?? действие` выполнит действие "
        "независимо от ответа пользователя — используйте askConfirm")


def test_screens_use_ask_confirm_not_window_confirm():
    """`window.confirm` во встроенном браузере Telegram часто не работает."""
    bad = []
    for path in screen_files():
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"(?<![\w.])confirm\s*\(", text):
            # askConfirm / showConfirm — это как раз правильные формы
            head = text[max(0, m.start() - 12):m.start()]
            if head.endswith("ask") or head.endswith("show"):
                continue
            bad.append(f"{path.name}:{text[:m.start()].count(chr(10)) + 1}")
    assert not bad, "голый confirm() вместо askConfirm: " + ", ".join(bad)


def test_push_refuses_to_stack_the_same_screen_twice():
    """Повторный push того же экрана — ловушка, а не переход."""
    src = miniapp_source()
    i = src.index("function push(sid)")
    body = src[i:i + 1400]
    assert "STACK[STACK.length - 1] === sid" in body, (
        "push снова кладёт тот же экран в стек — «назад» будет показывать его же")


def test_row_deletes_ask_first():
    """🗑 в строке списка не должен стирать объект с одного промаха пальцем."""
    src = miniapp_source()
    for fn in ("deleteComp", "deleteKw", "deleteTpl", "deleteMeshTarget",
               "spDelProduct", "spDelFaq", "spDelExample", "spDelPromo",
               "spDelDelivery", "spDelete", "deleteAr"):
        m = re.search(r"(?:async )?function " + fn + r"\s*\([^)]*\)\s*\{", src)
        assert m, f"функция {fn} не найдена"
        head = src[m.end():m.end() + 500]
        cut = head.find("await api(")
        assert cut != -1, f"{fn}: не нашли вызов api"
        assert "askConfirm" in head[:cut], (
            f"{fn} удаляет без подтверждения — одно касание 🗑 и объекта нет")


def test_fallback_tg_stub_answers_the_confirm():
    """Запасной `tg` без SDK обязан ОТВЕЧАТЬ на подтверждение.

    Когда telegram-web-app.js не загрузился, приложение работает на запасном
    объекте `tg`. В нём стояло `showConfirm() {}` — метод есть, колбэк не зовётся
    никогда. askConfirm проверяет ровно «метод есть?» и уходит ждать ответа,
    поэтому его промис висел вечно: диалог не показывался, действие не
    происходило, ошибки не было. Ни одно удаление в этом режиме не работало.
    """
    stub = _braced(miniapp_source(),
                   "const tg = (window.Telegram && window.Telegram.WebApp) ||")
    m = re.search(r"showConfirm\s*\(([^)]*)\)\s*\{", stub)
    assert m, "в запасном `tg` нет showConfirm — askConfirm не найдёт метод"
    args = [a.strip() for a in m.group(1).split(",") if a.strip()]
    assert len(args) >= 2, (
        "заглушка showConfirm обязана принимать колбэк вторым аргументом, "
        f"а принимает {m.group(1)!r}")
    cb = args[1]
    body = _braced(stub[m.start():], m.group(0)[:-1])
    assert re.search(rf"\b{re.escape(cb)}\s*\(", body), (
        "заглушка showConfirm не зовёт колбэк — askConfirm повиснет навсегда: "
        "промис не разрешится, диалога не будет, действие не произойдёт")
