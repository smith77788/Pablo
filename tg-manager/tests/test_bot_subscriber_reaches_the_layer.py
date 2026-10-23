"""Подписчик бота доходит до виртуального слоя, а не теряется по пути.

ЧТО БЫЛО. Состояния слоя ключуются по контакту (`unified_contacts.id`), а
подписчики бота живут в `bot_users` — и в контакты их не переносит НИКТО:
`unified_contacts` наполняет только синхронизация адресных книг аккаунтов.
`signal_for_telegram_user` контакт не находил и выходил молча. То есть все
сигналы главного входящего канала продукта — тап по кнопке, ответ в переписке,
отказ — пропадали до единого, и лестница, распад и каскад работали только по
тем, кто УЖЕ был в адресной книге владельца.

Отсюда же следовало, что «готов купить» в продукте почти не случалось:
подтверждать его было нечем, а значит и виртуальное событие
purchase_intent_detected, на которое подписаны автоматизации, не рождалось.

У сенсора намерений была та же дыра с другого конца: сигнал «ответил» он подавал
ПЕРЕД тем, как завести контакт, да ещё и заводил его только при совпадении
фразы-правила. Самое честное подтверждение — «человек ответил» — слой не видел
никогда, а у владельца без настроенных правил не видел вообще ничего.

ЧТО ТЕПЕРЬ. Источник передаёт слою то, что Telegram сказал о человеке в этом же
обновлении (`identity`), и на сигнале-ДЕЙСТВИИ слой заводит контакт через
единственную дверь контактов. На слабом сигнале («/start», «отписался») не
заводит: подписчиков у бота бывают десятки тысяч, и превращать их всех в
контакты значило бы утопить CRM.
"""
from __future__ import annotations

import inspect

import pytest

from services import virtual_layer as vl


class _Pool:
    """Пул, у которого контакта нет: ровно случай подписчика бота."""

    def __init__(self, contact_id=None):
        self.contact_id = contact_id
        self.lookups: list[tuple] = []

    async def fetchval(self, sql, *args):
        self.lookups.append(args)
        return self.contact_id


@pytest.fixture
def layer(monkeypatch):
    """Перехватываем и создание контакта, и запись сигнала."""
    created: list[dict] = []
    signalled: list[dict] = []

    async def _ensure(pool, owner_id, tg_user_id, **kw):
        created.append({"owner_id": owner_id, "tg": tg_user_id, **kw})
        return "c-new"

    async def _signal(pool, owner_id, entity_type, entity_id, name, **kw):
        signalled.append({"entity_id": entity_id, "name": name, **kw})
        return {"value": vl.SIGNAL_TARGET.get(name, "curious")}

    from services.contacts_hub import repository
    monkeypatch.setattr(repository, "ensure_contact_by_telegram_id", _ensure)
    monkeypatch.setattr(vl, "signal", _signal)
    return created, signalled


WHO = {"username": "vasya", "first_name": "Вася", "last_name": "Пупкин"}


async def test_button_tap_creates_the_contact_and_the_state(layer):
    created, signalled = layer
    out = await vl.signal_for_telegram_user(
        _Pool(None), 7, 555, "clicked_offer", confidence=0.7,
        source="bot_42", identity=WHO)

    assert created, "подписчик бота снова не доходит до слоя"
    assert created[0]["owner_id"] == 7 and created[0]["tg"] == 555
    assert created[0]["username"] == "vasya"
    assert created[0]["first_name"] == "Вася"
    assert signalled and signalled[0]["entity_id"] == "c-new"
    assert signalled[0]["name"] == "clicked_offer"
    assert out == {"value": "qualified"}


async def test_a_reply_creates_the_contact_too(layer):
    """«Ответил» — самое честное подтверждение из всех, что есть у продукта."""
    created, signalled = layer
    await vl.signal_for_telegram_user(
        _Pool(None), 7, 556, "replied", identity=WHO)
    assert created and signalled


@pytest.mark.parametrize("weak", ["opened", "unsubscribed", "blocked", "reacted"])
async def test_a_weak_signal_does_not_create_a_contact(layer, weak):
    """Иначе CRM утонет в подписчиках, которые только нажали /start."""
    created, signalled = layer
    assert await vl.signal_for_telegram_user(
        _Pool(None), 7, 557, weak, identity=WHO) is None
    assert not created, f"{weak} заводит контакт — CRM наполнится мусором"
    assert not signalled


async def test_without_identity_nothing_is_created(layer):
    """Безымянная строка в контактах хуже отсутствия строки."""
    created, signalled = layer
    assert await vl.signal_for_telegram_user(
        _Pool(None), 7, 558, "clicked_offer") is None
    assert not created and not signalled


async def test_known_contact_is_not_created_again(layer):
    created, signalled = layer
    await vl.signal_for_telegram_user(
        _Pool("c-old"), 7, 559, "clicked_offer", identity=WHO)
    assert not created, "контакт уже есть, а слой заводит второй"
    assert signalled and signalled[0]["entity_id"] == "c-old"


async def test_a_broken_contact_door_does_not_break_the_caller(monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("база лежит")

    from services.contacts_hub import repository
    monkeypatch.setattr(repository, "ensure_contact_by_telegram_id", _boom)
    assert await vl.signal_for_telegram_user(
        _Pool(None), 7, 560, "clicked_offer", identity=WHO) is None


# ── Проводка источников ──────────────────────────────────────────────────────

def test_auto_responder_tells_the_layer_who_wrote():
    src = inspect.getsource(__import__(
        "services.auto_responder", fromlist=["x"]))
    i = src.index("async def _vl_signal(")
    seg = src[i:i + 1200]
    assert "identity=who" in seg, (
        "автоответчик снова не передаёт слою, кто написал — контакт не "
        "заведётся, и сигнал бота пропадёт молча")
    # Оба осознанных действия обязаны приходить с личностью человека.
    assert '"clicked_offer", 0.7,' in src and "_cb_from)" in src, (
        "тап по кнопке подаётся без личности")
    assert 'else "replied",\n' in src and "from_user)" in src, (
        "ответ в переписке подаётся без личности")


def test_intent_sensor_tells_the_layer_who_wrote():
    src = inspect.getsource(__import__(
        "services.intent_sensor", fromlist=["x"]))
    i = src.index('"replied"')
    seg = src[max(0, i - 600):i + 600]
    assert "identity=" in seg, (
        "сенсор намерений снова подаёт «ответил» до появления контакта")
    assert "peer_username" in seg and "peer_name" in seg


def test_both_sources_use_the_one_contact_door():
    """Два своих поиска-или-создания контакта однажды разойдутся.

    Границы функции берём из дерева разбора, а не окном: отрицательная
    проверка внутри окна фиксированной длины выключается молча, как только
    код сдвинулся (это же стережёт test_no_silently_disabled_guards).
    """
    from services import intent_sensor
    from services.contacts_hub import repository
    assert hasattr(repository, "ensure_contact_by_telegram_id")
    seg = inspect.getsource(intent_sensor._ensure_contact)
    assert "ensure_contact_by_telegram_id(" in seg
    assert "INSERT INTO unified_contacts" not in seg, (
        "у сенсора снова свой запрос — он разойдётся с дверью контактов")


def test_the_creating_signals_are_the_ones_that_mean_an_action():
    """Контакт заводится на ДЕЙСТВИИ: человек написал, спросил или тапнул.

    Пассивное («открыл», «поставил реакцию») и негативное («отписался»,
    «заблокировал») контакт не заводят: первых бывают десятки тысяч, а вторые
    и так уходят из воронки, и строка в CRM по ним — мусор.
    """
    for name in vl._SIGNALS_WORTH_A_CONTACT:
        assert name in vl.SIGNAL_TARGET, (
            f"{name} не двигает лестницу — заводить контакт на нём незачем")
        assert name not in vl.NEGATIVE_SIGNALS, name
    for name in ("opened", "reacted"):
        assert name not in vl._SIGNALS_WORTH_A_CONTACT, (
            f"{name} заводит контакт — CRM наполнится теми, кто ничего не сделал")
    for name in vl.NEGATIVE_SIGNALS:
        assert name not in vl._SIGNALS_WORTH_A_CONTACT, name
    # Верх лестницы обязан быть внутри: на «готов платить» и «заплатил» контакт
    # нужен тем более.
    for name in ("asked_how_to_pay", "paid", "clicked_offer", "replied"):
        assert name in vl._SIGNALS_WORTH_A_CONTACT, name
