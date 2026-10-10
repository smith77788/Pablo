"""Молчащий бот перестал выглядеть работающим.

Разрывы.

1. Опрос ботов (`auto_responder`) уже умел отличать отозванный токен от сетевой
   ошибки и даже ставил такому боту экспоненциальный отступ — но знал об этом
   ТОЛЬКО серверный лог. На экране бот оставался «🟢 Активен»: подписчики ему
   писали, ответа не было, и владельцу никто ничего не говорил. Молчит не
   внутренний инструмент, а лицо, которым владелец повёрнут к своей аудитории.

2. Заменить токен было нельзя: `db.add_bot` при существующем bot_id возвращает
   «уже добавлен» и токен НЕ обновляет. Оставался единственный путь — удалить
   бота и добавить заново, а `bot_users` висит на `managed_bots` с
   `ON DELETE CASCADE`: вместе с ботом стиралась вся его аудитория. Продукт
   предлагал вылечить молчание ценой базы подписчиков.

3. Карточка бота отдавалась клиенту запросом `SELECT *` — вместе с колонкой
   `token`. У ботов, добавленных до включения шифрования, он лежит в открытом
   виде (`decrypt_token` намеренно пропускает legacy-строки как есть), то есть
   выдача отдавала полный контроль над ботом всякому, кто её видит — включая
   участника рабочего пространства, которому бот доступен только на чтение.
"""
from __future__ import annotations

import ast
import pathlib

from services import bot_health as BH

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_AR = (_ROOT / "services" / "auto_responder.py").read_text(encoding="utf-8")
_DB = (_ROOT / "database" / "db.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _func_src(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"функция {name} не найдена")


# ── Классификация ошибки ───────────────────────────────────────────────────

def test_revoked_token_is_recognised_by_code_and_by_text():
    assert BH.classify_error("Unauthorized", 401) == BH.UNAUTHORIZED
    assert BH.classify_error("", 401) == BH.UNAUTHORIZED
    assert BH.classify_error("Unauthorized") == BH.UNAUTHORIZED


def test_webhook_conflict_is_its_own_kind():
    assert BH.classify_error("Conflict: terminated by other getUpdates", 409) == BH.CONFLICT
    assert BH.classify_error("can't use getUpdates while webhook is active") == BH.CONFLICT


def test_rate_limit_and_network_are_not_alarming_kinds():
    assert BH.classify_error("Too Many Requests", 429) == BH.FLOOD
    assert BH.classify_error("connection timeout") == BH.NETWORK


def test_unknown_error_does_not_masquerade_as_a_dead_token():
    """Иначе любая случайная ошибка звала бы человека менять рабочий токен."""
    assert BH.classify_error("something odd happened", 500) == BH.OTHER


# ── Когда звать человека ───────────────────────────────────────────────────

def test_alert_only_after_a_streak():
    assert BH.decide_alert(BH.UNAUTHORIZED, 1, False) is False
    assert BH.decide_alert(BH.UNAUTHORIZED, BH.FAIL_STREAK_TO_ALERT, False) is True


def test_no_alert_for_things_that_pass_on_their_own():
    for kind in (BH.FLOOD, BH.NETWORK, BH.OTHER):
        assert BH.decide_alert(kind, 99, False) is False


def test_each_breakage_is_announced_once():
    assert BH.decide_alert(BH.UNAUTHORIZED, 99, True) is False


# ── Состояние для экрана ───────────────────────────────────────────────────

def test_healthy_bot_reads_as_working():
    d = BH.describe({"is_active": True, "fail_streak": 0})
    assert d["state"] == "ok" and d["attention"] is False


def test_revoked_token_bot_is_not_shown_as_active():
    d = BH.describe({"is_active": True, "fail_streak": 5, "last_error": BH.UNAUTHORIZED})
    assert d["state"] == "broken" and d["attention"] is True
    assert "токен" in d["label"].lower()
    assert "botfather" in d["hint"].lower()


def test_a_passing_problem_is_shown_without_raising_alarm():
    d = BH.describe({"is_active": True, "fail_streak": 2, "last_error": BH.NETWORK})
    assert d["state"] == "degraded" and d["attention"] is False


def test_switched_off_bot_is_not_called_broken():
    d = BH.describe({"is_active": False, "fail_streak": 9, "last_error": BH.UNAUTHORIZED})
    assert d["state"] == "off" and d["attention"] is False


def test_row_from_an_older_schema_does_not_break_the_screen():
    d = BH.describe({"is_active": True})
    assert d["state"] == "ok"


def test_unknown_stored_kind_falls_back_instead_of_crashing():
    d = BH.describe({"is_active": True, "fail_streak": 4, "last_error": "чепуха"})
    assert d["label"] and d["hint"]


def test_alert_names_the_cost_of_silence():
    txt = BH.build_alert("@shopbot", BH.UNAUTHORIZED, 4)
    assert "@shopbot" in txt and "4" in txt
    assert "подписчик" in txt.lower()


# ── Проводка: запись состояния и уведомление ───────────────────────────────

def test_poller_writes_the_reason_not_just_a_log_line():
    src = _func_src(_AR, "_process_bot")
    assert "_record_bot_error" in src
    assert "_record_bot_ok" in src


def test_success_clears_both_the_complaint_and_the_notice_mark():
    """Иначе о повторной поломке того же бота промолчали бы из-за старой
    отметки «уже сообщили»."""
    src = _func_src(_AR, "_record_bot_ok")
    assert "fail_streak=0" in src and "dead_notified_at=NULL" in src


def test_recording_state_never_breaks_the_poll():
    src = _func_src(_AR, "_record_bot_error")
    assert "except Exception" in src


def test_owner_is_notified_once_per_breakage():
    src = _func_src(_AR, "notify_broken_bots")
    assert "dead_notified_at IS NULL" in src
    assert "decide_alert" in src
    assert "dead_notified_at=now()" in src


def test_notifications_are_wired_into_the_polling_loop():
    src = _func_src(_AR, "run")
    assert "notify_broken_bots" in src


# ── Замена токена без потери аудитории ─────────────────────────────────────

def test_token_can_be_replaced_in_place():
    src = _func_src(_DB, "replace_bot_token")
    assert "UPDATE managed_bots" in src
    assert "added_by=$2" in src          # только свой бот
    assert "fail_streak=0" in src        # починили — жалоба снимается
    assert "encrypt_token" in src or "_enc_tok" in src


def test_adding_an_existing_bot_replaces_its_token():
    """Именно этим путём человек и чинит молчащего бота: вставляет новый токен
    туда же, где добавлял. Раньше здесь отвечали «уже добавлен» и не меняли
    ничего."""
    src = _func_src(_API, "bot_add")
    assert "replace_bot_token" in src
    assert "token_replaced" in src


def test_ui_says_the_audience_survived_the_token_swap():
    assert "token_replaced" in _UI
    assert "openBotTokenFix" in _UI


# ── Токен не уходит клиенту ────────────────────────────────────────────────

def test_bot_card_no_longer_ships_the_token_to_the_client():
    src = _func_src(_API, "bot_detail")
    assert '"token"' in src and "SELECT * FROM managed_bots" in src
    # Отсев идёт до формирования ответа.
    assert src.index('"token"') < src.index("_json_resp")


def test_bots_list_shows_the_real_state():
    src = _func_src(_API, "bots")
    assert "bot_health" in src
    assert "b.health" in _UI
    # Лаг миграции не должен опустошить список: пустая выдача читалась бы как
    # «ботов нет».
    assert src.count("_BOTS_SQL.format") == 2


def test_schema_and_self_heal_add_the_health_columns():
    sql = (_ROOT / "schema_v198_bot_health.sql").read_text(encoding="utf-8")
    for col in ("last_error", "fail_streak", "last_ok_at", "dead_notified_at"):
        assert col in sql
        assert f"ADD COLUMN IF NOT EXISTS {col}" in _API
