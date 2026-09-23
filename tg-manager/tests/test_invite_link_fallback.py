"""Массовый инвайт: недостижимым НИКАКИМ способом отправляется ссылка в ЛС.

Явный запрос владельца: "тех кого не удаётся заинвайтить никаким способом
нужно приглашать через сообщение со ссылкой. Нужно создать для этого все
условия с предварительной настройкой".

Цепочка деградации на одну цель: прямой инвайт (privacy/not_mutual) →
промоут-трюк (add_via_promote, тоже может не взять — «промоут-трюк не
помог») → ФИНАЛЬНЫЙ ФОЛБЭК: ссылка-приглашение в личные сообщения. Метод
"ссылка в ЛС" — самый безопасный для аккаунта из всех (человек вступает
сам, полностью обходит приватность), поэтому годится именно как последний
рубеж, а не как повод повышать риск.

Попутно исправлен реальный баг в services/mass_inviter_engine.py
add_via_promote: при ЧАСТИЧНОМ успехе (ok>0, но не все цели пакета) вызывающий
раньше помечал В invited_this_run ВЕСЬ пакет разом — реально не добавленные
цели тихо считались обработанными и больше никогда не получали ни повторной
попытки, ни (теперь) фолбэка со ссылкой. add_via_promote теперь возвращает
still_blocked — список целей, которых трюк РЕАЛЬНО не добавил.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.miniapp_source import miniapp_html, source_of

ROOT = Path(__file__).resolve().parents[1]
ENGINE = (ROOT / "services" / "mass_inviter_engine.py").read_text(encoding="utf-8")
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")


def _exec_mass_invite_body() -> str:
    m = re.search(r"async def _exec_mass_invite\(.*?(?=\nasync def )", WORKER, re.DOTALL)
    assert m
    return m.group(0)


def _add_via_promote_body() -> str:
    m = re.search(r"async def add_via_promote\(.*?(?=\n\nasync def )", ENGINE, re.DOTALL)
    assert m
    return m.group(0)


# ── mass_inviter_engine.add_via_promote: кто РЕАЛЬНО добавлен ──────────────

def test_add_via_promote_tracks_who_actually_succeeded():
    body = _add_via_promote_body()
    assert "succeeded: list = []" in body
    # оба успешных исхода обязаны попадать в succeeded — иначе still_blocked
    # ошибочно посчитает реально добавленную цель заблокированной
    i_main_ok = body.index("ok += 1\n                succeeded.append(ref)")
    i_already = body.index("except UserAlreadyParticipantError:")
    j_already = body.index("except ChatAdminRequiredError:", i_already)
    already_block = body[i_already:j_already]
    assert "succeeded.append(ref)" in already_block, (
        "UserAlreadyParticipantError — тоже успех (цель уже в чате), обязан "
        "попасть в succeeded, а не считаться заблокированным"
    )
    assert i_main_ok > 0


def test_add_via_promote_returns_still_blocked():
    body = _add_via_promote_body()
    assert 'still_blocked = [r for r in user_refs if str(r) not in _done]' in body
    assert '"still_blocked": still_blocked' in body


# ── op_worker: цепочка деградации direct → trick → link-фолбэк ─────────────

def test_link_fallback_param_defaults_on_but_not_for_link_method():
    body = _exec_mass_invite_body()
    i = body.index('_link_fallback = params.get("link_fallback", True)')
    line = body[i:i + 120]
    assert '_invite_method != "link"' in line, (
        "если основной метод и так «ссылка в ЛС», фолбэку падать некуда — "
        "должен быть отключён, а не дублировать рассылку"
    )


def test_invite_link_is_fetched_for_fallback_too_not_only_link_method():
    body = _exec_mass_invite_body()
    i = body.index('_invite_link = ""')
    seg = body[i:i + 1500]
    assert 'if _invite_method == "link" or _link_fallback:' in seg, (
        "ссылка нужна и основному методу link, и фолбэку — раньше её "
        "получали только для link, фолбэк остался бы без ссылки"
    )
    # неполучение ссылки НЕ должно ронять прогон, если это только фолбэк
    assert "_link_fallback = False" in seg


def test_still_blocked_defaults_to_full_list_before_trick_attempted():
    """Если промоут-трюк не запустился вовсе (нет промоутера/выключен/группа
    недоступна/флот перегрет) — фолбэку всё равно есть с чем работать: все
    цели из _privacy_blocked, а не пустой список."""
    body = _exec_mass_invite_body()
    i = body.index("_still_blocked: list = list(_all_blocked_uniq)")
    j = body.index("if _promoter is not None and _promote_trick", i)
    assert j > i, "дефолт должен стоять ДО условного запуска промоут-трюка"


def test_still_blocked_excludes_only_actually_added_targets():
    """Регресс на реальный баг: раньше при частичном успехе трюка (ok>0) в
    invited_this_run уходил ВЕСЬ пакет — реально не добавленные цели тихо
    терялись (не получали ни повтора, ни фолбэка)."""
    body = _exec_mass_invite_body()
    i = body.index('_tried_blocked = _tr.get("still_blocked") or []')
    j = body.index("await _safe_execute(\n                    pool, \"INSERT INTO operation_log", i)
    block = body[i:j]
    assert "_still_blocked_keys = {str(v) for v in _tried_blocked}" in block
    assert "_added_by_trick = [x for x in _uniq_blocked" in block
    assert "invited_this_run.update(str(x) for x in _added_by_trick)" in block, (
        "в invited_this_run обязаны попадать только РЕАЛЬНО добавленные "
        "трюком цели, не весь пакет разом"
    )


def test_never_tried_targets_over_cap_stay_blocked_for_fallback():
    body = _exec_mass_invite_body()
    i = body.index("_never_tried = _all_blocked_uniq[_cap:]")
    j = body.index("_still_blocked = list(_tried_blocked) + _never_tried")
    assert j > i, "цели сверх лимита прогона (не пробовали трюком вовсе) обязаны попасть в фолбэк"


def test_link_fallback_stage_exists_and_gated_like_promote_trick():
    body = _exec_mass_invite_body()
    i = body.index("# ── Финальный фолбэк: кого не взял НИ ОДИН способ — ссылка в ЛС")
    j = body.index("# Запомнить обработанные цели", i)
    block = body[i:j]
    assert "invite_via_link_batch(" in block
    assert "_link_fallback and _invite_link and _still_blocked" in block
    # те же защитные условия, что и у промоут-трюка — не отправлять при
    # недоступной группе/перегретом флоте/отменённой операции
    assert "not group_broken" in block
    assert "not flood_storm" in block
    assert "_is_cancelled(pool, op_id)" in block


def test_link_fallback_sender_falls_back_to_any_fleet_account():
    """Отправитель ЛС не обязан быть промоутером (право слать сообщения не
    связано с правом назначать админов) — иначе фолбэк молча не работал бы
    для метода "direct" без auto_promote, где промоутера просто нет."""
    body = _exec_mass_invite_body()
    i = body.index("_dm_sender = _promoter or next(")
    line = body[i:i + 200]
    assert 'a.get("session_str")' in line


def test_link_fallback_reported_in_summary():
    body = _exec_mass_invite_body()
    assert "Недостижимым отправлена ссылка в ЛС: {_link_fallback_ok}" in body


def test_link_fallback_logs_operation_step():
    body = _exec_mass_invite_body()
    assert "'link_fallback'" in body or '"link_fallback"' in body


# ── мини-апп: «предварительная настройка» — тумблер + видимое поле текста ──

def test_ui_has_link_fallback_toggle_checked_by_default():
    html = miniapp_html()
    i = html.index('id="massInviteLinkFallback"')
    tag = html[max(0, i - 60):i + 90]
    assert "checked" in tag, "фолбэк должен быть включён по умолчанию (как promote_trick)"
    assert 'onchange="massInviteMethodToggle()"' in tag


def test_ui_message_field_shows_for_fallback_not_only_link_method():
    src = source_of("massInviteMethodToggle")
    i = src.index("function massInviteMethodToggle")
    j = src.index("\n}", i)
    body = src[i:j]
    assert "massInviteLinkFallback" in body
    assert "fb" in body and "m === 'link' || fb" in body, (
        "поле текста сообщения обязано показываться и когда включён "
        "фолбэк, а не только при основном методе link"
    )


def test_ui_calls_toggle_on_screen_open_so_default_checked_shows_field():
    src = source_of("openMassInvite")
    # Точный якорь вместо приблизительного поиска границы функции («следующая
    # async function» находила слишком далеко и ловила ложный положительный
    # результат даже без вызова — проверено git stash) — ищем вызов СРАЗУ
    # после massInviteSrcToggle(), как он добавлен в openMassInvite().
    assert "massInviteSrcToggle();\n  massInviteMethodToggle();" in src, (
        "тумблер включён по умолчанию — без вызова при открытии экрана поле "
        "текста не появится, пока оператор не потрогает способ инвайта вручную"
    )


def test_ui_submit_sends_link_fallback_and_message():
    from tests.miniapp_source import miniapp_source
    full = miniapp_source()
    i = full.index("const _linkFallbackOn = document.getElementById('massInviteLinkFallback')")
    seg = full[i:i + 700]
    assert "body.link_fallback = false" in seg
    assert "body.link_message = _lm" in seg
    assert "body.invite_method === 'link' || _linkFallbackOn" in seg


# ── mini_app_api.py: параметры доходят до всех трёх мест сборки mass_invite ─

def test_api_forwards_link_fallback_param_on_main_screen():
    i = API.index('params["invite_method"] = _im')
    seg = API[i:i + 1000]
    assert 'params["link_fallback"] = False' in seg
    assert 'body.get("link_fallback") is not False' in seg, (
        "текст сообщения обязан доходить до бэкенда и когда основной метод "
        "не link, но фолбэк включён — иначе поле в UI ничего не значит"
    )


def test_api_forwards_link_fallback_on_single_contact_invite():
    i = API.index('async def uch_contact_invite(')
    j = API.index('async def uch_contact_invite_history(')
    seg = API[i:j]
    assert 'params["link_fallback"] = False' in seg
    assert 'link_message' in seg


def test_api_forwards_link_fallback_on_segment_invite():
    i = API.index('async def uch_segment_invite')
    j = API.index('n_targets = len(user_refs) + len(phones)', i)
    seg = API[i:j]
    assert 'params["link_fallback"] = False' in seg
    assert 'link_message' in seg
