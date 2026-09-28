"""Проверка НАШИХ каналов/чатов/ботов на ограничения и теневой бан.

Владелец: «сделаем возможность массово проверять наши каналы/чаты/боты — не в
теневом ли они бане и нет ли других ограничений; ботов тоже проверять на теневой
бан». Реализуемо в 2026 и проверяется здесь ТОЛЬКО то, что реально отдаёт
Telegram API: флаг `restricted`/`restriction_reason`, резолв @username снаружи,
видимость в глобальном поиске (contacts.Search) и ответ бота на /start. Выдуманных
сигналов («процент охвата») нет — их Telegram не отдаёт.

Модуль services/entity_restriction_check — чистая логика классификации; сеть
делает исполнитель операции. Здесь фиксируем вердикты по каждому сочетанию
сигналов и разбор причин ограничения.
"""
from __future__ import annotations

import asyncio

from services import entity_restriction_check as erc


# ── classify: каждая ветка вердикта ──────────────────────────────────────────

def test_clean_when_resolves_and_visible():
    r = erc.classify("channel", {"resolved": True, "has_username": True,
                                  "found_in_search": True})
    assert r["status"] == erc.STATUS_CLEAN


def test_hidden_when_resolves_but_not_in_search():
    r = erc.classify("channel", {"resolved": True, "has_username": True,
                                  "found_in_search": False})
    assert r["status"] == erc.STATUS_HIDDEN
    assert "поиск" in r["reason_ru"].lower()


def test_unreachable_when_not_resolved():
    r = erc.classify("channel", {"resolved": False, "has_username": True})
    assert r["status"] == erc.STATUS_UNREACHABLE


def test_restricted_beats_hidden_and_carries_reason():
    # Официальное ограничение приоритетнее «скрыт из поиска»: если Telegram прямо
    # назвал причину — показываем её, даже если сущность заодно не в поиске.
    r = erc.classify("channel", {
        "resolved": True, "has_username": True, "found_in_search": False,
        "restricted": True,
        "restriction_reasons": [{"platform": "ios", "reason": "porn",
                                 "text": "This channel can't be displayed"}],
    })
    assert r["status"] == erc.STATUS_RESTRICTED
    assert "can't be displayed" in r["reason_ru"]


def test_restricted_by_reasons_even_without_flag():
    # Непустой restriction_reason сам по себе = ограничение, даже если флаг не
    # выставлен: иначе реальная блокировка по стране прошла бы как «чисто».
    r = erc.classify("channel", {
        "resolved": True, "has_username": True,
        "restriction_reasons": [{"platform": "android", "reason": "copyright",
                                 "text": "blocked"}],
    })
    assert r["status"] == erc.STATUS_RESTRICTED


def test_bot_not_responding_is_unreachable():
    r = erc.classify("bot", {"resolved": True, "has_username": True,
                             "found_in_search": True, "responds": False})
    assert r["status"] == erc.STATUS_UNREACHABLE
    assert "/start" in r["reason_ru"]


def test_check_failed_on_probe_error_not_clean():
    # Сбой проверки НЕ должен маскироваться под «чисто» — иначе владелец решит,
    # что ограничений нет, хотя мы просто не смогли проверить.
    r = erc.classify("channel", {"probe_error": "timeout"})
    assert r["status"] == erc.STATUS_CHECK_FAILED
    assert not erc.is_problem(erc.STATUS_CHECK_FAILED)


def test_private_entity_without_username_is_clean_when_not_restricted():
    # У приватной сущности (нет @username) поиска нет по определению — «скрыт из
    # поиска» к ней неприменимо, только restricted.
    r = erc.classify("group", {"resolved": True, "has_username": False})
    assert r["status"] == erc.STATUS_CLEAN


# ── разбор причин и служебные мапперы ────────────────────────────────────────

def test_parse_restriction_reasons_handles_objects_and_strings():
    class _R:
        def __init__(self, platform, reason, text):
            self.platform, self.reason, self.text = platform, reason, text
    parsed = erc.parse_restriction_reasons(
        [_R("ios", "porn", "blocked"), "raw string", None])
    assert parsed[0] == {"platform": "ios", "reason": "porn", "text": "blocked"}
    assert parsed[1]["text"] == "raw string"
    assert len(parsed) == 2  # None отброшен


def test_event_type_and_severity_map():
    assert erc.event_type_for("channel", erc.STATUS_RESTRICTED) == "channel_restricted"
    assert erc.event_type_for("bot", erc.STATUS_HIDDEN) == "bot_search_hidden"
    assert erc.event_type_for("group", erc.STATUS_UNREACHABLE) == "group_unreachable"
    assert erc.severity_for(erc.STATUS_RESTRICTED) == "critical"
    assert erc.severity_for(erc.STATUS_HIDDEN) == "warning"


def test_is_problem_excludes_clean_and_failed():
    assert erc.is_problem(erc.STATUS_RESTRICTED)
    assert erc.is_problem(erc.STATUS_HIDDEN)
    assert erc.is_problem(erc.STATUS_UNREACHABLE)
    assert not erc.is_problem(erc.STATUS_CLEAN)
    assert not erc.is_problem(erc.STATUS_CHECK_FAILED)


def test_build_summary_lists_problems_and_clean_note():
    s = erc.build_summary({"clean": 3, "restricted": 1, "hidden": 2}, 6)
    assert "Проверено сущностей: 6" in s
    assert "🚫" in s and "🕶" in s
    s2 = erc.build_summary({"clean": 4}, 4)
    assert "не обнаружено" in s2.lower()


# ── probe_entity: сетевая проба на фейковом клиенте ──────────────────────────

class _Ent:
    def __init__(self, id, username=None, restricted=False, reasons=None,
                 title="T", first_name=None):
        self.id = id
        self.username = username
        self.restricted = restricted
        self.restriction_reason = reasons
        self.title = title
        self.first_name = first_name
        self.verified = False
        self.scam = False


class _Found:
    def __init__(self, chats=None, users=None):
        self.chats = chats or []
        self.users = users or []


class _FakeClient:
    def __init__(self, entity, search_hit=True, bot_reply=True, resolve_exc=None):
        self._e = entity
        self._hit = search_hit
        self._reply = bot_reply
        self._resolve_exc = resolve_exc

    async def get_entity(self, ref):
        if self._resolve_exc:
            raise self._resolve_exc
        return self._e

    async def __call__(self, req):
        return _Found(chats=[self._e]) if self._hit else _Found()

    async def send_message(self, *a, **k):
        return None

    async def get_messages(self, *a, **k):
        return [object()] if self._reply else []


def _run(coro):
    return asyncio.run(coro)


def test_probe_public_channel_visible_is_clean():
    c = _FakeClient(_Ent(100, "chan"), search_hit=True)
    r = _run(erc.probe_entity(c, kind="channel", entity_id=100, username="chan"))
    assert r["status"] == erc.STATUS_CLEAN


def test_probe_public_channel_missing_from_search_is_hidden():
    c = _FakeClient(_Ent(100, "chan"), search_hit=False)
    r = _run(erc.probe_entity(c, kind="channel", entity_id=100, username="chan"))
    assert r["status"] == erc.STATUS_HIDDEN


def test_probe_restricted_channel_skips_search_and_reports_reason():
    class _RR:
        platform, reason, text = "ios", "porn", "cannot be displayed"
    c = _FakeClient(_Ent(100, "chan", restricted=True, reasons=[_RR()]))
    r = _run(erc.probe_entity(c, kind="channel", entity_id=100, username="chan"))
    assert r["status"] == erc.STATUS_RESTRICTED
    assert "cannot be displayed" in r["reason_ru"]


def test_probe_unreachable_when_username_not_occupied():
    exc = Exception("The username is not occupied by anyone")
    c = _FakeClient(_Ent(100, "gone"), resolve_exc=exc)
    r = _run(erc.probe_entity(c, kind="channel", entity_id=100, username="gone"))
    assert r["status"] == erc.STATUS_UNREACHABLE


def test_probe_flood_is_check_failed_not_unreachable():
    # Флуд НАШЕЙ сессии — это сбой проверки, НЕ вердикт «сущность недоступна».
    exc = Exception("A wait of 42 seconds is required (FloodWaitError)")
    c = _FakeClient(_Ent(100, "chan"), resolve_exc=exc)
    r = _run(erc.probe_entity(c, kind="channel", entity_id=100, username="chan"))
    assert r["status"] == erc.STATUS_CHECK_FAILED


def test_probe_bot_not_replying_is_unreachable():
    c = _FakeClient(_Ent(200, "bot", first_name="B", title=None),
                    search_hit=True, bot_reply=False)
    r = _run(erc.probe_entity(c, kind="bot", entity_id=200, username="bot"))
    assert r["status"] == erc.STATUS_UNREACHABLE
