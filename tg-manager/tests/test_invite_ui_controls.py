"""Настройки инвайта, которые исполнитель читает, обязаны иметь контрол.

ДВА СЛУЧАЯ «настройка есть, а задать её нечем»:

  • link_message — движок разворачивает текст приглашения spintax'ом на каждую
    цель, эндпоинт мини-аппа его принимает, но поля не было ни на одной
    поверхности: метод «ссылка в ЛС» всегда слал одну и ту же фразу по умолчанию
    от всех аккаунтов;
  • skip_invited — исполнитель его читает и ПРЯМО СОВЕТУЕТ в итоге операции
    («соберите свежую аудиторию или отключите дедуп»), но переключателя не было
    нигде. Совет, указывающий на несуществующую настройку, хуже отсутствия совета.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Мини-апп больше не один файл: экраны выносятся в mini_app/screens/*.js.
# Берём исходник целиком, иначе каждый вынос ронял бы эти проверки.
from tests.miniapp_source import miniapp_source

UI = miniapp_source()
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
BOT = (ROOT / "bot" / "handlers" / "mass_inviter.py").read_text(encoding="utf-8")
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _submit_seg() -> str:
    i = API.index("async def mass_inviter_submit")
    return API[i:API.index("    async def ", i + 30)]


def test_link_message_has_a_control_on_both_surfaces():
    assert 'id="massInviteLinkMsg"' in UI, "в мини-аппе нет поля текста приглашения"
    assert "body.link_message" in UI, "введённый текст не уходит в запрос"
    assert "InviterFSM.link_message" in BOT, "в боте нет шага ввода текста"


def test_link_message_field_is_shown_only_for_the_link_method():
    """Лишнее поле на экране запуска читается как настройка, которая влияет."""
    assert "massInviteMethodToggle" in UI
    m = re.search(r"function massInviteMethodToggle\(\) \{(.*?)\n\}", UI, re.S)
    assert m and "'link'" in m.group(1) and "display" in m.group(1)


def test_reinvite_control_exists_and_maps_to_skip_invited():
    assert 'id="massInviteReinvite"' in UI, "нет переключателя повторного инвайта"
    assert "body.skip_invited = false" in UI
    assert 'body.get("skip_invited") is False' in _submit_seg(), (
        "эндпоинт не принимает флаг — контрол был бы декоративным"
    )
    assert 'params["skip_invited"] = False' in _submit_seg()


def test_the_advice_in_the_summary_points_at_a_real_control():
    """Исполнитель советует «отключите дедуп» — значит дедуп должно быть чем
    отключить."""
    assert "отключите дедуп" in WORKER
    assert 'params.get("skip_invited", True)' in WORKER or '"skip_invited"' in WORKER
    assert 'id="massInviteReinvite"' in UI


def test_every_param_the_executor_reads_can_be_set_somewhere():
    """Сквозная проверка: параметр, который читает исполнитель, задаётся хотя бы
    на одной поверхности (или ставится системой)."""
    i = WORKER.index("async def _exec_mass_invite")
    seg = WORKER[i:WORKER.index("\nasync def _exec_", i + 1)]
    read = set(re.findall(r'params\.get\(\s*["\']([a-z_0-9]+)["\']', seg))
    # ставятся системой, а не человеком
    system = {"invite_chain", "user_refs", "phones", "account_ids", "group",
              "source", "parse_run_id", "saved_segment_id", "segment_filters"}
    settable = UI + API + BOT
    missing = [k for k in sorted(read - system) if k not in settable]
    assert not missing, f"исполнитель читает, а задать нечем: {missing}"


# ── Механики, меняющие состав админов чата ───────────────────────────────────

def test_admin_granting_behaviours_can_be_turned_off():
    """Обе включены по умолчанию и МЕНЯЮТ администрирование чужого чата:
    автовыдача делает админами аккаунты оператора, промоут-трюк на секунды
    выдаёт админку постороннему. Работали молча и без выключателя."""
    for el in ("massInviteAutoPromote", "massInvitePromoteTrick"):
        assert f'id="{el}"' in UI, f"нет переключателя {el}"
    assert "body.auto_promote = false" in UI
    assert "body.promote_trick = false" in UI
    seg = _submit_seg()
    assert 'body.get("auto_promote") is False' in seg
    assert 'body.get("promote_trick") is False' in seg


def test_admin_granting_defaults_stay_on():
    """Поведение по умолчанию не меняется: галочки стоят, флаг уходит только
    когда его сняли."""
    for el in ("massInviteAutoPromote", "massInvitePromoteTrick"):
        m = re.search(rf'id="{el}"[^>]*>', UI)
        assert m and "checked" in m.group(0), f"{el} должен быть включён по умолчанию"


def test_user_is_told_what_these_toggles_do_to_their_chat():
    """Согласие без предупреждения — не согласие: рядом должно быть сказано,
    что аккаунты станут админами."""
    i = UI.index('id="massInviteAutoPromote"')
    seg = UI[i:i + 1400]
    assert "админ" in seg.lower()
    assert "снимите галочку" in seg.lower()
