"""Аналитика аудитории: сегмент открывает людей, инсайт — читается.

Два разрыва на одном экране. Первый: «Спящие · 1 240» ни во что не вели —
увидеть этих людей или забрать их номера для рассылки было нечем, хотя
условие сегмента уже посчитано. Второй: инсайты приходили с ключом `text`,
а экран читал `description`, поэтому каждый инсайт показывался одним
заголовком с пустой строкой под ним.

Условия сегментов теперь ОДИН источник: из них строится и запрос со
счётчиками, и выборка людей. Разъехавшиеся копии порогов этот модуль уже
однажды ломали — тогда сегмент становился недостижимым.
"""
from __future__ import annotations

import os
import re

from services import audience_analytics as aa

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()


def _fn(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена"
    i = HTML.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:j + 1]
    raise AssertionError(name)


def test_counting_and_listing_share_one_condition():
    """Счётчик сегмента и список его людей строятся из одного условия."""
    for key, cond in aa.SEGMENT_PREDICATES.items():
        assert cond in aa._SEGMENTS_SQL, f"условие {key} не попало в запрос счётчиков"
        members = aa.segment_members_sql(key, 10)
        # В выборке людей пороги подставлены числами, но условие то же.
        inlined = cond
        for ph, val in aa._SEG_PARAM_VALUES.items():
            inlined = inlined.replace(ph, str(int(val)))
        assert inlined in members, f"условие {key} разъехалось со списком"


def test_member_query_has_no_dangling_parameters():
    """Postgres откажется от запроса, которому передали лишний параметр."""
    for key in aa.SEGMENT_PREDICATES:
        sql = aa.segment_members_sql(key, 10)
        used = set(re.findall(r"\$(\d+)", sql))
        assert used == {"1", "2"}, f"{key}: в запросе параметры {sorted(used)}"


def test_every_segment_has_a_russian_name():
    assert set(aa.SEGMENT_NAMES) == set(aa.SEGMENT_PREDICATES)
    for name in aa.SEGMENT_NAMES.values():
        assert re.search(r"[А-Яа-яё]", name), name


def test_unknown_segment_returns_nothing():
    assert aa.segment_members_sql.__doc__
    try:
        aa.segment_members_sql("нет такого", 10)
    except KeyError:
        pass
    else:
        raise AssertionError("неизвестный ключ должен падать, а не строить запрос")


def test_route_is_registered_and_checks_the_key():
    assert '"/api/miniapp/audience_analytics/segment", audience_segment_members' in API
    m = re.search(r"\n    async def audience_segment_members\(request", API)
    assert m
    h = API[m.start():API.find("\n    async def ", m.end())]
    assert "SEGMENT_PREDICATES" in h, "ключ из запроса не проверяется"
    assert 'return _err("Неизвестный сегмент", 400)' in h
    assert "mb.added_by=$1" in h, "имена подтянутся из чужих ботов"


def test_segment_row_opens_its_people():
    r = _fn("renderAudSegments")
    assert "openAudSegment(" in r, "сегмент никуда не ведёт"
    o = _fn("openAudSegment")
    assert "audience_analytics/segment?key=" in o
    assert "audSegCopyIds(" in o, "номера нельзя забрать"
    assert "Пользователь " in o, "вместо имени останется голый номер"


def test_segment_key_reaches_the_screen():
    """По русскому имени маршрут не найти — экрану нужен ключ."""
    assert '"key": _key_by_name.get(sg.name)' in API


def test_insights_are_actually_readable():
    """Бэкенд клал text, экран читал description — подпись всегда пустая."""
    i = API.index("async def audience_analytics")
    chunk = API[i:API.index("async def audience_segment_members")]
    assert '"text":' not in chunk, "инсайт снова уедет в несуществующий ключ"
    assert chunk.count('"description":') >= 3
    assert "esc(i.description || '')" in _fn("renderAudInsights")


def test_chip_filter_does_not_refetch_everything():
    f = _fn("filterAudSegment")
    assert "loadAudienceAnalytics()" not in f, "фильтр чипа снова ходит в сеть"
    assert "renderAudSegments(" in f


def test_segment_names_do_not_go_into_onclick():
    """Имя сегмента — данные, а в атрибуте onclick это код."""
    c = _fn("renderAudSegmentChips")
    assert "filterAudSegment('" not in c, c
    assert re.search(r"filterAudSegment\(\$\{i\}", c), c
