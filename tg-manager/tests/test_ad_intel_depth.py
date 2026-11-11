"""Разведка рекламы: честные метрики, разбор рекламы и рекламодателя.

Что было не так (каждый пункт падает на прежнем коде):

* охват (просмотры ÷ подписчики) писался в `er_rate` и подписывался «ER»;
  настоящей вовлечённости (реакции, репосты, комментарии) не было вовсе;
* рекламодателем бралось первое @упоминание — часто это сам канал;
* одно слово «подписывайтесь» или «скидка» делало пост рекламой;
* ссылка в кнопке под постом и скрытая ссылка в тексте не видны совсем,
  хотя реклама обычно ведёт именно туда;
* цена — «звёзды» по выдуманной формуле, а рынок считает рубли за CPM.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services import ad_intelligence as ai


def test_own_channel_is_never_the_advertiser():
    sig = ai.analyze_ad_post(
        "Подпишись на @mychan! Реклама. Лучший VPN: t.me/fastvpn_bot",
        own_username="mychan")
    assert sig.is_ad
    assert sig.advertiser == "fastvpn_bot"
    assert sig.advertiser_kind == "bot"


def test_weak_words_alone_are_not_an_ad():
    for text in ("Подписывайтесь на наш канал, впереди много интересного",
                 "Скидка в кофейне у дома закончилась, жаль",
                 "Переходите в комментарии, обсудим"):
        assert not ai.analyze_ad_post(text, own_username="mychan").is_ad, text


def test_button_url_and_hidden_link_count():
    sig = ai.analyze_ad_post(
        "Заберите бесплатный гайд — жмите кнопку",
        own_username="mychan", buttons=["https://shop.example.ru/promo?utm=tg"])
    assert sig.is_ad and sig.advertiser == "shop.example.ru"
    assert sig.advertiser_kind == "site"
    sig2 = ai.analyze_ad_post(
        "Переходи и забирай подарок", own_username="mychan",
        links=["https://t.me/gift_channel"])
    assert sig2.advertiser == "gift_channel" and sig2.advertiser_kind == "channel"


def test_erid_is_certain_and_invite_link_is_named():
    sig = ai.analyze_ad_post("Курс по Python. erid: 2SDnjeXyz12 t.me/+AbCdEf123",
                             own_username="mychan")
    assert sig.is_ad and sig.has_erid and sig.confidence == 100
    assert sig.advertiser_kind == "invite" and sig.advertiser.startswith("+")


def test_reach_and_engagement_are_different_numbers():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    posts = [ai.PostStat(views=1000 + i * 37, reactions=20, forwards=5, replies=5,
                         date=now - timedelta(days=i), is_ad=(i % 5 == 0))
             for i in range(20)]
    m = ai.channel_metrics(posts, subscribers=4000)
    assert 0.25 < m["reach_rate"] < 0.35          # ~1350 / 4000
    assert 0.02 < m["engagement_rate"] < 0.03     # 30 / ~1350
    assert m["ads_per_week"] > 1
    assert not m["fraud_flags"]


def test_view_cheating_is_flagged_and_lowers_quality():
    posts = [ai.PostStat(views=9000, reactions=0, forwards=0, replies=0)
             for _ in range(15)]
    m = ai.channel_metrics(posts, subscribers=6000)
    assert "просмотров больше, чем подписчиков" in m["fraud_flags"]
    assert "просмотры почти одинаковые у всех постов" in m["fraud_flags"]
    clean = ai.score_quality(6000, 0.4, 0.03, [], 0.1)
    dirty = ai.score_quality(6000, m["reach_rate"], m["engagement_rate"],
                             m["fraud_flags"], m["ad_share"])
    assert dirty < clean


def test_price_is_rubles_by_niche_cpm():
    assert ai.estimate_ad_price_rub(10_000, ["крипта"]) == 16_000
    assert ai.estimate_ad_price_rub(10_000, []) == 9_000
    assert ai.estimate_ad_price_rub(0, ["крипта"]) == 0


def test_sorts_come_only_from_the_whitelist():
    # строка с экрана в ORDER BY не попадает: только ключи словаря
    for v in ai.CHANNEL_SORTS.values():
        assert ";" not in v and "$" not in v
    assert ai._like("50%_x") == "%50\\%\\_x%"
