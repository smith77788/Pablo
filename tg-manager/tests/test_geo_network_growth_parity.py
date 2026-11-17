"""Гео-сеть имеет те же настройки роста, что и Фабрика каналов.

Было: гео-исполнитель публиковал лишь дженерик-welcome (без пользовательского
текста и закрепа) и не делал ни автопосева, ни оживления — всё это было только у
Фабрики. Пустой гео-канал быстро попадал в теневой бан, а набрать стартовую
аудиторию/активность было нечем.

Стало: _exec_global_presence_channel применяет к КАЖДОМУ созданному ресурсу
первый пост (спинтакс {A|B} + плейсхолдеры), закреп, автопосев (boost_subscribers)
и оживление (boost_reactions + boost_views) — те же проверенные операции шины,
что у Фабрики. Параметры хранятся в плане (geo_selection) и читаются исполнителем.

Эти ратчеты сторожат всю цепочку: хендлер → план → исполнитель → UI.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from services import op_worker

_ROOT = Path(__file__).resolve().parent.parent
_PRES = (_ROOT / "services" / "mini_app_presence.py").read_text(encoding="utf-8")
_HTML = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_handler_stores_growth_settings_in_plan():
    # Без сохранения в geo_selection исполнителю неоткуда взять настройки роста.
    for key in ('"first_post": first_post', '"pin_first_post": pin_first_post',
                '"seed_count": seed_count', '"engage_count": engage_count'):
        assert key in _PRES, f"хендлер обязан класть {key} в geo_selection"
    # И честно читать/ограничивать их из тела запроса.
    assert 'body.get("first_post")' in _PRES
    assert 'body.get("seed_count")' in _PRES
    assert 'body.get("engage_count")' in _PRES


def test_executor_reads_growth_from_plan():
    src = inspect.getsource(op_worker._exec_global_presence_channel)
    for token in ("_gp_first_post", "_gp_pin_first", "_gp_seed_count", "_gp_engage_count"):
        assert token in src, f"исполнитель обязан читать {token} из плана"
    # Пул аккаунтов для посева/оживления строится из флота.
    assert "_gp_seed_pool" in src
    assert "select_all_active" in src


def test_executor_applies_first_post_pin_seed_engage():
    src = inspect.getsource(op_worker._exec_global_presence_channel)
    # Первый пост публикуется и может быть закреплён.
    assert "post_to_channel" in src and "pin_last_channel_post" in src
    # Оживление — реакции и просмотры флотом через шину.
    assert '"boost_reactions"' in src and '"boost_views"' in src
    # Автопосев — подписчики через шину.
    assert '"boost_subscribers"' in src
    # Первый пост — пользовательский текст рендерится (спинтакс + плейсхолдеры),
    # а не только дженерик: одинаковый текст в сотнях каналов — спам-сигнал.
    assert "render_text" in src


def test_engage_requires_public_channel_and_post():
    # Реакции/просмотры имеют смысл только для публичного канала (@) и когда есть
    # пост; иначе аккаунтам пришлось бы вступать — это другой след.
    src = inspect.getsource(op_worker._exec_global_presence_channel)
    i = src.find('"boost_reactions"')
    assert i != -1
    guard = src[max(0, i - 800):i]
    assert "applied_username" in guard and "_gp_msg_id" in guard and "not is_group" in guard


def test_ui_has_growth_controls_and_sends_them():
    for el in ('id="gpFirstPost"', 'id="gpPinFirst"', 'id="gpSeedCount"', 'id="gpEngageCount"'):
        assert el in _HTML, f"в гео-модале нет контрола {el}"
    for field in ("first_post: firstPost", "pin_first_post: pinFirst",
                  "seed_count: seedCount", "engage_count: engageCount"):
        assert field in _HTML, f"submitGpCreate не шлёт {field}"
