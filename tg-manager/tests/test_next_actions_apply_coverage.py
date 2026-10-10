"""Гейт: у каждой подсказки Copilot с кнопкой «в один клик» есть серверный
обработчик — иначе тап по кнопке молча возвращает 400 «нет действия».

Подсказки с полем "apply" рендерят кнопку прямого исполнения (apply-эндпоинт
шлёт только id). Обработчик — единственная точка `_apply_next_action`. Легко
добавить подсказку с "apply" и забыть ветку обработчика: кнопка становится
мёртвой. Этот гейт ловит расхождение статически, без БД и Telegram.

Симметрично: у каждой подсказки с CTA `fn` цель должна существовать во фронте
(мёртвый тап иначе). Проверяем оба контракта.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NA = (ROOT / "services" / "next_actions.py").read_text(encoding="utf-8")
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
# Весь мини-апп, а не только index.html: экраны вынесены в mini_app/screens/*.js,
# и цель подсказки может жить там. Гейт «цель существует во фронте» это
# проглядел — openMassInvite переехала в screens/invite.js, а проверка
# продолжала проходить лишь потому, что в index.html случайно попадалась
# подстрока «openMassInvite=» из сравнения `typeof openMassInvite==='function'`.
# Стоило этому сравнению уйти — и стало видно, что гейт давно ничего не сторожит.
from tests.miniapp_source import miniapp_source  # noqa: E402

HTML = miniapp_source()


def _suggestions():
    """Разбивает build_suggestions на блоки по "id" и собирает поля каждого."""
    out = []
    for m in re.finditer(r'"id":\s*"([a-z_]+)"', NA):
        sid = m.group(1)
        seg = NA[m.end():m.end() + 700]
        nxt = seg.find('"id":')
        if nxt != -1:
            seg = seg[:nxt]
        has_apply = '"apply"' in seg
        fn = re.search(r'"fn":\s*"([a-zA-Z0-9_]+)"', seg)
        out.append((sid, has_apply, fn.group(1) if fn else None))
    return out


def test_every_apply_suggestion_has_server_handler():
    apply_ids = {sid for sid, has_apply, _ in _suggestions() if has_apply}
    assert apply_ids, "не нашли ни одной apply-подсказки — сломался парсер"
    # тело обработчика: от def до следующего верхнеуровневого async def
    i = API.find("async def _apply_next_action(")
    assert i != -1, "_apply_next_action не найден"
    body = API[i:i + 4000]
    for sid in sorted(apply_ids):
        assert re.search(rf'action_id\b[^\n]*"{sid}"', body), (
            f"подсказка '{sid}' имеет кнопку «в один клик», но нет ветки в "
            f"_apply_next_action — тап вернёт 400"
        )


def test_every_cta_fn_exists_in_frontend():
    for sid, _, fn in _suggestions():
        if not fn:
            continue
        assert re.search(rf"function {fn}\b", HTML) or f"{fn}=" in HTML, (
            f"подсказка '{sid}' ведёт в {fn}(), которой нет во фронте — мёртвый тап"
        )
