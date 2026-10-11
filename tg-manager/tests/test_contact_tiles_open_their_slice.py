"""Шесть плиток хранилища контактов показывали числа и ничего не открывали.

Экран «📇 Контакты» рисовал полосу счётчиков — «Всего», «⭐ Избр.»,
«💎 Premium», «📱 Телефон», «С ником», «↔ Взаимные» — обычным текстом. Срезы под
большинством из них на сервере УЖЕ были (`favorite`, `premium`, `mutual`), а под
телефоном и ником не было вообще: условие жило только в счётчике
(`get_contact_stats`), и открыть «812 с телефоном» было негде.

Отдельно: фильтр «контакты этого аккаунта» фронт отправлял (`account_id`),
репозиторий понимал, а обработчик списка его не читал — экран обещал контакты
одного аккаунта и отдавал весь справочник. Тот же класс, что срез «Прокси
мёртв», который молча превращался во весь флот.

Поэтому проверка здесь ВЫЧИСЛЯЕТСЯ, а не перечисляется руками: берём все
параметры фильтра, которые фронт реально умеет отправить из `loadContacts`, и
требуем, чтобы каждый читался обработчиком и доходил до репозитория. Добавят
седьмую плитку и забудут про сервер — красный тест, а не число, которое никуда
не ведёт.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _js_function(src: str, name: str) -> str:
    """Тело JS-функции: от её объявления до следующего объявления.

    Срез фиксированной длины рвёт функцию пополам и однажды начинает лгать
    («искомого нет») просто потому, что код сдвинулся.
    """
    i = src.index("function " + name + "(")
    nxt = re.search(r"\n(?:async function|function) \w+\(", src[i + 10:])
    return src[i:i + 10 + nxt.start()] if nxt else src[i:]


def _filter_params(src: str) -> dict[str, str]:
    """{параметр запроса: значение CONTACT_FILTER} из loadContacts."""
    body = _js_function(src, "loadContacts")
    out = {}
    for m in re.finditer(
            r"CONTACT_FILTER === '(\w+)'\)\s*params\.set\('(\w+)'", body):
        out[m.group(2)] = m.group(1)
    return out


def _handler(src: str, name: str) -> str:
    i = src.index("async def " + name + "(")
    nxt = re.search(r"\n    async def \w+\(", src[i:])
    return src[i:i + nxt.start()] if nxt else src[i:]


# ─── Пробники: измеритель должен видеть заведомо плохой пример ───

_BAD_JS = """
async function loadContacts(append=false) {
  const params = new URLSearchParams();
  if (CONTACT_FILTER === 'favorite') params.set('favorite', '1');
  if (CONTACT_FILTER === 'beard') params.set('with_beard', '1');
}
function other() {}
"""


def test_probe_reads_the_filters_the_front_can_send():
    live = _filter_params(_read("mini_app/index.html"))
    assert live, "детектор не видит НИ ОДНОГО фильтра — проверка была бы пустой"
    assert live.get("favorite") == "favorite", live
    bad = _filter_params(_BAD_JS)
    assert bad.get("with_beard") == "beard", (
        "детектор не замечает нового фильтра — значит и забытый не заметит")


def test_probe_cuts_the_function_by_the_next_definition():
    body = _js_function(_BAD_JS, "loadContacts")
    assert "with_beard" in body and "function other" not in body


# ─── Сама проверка ───

def test_every_filter_the_front_sends_is_read_by_the_list_handler():
    api = _read("services/mini_app_api.py")
    handler = _handler(api, "uch_contacts")
    for param, filt in sorted(_filter_params(_read("mini_app/index.html")).items()):
        assert f"request.query.get('{param}')" in handler, (
            f"фронт отправляет {param}=1 (плитка/чип «{filt}»), а обработчик "
            f"списка его не читает — экран покажет ВЕСЬ справочник вместо среза")


def test_account_slice_is_not_silently_the_whole_vault():
    """Фильтр по аккаунту: фронт отправлял, сервер игнорировал."""
    api = _read("services/mini_app_api.py")
    handler = _handler(api, "uch_contacts")
    assert "request.query.get('account_id')" in handler, (
        "срез «контакты этого аккаунта» снова молча отдаёт весь справочник")
    assert "account_id=account_id" in handler, (
        "параметр прочитан, но в репозиторий не передан")


def test_segment_where_knows_the_new_slices():
    """Срез строит ОДИН конструктор — им же пользуются рассылки и инвайты."""
    from services.contacts_hub import repository as repo

    where, params, _ = repo._segment_where(7, {"with_phone_only": True})
    assert repo.HAS_PHONE_SQL in where, where
    assert params == [7]
    where, _, _ = repo._segment_where(7, {"with_username_only": True})
    assert repo.HAS_USERNAME_SQL in where, where
    # Без фильтра условие не навязывается.
    where, _, _ = repo._segment_where(7, {})
    assert repo.HAS_PHONE_SQL not in where and repo.HAS_USERNAME_SQL not in where


def test_counter_and_slice_share_one_condition():
    """Главная поломка: число на плитке и открытый список могли разойтись."""
    src = _read("services/contacts_hub/repository.py")
    i = src.index("async def get_contact_stats")
    stats = src[i:src.index("\nasync def ", i + 10)]
    assert "HAS_USERNAME_SQL" in stats and "HAS_PHONE_SQL" in stats, (
        "счётчик снова пишет условие своими словами — разойдётся со срезом молча")
    assert "username IS NOT NULL'" not in stats, (
        "вернулось старое условие счётчика, которого нет у среза")


def test_new_slices_survive_a_saved_segment():
    """Сегмент — сущность продукта: срез, сохранённый с экрана, должен ожить."""
    from services.contacts_hub import repository as repo

    kept = repo._clean_filters({"with_phone_only": True, "premium_only": False})
    assert kept == {"with_phone_only": True}, (
        "новый срез нельзя сохранить сегментом — фильтр теряется при сохранении")


def test_each_tile_points_at_a_filter_the_list_has_a_chip_for():
    html = _read("mini_app/index.html")
    body = _js_function(html, "loadContactStats")
    tiles = re.findall(r"contactsKpiCard\([^,]+,[^,]+,[^,]+,\s*(null|'(\w+)')", body)
    assert len(tiles) >= 6, f"плиток стало меньше шести: {tiles}"
    chips = set(re.findall(r'seg-opt[^>]*data-f="(\w+)"', html))
    for raw, name in tiles:
        want = "all" if raw == "null" else name
        assert want in chips, (
            f"плитка открывает срез {want!r}, а чипа с ним в фильтре нет — "
            f"срез нельзя будет снять")


def test_tiles_are_tappable_and_say_so():
    body = _js_function(_read("mini_app/index.html"), "contactsKpiCard")
    assert "tap-card" in body, "плитка не выглядит нажимаемой"
    assert "onclick=" in body and "contactsSlice(" in body, (
        "число снова ни во что не ведёт")
    assert "›" in body, "у плитки нет признака перехода"
