"""Сверка списка админов канала: отработавший флот гарантированно уходит из админов.

Жалоба владельца (дословно): «в итоге после каждого инвайтинга я вижу у себя в
списке администраторов весь мой флот, он не покидает каналы/чаты и мешает
инвайтингу через админку потому что места нету».

Корень: места админа (_admin_seats) хранились в памяти ОДНОГО прогона. Цепочка-
продолжение (инвайт растянут на дни) и аварийно оборванный прогон оставляли
инвайтеров админами, а следующий прогон про те места ничего не знал и их не
чистил — админы (лимит Telegram ~50) копились прогонами, и назначать через
админку было уже некуда.

Фикс: в НАЧАЛЕ и КОНЦЕ прогона свериться с РЕАЛЬНЫМ списком админов канала
(get_channel_admins промоутером) и снять+вывести наших инвайтеров флота —
опираясь на истину Telegram, а не на память процесса. Ядро отбора («кого
снять») вынесено в чистую функцию _select_reconcile_admin_targets и тестируется
изолированно: ошибка там = разжалование создателя канала или чужого админа.

Плюс анти-бан: «когда мы вступаем в канал — Telegram видит что мы не оставляем
просмотров на постах, а это триггер для бана». После вступления инвайтер
оставляет просмотры (view_recent_posts → GetMessagesViews increment=True).
"""
from __future__ import annotations

import inspect
import os
import re

from services.op_worker import _select_reconcile_admin_targets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# Подпись НАШЕГО промоута инвайтера: анонимный, может приглашать, но не менять
# инфо и не постить (ровно так его повышает _exec_mass_invite).
_INVITER_RIGHTS = {"invite_users": True, "change_info": False,
                   "post_messages": False, "add_admins": False, "anonymous": True}
# Со-админ, назначенный владельцем вручную, — с полными правами.
_COADMIN_RIGHTS = {"invite_users": True, "change_info": True,
                   "post_messages": True, "add_admins": True, "anonymous": False}


# ── чистое ядро отбора: кого снимаем, а кого НЕЛЬЗЯ ────────────────────────

def test_whole_fleet_of_inviters_is_selected():
    """Главный сценарий жалобы: весь флот повис в админах после прогона —
    сверка обязана выбрать ВСЕХ наших инвайтеров к снятию."""
    fleet = {1001, 1002, 1003, 1004}
    admins = [{"user_id": u, "is_creator": False, "rights": dict(_INVITER_RIGHTS)}
              for u in fleet]
    got = _select_reconcile_admin_targets(admins, promoter_uid=None, fleet_uids=fleet)
    assert set(got) == fleet, got


def test_creator_is_never_demoted():
    """Создателя канала снять нельзя НИКОГДА — даже если он из флота владельца
    (владелец часто создаёт канал своим же аккаунтом)."""
    fleet = {500, 1001}
    admins = [
        {"user_id": 500, "is_creator": True, "rights": dict(_INVITER_RIGHTS)},
        {"user_id": 1001, "is_creator": False, "rights": dict(_INVITER_RIGHTS)},
    ]
    got = _select_reconcile_admin_targets(admins, promoter_uid=500, fleet_uids=fleet)
    assert got == [1001], got


def test_promoter_is_never_demoted():
    """Промоутер (аккаунт, раздающий админку) должен остаться — иначе следующий
    прогон останется без того, кто назначает права."""
    fleet = {700, 1001}
    admins = [
        {"user_id": 700, "is_creator": False, "rights": dict(_INVITER_RIGHTS)},
        {"user_id": 1001, "is_creator": False, "rights": dict(_INVITER_RIGHTS)},
    ]
    got = _select_reconcile_admin_targets(admins, promoter_uid=700, fleet_uids=fleet)
    assert got == [1001], got


def test_foreign_admin_not_in_fleet_is_untouched():
    """Чужой админ (не из флота владельца) — не наш, не трогаем ни при каких
    правах."""
    fleet = {1001}
    admins = [
        {"user_id": 9999, "is_creator": False, "rights": dict(_INVITER_RIGHTS)},
        {"user_id": 1001, "is_creator": False, "rights": dict(_INVITER_RIGHTS)},
    ]
    got = _select_reconcile_admin_targets(admins, promoter_uid=None, fleet_uids=fleet)
    assert got == [1001], got


def test_manual_full_rights_coadmin_is_untouched():
    """Со-админ с полными правами (change_info/post_messages) назначен владельцем
    вручную — это НЕ подпись нашего инвайтера, его не снимаем, даже если он
    оказался из флота."""
    fleet = {1001, 1002}
    admins = [
        {"user_id": 1001, "is_creator": False, "rights": dict(_COADMIN_RIGHTS)},
        {"user_id": 1002, "is_creator": False, "rights": dict(_INVITER_RIGHTS)},
    ]
    got = _select_reconcile_admin_targets(admins, promoter_uid=None, fleet_uids=fleet)
    assert got == [1002], got


def test_admin_without_invite_right_is_untouched():
    """Без права приглашать это точно не наш инвайтер — не наша подпись."""
    fleet = {1001}
    rights = dict(_INVITER_RIGHTS, invite_users=False)
    admins = [{"user_id": 1001, "is_creator": False, "rights": rights}]
    got = _select_reconcile_admin_targets(admins, promoter_uid=None, fleet_uids=fleet)
    assert got == [], got


def test_mixed_channel_picks_only_our_inviters():
    """Реалистичный смешанный канал: создатель+промоутер+вручную-назначенный
    со-админ+чужой админ+наши инвайтеры. Снимаем РОВНО наших инвайтеров."""
    fleet = {500, 700, 1001, 1002, 1003, 2002}
    admins = [
        {"user_id": 500, "is_creator": True, "rights": dict(_COADMIN_RIGHTS)},   # создатель
        {"user_id": 700, "is_creator": False, "rights": dict(_INVITER_RIGHTS)},  # промоутер
        {"user_id": 2002, "is_creator": False, "rights": dict(_COADMIN_RIGHTS)}, # ручной со-админ
        {"user_id": 8888, "is_creator": False, "rights": dict(_INVITER_RIGHTS)}, # чужой (не флот)
        {"user_id": 1001, "is_creator": False, "rights": dict(_INVITER_RIGHTS)}, # наш
        {"user_id": 1002, "is_creator": False, "rights": dict(_INVITER_RIGHTS)}, # наш
        {"user_id": 1003, "is_creator": False, "rights": dict(_INVITER_RIGHTS)}, # наш
    ]
    got = _select_reconcile_admin_targets(admins, promoter_uid=700, fleet_uids=fleet)
    assert set(got) == {1001, 1002, 1003}, got


# ── wiring в _exec_mass_invite: сверка И в начале, И в конце ────────────────

def _exec_mass_invite_source() -> str:
    src = _read("services/op_worker.py")
    i = src.index("async def _exec_mass_invite(")
    j = src.index("\nasync def ", i + 20)
    return src[i:j]


def test_reconcile_runs_at_start_and_end_of_run():
    """Сверка обязана вызываться И на старте (освободить места от прошлых
    прогонов ДО раздачи прав этому кругу), И в конце (дочистить то, что этот
    прогон мог оставить при обрыве). Иначе наследные админы копятся."""
    seg = _exec_mass_invite_source()
    calls = [m.start() for m in re.finditer(r"await _reconcile_channel_admins\(", seg)]
    assert len(calls) >= 2, (
        f"ожидали сверку админов и в начале, и в конце прогона, нашли {len(calls)}")
    assert 'await _reconcile_channel_admins("старт прогона")' in seg
    assert 'await _reconcile_channel_admins("конец прогона")' in seg


def test_reconcile_uses_real_admin_list_and_batch_demote():
    """Сверка опирается на РЕАЛЬНЫЙ список админов Telegram (get_channel_admins),
    снимает права ПАЧКОЙ через промоутера (одно подключение) и каждый выходит
    своей сессией (leave_channel), а не памятью процесса."""
    seg = _exec_mass_invite_source()
    i = seg.index("async def _reconcile_channel_admins")
    body = seg[i:seg.index("async def _view_after_join", i)]
    assert "get_channel_admins(" in body
    assert "demote_from_admin_batch(" in body
    assert "leave_channel(" in body
    assert "_select_reconcile_admin_targets(" in body
    # снятие прав — через промоутера; выход — у КАЖДОГО своя сессия (параллель)
    assert "asyncio.gather(" in body


def test_reconcile_skips_link_method():
    """Метод «ссылка в ЛС» не раздаёт админку вовсе — сверять и снимать нечего."""
    seg = _exec_mass_invite_source()
    i = seg.index("async def _reconcile_channel_admins")
    body = seg[i:seg.index("async def _view_after_join", i)]
    assert '_invite_method == "link"' in body


# ── анти-бан: просмотры на постах сразу после вступления ───────────────────

def test_view_after_join_wired_on_both_join_points():
    """Просмотры оставляются после ОБОИХ вступлений инвайтера (упреждающая
    раздача прав и выдача «на лету») — иначе часть вступивших не оставит
    просмотров и получит триггер бана."""
    seg = _exec_mass_invite_source()
    assert seg.count("await _view_after_join(") >= 2, (
        "просмотры после вступления должны стоять на обоих join-точках инвайта")


def test_view_recent_posts_increments_views_and_reads_history():
    """view_recent_posts обязан именно УВЕЛИЧИВАТЬ счётчик просмотров
    (GetMessagesViews increment=True) по постам и затем «дочитать» историю."""
    src = inspect.getsource(__import__("services.account_manager",
                                       fromlist=["view_recent_posts"]).view_recent_posts)
    assert "GetMessagesViewsRequest" in src
    assert "increment=True" in src
    assert "ReadHistoryRequest" in src
    # по ОДНОМУ посту с паузами «чтения» (батч ≠ просмотр N постов)
    assert "id=[" in src and "asyncio.sleep" in src


def test_get_channel_admins_returns_rights_and_creator_flag():
    """get_channel_admins обязан отдавать по каждому админу user_id, флаг
    создателя и права (invite_users/change_info/post_messages) — без них сверка
    не отличит нашего инвайтера от создателя и ручного со-админа."""
    src = inspect.getsource(__import__("services.account_manager",
                                       fromlist=["get_channel_admins"]).get_channel_admins)
    assert "ChannelParticipantsAdmins" in src
    assert "is_creator" in src and "ChannelParticipantCreator" in src
    for key in ("invite_users", "change_info", "post_messages"):
        assert key in src, f"в правах нет {key} — сверка не отличит инвайтера"
