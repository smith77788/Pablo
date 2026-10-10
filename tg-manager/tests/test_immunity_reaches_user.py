"""Ban Weather: данные о потерях доходят до пользователя, а не копятся молча.

Половина системы была построена и выключена: триггер БД писал каждую смерть
аккаунта в `account_status_events`, движок иммунитета умел считать по ним
автопсию — но цикл не запускался, а результат никто не показывал.

Запуск цикла без экрана закрывает проблему только наполовину: данные начинают
считаться, но пользователь по-прежнему узнаёт лишь «аккаунт улетел». Петля
«смерть → вывод → защита живых» замыкается на человеке, поэтому здесь
проверяется вся цепочка: триггер → цикл → автопсия → экран.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from services.immunity_engine import build_autopsy, compute_signature

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
HUB = ROOT / "bot" / "handlers" / "account_shield_hub.py"
SCHEMA = ROOT / "schema_v146_ban_weather.sql"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── Цепочка подключена ──────────────────────────────────────────────────────

def test_engine_loop_is_started_exactly_once():
    """Цикл обязан запускаться — и ровно один раз.

    Два запуска дали бы двойную обработку одних и тех же событий: оба прохода
    видят `processed_at IS NULL` и считают автопсию параллельно.
    """
    src = _src(MAIN)
    starts = src.count("immunity_engine.start")
    assert starts >= 1, "движок иммунитета не запускается — события копятся необработанными"
    assert starts == 1, f"движок запускается {starts} раза — дубль цикла"


def test_capture_trigger_exists_in_schema():
    # Без триггера цикл будет вечно разбирать пустую таблицу.
    sql = _src(SCHEMA)
    assert "trg_immunity_capture_status" in sql
    assert "account_status_events" in sql


def test_autopsy_is_shown_to_user():
    """Регресс: автопсия считалась и не читалась НИКЕМ за пределами движка.

    Это ровно «данные копятся невидимо» — сценарий, доведённый до половины.
    """
    src = _src(HUB)
    assert "account_status_events" in src, "экран не читает события статусов"
    assert "autopsy" in src, "экран не показывает разбор"
    assert 'F.action == "autopsy"' in src, "нет обработчика экрана"


def test_autopsy_screen_reachable_from_menu():
    """Экран без входа — мёртвый код.

    Кнопка обязана быть в меню Shield Hub, а не только в коде обработчика.
    """
    src = _src(HUB)
    menu = src[src.find('F.action == "menu"') : src.find('F.action == "top10"')]
    assert 'action="autopsy"' in menu, "кнопка разбора потерь отсутствует в меню"


def test_screen_is_owner_scoped():
    """Смерти чужих аккаунтов не должны утекать между владельцами."""
    src = _src(HUB)
    screen = src[src.find('F.action == "autopsy"') :]
    assert "owner_id = $1" in screen or "e.owner_id = $1" in screen


def test_screen_tolerates_missing_schema():
    """Схема v146 могла быть не применена — это не ошибка пользователя.

    Без перехвата экран превратился бы в тупик с сырым текстом исключения.
    """
    src = _src(HUB)
    screen = src[src.find('F.action == "autopsy"') :]
    assert "except Exception" in screen


# ── Содержательность разбора ────────────────────────────────────────────────

_FEATURES = {
    "actions_72h": 340,
    "op_counts": {"mass_invite": 300, "bulk_join": 40},
    "geo_country": "RU",
    "warming_age_days": 2.0,
    "trust_score": 0.35,
}


def test_autopsy_names_cause_and_suggests_action():
    """Разбор должен отвечать «почему» и «что делать», а не пересказывать факт.

    Отчёт без причины и без предложения не даёт пользователю ничего сверх того,
    что он уже видел в уведомлении о бане.
    """
    a = build_autopsy(
        {"acc_id": 42, "new_status": "banned", "is_death": True},
        _FEATURES,
        {"median_actions_72h": 60},
    )
    assert a["probable_cause"], "причина не определена"
    assert a["suggested_rule"].get("action") in ("quarantine", "throttle")
    assert a["suggested_rule"].get("note"), "правило без пояснения бесполезно"
    # Сравнение с выжившими — то, чего нет в обычном уведомлении о бане.
    assert "выживших" in (a["differed_from_survivors"] or "")


def test_screen_translates_action_to_human_words():
    """«throttle» и «quarantine» в интерфейсе ничего не значат для человека."""
    src = _src(HUB)
    screen = src[src.find('F.action == "autopsy"') :]
    assert "карантин" in screen and "темп" in screen


def test_signature_is_stable_for_same_features():
    # Повторяющаяся сигнатура — главный сигнал вспышки; нестабильная сигнатура
    # сделала бы группировку по паттерну бессмысленной.
    assert compute_signature(_FEATURES) == compute_signature(dict(_FEATURES))


def test_repeating_pattern_is_surfaced():
    """Одна смерть — случай, три с одной сигнатурой — выкашивающий паттерн.

    Ради этого разбор и нужен, поэтому агрегат обязан быть на экране.
    """
    src = _src(HUB)
    screen = src[src.find('F.action == "autopsy"') :]
    assert "GROUP BY signature" in screen
    assert "HAVING COUNT(*) > 1" in screen


def test_pending_events_reported_honestly():
    """«Ещё в обработке» ≠ «причина неизвестна».

    Цикл ходит раз в 2 минуты; без явной пометки свежая потеря выглядела бы
    как потеря без причины.
    """
    src = _src(HUB)
    screen = src[src.find('F.action == "autopsy"') :]
    assert "processed_at" in screen
    assert "обработке" in screen


def test_autopsy_json_from_db_is_parsed():
    """asyncpg отдаёт jsonb то словарём, то строкой — экран обязан пережить оба."""
    src = _src(HUB)
    screen = src[src.find('F.action == "autopsy"') :]
    assert "isinstance(data, str)" in screen
    assert "json" in screen.lower()


def test_empty_state_explains_what_will_appear():
    """Пустой экран обязан объяснять, что здесь появится.

    Иначе «потерь нет» читается как «раздел не работает».
    """
    src = _src(HUB)
    screen = src[src.find('F.action == "autopsy"') :]
    assert "разбирать нечего" in screen
    assert "появится" in screen


def test_build_autopsy_survives_empty_features():
    # Свежий аккаунт без истории не должен ронять разбор.
    a = build_autopsy({"acc_id": 1, "new_status": "banned", "is_death": True}, {}, {})
    assert isinstance(a, dict) and a.get("summary")
    json.dumps(a, ensure_ascii=False)  # должен быть сериализуем для jsonb
