"""Размер аудитории показывается ДО запуска инвайта.

ЖАЛОБА ПОЛЬЗОВАТЕЛЯ: «инвайтинг не работает совсем».

ЧТО ПРОИСХОДИЛО. Источник аудитории по умолчанию — «Parsed Audience (из
парсера)». У пользователя, который парсер ещё не запускал, он пуст. Пользователь
вводил группу, жал «Запустить», получал тост «инвайт запущен (#123)» — и НИЧЕГО
не происходило: операция уходила в очередь и падала с «⚠️ Аудитория пуста»,
увидеть что можно было только в истории операций. Снаружи это неотличимо от
«продукт не работает».

Асимметрия была прямо в коде: DM-кампании считают получателей ДО старта
(`total_targets` в `dm_campaign_create`), инвайт — нет.

Запросы счётчика намеренно повторяют те, что делает исполнитель при загрузке
аудитории: показанное число обязано совпасть с тем, что реально возьмёт
операция. Расхождение хуже отсутствия счётчика — оно врёт с видом точности.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
# Мини-апп больше не один файл: экраны вынесены в mini_app/screens/*.js.
# Источник берём целиком, иначе вынос экрана роняет проверку, хотя
# функциональность на месте.
from tests.miniapp_source import miniapp_source

HTML = miniapp_source()
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _handler() -> str:
    m = re.search(r"    async def invite_audience_size\(request.*?\n    async def ",
                  API, re.DOTALL)
    assert m, "эндпойнт размера аудитории не найден"
    return m.group(0)


def _exec_body() -> str:
    m = re.search(r"async def _exec_mass_invite\(.*?(?=\nasync def )", WORKER, re.DOTALL)
    assert m
    return m.group(0)


def test_route_registered():
    assert '"/api/miniapp/invite/audience"' in API, "экран стучался бы в никуда"


def test_counts_every_source_the_executor_supports():
    body = _handler()
    for src in ("parsed", "crm", "bot_users"):
        assert f'"{src}"' in body, f"источник {src} не считается"


def test_counter_matches_what_the_executor_loads():
    """Счётчик и исполнитель обязаны смотреть в одни и те же таблицы."""
    body, ex = _handler(), _exec_body()
    for table in ("parsed_audiences", "crm_contacts", "bot_users"):
        assert table in body and table in ex, (
            f"{table}: счётчик и исполнитель разошлись по источнику данных"
        )
    assert "parse_run_id" in body, (
        "заход «из парсера» инвайтит КОНКРЕТНЫЙ запуск — счётчик обязан считать его же"
    )


def test_cap_is_disclosed():
    """Исполнитель берёт не больше 2000 за прогон — счётчик не должен обещать больше."""
    assert "LIMIT 2000" in _exec_body(), "предпосылка изменилась — проверить потолок"
    assert "capped_at" in _handler(), "потолок обязан доезжать до пользователя"


def test_db_failure_returns_unknown_not_zero():
    """Ноль означает «источник пуст» и оттолкнул бы от запуска РАБОЧЕЙ операции.
    При сбое надо честно молчать, а не показывать ложный ноль."""
    body = _handler()
    m = re.search(r"except Exception:(.*?)return _json_resp\(\{[^}]*\}\)", body, re.DOTALL)
    assert m, "нет обработки сбоя"
    assert '"total": None' in m.group(0), "при сбое total обязан быть null, а не 0"


def test_empty_source_offers_a_next_step():
    """Пустой источник — не тупик: пользователь должен узнать, откуда брать аудиторию."""
    body = _handler()
    assert "hint" in body
    assert "Парсер" in body, "подсказка должна называть конкретное место"


# ── фронт ────────────────────────────────────────────────────────────────────

def test_ui_loads_and_shows_the_size():
    assert 'id="massInviteAudience"' in HTML, "негде показывать"
    assert "loadInviteAudienceSize" in HTML
    m = re.search(r"function massInviteSrcToggle\(\)\s*\{.*?\n\}", HTML, re.DOTALL)
    assert m and "loadInviteAudienceSize()" in m.group(0), (
        "смена источника обязана пересчитывать аудиторию, иначе число врёт"
    )


def test_empty_source_blocks_launch():
    m = re.search(r"async function submitMassInvite\(\)\s*\{.*?\n\}", HTML, re.DOTALL)
    assert m, "обработчик запуска не найден"
    body = m.group(0)
    assert "INV_AUDIENCE === 0" in body, (
        "заведомо пустой инвайт не должен уходить в очередь — иначе пользователь "
        "снова получит «запущено» и тишину"
    )
    assert "import_list" in body.split("INV_AUDIENCE === 0")[1][:200], (
        "свой список считается на клиенте — на него блокировка не распространяется"
    )


def test_unknown_size_does_not_block_launch():
    """Сбой счётчика не имеет права запретить рабочую операцию."""
    m = re.search(r"async function submitMassInvite\(\)\s*\{.*?\n\}", HTML, re.DOTALL)
    assert "INV_AUDIENCE === 0" in m.group(0), "сравнение обязано быть строгим"
    assert "INV_AUDIENCE === null" not in m.group(0).split("const body")[0], (
        "null (не знаем) не должен блокировать запуск"
    )


def test_pace_auto_has_a_label():
    """В подтверждении показывалось сырое «Темп: auto» — режим добавили в селектор,
    а подпись для диалога забыли."""
    m = re.search(r"const _paceLbl = \{[^}]*\}", HTML)
    assert m and "auto:" in m.group(0), "у режима «Авто» нет человеческой подписи"
