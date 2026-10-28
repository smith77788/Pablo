"""Понятность «Модели поведения»: экран объясняет сам себя и ведёт к действию.

Жалоба владельца: «ни я, ни пользователи не понимают, как это работает». Движок
был сильный, но наружу отдавал коды и голые числа. Здесь проверяем слой смысла:
причины переходов по-русски, смысл+следующий шаг у каждого состояния (один
источник правды STATE_GUIDE), обогащение истории (таймер распада, гайд), полную
лестницу в сводке и то, что экран несёт объяснение и прямые действия.
"""
from __future__ import annotations

import asyncio
import os

from services import virtual_layer as V

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── причины переходов по-русски ───────────────────────────────────────────────

def test_reason_label_decodes_signal_codes():
    assert V.reason_label("signal:asked_price") == "спросил цену"
    assert V.reason_label("signal:paid") == "оплатил"
    assert V.reason_label("reaffirm") == "подтвердил интерес ещё раз"
    assert V.reason_label("decay").startswith("распад")
    assert V.reason_label("cascade:hot").startswith("пересчёт")


def test_reason_label_is_safe_on_unknown_and_empty():
    assert V.reason_label(None) == ""
    assert V.reason_label("") == ""
    # неизвестный код не теряется (как у restriction-причин) — отдаём как есть
    assert V.reason_label("signal:teleport") == "teleport"
    assert V.reason_label("весёлый код") == "весёлый код"


def test_every_positive_signal_has_russian_label():
    # каждый сигнал, двигающий вверх, должен читаться по-русски — иначе история
    # снова покажет машинный код
    for sig in V.SIGNAL_TARGET:
        assert V.SIGNAL_LABEL.get(sig), f"нет русской подписи сигнала: {sig}"
    for sig in V.NEGATIVE_SIGNALS:
        assert V.SIGNAL_LABEL.get(sig), f"нет русской подписи негатива: {sig}"


# ── смысл + что делать у каждого состояния ─────────────────────────────────────

def test_state_guide_covers_every_ladder_value_and_temperature():
    for v in V.LADDER + [V.LOST, V.HOT, V.WARM, V.COLD]:
        g = V.state_guide(v)
        assert g["meaning"] and g["action"], f"нет смысла/шага для состояния: {v}"


def test_state_guide_safe_on_unknown():
    g = V.state_guide("qwerty")
    assert g == {"meaning": "", "action": ""}


# ── обогащение истории: таймер распада, гайд, причины ──────────────────────────

class _Pool:
    """Фейковый пул: история переходов + текущее состояние по тексту запроса."""
    def __init__(self, history, state):
        self._history = history
        self._state = state

    async def fetch(self, q, *a):
        return self._history if "virtual_state_history" in q else []

    async def fetchrow(self, q, *a):
        if "FROM virtual_states" in q:
            return self._state
        return None


def _run(c):
    return asyncio.run(c)


def test_entity_history_translates_reasons_and_adds_decay_and_guide():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    history = [
        {"from_value": "curious", "to_value": "interested",
         "reason": "signal:replied", "confidence": 0.7, "created_at": now},
    ]
    state = {"value": "ready", "confidence": 0.9, "source": "bot_17",
             "expires_at": now + timedelta(hours=10), "updated_at": now}
    d = _run(V.entity_history(_Pool(history, state), 7, V.USER, "c1"))
    # причина перехода — по-русски
    assert d["transitions"][0]["reason_label"] == "ответил в переписке"
    cur = d["current"]
    # таймер распада и TTL рунга «ready» (24ч) проброшены, состояние не протухло
    assert cur["ttl_hours"] == 24 and cur["expired"] is False
    assert cur["expires_at"] is not None
    # смысл + следующий шаг для «готов купить»
    assert cur["guide"]["meaning"] and "Дожимайте" in cur["guide"]["action"]


def test_entity_history_marks_expired_state():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    state = {"value": "interested", "confidence": 0.5, "source": "x",
             "expires_at": now - timedelta(hours=1), "updated_at": now}
    d = _run(V.entity_history(_Pool([], state), 7, V.USER, "c1"))
    assert d["current"]["expired"] is True


# ── экран: объяснение + прямые действия ───────────────────────────────────────

def test_screen_is_self_explaining_and_actionable():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    # пояснение «как читать»
    assert "Как читать этот экран" in html
    # полная лестница из d.ladder (а не только непустые ступени)
    assert "d.ladder" in html
    # прямые действия из состояний ведут в существующие экраны
    assert "openContactsHot()" in html          # дожать готовых
    assert "openCampaignPlanner()" in html       # рассылка/оффер по тёплой аудитории
    assert "openChatWarmup()" in html            # разогрев остывшей
    # карточка человека: таймер распада, смысл и «что делать»
    assert "_vlDecayText" in html
    assert "Что делать:" in html
    # причина перехода показывается по-русски
    assert "t.reason_label" in html


def test_api_passes_ladder_and_entity_enrichment():
    # Источник API-слоя берём через общий хелпер, а не по пути к монолиту
    # (tests.test_tests_do_not_pin_the_monolith) — иначе тест ослепнет при выносе.
    from tests.miniapp_source import api_source
    api = api_source()
    assert '"ladder": ov.get("ladder")' in api            # сводка отдаёт лестницу
    assert 'expires_at' in api and 'isoformat' in api      # таймер распада сериализуется
