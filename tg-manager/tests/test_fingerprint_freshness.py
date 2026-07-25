"""Гейт: пул отпечатков клиента не протухает молча (anti-detection).

Telegram-клиент АВТООБНОВЛЯЕТСЯ у живых пользователей. Значит пул версий
приложения — стареющий актив: если его не пересматривать, все наши аккаунты
начинают заявлять прошлогоднюю версию и превращаются в КОГОРТНУЮ СИГНАТУРУ
(похожи друг на друга, не похожи на живую популяцию). Это дороже обычного бага,
и «протухание» происходит без единой ошибки в логах — само, от течения времени.

Поэтому свежесть сторожит тест: он краснеет, когда пул не пересматривали дольше
FINGERPRINT_MAX_AGE_DAYS. Что делать при красном:
  1. сверить актуальную ветку Telegram Android (и версии ОС);
  2. обновить _DEFAULT_APP_VERSIONS / _DEFAULT_ANDROID_DEVICES;
  3. поднять FINGERPRINT_REVIEWED на сегодняшнюю дату.
Срочная актуализация без деплоя — через env TG_APP_VERSIONS/TG_ANDROID_DEVICES.

Найдено ревизией 2026-07: пул стоял на Telegram 11.x/Android 14 при актуальных
12.9.x/Android 17 — отставание примерно на полтора года.
"""
from __future__ import annotations

import datetime as dtm
import os
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "services" / "account_manager.py"


def _const(name: str) -> str:
    m = re.search(rf'^{name}\s*=\s*["\']?([^"\'\n#]+)', SRC.read_text(encoding="utf-8"), re.M)
    assert m, f"{name} не найдена"
    return m.group(1).strip()


def test_fingerprint_pool_reviewed_recently():
    reviewed = dtm.date.fromisoformat(_const("FINGERPRINT_REVIEWED"))
    max_age = int(_const("FINGERPRINT_MAX_AGE_DAYS"))
    age = (dtm.date.today() - reviewed).days
    assert age <= max_age, (
        f"пул отпечатков не пересматривали {age} дн. (лимит {max_age}). "
        "Версия Telegram у живых людей обновляется сама — устаревший пул делает "
        "наши аккаунты отличимой когортой. Сверьте актуальную версию клиента, "
        "обновите _DEFAULT_APP_VERSIONS и поднимите FINGERPRINT_REVIEWED."
    )
    assert age >= 0, "FINGERPRINT_REVIEWED из будущего — дата ревизии недостоверна"


def test_app_versions_are_a_tight_recent_window():
    """Версии приложения — узкое свежее окно, без хвоста на старых мажорах."""
    src = SRC.read_text(encoding="utf-8")
    m = re.search(r"_DEFAULT_APP_VERSIONS[^=]*=\s*\[(.*?)\n\]", src, re.DOTALL)
    assert m, "_DEFAULT_APP_VERSIONS не найден"
    versions = re.findall(r'"(\d+)\.(\d+)\.(\d+)"', m.group(1))
    assert versions, "версии не разобраны"
    majors = {int(v[0]) for v in versions}
    assert len(majors) <= 2, (
        f"в пуле мажоры {sorted(majors)}: клиент автообновляется, поэтому широкий "
        "разброс по мажорным версиям неправдоподобен"
    )


def test_device_pool_spans_multiple_os_generations():
    """Устройства, наоборот, ОБЯЗАНЫ быть разных лет: пул из одних новинок так же
    неправдоподобен, как пул из одного старья (люди держат телефон 3-4 года)."""
    src = SRC.read_text(encoding="utf-8")
    m = re.search(r"_DEFAULT_ANDROID_DEVICES[^\[]*\[(.*?)\n\]", src, re.DOTALL)
    assert m, "_DEFAULT_ANDROID_DEVICES не найден"
    os_versions = {int(x) for x in re.findall(r'"Android (\d+)"', m.group(1))}
    assert len(os_versions) >= 3, (
        f"версий ОС в пуле: {sorted(os_versions)} — нужен разброс поколений, "
        "иначе устройства выглядят однородной фермой"
    )


def test_pools_overridable_without_deploy(monkeypatch):
    """Срочная актуализация должна быть возможна через env, без релиза."""
    src = SRC.read_text(encoding="utf-8")
    assert "TG_APP_VERSIONS" in src and "TG_ANDROID_DEVICES" in src, (
        "нужен env-переопределяемый пул: новая версия Telegram выходит не по "
        "расписанию наших деплоев"
    )


def test_broken_env_falls_back_not_crashes():
    """Опечатка в env не должна ронять старт — fail-safe на встроенный пул."""
    src = SRC.read_text(encoding="utf-8")
    m = re.search(r"def _pool_from_env.*?(?=\n\n_ANDROID_DEVICES)", src, re.DOTALL)
    assert m, "_pool_from_env не найден"
    body = m.group(0)
    assert "except Exception" in body and "return fallback" in body, (
        "разбор env обязан быть fail-safe: лучше встроенный пул, чем падение старта"
    )
