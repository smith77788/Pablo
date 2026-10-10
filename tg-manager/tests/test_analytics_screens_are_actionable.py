"""Аналитические витрины: граф, разведка рекламы, сеть ботов.

Три экрана были списками чисел и строк, по которым нельзя нажать: «Нодов 12»
(сырое слово, непонятно о чём), пара пересекающихся каналов без возможности их
открыть, роль бота по-английски («Entry», «Conversion») и строка бота, не
ведущая к самому боту. Пустые состояния разведки были тупиками.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()


def _fn(name: str, js: str = HTML) -> str:
    m = re.search(r"(?:async )?function %s\s*\([^)]*\)\s*\{" % re.escape(name), js)
    assert m, f"нет функции {name}"
    i = m.end() - 1
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[i:j + 1]
    raise AssertionError(name)


def _no_comments(js: str) -> str:
    return "\n".join(ln for ln in js.splitlines() if not ln.strip().startswith("//"))


# ── Общий переход к внешнему объекту ─────────────────────────────────────────

def test_external_object_can_be_opened():
    body = _no_comments(_fn("openTgUser"))
    assert "https://t.me/" in body
    assert "openTelegramLink" in body
    assert "copyToClipboard" in body, "без Telegram-клиента ссылку нечем забрать"
    assert "toast(" in body, "пустое имя должно объясняться, а не молчать"


# ── Граф-хаб ─────────────────────────────────────────────────────────────────

def test_graph_numbers_are_explained_and_lead_somewhere():
    body = _no_comments(_fn("openGraph"))
    assert "Нодов" not in body, "сырое слово «Нодов» осталось на экране"
    assert "Моих каналов в графе" in body
    assert "goTab('channels')" in body
    # У каждого числа есть пояснение, что оно значит.
    assert "Связей между каналами" in body
    assert "сколько раз ваши каналы" in body


def test_graph_overlap_pairs_can_be_opened():
    body = _no_comments(_fn("openGraph"))
    assert body.count("openTgUser(") == 2, "обе стороны пары должны открываться"
    assert "r.username_a" in body and "r.username_b" in body
    # Процент остаётся долей → процентами: единица измерения не поехала.
    assert "(r.overlap_pct||0)*100" in body


# ── Разведка рекламы ─────────────────────────────────────────────────────────

def test_ad_intel_rows_open_the_channel():
    body = _no_comments(_fn("openAdIntel"))
    assert body.count("openTgUser(") == 2, "ни канал, ни рекламодатель не открываются"
    assert "c.username?' tap'" in body or "c.username ? ' tap'" in body


def test_ad_intel_empty_states_are_not_dead_ends():
    body = _no_comments(_fn("openAdIntel"))
    assert "Каналов на разведке нет" in body
    assert "openAdIntelModal()" in body
    assert "Рекламодателей пока нет" in body
    # Два разных случая: каналов нет вовсе и реклама ещё не попадалась.
    assert "chs.length ?" in body


def test_ad_intel_error_clears_both_lists():
    """Ошибка оставляла список рекламодателей со старыми строками."""
    body = _no_comments(_fn("openAdIntel"))
    tail = body[body.rindex("catch"):]
    assert "errHtml" in tail
    assert "adIntelAdv" in tail


# ── Сеть ботов ───────────────────────────────────────────────────────────────

def test_bot_roles_are_russian():
    m = re.search(r"const ROLE_LABELS = \{(.*?)\};", HTML, re.S)
    assert m, "нет ROLE_LABELS"
    labels = m.group(1)
    for eng in ("Entry", "Conversion", "Retention", "General"):
        assert eng not in labels, f"роль «{eng}» осталась по-английски"
    for ru in ("Вход", "Конверсия", "Удержание", "Общий"):
        assert ru in labels, ru


def test_network_bot_row_leads_to_the_bot():
    body = _no_comments(_fn("openNetwork"))
    assert "openBot(" in body
    assert "event.stopPropagation();openNetRoleModal" in body, \
        "кнопка роли не должна открывать карточку бота вместе с собой"
    assert "⚡ в рою" in body and "swarm ×" not in body


def test_network_shows_swarm_count_and_it_leads_to_swarm():
    body = _no_comments(_fn("openNetwork"))
    assert "В рое" in body
    assert "openSwarm()" in body
    assert "b.swarm" in body


def test_network_cluster_header_says_how_many():
    body = _no_comments(_fn("openNetwork"))
    assert "plural((cl.bots||[]).length" in body


def test_all_targets_exist():
    for name in ("openTgUser", "openBot", "openSwarm", "openAdIntelModal",
                 "openNetRoleModal", "goTab", "plural"):
        assert re.search(r"(?:async )?function %s\s*\(" % name, HTML), f"нет {name}"

def test_ad_price_says_its_unit():
    """Цена размещения считается в Telegram Stars, а показывалась голым числом:
    «~15,0К» читается как рубли и завышает смету в разы."""
    body = _no_comments(_fn("openAdIntel"))
    assert "размещение ~" in body and "⭐" in body
    est = open(os.path.join(ROOT, "services", "ad_intelligence.py"), encoding="utf-8").read()
    assert "в Stars" in est, "единица измерения цены изменилась — проверьте подпись в UI"
