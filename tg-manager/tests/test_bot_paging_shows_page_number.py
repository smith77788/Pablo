"""Листание в боте показывает номер страницы, а раскладка не путает строки."""
from bot.utils.paging import page_label


def test_page_label():
    assert page_label(0, 5, 10) is None
    assert page_label(0, 10, 10) is None
    assert page_label(0, 11, 10) == "📄 1/2"
    assert page_label(6, 70, 10) == "📄 7/7"
    assert page_label(99, 70, 10) == "📄 7/7"
    assert page_label(0, 3, 0) is None


def test_paged_screens_use_it():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "bot/handlers"
    for name, needle in [
        ("infra_analytics.py", "page_label(page, total, _PAGE)"),
        ("graph_hub.py", "page_label(page, len(overlaps), per_page)"),
        ("audience_parser.py", "page_label(page, total, _PAGE_SIZE)"),
        ("bot_factory.py", "page_label(page, len(bots), PAGE_SIZE)"),
    ]:
        src = (root / name).read_text(encoding="utf-8")
        assert needle in src, name
        assert "from bot.utils.paging import page_label" in src, name
    bf = (root / "bot_factory.py").read_text(encoding="utf-8")
    assert bf.count("page_label(page, len(bots), PAGE_SIZE)") == 2
    ap = (root / "audience_parser.py").read_text(encoding="utf-8")
    assert "kb.adjust(2, 1, 1)" not in ap.split("def cb_parser_export")[0].split("page_label(page, total, _PAGE_SIZE)")[1]
