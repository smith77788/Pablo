"""Витрина пускает людей в боевой канал темпом, а не всплеском.

«Мать-Дочка» закрывает бан за инвайт, но создаёт второй вектор: приглашённые
идут в мать по одной закреплённой ссылке, и канал получает двести вступлений за
два часа — сигнал, по которому его и закрывают.

Промежуточный слой сам по себе тут не помогает: если витрина просто закрепляет
ссылку на мать, бросок сдвинется на шаг и только. Работает не косвенность, а
пропускная способность — ссылка выпускается с лимитом вступлений, исчерпалась,
следующая волна выпускает новую.

Здесь проверяется именно эта логика: она чистая, считается без сети и базы.
"""
from __future__ import annotations

import importlib

import pytest

from services import showcase_layer as sl


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Тест не должен зависеть от env хоста и не должен его за собой оставлять."""
    for k in ("SHOWCASE_WAVE_MIN", "SHOWCASE_WAVE_MAX", "SHOWCASE_WAVE_INTERVAL_SEC"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(sl)
    yield
    importlib.reload(sl)


# ── размер волны ─────────────────────────────────────────────────────────────

def test_wave_scales_with_channel_size():
    """+50 человек к каналу на 200 — это +25% за полчаса, заметный скачок;
    для канала на 50 000 те же 50 не видны вовсе. Значит волна обязана
    зависеть от размера."""
    assert sl.wave_size(200) < sl.wave_size(50_000)


def test_small_channel_still_grows():
    """Нижняя граница: без неё канал на 20 участников получал бы волну в одного
    человека и рос бы вечно."""
    assert sl.wave_size(20) >= 10
    assert sl.wave_size(0) >= 10


def test_huge_channel_does_not_get_a_burst():
    """Потолок: «5% от 100 000» — это 5000 вступлений за волну, то есть тот же
    всплеск, только крупнее."""
    assert sl.wave_size(100_000) <= 50


def test_wave_bounds_are_configurable(monkeypatch):
    monkeypatch.setenv("SHOWCASE_WAVE_MIN", "3")
    monkeypatch.setenv("SHOWCASE_WAVE_MAX", "7")
    importlib.reload(sl)
    assert sl.wave_size(10) == 3
    assert sl.wave_size(1_000_000) == 7


def test_broken_env_does_not_disable_pacing(monkeypatch):
    """Мусор в переменной не должен превращаться в «пускаем всех сразу»."""
    monkeypatch.setenv("SHOWCASE_WAVE_MAX", "не число")
    importlib.reload(sl)
    assert sl.wave_size(100_000) <= 50


def test_wave_size_survives_garbage_input():
    """Размер канала приходит из Telegram и может быть None."""
    assert sl.wave_size(None) >= 10
    assert sl.wave_size("много") >= 10


# ── темп ─────────────────────────────────────────────────────────────────────

def test_first_wave_goes_immediately():
    """Буфер без единой открытой двери — это не темп, а остановка."""
    assert sl.next_wave_due(None, now_ts=1000.0) is True


def test_second_wave_waits_the_interval():
    now = 100_000.0
    assert sl.next_wave_due(now - 10, now) is False
    assert sl.next_wave_due(now - sl.wave_interval_sec() - 1, now) is True


def test_interval_cannot_be_collapsed_to_zero(monkeypatch):
    """Иначе «настройкой» можно вернуть ровно тот всплеск, от которого уходим."""
    monkeypatch.setenv("SHOWCASE_WAVE_INTERVAL_SEC", "0")
    importlib.reload(sl)
    assert sl.wave_interval_sec() >= 60


# ── план для интерфейса ──────────────────────────────────────────────────────

def test_plan_shows_the_price_of_pacing():
    """Владелец должен видеть, во что обойдётся темп, ДО запуска."""
    plan = sl.release_plan(audience_left=200, mother_members=1_000)
    assert plan["size"] == sl.wave_size(1_000)
    assert plan["waves_left"] == (200 + plan["size"] - 1) // plan["size"]
    assert plan["eta_sec"] > 0


def test_first_wave_is_not_counted_as_waiting():
    """Она уходит сразу, поэтому ждать надо на одну паузу меньше."""
    plan = sl.release_plan(audience_left=1, mother_members=1_000)
    assert plan["waves_left"] == 1
    assert plan["eta_sec"] == 0


def test_empty_audience_needs_no_waves():
    plan = sl.release_plan(audience_left=0, mother_members=1_000)
    assert plan["waves_left"] == 0 and plan["eta_sec"] == 0


def test_eta_is_human_readable():
    assert sl.humanize_eta(0) == "меньше минуты"
    assert sl.humanize_eta(90 * 60) == "1 ч 30 мин"
    assert sl.humanize_eta(2 * 3600) == "2 ч"
    assert sl.humanize_eta(45 * 60) == "45 мин"


# ── проводка: галочка → API → исполнитель ────────────────────────────────────
#
# Самый частый дефект этого проекта — настройка, которая есть на экране и
# никуда не доходит. Проверяем всю цепочку, а не только ядро.

def _read(rel: str) -> str:
    import pathlib
    return (pathlib.Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")


def test_toggle_exists_and_is_sent():
    ui = _read("mini_app/index.html")
    js = _read("mini_app/screens/invite.js")
    assert 'id="invShowcase"' in ui, "переключателя витрины нет на экране"
    assert "use_showcase = true" in js, "галочка ничего не отправляет"


def test_api_accepts_the_flag():
    api = _read("services/mini_app_api.py")
    assert '"use_showcase"' in api and 'params["use_showcase"] = True' in api, (
        "API не принимает параметр — галочка не доедет до исполнителя"
    )


def test_executor_requires_daughter_groups():
    """Витрина без дочерней группы — лишнее звено без защиты: инвайт всё равно
    шёл бы прямо в боевой канал, просто с буфером сбоку."""
    src = _read("services/op_worker.py")
    assert 'params.get("use_showcase")) and _use_daughter' in src, (
        "витрину можно включить без дочерних групп — защита станет декорацией"
    )


def test_daughter_redirects_to_showcase():
    """Смысл слоя в том, что закреплённая ссылка ведёт в буфер, а не в мать."""
    dg = _read("services/daughter_groups.py")
    ow = _read("services/op_worker.py")
    assert "redirect_ref" in dg, "дочерняя не умеет вести никуда, кроме матери"
    assert "redirect_ref=_showcase_ref" in ow, "инвайт не передаёт ссылку витрины"


def test_rotation_keeps_the_showcase():
    """Сгорела дочерняя — новая обязана вести туда же, иначе после первой
    ротации люди снова пойдут прямо в канал."""
    ow = _read("services/op_worker.py")
    assert ow.count("redirect_ref=_showcase_ref") >= 2, (
        "при ротации дочерней ссылка на витрину теряется"
    )


def test_failed_showcase_does_not_pretend_to_protect():
    """Витрина не поднялась — прогон продолжается, но молчать нельзя: владелец
    рассчитывал на защиту, а её нет."""
    ow = _read("services/op_worker.py")
    assert "витрина не создана" in ow and "_use_showcase = False" in ow


def test_summary_tells_which_protection_worked():
    ow = _read("services/op_worker.py")
    assert "🏪 Витрина:" in ow, "отчёт молчит о витрине — проверить её работу нечем"
    assert "Снимает это витрина" in ow, (
        "предупреждение о всплеске не подсказывает, чем он снимается"
    )


# ── фоновый выпуск волн ──────────────────────────────────────────────────────

def test_wave_link_expires_with_the_interval():
    """Ссылка обязана жить ровно одну паузу.

    Без срока жизни неиспользованные места НАКАПЛИВАЮТСЯ: десять волн по 50
    оставили бы 500 открытых дверей одновременно — тот самый всплеск, от
    которого уходим, просто отложенный на пять часов.
    """
    src = _read("services/showcase_layer.py")
    assert "expire_seconds=wave_interval_sec()" in src, (
        "у ссылки волны нет срока жизни — места будут копиться"
    )


def test_loop_exists_and_is_started():
    """Без цикла витрина отдала бы первую порцию и замерла: аудитория копится,
    а войти некуда."""
    src = _read("services/showcase_layer.py")
    main = _read("main.py")
    assert "async def run(pool" in src, "фонового цикла нет"
    assert "showcase_waves" in main, "цикл не запускается вместе с процессом"


def test_one_broken_showcase_does_not_stop_the_rest():
    src = _read("services/showcase_layer.py")
    assert "не должна ронять проход по остальным" in src


def test_mother_is_resolved_explicitly_not_guessed():
    """Ошибиться каналом здесь дороже, чем не выпустить волну: ссылку с местами
    выпустили бы в чужой канал."""
    src = _read("services/showcase_layer.py")
    assert "managed_channels" in src and "волна пропущена" in src


def test_wave_size_uses_real_member_count():
    """Размер волны зависит от размера канала — значит нужен реальный счётчик.
    Колонку members_count заполняет обход диалогов; до этой починки она у всех
    стояла в нуле, и волна всегда была бы минимальной."""
    src = _read("services/showcase_layer.py")
    assert "COALESCE(members_count,0)" in src
