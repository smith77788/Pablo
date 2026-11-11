"""Экран «🔭 Радар ниши»: поиск чужих ресурсов и присутствие в них.

* экран есть, он вкладка «Мотора роста», и у него два вида: ниши и каталог;
* поиск и присутствие ставятся ОПЕРАЦИЯМИ (их видно и можно остановить);
* присутствие спрашивает подтверждение и называет, что именно будет делать —
  это действия от аккаунтов владельца в ЧУЖИХ чатах;
* исполнители заведены в реестре операций и в диспетчере op_worker.
"""
from __future__ import annotations

import pathlib

from tests.miniapp_source import js_function, miniapp_html

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_screen_exists_and_is_a_tab_of_the_growth_module():
    html = miniapp_html()
    for el in ('id="s-niche"', 'id="s-niche-assets"', 'id="nrList"', 'id="nrAssets"',
               'id="nrPresenceBtn"', 'id="nrKeywords"', 'id="nrTone"'):
        assert el in html, el
    assert "{key:'niche', label:'🔭 Радар ниши', sid:'s-niche', open: () => openNiche()}" in html
    assert "/api/miniapp/niche/overview" in js_function("nrLoad")


def test_search_and_presence_are_queued_as_operations():
    assert "/scan`" in js_function("nrScan")
    f = js_function("nrPresence")
    assert "/presence`" in f and "actions" in f
    srv = (ROOT / "services" / "mini_app_niche.py").read_text(encoding="utf-8")
    assert '"niche_scan"' in srv and '"niche_presence"' in srv
    worker = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
    assert '"niche_scan": _exec_niche_scan,' in worker
    assert '"niche_presence": _exec_niche_presence,' in worker
    bus = (ROOT / "services" / "operation_bus.py").read_text(encoding="utf-8")
    assert '"niche_scan": {' in bus and '"niche_presence": {' in bus


def test_presence_asks_before_acting_in_other_peoples_chats():
    f = js_function("nrPresence")
    assert "askConfirm(" in f, "действия в чужих чатах без подтверждения"
    assert f.index("askConfirm(") < f.index("/presence`")
    assert "ЧУЖИХ" in f and "Операциях" in f


def test_the_catalogue_shows_where_a_resource_came_from():
    f = js_function("nrOpenAssets")
    assert "source_ru" in f and "status_ru" in f
    assert "nrSkip(" in f, "ненужный ресурс нельзя убрать из работы"


def test_deleting_a_niche_warns_about_the_catalogue():
    f = js_function("nrDelete")
    assert "confirmDelete(" in f and "Каталог" in f


def test_presence_can_comment_in_groups_not_only_channels():
    """Большинство найденного в нише — ГРУППЫ, а не каналы.

    `post_ai_comment` умеет только канал: ищет чат обсуждений и без него
    отказывает «не канал». Если бы присутствие звало её, оно молча отказывало
    бы на большинстве каталога.
    """
    eng = (ROOT / "services" / "ai_comment_engine.py").read_text(encoding="utf-8")
    assert "async def post_ai_comment_anywhere(" in eng
    assert "reply_to=post.id" in eng, "в группе отвечают прямо в ней"
    worker = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
    i = worker.index("async def _exec_niche_presence(")
    body = worker[i:i + 9000]
    assert "post_ai_comment_anywhere(" in body


def test_a_repeat_run_does_not_act_in_the_same_chat_twice():
    """Повтор операции ведёт по списку сначала.

    Без журнала целей аккаунт вступил бы в тот же чат и написал бы туда второй
    комментарий — ровно та сигнатура, от которой защищают паузы.
    """
    worker = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
    i = worker.index("async def _exec_niche_presence(")
    body = worker[i:i + 11000]
    assert "settled_targets(pool, op_id)" in body, "нет двери идемпотентности"
    assert "if str(aid) in _settled:" in body, "закрытая цель не пропускается"
    assert body.count("_journal_done(") >= 3, "успех цели не попадает в журнал"
    assert "'skip'" in body, "окончательный отказ не закрывает цель"
