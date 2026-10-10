"""Мини-апп: инвайт из хранилища контактов не только «весь список» и сегменты,
но и быстрые срезы — избранные и по тегу — без предварительного сегмента.

Бэкенд инвайта уже умел source=segment с segment_filters (исполнитель их читает,
_segment_where поддерживает tag/favorite_only). Не хватало трёх вещей, и все три
здесь под охраной:
  • превью аудитории считало только сохранённый сегмент, а быстрый срез — нет
    (счётчик показал бы «весь список», а операция взяла бы срез — врёт точностью);
  • не было среза быстрых опций (избранные + топ-тегов);
  • фронт не давал их выбрать.

Ключевой инвариант: превью и запуск разбирают выбор ОДНОЙ функцией (_inviteSegSel),
иначе показанное число разойдётся с приглашёнными.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
# Мини-апп больше не один файл: экраны вынесены в mini_app/screens/*.js.
# Источник берём целиком, иначе вынос экрана роняет проверку, хотя
# функциональность на месте.
from tests.miniapp_source import miniapp_source

HTML = miniapp_source()


# ── бэкенд ───────────────────────────────────────────────────────────────────

def test_segment_options_route_registered():
    assert '"/api/miniapp/invite/segment_options"' in API, (
        "эндпоинт быстрых срезов не зарегистрирован — фронт стучался бы в никуда")


def test_segment_options_handler_exists_and_uses_segment_engine():
    m = re.search(r"    async def invite_segment_options\(request.*?\n    async def ",
                  API, re.DOTALL)
    assert m, "обработчик быстрых срезов не найден"
    body = m.group(0)
    # Счёт — тем же движком сегментов, что и превью/исполнитель.
    assert "count_segment" in body, "срезы считаются мимо движка сегментов"
    assert "favorite_only" in body, "нет счётчика избранных"
    assert "unnest(tags)" in body, "нет топ-тегов"


def test_audience_counter_honours_segment_filters():
    """Превью обязано считать быстрый срез, а не только сохранённый сегмент."""
    m = re.search(r"    async def invite_audience_size\(request.*?\n    async def ",
                  API, re.DOTALL)
    assert m
    body = m.group(0)
    # ветка segment должна читать segment_filters из запроса
    seg = body[body.index('source == "segment"'):]
    # Требуем именно ЧТЕНИЕ параметра, а не упоминание слова (комментарий бы
    # прошёл фальшиво): без реального query.get счётчик игнорирует срез.
    assert 'request.query.get("segment_filters")' in seg, (
        "счётчик аудитории не читает segment_filters — число соврёт для "
        "избранных/тега")


# ── фронт ────────────────────────────────────────────────────────────────────

def test_single_parser_for_segment_selection():
    """Разбор выбора сегмента — ОДНА функция, и превью, и запуск зовут её.

    Если превью и submit разберут выбор по-разному, оператор увидит одно число,
    а пригласит другое — тот самый «врёт с видом точности».
    """
    assert "function _inviteSegSel(" in HTML, "нет единого разбора выбора сегмента"
    # оба пути обязаны идти через него
    aud = HTML[HTML.index("async function loadInviteAudienceSize"):]
    aud = aud[:aud.index("async function", 1)]
    assert "_inviteSegSel()" in aud, "превью аудитории не через единый разбор"
    sub = HTML[HTML.index("async function submitMassInvite"):]
    sub = sub[:sub.index("async function", 1)]
    assert "_inviteSegSel()" in sub, "запуск инвайта не через единый разбор"


def test_parser_maps_fav_and_tag_and_saved():
    """_inviteSegSel обязан различать избранные, тег и сохранённый сегмент."""
    m = re.search(r"function _inviteSegSel\(\)\s*\{.*?\n\}", HTML, re.DOTALL)
    assert m, "функция разбора не найдена"
    fn = m.group(0)
    assert "favorite_only" in fn, "избранные не распознаются"
    assert "tag:" in fn and "segment_filters" in fn, "тег не распознаётся"
    assert "saved_segment_id" in fn, "сохранённый сегмент не распознаётся"


def test_dropdown_offers_favorites_and_tags():
    seg = HTML[HTML.index("async function loadInviteSegments"):]
    seg = seg[:seg.index("async function", 1)]
    assert "invite/segment_options" in seg, "дропдаун не тянет быстрые срезы"
    assert 'value="fav"' in seg, "нет опции «Избранные»"
    assert 'value="tag:' in seg, "нет опций по тегу"


# ── живой Postgres: срезы считаются верно на реальной схеме ──────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
def test_segment_counts_on_real_schema():
    import asyncio
    import asyncpg
    from services.contacts_hub import repository as repo

    async def _run():
        conn = await asyncpg.connect(DSN)
        try:
            with open(ROOT / "schema_v138.sql", encoding="utf-8") as f:
                await conn.execute(f.read())
            await conn.execute("DELETE FROM unified_contacts WHERE owner_id=$1", 9100)
            await conn.execute(
                "INSERT INTO unified_contacts(owner_id, username, tags, is_favorite) VALUES "
                "($1, 'a', ARRAY['vip'], TRUE),"
                "($1, 'b', ARRAY['vip','cold'], FALSE),"
                "($1, 'c', ARRAY['cold'], FALSE)",
                9100)

            # count_segment — тот же движок, что и в эндпоинте быстрых срезов.
            all_n = await repo.count_segment(conn, 9100, {})
            fav_n = await repo.count_segment(conn, 9100, {"favorite_only": True})
            vip_n = await repo.count_segment(conn, 9100, {"tag": "vip"})
            assert all_n == 3
            assert fav_n == 1, "избранные посчитаны неверно"
            assert vip_n == 2, "срез по тегу посчитан неверно"

            # топ-тегов (тот же запрос, что в эндпоинте)
            rows = await conn.fetch(
                "SELECT t AS tag, COUNT(*) AS cnt FROM unified_contacts, unnest(tags) AS t "
                "WHERE owner_id=$1 GROUP BY t ORDER BY cnt DESC, t LIMIT 8", 9100)
            counts = {r["tag"]: r["cnt"] for r in rows}
            assert counts == {"vip": 2, "cold": 2}
        finally:
            await conn.execute("DELETE FROM unified_contacts WHERE owner_id=$1", 9100)
            await conn.close()

    asyncio.run(_run())
