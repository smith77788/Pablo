"""«Нотариус»: правила вывода. Здесь решается, стоит продукт денег или нет.

Протокол продаётся второй стороне сделки, поэтому цена ошибок несимметрична:
не заметить нарушение — потерять один случай; обвинить невиновного — потерять
продукт. Отсюда четыре запрета, которые и стережёт этот файл.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services import notary as N

T0 = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


def _o(minutes: int, state: str, views: int | None = None) -> dict:
    return {"observed_at": T0 + timedelta(minutes=minutes),
            "state": state, "views": views}


def _fold(obs, hours: float = 24, now_min: int = 0):
    return N.fold(obs, promised_from=T0,
                  promised_until=T0 + timedelta(hours=hours),
                  now=T0 + timedelta(minutes=now_min))


# ── Запрет №1: несостоявшееся наблюдение — не улика ────────────────────────

def test_network_failures_never_accuse_the_channel():
    """Ряд из ошибок наблюдения не превращается в «снял раньше срока»."""
    obs = [_o(0, N.PRESENT), _o(5, N.UNKNOWN), _o(10, N.UNKNOWN),
           _o(15, N.UNKNOWN), _o(20, N.UNKNOWN)]
    assert _fold(obs, now_min=25)["verdict"] != N.V_EARLY


def test_unknown_does_not_count_towards_confirmed_absence():
    obs = [_o(0, N.PRESENT), _o(5, N.UNKNOWN), _o(10, N.UNKNOWN)]
    assert _fold(obs, now_min=15)["absent_since"] is None


def test_unknown_between_absences_does_not_break_the_streak():
    """Пропущенный такт не «лечит» отсутствие: пост как не было, так и нет."""
    obs = [_o(0, N.PRESENT), _o(5, N.ABSENT), _o(10, N.UNKNOWN), _o(15, N.ABSENT)]
    f = _fold(obs, now_min=20)
    assert f["absent_since"] == T0 + timedelta(minutes=5)
    assert f["verdict"] == N.V_EARLY


# ── Запрет №2: одно отсутствие ничего не доказывает ────────────────────────

def test_single_absence_is_not_enough():
    obs = [_o(0, N.PRESENT), _o(5, N.PRESENT), _o(10, N.ABSENT)]
    assert _fold(obs, now_min=12)["absent_since"] is None


def test_two_absences_confirm_removal():
    obs = [_o(0, N.PRESENT), _o(5, N.ABSENT), _o(10, N.ABSENT)]
    assert _fold(obs, now_min=12)["absent_since"] == T0 + timedelta(minutes=5)


def test_post_returning_resets_the_streak():
    """Моргнул API, пост вернулся — обвинения нет."""
    obs = [_o(0, N.PRESENT), _o(5, N.ABSENT), _o(10, N.PRESENT),
           _o(15, N.ABSENT), _o(20, N.PRESENT)]
    assert _fold(obs, now_min=25)["absent_since"] is None


# ── Запрет №3: не выдумывать точность ──────────────────────────────────────

def test_removal_is_reported_as_a_window_not_a_point():
    obs = [_o(0, N.PRESENT), _o(30, N.PRESENT), _o(45, N.ABSENT), _o(60, N.ABSENT)]
    win = _fold(obs, now_min=70)["removal_window"]
    assert win == (T0 + timedelta(minutes=30), T0 + timedelta(minutes=45))


def test_certificate_prints_the_interval():
    obs = [_o(0, N.PRESENT), _o(30, N.PRESENT), _o(45, N.ABSENT), _o(60, N.ABSENT)]
    f = _fold(obs, now_min=70)
    text = N.render_certificate(
        {"id": 1, "channel_ref": "c", "msg_id": 5,
         "promised_from": T0, "promised_until": T0 + timedelta(hours=24)},
        f, "sig")
    assert "Исчез в промежутке" in text


def test_held_time_is_stated_as_a_lower_bound():
    obs = [_o(0, N.PRESENT), _o(120, N.PRESENT), _o(130, N.ABSENT), _o(140, N.ABSENT)]
    f = _fold(obs, now_min=150)
    assert abs(f["held_min_h"] - 2.0) < 0.01
    text = N.render_certificate({"id": 1, "channel_ref": "c", "msg_id": 1,
                                 "promised_from": T0,
                                 "promised_until": T0 + timedelta(hours=24)}, f, None)
    assert "не менее" in text


# ── Запрет №4: мало данных — нет вывода ────────────────────────────────────

def test_too_few_observations_give_no_verdict():
    assert _fold([_o(0, N.PRESENT), _o(5, N.PRESENT)], now_min=10)["verdict"] \
        == N.V_INCONCLUSIVE


def test_low_coverage_gives_no_verdict_even_when_picture_seems_obvious():
    """Две подтверждённые пропажи на фоне восьми провалов — не вывод."""
    obs = [_o(0, N.PRESENT)] + [_o(i, N.UNKNOWN) for i in range(5, 45, 5)] \
        + [_o(50, N.ABSENT), _o(55, N.ABSENT)]
    f = _fold(obs, now_min=60)
    assert f["coverage"] < N.MIN_COVERAGE
    assert f["verdict"] == N.V_INCONCLUSIVE


# ── Сами вердикты ──────────────────────────────────────────────────────────

def test_kept_when_post_survives_the_promised_window():
    obs = [_o(m, N.PRESENT) for m in (0, 300, 700, 1441)]
    assert _fold(obs, hours=24, now_min=1500)["verdict"] == N.V_KEPT


def test_running_while_the_window_is_open():
    obs = [_o(m, N.PRESENT) for m in (0, 30, 60)]
    assert _fold(obs, hours=24, now_min=90)["verdict"] == N.V_RUNNING


def test_removal_after_the_deadline_is_lawful():
    obs = [_o(0, N.PRESENT), _o(1400, N.PRESENT),
           _o(1500, N.ABSENT), _o(1520, N.ABSENT)]
    assert _fold(obs, hours=24, now_min=1530)["verdict"] == N.V_KEPT


def test_never_appeared_requires_watching_from_the_start():
    obs = [_o(0, N.ABSENT), _o(30, N.ABSENT), _o(60, N.ABSENT), _o(90, N.ABSENT)]
    assert _fold(obs, hours=1, now_min=100)["verdict"] == N.V_NEVER


def test_late_arrival_cannot_claim_the_post_never_existed():
    """Пришли наблюдать через сутки — «не публиковался» сказать не вправе."""
    obs = [_o(m, N.ABSENT) for m in (2000, 2030, 2060, 2090)]
    assert _fold(obs, hours=24, now_min=2100)["verdict"] == N.V_INCONCLUSIVE


# ── Пейсинг наблюдений ─────────────────────────────────────────────────────

def test_checks_are_dense_in_the_first_hour():
    nxt = N.plan_next_check(now=T0 + timedelta(minutes=10), promised_from=T0,
                            promised_until=T0 + timedelta(hours=24))
    assert (nxt - (T0 + timedelta(minutes=10))) <= timedelta(minutes=5)


def test_checks_get_rare_later():
    nxt = N.plan_next_check(now=T0 + timedelta(hours=10), promised_from=T0,
                            promised_until=T0 + timedelta(hours=48))
    assert (nxt - (T0 + timedelta(hours=10))) >= timedelta(minutes=30)


def test_watching_starts_at_the_promised_beginning_not_before():
    """Иначе вердикт «не публиковался» опёрся бы на наблюдения до срока."""
    nxt = N.plan_next_check(now=T0 - timedelta(hours=2), promised_from=T0,
                            promised_until=T0 + timedelta(hours=24))
    assert nxt == T0


def test_observation_stops_after_the_tail():
    assert N.plan_next_check(now=T0 + timedelta(hours=30), promised_from=T0,
                             promised_until=T0 + timedelta(hours=24)) is None


def test_a_few_checks_happen_after_the_deadline():
    """Снятие через минуту после дедлайна — тоже факт, его надо застать."""
    assert N.plan_next_check(now=T0 + timedelta(hours=24, minutes=5),
                             promised_from=T0,
                             promised_until=T0 + timedelta(hours=24)) is not None


# ── Подозрение на накрутку ─────────────────────────────────────────────────

def test_organic_growth_raises_no_suspicion():
    obs = [_o(0, N.PRESENT, 0), _o(60, N.PRESENT, 900), _o(120, N.PRESENT, 1150),
           _o(240, N.PRESENT, 1260), _o(480, N.PRESENT, 1300)]
    assert N.views_burst(obs) is None


def test_a_late_step_is_flagged():
    obs = [_o(0, N.PRESENT, 0), _o(60, N.PRESENT, 500), _o(120, N.PRESENT, 560),
           _o(180, N.PRESENT, 600), _o(240, N.PRESENT, 40000)]
    b = N.views_burst(obs)
    assert b and b["gained"] == 39400 and b["factor"] >= N.BURST_RATE_FACTOR


def test_the_first_hour_spike_is_not_a_burst():
    """Органика набирает основное в первый час — это не накрутка."""
    obs = [_o(0, N.PRESENT, 0), _o(20, N.PRESENT, 9000), _o(40, N.PRESENT, 9200),
           _o(70, N.PRESENT, 9300), _o(200, N.PRESENT, 9400)]
    assert N.views_burst(obs) is None


def test_view_counter_going_backwards_is_ignored():
    """Счётчик просмотров не убывает; отрицательная дельта — шум API."""
    obs = [_o(0, N.PRESENT, 100), _o(30, N.PRESENT, 50), _o(60, N.PRESENT, 120),
           _o(90, N.PRESENT, 130), _o(120, N.PRESENT, 140)]
    assert N.views_burst(obs) is None


# ── Устойчивость ───────────────────────────────────────────────────────────

def test_naive_datetimes_do_not_crash_the_verdict():
    """Из БД даты приходят без таймзоны — сравнение с aware упало бы TypeError
    прямо посреди вердикта."""
    obs = [{"observed_at": (T0 + timedelta(minutes=m)).replace(tzinfo=None),
            "state": N.PRESENT, "views": 1} for m in (0, 30, 60)]
    f = N.fold(obs, promised_from=T0.replace(tzinfo=None),
               promised_until=(T0 + timedelta(hours=24)).replace(tzinfo=None),
               now=(T0 + timedelta(hours=1)).replace(tzinfo=None))
    assert f["verdict"] == N.V_RUNNING


def test_garbage_observations_are_skipped_not_fatal():
    obs = [_o(0, N.PRESENT), {"state": "мусор"}, {}, None,
           _o(5, N.PRESENT), _o(10, N.PRESENT)]
    assert N.fold(obs, promised_from=T0,
                  promised_until=T0 + timedelta(hours=1),
                  now=T0 + timedelta(minutes=20))["n_total"] == 3


def test_empty_history_is_inconclusive():
    assert _fold([], now_min=10)["verdict"] == N.V_INCONCLUSIVE


def test_every_verdict_has_a_label_and_a_hint():
    for v in (N.V_RUNNING, N.V_KEPT, N.V_EARLY, N.V_NEVER, N.V_INCONCLUSIVE):
        assert N.VERDICT_LABEL.get(v) and N.VERDICT_HINT.get(v)
