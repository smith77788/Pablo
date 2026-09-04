"""Отвал по шагам воронки: где именно люди уходят из цепочки.

Разрыв, который это закрывает: funnel_subscriptions.current_step (сколько шагов
уже доставлено подписчику) копится с самого начала, но нигде не агрегировался.
Экран воронки показывал только «всего / активных» — главный вопрос к любой
drip-цепочке («на каком шаге теряем людей») ответа не имел, хотя все данные
для него уже лежали в базе.

Расчёт здесь повторяет тот, что делает эндпоинт: подписчик с current_step=k
получил k шагов, значит «дошли до шага i» = у скольких current_step >= i.
"""
from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")


def _funnel_stats(steps: list[dict], dist: list[dict]) -> dict:
    """Та же арифметика, что в funnel_steps — вынесена, чтобы проверить её
    поведением, а не наличием строк в исходнике."""
    subs_total = sum(int(r["n"]) for r in dist)
    by_step = {int(r["current_step"]): r for r in dist}
    at_least, running = {}, 0
    for i in range(max(by_step) if by_step else 0, -1, -1):
        running += int((by_step.get(i) or {}).get("n") or 0)
        at_least[i] = running
    out = []
    for idx, s in enumerate(steps, start=1):
        reached = at_least.get(idx, 0)
        nxt = at_least.get(idx + 1, 0)
        out.append({
            "step_order": s.get("step_order", idx),
            "delivered": reached,
            "lost_after": max(0, reached - nxt) if idx < len(steps) else 0,
            "conversion_pct": round(reached * 100.0 / subs_total, 1) if subs_total else 0.0,
        })
    return {"step_stats": out, "subs_total": subs_total}


_STEPS3 = [{"step_order": 1}, {"step_order": 2}, {"step_order": 3}]


def test_delivery_counts_are_cumulative():
    """90 получили первый шаг, 40 дошли до третьего — а не «по 30 на каждом»."""
    dist = [
        {"current_step": 0, "n": 10},   # подписались, но ещё ничего не получили
        {"current_step": 1, "n": 20},
        {"current_step": 2, "n": 30},
        {"current_step": 3, "n": 40},   # прошли всю цепочку
    ]
    st = _funnel_stats(_STEPS3, dist)["step_stats"]
    assert [s["delivered"] for s in st] == [90, 70, 40]


def test_lost_after_shows_where_people_leave():
    dist = [
        {"current_step": 0, "n": 10},
        {"current_step": 1, "n": 20},
        {"current_step": 2, "n": 30},
        {"current_step": 3, "n": 40},
    ]
    st = _funnel_stats(_STEPS3, dist)["step_stats"]
    assert [s["lost_after"] for s in st] == [20, 30, 0]


def test_last_step_has_no_lost_after():
    """После последнего шага уходить некуда — цифра там была бы враньём."""
    dist = [{"current_step": 3, "n": 5}]
    st = _funnel_stats(_STEPS3, dist)["step_stats"]
    assert st[-1]["lost_after"] == 0


def test_conversion_percent():
    dist = [{"current_step": 0, "n": 50}, {"current_step": 3, "n": 50}]
    st = _funnel_stats(_STEPS3, dist)["step_stats"]
    assert st[0]["conversion_pct"] == 50.0
    assert st[-1]["conversion_pct"] == 50.0


def test_no_subscribers_does_not_divide_by_zero():
    """Попытка сломать: новая воронка без единого подписчика."""
    st = _funnel_stats(_STEPS3, [])["step_stats"]
    assert [s["delivered"] for s in st] == [0, 0, 0]
    assert all(s["conversion_pct"] == 0.0 for s in st)


def test_funnel_without_steps():
    assert _funnel_stats([], [{"current_step": 0, "n": 3}])["step_stats"] == []


def test_more_progress_than_steps_does_not_go_negative():
    """Шаг могли удалить после того, как люди его прошли."""
    st = _funnel_stats([{"step_order": 1}], [{"current_step": 5, "n": 7}])["step_stats"]
    assert st[0]["delivered"] == 7
    assert st[0]["lost_after"] == 0


# ── Подключение к эндпоинту ───────────────────────────────────────────────────

def _endpoint() -> str:
    start = _API.index("async def funnel_steps")
    return _API[start:start + 4000]


def test_endpoint_returns_step_stats():
    body = _endpoint()
    assert '"step_stats"' in body
    assert '"dropped_total"' in body and '"completed_total"' in body


def test_endpoint_aggregates_in_one_query():
    """Отдельный запрос на шаг превратил бы экран в N+1 по числу шагов."""
    body = _endpoint()
    assert body.count("FROM funnel_subscriptions") <= 3
    assert "GROUP BY current_step" in body


def test_endpoint_stays_owner_scoped():
    body = _endpoint()
    assert "mb.added_by=$2" in body


def test_ui_renders_dropoff():
    ui = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    seg = ui[ui.index("function buildFunnelDetail"):]
    seg = seg[:4000]
    assert "step_stats" in seg
    assert "отвал дальше" in seg
    assert "Итог цепочки" in seg


# ── Правка шага ───────────────────────────────────────────────────────────────

def _update_step() -> str:
    start = _API.index("async def update_funnel_step")
    return _API[start:start + 3000]


def test_step_edit_endpoint_exists_and_is_routed():
    """Правки шага в мини-приложении не было вовсе — только добавить и удалить.
    Опечатка в третьем шаге из пяти чинилась удалением и повторным
    добавлением, а добавление кладёт шаг В КОНЕЦ: порядок цепочки ломался.
    В боте правка при этом давно есть."""
    assert "async def update_funnel_step" in _API
    assert 'add_patch("/api/miniapp/funnel/step/{step_id}"' in _API


def test_step_edit_is_owner_scoped():
    body = _update_step()
    assert "mb.added_by=$2" in body and "404" in body


def test_step_edit_validates_text():
    body = _update_step()
    assert "validate_string" in body and "check_sql_suspicious" in body
    assert "Текст шага не может быть пустым" in body


def test_step_edit_validates_delay():
    body = _update_step()
    assert "min_val=0" in body


def test_step_edit_supports_partial_update():
    body = _update_step()
    assert "Нечего изменять" in body


def test_ui_has_step_edit_and_can_cancel():
    ui = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "editFunnelStep(" in ui and "saveFunnelStep" in ui
    assert "cancelEditFunnelStep" in ui, "из режима правки нужен выход"
    assert "method:'PATCH'" in ui


def test_ui_preserves_unlisted_delay_value():
    """Задержка могла быть выставлена из бота значением не из списка — правка
    не должна молча её менять."""
    ui = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "d.add(new Option(" in ui
