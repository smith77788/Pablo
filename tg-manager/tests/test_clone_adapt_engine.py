"""«Клон и адаптация»: движок плана, адаптация текста, откат и исполнитель.

Найдено при аудите модуля 10.11.2026:

* исполнитель `_exec_clone_adapt` читал токены ботов из базы КАК ЕСТЬ, а они
  зашифрованы (token_vault, маркер ENC:) — каждый вызов Telegram уходил с
  мусорным токеном, и клон из мини-аппа не срабатывал никогда;
* галочка «Фото» на экране была, а исполнитель поле `photo` молча
  пропускал — ни изменения, ни ошибки;
* бот клонировал прямо в обработчике кнопки, мимо очереди операций, без
  журнала и без снимка для отката.
"""
from __future__ import annotations

import ast
import os

from services import clone_adapt_engine as ca

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _prof(name="", desc="", short="", cmds=None, username="", langs=None, extra_cmds=None):
    p = {"username": username,
         "langs": {"": {"name": name, "description": desc, "short_description": short}},
         "commands": {"": cmds or []}}
    for lc, v in (langs or {}).items():
        p["langs"][lc] = v
    for lc, v in (extra_cmds or {}).items():
        p["commands"][lc] = v
    return p


def _step(plan, key):
    return next(s for s in plan if s["key"] == key)


# ── Настройки ────────────────────────────────────────────────────────────────

def test_fields_accept_old_string_and_new_list_and_drop_unknown():
    assert ca.normalize_fields("name,desc,photo") == ["name", "desc", "photo"]
    assert ca.normalize_fields(["commands", "zzz", "name"]) == ["name", "commands"]
    assert ca.normalize_fields(None) == []


def test_settings_defaults_are_safe():
    s = ca.normalize_settings(None)
    assert s["name_template"] == "{name}"
    assert s["swap_mentions"] is True and s["skip_same"] is True
    assert s["replacements"] == [] and s["langs"] == []


def test_settings_reject_garbage_langs_and_cap_replacements():
    s = ca.normalize_settings({"langs": ["en", "EN", "x1", "de", ""],
                               "replacements": [["a", "b"]] * 50 + [["", "x"]]})
    assert s["langs"] == ["en", "de"]
    assert len(s["replacements"]) == ca.MAX_REPLACEMENTS


# ── План ─────────────────────────────────────────────────────────────────────

def test_name_template_and_numbering():
    src = _prof(name="Доставка")
    tgt = _prof(name="Старое", username="shop2_bot")
    st = ca.normalize_settings({"name_template": "{name} #{n} ({username})"})
    plan = ca.build_plan(src, tgt, ["name"], st, index=3)
    assert _step(plan, "name")["after"] == "Доставка #3 (shop2_bot)"
    assert _step(plan, "name")["before"] == "Старое"


def test_keep_own_name_template_changes_nothing():
    src = _prof(name="Доставка")
    tgt = _prof(name="Своё имя")
    plan = ca.build_plan(src, tgt, ["name"], ca.normalize_settings({"name_template": "{target}"}))
    assert _step(plan, "name")["same"] is True


def test_empty_source_name_never_wipes_the_target():
    plan = ca.build_plan(_prof(name=""), _prof(name="Живое"), ["name"],
                         ca.normalize_settings({}))
    assert not any(s["key"] == "name" for s in plan)


def test_replacements_and_mentions_adapt_every_text():
    src = _prof(name="Доставка Москва", desc="Пишите @src_bot или t.me/src_bot — Москва",
                short="Москва @SRC_bot",
                cmds=[{"command": "start", "description": "Старт в Москва"}],
                username="src_bot")
    tgt = _prof(username="dst_bot")
    st = ca.normalize_settings({"replacements": [["Москва", "Казань"]]})
    plan = ca.build_plan(src, tgt, ["name", "desc", "short", "commands"], st)
    assert _step(plan, "name")["after"] == "Доставка Казань"
    assert _step(plan, "description")["after"] == "Пишите @dst_bot или t.me/dst_bot — Казань"
    assert _step(plan, "short_description")["after"] == "Казань @dst_bot"
    assert _step(plan, "commands")["after"] == [{"command": "start", "description": "Старт в Казань"}]


def test_mention_swap_respects_word_boundary_and_can_be_disabled():
    src = _prof(desc="@shop_bot и @shop_bot2", username="shop_bot")
    tgt = _prof(username="new_bot")
    on = ca.build_plan(src, tgt, ["desc"], ca.normalize_settings({}))
    assert _step(on, "description")["after"] == "@new_bot и @shop_bot2"
    off = ca.build_plan(src, tgt, ["desc"], ca.normalize_settings({"swap_mentions": False}))
    assert _step(off, "description")["after"] == "@shop_bot и @shop_bot2"


def test_braces_in_text_are_not_a_template_error():
    src = _prof(desc="Цена {от 100} руб, {name}")
    plan = ca.build_plan(src, _prof(), ["desc"], ca.normalize_settings({}))
    assert _step(plan, "description")["after"] == "Цена {от 100} руб, "


def test_same_values_are_skipped_unless_asked():
    src = _prof(name="A", desc="Описание")
    tgt = _prof(name="A", desc="Другое")
    plan = ca.build_plan(src, tgt, ["name", "desc"], ca.normalize_settings({}))
    assert _step(plan, "name")["same"] is True
    assert _step(plan, "description")["same"] is False
    assert ca.plan_counts(plan) == {"change": 1, "same": 1, "truncated": 0}
    force = ca.build_plan(src, tgt, ["name", "desc"], ca.normalize_settings({"skip_same": False}))
    assert all(not s["same"] for s in force)


def test_telegram_limits_are_respected_and_reported():
    src = _prof(name="Я" * 70, short="к" * 200, desc="д" * 600)
    plan = ca.build_plan(src, _prof(), ["name", "short", "desc"], ca.normalize_settings({}))
    assert len(_step(plan, "name")["after"]) == 64 and _step(plan, "name")["truncated"]
    assert len(_step(plan, "short_description")["after"]) == 120
    assert len(_step(plan, "description")["after"]) == 512


def test_languages_selected_and_all():
    src = _prof(name="Shop", langs={"en": {"name": "Shop EN", "description": "d",
                                           "short_description": ""}},
                extra_cmds={"de": [{"command": "start", "description": "Los"}]})
    tgt = _prof()
    picked = ca.build_plan(src, tgt, ["name"], ca.normalize_settings({"langs": ["en"]}))
    assert _step(picked, "name:en")["after"] == "Shop EN"
    every = ca.normalize_settings({"langs": "all"})
    assert ca.effective_langs(every, src) == ["", "en", "de"]
    plan = ca.build_plan(src, tgt, ["commands"], every)
    assert _step(plan, "commands:de")["after"] == [{"command": "start", "description": "Los"}]


def test_photo_is_planned_only_when_source_has_one():
    src = _prof()
    src["has_photo"] = False
    plan = ca.build_plan(src, _prof(), ["photo"], ca.normalize_settings({}))
    assert _step(plan, "photo")["same"] and "нет фото" in _step(plan, "photo")["note"]
    src["has_photo"] = True
    plan = ca.build_plan(src, _prof(), ["photo"], ca.normalize_settings({}))
    assert _step(plan, "photo")["same"] is False


# ── Снимок и откат ───────────────────────────────────────────────────────────

def test_snapshot_keeps_only_what_changes_and_never_the_photo():
    src = _prof(name="A", desc="new")
    src["has_photo"] = True
    tgt = _prof(name="A", desc="old")
    plan = ca.build_plan(src, tgt, ["name", "desc", "photo"], ca.normalize_settings({}))
    assert ca.snapshot_before(plan) == {"description": "old"}


def test_restore_calls_cover_languages_commands_menu_and_rights():
    calls = {m + ":" + str(sorted(p.items())): lbl for lbl, m, p in ca.restore_calls({
        "name:en": "Old EN",
        "commands": [],
        "commands:de": [{"command": "a", "description": "b"}],
        "menu_button": {},
        "rights_channels": {},
        "greeting": "не наше",
    })}
    methods = sorted(k.split(":")[0] for k in calls)
    assert methods == ["deleteMyCommands", "setChatMenuButton",
                       "setMyCommands", "setMyDefaultAdministratorRights", "setMyName"]
    by = {lbl: (m, p) for lbl, m, p in ca.restore_calls({"name:en": "Old EN"})}
    assert by["Имя (английский)"] == ("setMyName", {"name": "Old EN", "language_code": "en"})


def test_base_keys_stay_compatible_with_bulk_profile_snapshots():
    """Откат общий с массовой правкой профиля: её снимки — name/description/
    short_description без языка, и они обязаны откатываться как раньше."""
    got = {m: p for _l, m, p in ca.restore_calls(
        {"name": "N", "description": "D", "short_description": "S"})}
    assert got == {"setMyName": {"name": "N"},
                   "setMyDescription": {"description": "D"},
                   "setMyShortDescription": {"short_description": "S"}}


# ── Исполнитель и вход из бота ───────────────────────────────────────────────

def _func_src(path, name):
    src = open(os.path.join(ROOT, path), encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} не найден в {path}")


def test_executor_reads_decrypted_tokens():
    body = _func_src("services/op_worker.py", "_exec_clone_adapt")
    assert "fetch_bots" in body, (
        "токены ботов зашифрованы — читать их можно только через fetch_bots, "
        "иначе в Telegram уходит шифротекст и клон не работает никогда")
    assert "_safe_fetchrow" not in body


def test_executor_snapshots_before_changing_and_writes_history_details():
    body = _func_src("services/op_worker.py", "_exec_clone_adapt")
    snap = body.index("INSERT INTO bulk_edit_snapshots")
    apply_ = body.index("apply_step(")
    assert snap < apply_, "снимок «как было» обязан сниматься ДО изменения"
    assert "completed_targets(" in body, "повтор обязан пропускать сделанных"
    assert "changes" in body and "op_id" in body
    assert "_is_cancelled(" in body


def test_rollback_executor_uses_engine_restore_and_clone_has_its_own_op_type():
    body = _func_src("services/op_worker.py", "_exec_bulk_bot_profile_rollback")
    assert "restore_calls" in body
    from services.operation_bus import OP_REGISTRY
    assert OP_REGISTRY["clone_adapt_rollback"]["min_plan"] == OP_REGISTRY["clone_adapt"]["min_plan"]
    src = open(os.path.join(ROOT, "services/op_worker.py"), encoding="utf-8").read()
    assert '"clone_adapt_rollback": _exec_bulk_bot_profile_rollback' in src


def test_bot_wizard_goes_through_the_operation_queue():
    body = _func_src("bot/handlers/clone_adapt_hub.py", "cb_ca_run")
    assert "_obus.submit(" in body and '"clone_adapt"' in body
    assert "set_name(" not in body and "set_photo(" not in body, (
        "бот снова клонирует в обработчике кнопки, мимо очереди и отката")
