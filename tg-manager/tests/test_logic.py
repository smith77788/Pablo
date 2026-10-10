"""Регресс-тесты чистой логики (классификаторы, парсинг), без БД/Telethon."""
from __future__ import annotations


def test_detect_niches_basic():
    from services.ad_intelligence import detect_niches
    assert detect_niches("Крипто Новости", ["Купи bitcoin и токены", "NFT дропы блокчейн"]) == ["крипта"]
    assert detect_niches("Лучшее казино", ["ставки на спорт слоты"]) == ["гемблинг"]
    assert detect_niches("", ["просто текст без маркеров"]) == []


def test_detect_niches_multi_ranked():
    from services.ad_intelligence import detect_niches
    res = detect_niches("Инвест канал", ["крипта bitcoin эфир", "инвестиции forex дивиденд", "курс обучение вебинар"])
    assert set(res) >= {"финансы", "крипта", "обучение"}
    assert len(res) <= 5  # max_niches


def test_detect_niches_max_limit():
    from services.ad_intelligence import detect_niches
    text = "крипта казино инвест заработок курс здоровье недвижимость авто бьюти нейросеть маркетплейс эскорт"
    assert len(detect_niches("", [text], max_niches=3)) == 3


def test_failed_channel_ids_parsing():
    """Логика извлечения упавших channel_id из operation_log (mass_publish retry).

    target хранит channel_id строкой (в т.ч. отрицательные) — берём только числовые,
    уникальные, с сохранением порядка.
    """
    rows = [
        {"target": "1001"}, {"target": "-1002"}, {"target": "1001"},
        {"target": "My Channel Title"}, {"target": ""}, {"target": "2003"},
    ]
    failed = []
    for r in rows:
        t = (r["target"] or "").strip().lstrip("-")
        if t.isdigit():
            failed.append(int(r["target"]))
    failed = list(dict.fromkeys(failed))
    assert failed == [1001, -1002, 2003]  # titles/пустые отброшены, дубли убраны


def test_insert_result_row_count():
    """Разбор статуса pool.execute ('INSERT 0 1' → вставлено; '0 0' → дубликат)."""
    def inserted(result: str) -> bool:
        return str(result).split()[-1] != "0"
    assert inserted("INSERT 0 1") is True
    assert inserted("INSERT 0 0") is False
    assert inserted("UPDATE 3") is True
