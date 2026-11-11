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


def test_a_found_chat_can_be_warmed_up_by_the_fleet():
    """У найденного чата должна быть видимая кнопка прогрева флотом.

    Просьба владельца (11.11.2026): «для чатов должна быть возможность прогрева
    флотом, и кнопка должна быть явно видимой и рабочей». Прогрев предлагается
    только ГРУППАМ: в канал флоту писать негде.
    """
    src = (ROOT / "mini_app" / "screens" / "niche.js").read_text(encoding="utf-8")
    assert "startChatWarmup(" in src and "🔥 Прогрев флотом" in src
    assert "a.kind === 'group' ?" in src, "кнопка предлагается и каналам"


def test_chat_warmup_has_a_tile_in_the_catalogue():
    """Экран жил одной кнопкой среди десятка на вкладке «Аккаунты».

    Каталог «Все функции» и боковое меню строятся из ПЛИТОК экрана «Разделы»:
    без плитки экран не показывается нигде (CLAUDE.md).
    """
    html = miniapp_html()
    assert '<div class="mgmt-tile-lbl">Разогрев чата</div>' in html
    i = html.index('<div class="mgmt-tile-lbl">Разогрев чата</div>')
    assert 'onclick="openChatWarmup()"' in html[i - 300:i]
    from services import task_sections
    accounts = next(s for s in task_sections.SECTIONS if s.key == "accounts")
    assert "Разогрев чата" in accounts.tiles


def test_any_own_chat_can_be_warmed_up_in_one_tap():
    """Просьба владельца (11.11.2026): быстрый запуск разогрева у ЛЮБОГО своего
    чата, не только у находок радара. Запуск один на все места — иначе второй
    экран рано или поздно разойдётся с первым.
    """
    html = miniapp_html()
    assert "🔥 Разогрев флотом" in html, "в карточке чата нет кнопки разогрева"
    assert "startChatWarmup(" in html
    # Каналу разогрев не предлагается: флоту там писать негде.
    assert "c.type==='channel' ? '' :" in html
    f = js_function("startChatWarmup")
    assert "/api/miniapp/chatwarmup/session" in f, "кнопка ничего не запускает"
    assert "askConfirm(" in f and f.index("askConfirm(") < f.index("chatwarmup/session")
    assert "openChatWarmup()" in f, "после запуска не видно, куда смотреть"

    # И «быстро» — это не «девятой кнопкой в ряду из тринадцати одинаковых
    # серых»: владелец её там не нашёл (11.11.2026). Кнопка идёт ПЕРВОЙ в ряду
    # действий карточки и во всю ширину, то есть видна без разглядывания.
    row = html[html.index('<div class="actions"', html.index("function buildChDetail")):]
    row = row[:row.index("</div>\n    <div id=\"chdAutoposts\"")]
    assert row.index("🔥 Разогрев флотом") < row.index("📝 Опубликовать"), (
        "разогрев стоит после других действий — владелец его не найдёт")
    assert "flex:1 1 100%" in row.split("🔥 Разогрев флотом")[0].rsplit("<button", 1)[1], (
        "кнопка разогрева не во всю ширину ряда")


def test_chat_opened_from_account_dialogs_can_be_warmed_up_too():
    """Своих чатов в мини-аппе ДВА списка, и владелец спрашивал про «любой»
    (11.11.2026, трижды): каталог «Каналы и чаты» → карточка чата, и диалоги
    аккаунта → консоль чата. Кнопка была только в первом, поэтому для него
    вопрос выглядел незакрытым.

    В консоли чата кнопка живёт в шапке и показывается только для группы или
    канала: в личке и у бота разогревать нечего, а мёртвая кнопка хуже её
    отсутствия.
    """
    html = miniapp_html()
    hdr = html[html.index('id="s-accchat"'):]
    hdr = hdr[:hdr.index('class="sub-sb"')]   # вся шапка экрана консоли чата
    assert 'id="achat-warm"' in hdr, "в шапке консоли чата нет кнопки разогрева"
    assert "startChatWarmup(" in hdr, "кнопка в шапке ничего не запускает"

    f = js_function("openAccountChat")
    assert "ACC_DIALOG_KIND[peer]" in f, "видимость кнопки не зависит от вида диалога"
    assert "'group'" in f and "'channel'" in f, (
        "разогрев предлагается в личке или боту, где он невозможен")
    assert "ACC_DIALOG_USER[peer]" in f, "в консоли не берётся @ник чата"
    assert "dg.username" in js_function("openAccountDialogs"), (
        "список диалогов не запоминает @ник — разогреву нечего передать")


def test_warmup_without_a_public_link_explains_instead_of_failing():
    """Вступить в чат флот может только по публичной ссылке или инвайту: по
    внутреннему id чужой аккаунт чат не найдёт. Раньше кнопка отправляла на
    сервер числовой id и запуск падал непонятной ошибкой.
    """
    f = js_function("startChatWarmup")
    head = f[:f.index("askConfirm(")]
    assert "openChatWarmup()" in head, (
        "без ссылки кнопка — тупик: не открывает экран, где ссылку можно вставить")
    assert "#" not in f.split("chat_ref")[1].split("\n")[0], (
        "на сервер по-прежнему уходит внутренний id вместо @ника")
