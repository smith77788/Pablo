"""Разведка рекламы: показанное во вкладке выгружается в CSV — доли в
процентах как на экране, ссылки на каналы, кавычки в тексте экранированы,
а «Показать ещё» дополняет выгрузку, не начиная её заново."""
import json
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mini_app/screens/ad_intel.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", SRC)
    assert m, name
    return m.group(0)


def _csv(tab, items):
    js = _fn("adIntelCsv") + "console.log(JSON.stringify(adIntelCsv(%s,%s)))" % (json.dumps(tab), json.dumps(items))
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout).split("\r\n")


def test_channels_csv_uses_percent_and_links():
    lines = _csv("channels", [{"title": "Новости", "username": "news", "subscribers": 1200, "reach_rate": 0.35,
                                "engagement_rate": 0.042, "ads_per_week": 1.5, "ad_price_rub": 900, "quality_score": 71.6}])
    assert lines[0].startswith('"Канал";"Ссылка";"Подписчики";"Охват, %"')
    assert lines[1] == '"Новости";"https://t.me/news";"1200";"35,0";"4,2";"1,5";"900";"72";""'


def test_ads_csv_escapes_quotes_and_flattens_text():
    lines = _csv("ads", [{"date": "2026-11-20", "channel": "news", "advertiser": "shop", "views": 10, "er": 0.1,
                          "has_erid": True, "text": 'Скидка "50%"\nсегодня'}])
    assert lines[1].endswith('"да";"Скидка ""50%"" сегодня"')
    assert _csv("nope", []) == [""]


def test_load_accumulates_for_more_and_resets_otherwise():
    load = _fn("adIntelLoad")
    assert "adIntelLoad.items = (append && adIntelLoad.tab === tab ? adIntelLoad.items || [] : []).concat(items);" in load
    assert "adIntelExportCsv()" in load
