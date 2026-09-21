"""Живой прогресс операции — место, откуда с операцией можно что-то сделать.

Разрыв, который это закрывает. Панель «⏳ Выполняется» на главном экране
рисовала подпись, процент и полоску из символов «█░» — и всё. Ни открыть
операцию, ни остановить её оттуда было нельзя, хотя id операции приходит в том
же событии SSE (`fetch_op_progress` кладёт его в каждый элемент). Запущенную
массовую операцию оставалось только досматривать: чтобы её отменить, надо было
сначала найти экран операций.

Вторая половина того же: события SSE приходят раз в 15 секунд. Отменённая
операция всё это время оставалась в панели с живой кнопкой «Отмена» и растущей
полоской — повторное нажатие приносило ошибку, а экран показывал работу,
которой уже нет.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_source, source_of


def _render_op_progress() -> str:
    src = source_of("renderOpProgress")
    m = re.search(r"^function renderOpProgress\s*\(", src, re.M)
    assert m, "renderOpProgress не найдена"
    i = src.index("{", m.end() - 1)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[i:j + 1]


def test_progress_row_opens_the_operation():
    body = _render_op_progress()
    assert "openOpDetail(" in body, (
        "строка живого прогресса не ведёт в операцию — наблюдать можно, "
        "сделать ничего нельзя")


def test_progress_row_can_stop_the_operation():
    body = _render_op_progress()
    assert "cancelOp(" in body, "из живого прогресса нельзя остановить операцию"
    assert "event.stopPropagation()" in body, (
        "кнопка остановки без stopPropagation откроет экран операции вместо отмены")


def test_progress_uses_real_bar_not_ascii():
    body = _render_op_progress()
    assert "prog-fill" in body, "полоска прогресса должна быть настоящей"
    # Комментарии рассказывают, КАК было, и «█» в них законен — смотрим на код.
    code = "\n".join(re.sub(r"//.*$", "", ln) for ln in body.splitlines())
    assert "█" not in code, "полоска из символов «█░» вместо полосы прогресса"


def test_cancel_marks_the_live_row_at_once():
    """События SSE идут раз в 15 секунд — столько «отменённая» работать не может."""
    src = miniapp_source()
    assert "_opProgressMarkCancelled" in src
    i = src.index("async function cancelOp(")
    body = src[i:i + 700]
    assert "_opProgressMarkCancelled(id)" in body, (
        "после отмены строка в панели остаётся живой до следующего события SSE")
