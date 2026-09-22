"""Массовый инвайт: все права админа, выдаваемые в ходе прогона, — анонимные.

Явный запрос владельца: "во время инвайтинга все админы должны быть
анонимными". Анонимный админ (ChatAdminRights.anonymous=True) не подписывается
своим именем в списке участников/системных событиях чата — его действия видны
как от лица самого чата. Для массового инвайта это снижает заметность на двух
уровнях:

1. Bulk-выдача прав ДО старта прогона и выдача «на лету» внутри круга
   (services/op_worker.py, account_manager.promote_to_admin_ex) — иначе
   всплеск свежепромоутнутых ВИДИМЫХ инвайтеров сам по себе заметный сигнал
   для участников/владельца чата.
2. Промоут-трюк (services/mass_inviter_engine.py add_via_promote) — цель на
   доли секунды становится админом, чтобы обойти приватность; без anonymous
   её имя мелькнёт в списке админов даже за это короткое время.

Снятие прав (demote_from_admin/demote_from_admin_batch/_revoke в
add_via_promote) намеренно НЕ трогается: там ВСЕ флаги ChatAdminRights сняты
(аккаунт перестаёт быть админом вовсе), anonymous в их числе — это корректно,
а не упущение.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT_MGR = (ROOT / "services" / "account_manager.py").read_text(encoding="utf-8")
ENGINE = (ROOT / "services" / "mass_inviter_engine.py").read_text(encoding="utf-8")
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _promote_to_admin_ex_body() -> str:
    i = ACCOUNT_MGR.index("async def promote_to_admin_ex(")
    j = ACCOUNT_MGR.index("\nasync def demote_from_admin(", i)
    return ACCOUNT_MGR[i:j]


def _exec_mass_invite_body() -> str:
    m = re.search(r"async def _exec_mass_invite\(.*?(?=\nasync def )", WORKER, re.DOTALL)
    assert m
    return m.group(0)


def _add_via_promote_body() -> str:
    m = re.search(r"async def add_via_promote\(.*?(?=\n\nasync def )", ENGINE, re.DOTALL)
    assert m
    return m.group(0)


# ── account_manager.promote_to_admin_ex: параметр + реальное использование ──

def test_promote_to_admin_ex_accepts_anonymous_param():
    body = _promote_to_admin_ex_body()
    sig = body[:body.index(") -> tuple[bool, str]:")]
    assert "anonymous: bool = False" in sig, (
        "promote_to_admin_ex обязан принимать anonymous — иначе вызывающему "
        "нечем попросить скрытого админа"
    )


def test_promote_to_admin_ex_wires_anonymous_into_rights_not_hardcoded():
    body = _promote_to_admin_ex_body()
    i = body.index("rights = ChatAdminRights(")
    j = body.index(")", body.index("manage_topics=False", i))
    rights_block = body[i:j]
    assert "anonymous=anonymous" in rights_block, (
        "ChatAdminRights внутри promote_to_admin_ex обязан использовать "
        "параметр anonymous, а не захардкоженное False — иначе флаг "
        "принимается функцией, но никуда не идёт"
    )


# ── op_worker._exec_mass_invite: оба места выдачи прав просят анонимность ──

def test_bulk_promote_requests_anonymous_admin():
    body = _exec_mass_invite_body()
    i = body.index('elif _auto_promote:')
    j = body.index("_rights_word =", i)
    bulk_block = body[i:j]
    assert "promote_to_admin_ex(" in bulk_block
    assert "anonymous=True" in bulk_block, (
        "bulk-выдача прав ДО старта прогона обязана просить анонимного админа"
    )


def test_on_demand_promote_requests_anonymous_admin():
    body = _exec_mass_invite_body()
    i = body.index('if res.get("no_rights"):')
    j = body.index("Промоутера нет / попытки исчерпаны", i)
    on_demand_block = body[i:j]
    assert "promote_to_admin_ex(" in on_demand_block
    assert "anonymous=True" in on_demand_block, (
        "выдача прав «на лету» внутри круга обязана просить анонимного админа "
        "точно так же, как bulk-выдача"
    )


# ── mass_inviter_engine.add_via_promote: промоут-трюк тоже анонимный ────────

def test_promote_trick_grant_is_anonymous():
    body = _add_via_promote_body()
    i = body.index("_grant = ChatAdminRights(")
    j = body.index("_revoke = ChatAdminRights(", i)
    grant_block = body[i:j]
    assert "anonymous=True" in grant_block, (
        "промоут-трюк на доли секунды делает ЦЕЛЬ админом — без anonymous её "
        "имя мелькнёт в списке админов чата даже за это короткое время"
    )


def test_promote_trick_revoke_still_clears_everything():
    """Снятие прав НЕ трогаем: там снимаются ВСЕ флаги (аккаунт перестаёт быть
    админом), anonymous в их числе — это правильно, не регрессия."""
    body = _add_via_promote_body()
    i = body.index("_revoke = ChatAdminRights(")
    j = body.index(")", i)
    revoke_block = body[i:j]
    assert "anonymous=False" in revoke_block


def test_demote_paths_unaffected_all_flags_stay_off():
    """demote_from_admin/demote_from_admin_batch снимают ВСЕ права — это не
    место для anonymous=True (снимаемый аккаунт вообще перестаёт быть
    админом, у него нет статуса, который можно было бы прятать)."""
    for fn in ("async def demote_from_admin(", "async def demote_from_admin_batch("):
        i = ACCOUNT_MGR.index(fn)
        j = ACCOUNT_MGR.index("\n\nasync def ", i)
        body = ACCOUNT_MGR[i:j]
        assert "anonymous=False" in body, fn
