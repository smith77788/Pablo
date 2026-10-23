"""Выгрузка умеет то, что видно: срез, отмеченных и один контакт.

НАХОДКА (23.10.2026). Экспорт хаба умел РОВНО «все контакты». Фильтр хаба —
поиск, тег, стадия, группа, аккаунт, пол, личные — на выгрузку не влиял
никак: владелец, собравший нужный срез, получал вместо него весь справочник
и отбирал нужных руками в скачанном файле. Три почти одинаковых обработчика
(csv/vcf/json) не принимали ни одного параметра.

Ручка на отмеченных при этом в API была — POST /uch/bulk/export, — и её не
звал ни один экран: «выгрузить отмеченных» в интерфейсе не существовало. Она
удалена как вторая реализация той же выгрузки; теперь всё идёт одним путём,
GET /uch/export/{fmt}: без параметров — весь хаб, с `ids=` — отмеченные (или
один контакт из карточки), с фильтрами списка — текущий срез.

ПОЧЕМУ РАЗБОР ФИЛЬТРОВ ОБЩИЙ. «Вижу одно — выгружаю другое» вернулось бы на
первой же правке фильтра, если бы экспорт разбирал параметры своим кодом.
Поэтому на сервере один `_contact_filters`, на экране — один
`contactFilterParams()`, и оба зовутся и списком, и выгрузкой.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся, не предполагался):
  1. убрать проверку `_UUID_RE` в `_export_target_ids`
     -> красный test_the_ids_are_checked_before_the_query,
        test_a_malformed_id_is_refused_before_the_query;
  2. заставить `_export_target_ids` всегда отдавать «весь хаб»
     -> красные три теста среза (срез, потолок, пустой срез);
  3. снять потолок `_EXPORT_SLICE_CAP`
     -> красный test_a_slice_over_the_cap_is_refused_before_the_export;
  4. заменить `contactFilterParams()` в `uchExport` на пустые параметры
     -> красный test_the_screen_builds_filters_in_one_place.

Контроли 2 и 3 в первой редакции этого файла не краснели НИЧЕГО: тесты читали
исходник вместо того, чтобы звать ручку. Поэтому ниже появились живые проверки
на заглушке. Первый же их прогон поймал мою собственную ошибку: параметр
фильтра стадии зовётся `crm_stage`, а не `stage`, — поиск по тексту такого не
видит в принципе.
"""
from __future__ import annotations

from tests.miniapp_source import js_function, miniapp_html, py_function


def test_the_server_parses_list_filters_in_one_place():
    fn = py_function("services/mini_app_uch.py", "uch_contacts")
    assert "_contact_filters(request)" in fn, (
        "список разбирает параметры своим кодом — выгрузка разойдётся с ним")
    target = py_function("services/mini_app_uch.py", "_export_target_ids")
    assert "_contact_filters(request)" in target, (
        "выгрузка среза считает фильтры отдельно от списка")
    assert "get_contacts" in target, (
        "срез для выгрузки отбирается не тем же запросом, что список")


def test_the_screen_builds_filters_in_one_place():
    body = js_function("loadContacts")
    assert "contactFilterParams()" in body, "список собирает параметры своим кодом"
    exp = js_function("uchExport")
    assert "contactFilterParams()" in exp, (
        "выгрузка среза собирает параметры отдельно от списка")


def test_the_export_screen_offers_the_slice_and_everything():
    body = js_function("openUchExport")
    assert "Текущий срез" in body, "выгрузить текущий срез негде"
    assert "slice:true" in body.replace(" ", ""), "кнопки среза не передают срез"
    assert "Все контакты" in body, "выгрузка всего хаба пропала"


def test_the_selection_and_the_card_can_be_exported():
    html = miniapp_html()
    assert "bulkExportSelected" in html, "выгрузить отмеченных негде"
    assert "uchExportSelected" in js_function("bulkExportSelected")
    sel = js_function("uchExportSelected")
    assert "CONTACT_SELECTED" in sel, "в выгрузку уходят не отмеченные"
    assert "Никто не выбран" in sel, "на пустом выборе откроется пустой файл"
    assert "cdExportVcard()" in html, "визитку одного контакта негде скачать"
    assert "'vcf'" in js_function("cdExportVcard")


def test_a_too_big_slice_is_refused_with_a_number():
    """Молча обрезанная выгрузка — это файл, в котором чего-то нет.

    Отказ сервера открылся бы в браузере как JSON, поэтому про размер экран
    говорит сам — числом, которое владелец только что видел в списке.
    """
    exp = js_function("uchExport")
    assert "UCH_EXPORT_CAP" in exp, "предела выгрузки нет"
    assert "CONTACT_SLICE_TOTAL" in exp, "экран не знает, сколько в срезе"
    assert "Сузьте фильтр" in exp, "отказ не говорит, что делать"
    target = py_function("services/mini_app_uch.py", "_export_target_ids")
    assert "_EXPORT_SLICE_CAP" in target, "сервер готов выгрузить любой объём"
    assert "Сузьте фильтр" in target, "сервер отказывает без подсказки"


def test_the_ids_are_checked_before_the_query():
    """Одна нестрогая строка в списке id роняет ВСЮ выгрузку.

    contact_id — uuid, asyncpg биндит его без каста: на мусоре в списке
    Postgres отбивает запрос целиком, то есть владелец получает не «часть
    выгрузки», а пустой отказ.
    """
    target = py_function("services/mini_app_uch.py", "_export_target_ids")
    assert "_UUID_RE" in target, "id не проверяются по форме"
    assert "Неверный идентификатор" in target


def test_there_is_one_export_path():
    import services.contacts_hub.export_engine as ex
    from services.contacts_hub import bulk_ops_engine as bulk

    src = open("services/mini_app_uch.py", encoding="utf-8").read()
    assert 'add_get("/api/miniapp/uch/export/{fmt}"' in src, "единой ручки нет"
    # Проверяем именно РЕГИСТРАЦИЮ ручки, а не подстроку: на месте удалённого
    # обработчика остался комментарий, в котором её путь назван словами.
    assert 'add_post("/api/miniapp/uch/bulk/export"' not in src, (
        "вторая ручка выгрузки вернулась")
    assert 'add_get("/api/miniapp/uch/export/csv"' not in src, (
        "вернулись ручки на формат")
    assert not hasattr(bulk, "bulk_export"), (
        "обёртка bulk_export вернулась: это второй вход в ту же выгрузку")
    for dead in ("export_vcard", "export_csv_streaming", "get_export_stats"):
        assert not hasattr(ex, dead), (
            f"{dead} вернулся: визитка и поток собирали поля своим кодом и "
            "расходились с настоящей выгрузкой")


def test_the_vcard_keeps_the_fields_the_deleted_one_had():
    """Свести две реализации в одну нельзя за счёт потери полей."""
    fn = py_function("services/contacts_hub/export_engine.py", "export_vcf")
    assert "URL:" in fn, "сайты пропали из визитки"
    assert "BDAY:" in fn, "день рождения пропал из визитки"


# ---------------------------------------------------------------------------
# ЖИВЫЕ ПРОВЕРКИ ПОВЕДЕНИЯ.
#
# Обратный контроль над тестами выше показал дыру в них самих: если сделать
# `_export_target_ids` всегда возвращающей «весь хаб» (срез игнорируется) или
# убрать потолок среза, НИ ОДИН из них не краснеет — они читают исходник, а не
# зовут ручку. Поиск по тексту годится, чтобы доказать, что фильтры разбираются
# в одном месте; он не доказывает, что выгрузка отдаёт именно срез. Поэтому
# ниже ручка собирается на заглушке и вызывается по-настоящему.
# ---------------------------------------------------------------------------

_SLICE_IDS = ["11111111-1111-4111-8111-111111111111",
              "22222222-2222-4222-8222-222222222222"]


def _export_app(monkeypatch, *, contacts=None, total=None):
    """Собрать приложение мини-аппа с подменёнными срезом и выгрузкой.

    Возвращает (app, calls): в calls попадает то, с какими id позвали движок
    выгрузки — то есть ответ на вопрос «выгрузили то, что видно?».
    """
    from aiohttp import web

    from services import mini_app_api, mini_app_uch
    from services.contacts_hub import export_engine
    from services.contacts_hub import repository

    monkeypatch.setattr(mini_app_api, "_get_uid",
                        lambda request: int(request.headers["X-Uid"])
                        if request.headers.get("X-Uid") else None)

    rows = [{"id": i} for i in (contacts if contacts is not None else _SLICE_IDS)]

    async def _fake_get_contacts(pool, uid, **kw):
        return {"contacts": rows,
                "total": total if total is not None else len(rows)}
    monkeypatch.setattr(repository, "get_contacts", _fake_get_contacts)

    calls = []

    async def _fake_export(pool, uid, ids=None):
        calls.append(ids)
        return b"name\n"
    for name in ("export_csv", "export_vcf", "export_json"):
        monkeypatch.setattr(export_engine, name, _fake_export)

    app = web.Application()
    mini_app_uch.setup_routes(app, object())
    return app, calls


def _get(app, path):
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.get(path, headers={"X-Uid": "7001"})
            return r.status, await r.text()
        finally:
            await client.close()
    return asyncio.new_event_loop().run_until_complete(go())


def test_a_filtered_export_really_gets_the_slice(monkeypatch):
    """С фильтром в запросе движок должен получить id среза, а не None."""
    app, calls = _export_app(monkeypatch)
    status, body = _get(app, "/api/miniapp/uch/export/csv?crm_stage=lead")
    assert status == 200, body
    assert calls == [_SLICE_IDS], (
        "выгрузка позвала движок не на срез: владелец видит фильтр, "
        f"а получает {calls}")


def test_an_unfiltered_export_still_takes_the_whole_hub(monkeypatch):
    app, calls = _export_app(monkeypatch)
    status, body = _get(app, "/api/miniapp/uch/export/csv")
    assert status == 200, body
    assert calls == [None], f"без фильтров выгрузка перестала брать весь хаб: {calls}"


def test_the_selected_ids_reach_the_engine(monkeypatch):
    app, calls = _export_app(monkeypatch)
    status, body = _get(
        app, "/api/miniapp/uch/export/vcf?ids=" + ",".join(_SLICE_IDS))
    assert status == 200, body
    assert calls == [_SLICE_IDS], f"отмеченные не доехали до движка: {calls}"


def test_a_slice_over_the_cap_is_refused_before_the_export(monkeypatch):
    app, calls = _export_app(monkeypatch, total=50000)
    status, body = _get(app, "/api/miniapp/uch/export/csv?crm_stage=lead")
    assert status == 400, body
    assert "50000" in body, f"отказ не называет размер среза: {body}"
    assert calls == [], "превышение потолка всё равно запустило выгрузку"


def test_a_malformed_id_is_refused_before_the_query(monkeypatch):
    app, calls = _export_app(monkeypatch)
    status, body = _get(app, "/api/miniapp/uch/export/json?ids=not-a-uuid")
    assert status == 400, body
    assert "Неверный идентификатор" in body, body
    assert calls == [], "мусорный id дошёл до запроса"


def test_an_empty_slice_says_so_instead_of_an_empty_file(monkeypatch):
    app, calls = _export_app(monkeypatch, contacts=[])
    status, body = _get(app, "/api/miniapp/uch/export/csv?crm_stage=lost")
    assert status == 400, body
    assert "нет контактов" in body, body
    assert calls == [], "пустой срез открылся бы пустым файлом"


def test_an_unknown_format_is_refused(monkeypatch):
    app, calls = _export_app(monkeypatch)
    status, body = _get(app, "/api/miniapp/uch/export/xlsx")
    assert status == 400, body
    assert calls == []
