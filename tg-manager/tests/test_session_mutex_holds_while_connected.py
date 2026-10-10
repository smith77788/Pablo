"""Мьютекс сессии не отпускает её из-под работающего коннекта.

Одну сессию Telegram разрешает держать ровно одному живому подключению. Два
коннекта на один auth-key — это AUTH_KEY_DUPLICATED, после которого ключ
отозван НАВСЕГДА: аккаунт не чинится ничем. Ради этого и существует
процессный мьютекс `_session_inuse`, через который проходит любое подключение
продукта.

ЧТО БЫЛО. Запись мьютекса считалась протухшей просто по возрасту:
`(now - held) < _SESSION_INUSE_STALE_S`, то есть 300 секунд. Комментарий рядом
обещал, что этого «больше самого долгого удержания коннекта на операцию», — и
это неправда. Прогрев аккаунта (`account_warmer`) создаёт ОДНОГО клиента и
держит его через весь цикл действий до отключения, а в цикле есть адаптивная
пауза `random.uniform(300, 1800)` — до получаса на одном коннекте.

Значит через пять минут работающего прогрева мьютекс объявлял сессию
свободной, и следующая подсистема — операция, монитор, консоль, проверка
здоровья — открывала ВТОРОЙ живой коннект на той же сессии. Ровно то, что
мьютекс должен был предотвращать, и ценой в отозванный ключ.

ЧТО СТАЛО. Срок применяется только к записи, за которой нет живого коннекта.
Пока держащий клиент жив и подключён, сессия занята сколько угодно долго:
занятость проходит (`SessionBusyError` — транзиентный скип), отозванный ключ
не проходит никогда.
"""
from __future__ import annotations

import gc
import time

import pytest

from services import account_manager as am

KEY = "ключ-сессии"


class _Client:
    """Клиент со слабой ссылкой и внятным признаком подключения."""

    def __init__(self, connected=True):
        self.connected = connected

    def is_connected(self):
        return self.connected


@pytest.fixture(autouse=True)
def _clean():
    with am._session_inuse_lock:
        am._session_inuse.clear()
    yield
    with am._session_inuse_lock:
        am._session_inuse.clear()


def _age_entry(key: str, seconds: float) -> None:
    """Состарить запись мьютекса на указанное число секунд.

    Понимает и прежний вид записи (голый момент захвата), чтобы главная
    проверка падала на своём утверждении, а не на распаковке.
    """
    with am._session_inuse_lock:
        entry = am._session_inuse[key]
        if isinstance(entry, tuple):
            am._session_inuse[key] = (entry[0] - seconds,) + tuple(entry[1:])
        else:
            am._session_inuse[key] = entry - seconds


def test_an_operation_longer_than_five_minutes_keeps_its_session():
    """Главная проверка, и она намеренно не трогает новых имён.

    Прогрев держит один коннект до получаса. Раньше через 300 секунд мьютекс
    отдавал сессию следующему подключению — второй живой коннект на том же
    auth-key, и ключ отозван навсегда.
    """
    assert am._try_acquire_session(KEY) is True
    _age_entry(KEY, 301)

    assert am._try_acquire_session(KEY) is False, (
        "сессия отдана второму подключению через пять минут работы — это "
        "AUTH_KEY_DUPLICATED, и восстановить аккаунт будет нечем"
    )


def test_a_long_running_connection_keeps_the_session():
    """Прогрев держит коннект до получаса — отнимать его нельзя."""
    holder = _Client(connected=True)
    assert am._try_acquire_session(KEY) is True
    am._note_session_holder(KEY, holder)

    _age_entry(KEY, am._SESSION_INUSE_STALE_S * 10)

    assert am._try_acquire_session(KEY) is False, (
        "сессия отдана второму подключению из-под живого коннекта — это "
        "AUTH_KEY_DUPLICATED и отозванный ключ"
    )


def test_a_finished_connection_releases_the_session():
    holder = _Client(connected=True)
    am._try_acquire_session(KEY)
    am._note_session_holder(KEY, holder)
    am._release_session(KEY)

    assert am._try_acquire_session(KEY) is True


def test_a_disconnected_holder_does_not_hold_forever():
    """Коннект оборвался, release потерялся — сессия не должна залипнуть."""
    holder = _Client(connected=False)
    am._try_acquire_session(KEY)
    am._note_session_holder(KEY, holder)

    assert am._try_acquire_session(KEY) is False, "слишком рано отпускаем"

    _age_entry(KEY, am._SESSION_INUSE_STALE_S + 1)
    assert am._try_acquire_session(KEY) is True


def test_a_vanished_holder_does_not_hold_forever():
    """Клиент собрал сборщик мусора, отключить его уже некому."""
    holder = _Client(connected=True)
    am._try_acquire_session(KEY)
    am._note_session_holder(KEY, holder)

    del holder
    gc.collect()

    _age_entry(KEY, am._SESSION_INUSE_STALE_S + 1)
    assert am._try_acquire_session(KEY) is True


def test_an_entry_without_a_holder_waits_much_longer():
    """Держатель не привязался: обычного срока мало, тут решает только время."""
    am._try_acquire_session(KEY)

    _age_entry(KEY, am._SESSION_INUSE_STALE_S + 1)
    assert am._try_acquire_session(KEY) is False, (
        "запись без держателя отпущена по короткому сроку — о занятости она "
        "ничего не сообщает, а второй коннект стоит ключа"
    )

    _age_entry(KEY, am._SESSION_INUSE_BLIND_STALE_S + 1)
    assert am._try_acquire_session(KEY) is True


def test_the_blind_window_is_longer_than_any_operation():
    assert am._SESSION_INUSE_BLIND_STALE_S > 1800, (
        "самая долгая пауза прогрева — 1800 секунд на одном коннекте"
    )


def test_an_empty_key_is_never_blocking():
    assert am._try_acquire_session("") is True
    assert am._try_acquire_session("") is True


def test_a_client_without_the_flag_is_not_treated_as_connected():
    class _Bare:
        pass

    assert am._client_is_connected(_Bare()) is False


def test_doubt_counts_as_connected():
    """Отобрать сессию у, возможно, живого коннекта дороже, чем подождать."""
    class _Weird:
        def is_connected(self):
            raise RuntimeError("не отвечает")

    assert am._client_is_connected(_Weird()) is True


def test_a_late_release_does_not_unlock_someone_elses_session():
    """Опоздавший release прежнего держателя — второй коннект поверх живого."""
    first = _Client(connected=False)
    am._try_acquire_session(KEY)
    am._note_session_holder(KEY, first)
    _age_entry(KEY, am._SESSION_INUSE_STALE_S + 1)

    second = _Client(connected=True)
    assert am._try_acquire_session(KEY) is True
    am._note_session_holder(KEY, second)

    # Прежний клиент наконец отключается и зовёт release — запись уже не его.
    am._release_session(KEY, first)

    assert am._try_acquire_session(KEY) is False, (
        "замок снят чужим коннектом: третий подключится поверх живого второго"
    )
    # А свой release работает как раньше.
    am._release_session(KEY, second)
    assert am._try_acquire_session(KEY) is True


def test_release_without_a_holder_still_works():
    """Старый вызов без держателя не должен перестать освобождать сессию."""
    am._try_acquire_session(KEY)
    am._release_session(KEY)
    assert am._try_acquire_session(KEY) is True

def test_the_self_managed_check_registers_its_holder():
    """Проверка статуса берёт мьютекс сама — держателя обязана записать тоже.

    Иначе её запись «слепая»: на аварии, съевшей release, аккаунт залипнет не
    на пять минут, а на час.
    """
    import inspect

    src = inspect.getsource(am.check_account_status_full)
    assert "_note_session_holder" in src, (
        "коннект проверки статуса не регистрирует держателя мьютекса"
    )
    assert "_release_session(_skey, client)" in src, (
        "освобождение неименное — опоздавший release снимет чужой замок"
    )
