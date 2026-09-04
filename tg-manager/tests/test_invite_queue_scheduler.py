"""Уровень 4: очередь-планировщик вместо предварительной нарезки аудитории.

ЧТО БЫЛО СЛОМАНО. Аудитория резалась по аккаунтам ЗАРАНЕЕ (`_chunks`), и кусок
намертво принадлежал своему аккаунту. Отсюда два тихих провала, которые не видно
в отчёте операции:

  • аккаунт словил PeerFlood на первом же батче — `break`, и весь остаток его
    куска (в тесте 9 из 10 целей) исчезал НАВСЕГДА, хотя рядом стоял свежий
    аккаунт, который в это время уже закончил свою половину;
  • аккаунт, у которого суточный лимит исчерпан, делал `continue` — и его кусок
    тоже испарялся вместе с ним.

Плюс счётчик прогресса врал: на оборванном батче в `done_items` прибавлялся
ВЕСЬ размер батча, хотя движок успел попробовать одну цель.

Теперь цели лежат в общей очереди, аккаунты разбирают её по батчу за круг,
неотработанный хвост возвращается в голову очереди, а остаток честно
показывается в итоге.
"""
from __future__ import annotations

import asyncio

import pytest

from services import op_worker


@pytest.fixture(autouse=True)
def _reset_account_claims():
    """_exec_mass_invite теперь клеймит аккаунты (_claim_available_accounts). В
    проде claim освобождается в finally _run_op_task, но тут мы зовём исполнитель
    НАПРЯМУЮ — авто-release не срабатывает, и claim протекает в глобальный
    _accounts_in_use, отравляя следующие тесты (аккаунты видятся «занятыми»).
    Сбрасываем состояние вокруг каждого теста."""
    op_worker._accounts_in_use.clear()
    op_worker._operation_account_locks.clear()
    yield
    op_worker._accounts_in_use.clear()
    op_worker._operation_account_locks.clear()


# ── стенд ────────────────────────────────────────────────────────────────────

class _Pool:
    def __init__(self):
        self.done_items = 0

    async def execute(self, q, *a):
        if "done_items=done_items+" in q:
            self.done_items += int(a[1])
        return "OK"

    async def fetchrow(self, q, *a):
        return None

    async def fetch(self, q, *a):
        return []


ACCOUNTS = [
    {"id": 1, "session_str": "s1", "proxy_url": None},
    {"id": 2, "session_str": "s2", "proxy_url": None},
]


class _Stand:
    """Собирает, какие цели РЕАЛЬНО были отданы движку инвайта."""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[int, list]] = []
        self.pace_seen: list[float] = []
        # bulk: пакетное добавление одним запросом — opt-in, по умолчанию None
        # (решает окружение), и стенд обязан это видеть.
        self.bulk_seen: list = []

    async def invite_batch(self, session_str, acc, group, refs, pace_mult=1.0,
                           bulk=None):
        # pace_mult: исполнитель прокидывает множитель темпа ВНУТРЬ батча —
        # до этого движок спал фиксированные 2–4с на цель, и режим «быстро»
        # не влиял на основную задержку прогона.
        self.pace_seen.append(float(pace_mult))
        self.bulk_seen.append(bulk)
        self.calls.append((int(acc["id"]), list(refs)))
        return self.responder(int(acc["id"]), list(refs))

    async def invite_by_phones(self, session_str, acc, group, refs):
        return await self.invite_batch(session_str, acc, group, refs)

    @property
    def attempted(self) -> set:
        """Цели, до которых дело реально дошло (успех или отказ).

        Оборванный батч засчитываем только по фактически обработанным целям —
        ровно так же, как их считает исполнитель.
        """
        seen = set()
        for acc_id, refs in self.calls:
            res = self.responder(acc_id, refs, dry=True)
            n = min(len(refs), int(res.get("ok") or 0) + int(res.get("failed") or 0))
            seen.update(refs[:n])
        return seen


def _ok(n_ok, n_fail=0, **extra):
    return {"ok": n_ok, "failed": n_fail, "errors": [], **extra}


@pytest.fixture
def stand(monkeypatch):
    import services.mass_inviter_engine as inv
    from services import flood_engine as fe

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(op_worker.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(op_worker, "_is_cancelled", lambda *a, **k: _false())
    monkeypatch.setattr(op_worker, "_safe_execute", _anoop)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _afalse)
    monkeypatch.setattr(fe, "recommended_delay", lambda *a, **k: 0.0)
    monkeypatch.setattr(fe, "gaussian_delay", lambda base, **k: 0.0)
    monkeypatch.setattr(fe, "record_success", _anoop)
    monkeypatch.setattr(fe, "record_peer_flood", _anoop)
    monkeypatch.setattr(fe, "record_flood", _anoop)

    def _install(responder, limits=None):
        s = _Stand(responder)
        monkeypatch.setattr(inv, "invite_batch", s.invite_batch)
        monkeypatch.setattr(inv, "invite_by_phones", s.invite_by_phones)

        async def _limit(pool, acc_id):
            lim = (limits or {}).get(int(acc_id), 1000)
            return {"limit": lim, "used_today": 0, "remaining": lim, "basis": "тест"}

        monkeypatch.setattr(fe, "recommended_daily_limit", _limit)

        async def _accounts(pool, q, *a):
            return ACCOUNTS if "LEFT JOIN user_proxies" in q else []

        monkeypatch.setattr(op_worker, "_safe_fetch", _accounts)

        # Загрузка аккаунтов операции теперь идёт через флуд-осознанный
        # resource_selector.select_all_active (одна дверь), а не сырой _safe_fetch.
        async def _sel_all(pool, owner_id, **k):
            return ACCOUNTS
        monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _sel_all)
        return s

    return _install


async def _anoop(*a, **k):
    return None


async def _afalse(*a, **k):
    return False


def _false():
    async def _f():
        return False
    return _f()


def _run(pool, refs, **params):
    p = {"group": "@g", "account_ids": [1, 2], "user_refs": refs, "batch_size": 5}
    p.update(params)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            op_worker._exec_mass_invite(pool, None, 42, 100, p)
        )
    finally:
        # В проде claim аккаунтов освобождает finally _run_op_task; здесь зовём
        # исполнитель напрямую, поэтому освобождаем сами (op_id=42), иначе второй
        # _run в одном тесте увидит аккаунты «занятыми».
        loop.run_until_complete(op_worker.release_operation_accounts(42))
        loop.close()


TARGETS = [f"u{i}" for i in range(1, 21)]


# ── потеря целей ─────────────────────────────────────────────────────────────

def test_flooded_account_does_not_swallow_its_targets(stand):
    """Аккаунт словил флуд на первом батче — его цели должен подобрать сосед."""
    def responder(acc_id, refs, dry=False):
        if acc_id == 1:
            return _ok(0, 1, peer_flood=True)
        return _ok(len(refs))

    s = stand(responder)
    res = _run(_Pool(), TARGETS)

    lost = set(TARGETS) - s.attempted
    assert not lost, f"цели потеряны при флуде: {sorted(lost)}"
    assert res["left"] == 0, "очередь должна быть разобрана до конца"


def test_account_out_of_daily_limit_does_not_swallow_its_targets(stand):
    """Исчерпанный суточный лимит выводит АККАУНТ, а не его долю аудитории."""
    s = stand(lambda acc_id, refs, dry=False: _ok(len(refs)), limits={1: 0})
    _run(_Pool(), TARGETS)

    assert {a for a, _ in s.calls} == {2}, "работать должен только не исчерпанный аккаунт"
    assert set(TARGETS) - s.attempted == set(), "аудитория не должна пропадать вместе с аккаунтом"


def test_summary_reports_account_engagement_honestly(stand):
    """Итог честно объясняет «выбрал N, работали M» (жалоба: 28 выбрано, ~20 работали).

    Аккаунт 1 исчерпал суточный лимит и ни разу не сработал — итог обязан это
    назвать, а не молча показать меньше задействованных аккаунтов.
    """
    s = stand(lambda acc_id, refs, dry=False: _ok(len(refs)), limits={1: 0})
    res = _run(_Pool(), TARGETS)
    summary = res["summary"]
    assert "работали 1 из 2" in summary, f"нет честного учёта аккаунтов: {summary!r}"
    assert "суточный лимит" in summary, "не назван мотив, почему аккаунт не задействован"


def test_summary_warns_about_direct_no_proxy(stand):
    """Аккаунты без назначенного прокси работают напрямую с host-IP — итог обязан
    ПРЕДУПРЕДИТЬ оператора о риске (прямой выход разрешён, но палевно)."""
    # оба аккаунта из стенда без proxy_url/proxy_id → оба «без прокси»
    s = stand(lambda acc_id, refs, dry=False: _ok(len(refs)))
    res = _run(_Pool(), TARGETS)
    assert "Без прокси" in res["summary"] and "риск" in res["summary"], (
        f"нет предупреждения о прямом выходе без прокси: {res['summary']!r}")


def test_free_account_picks_up_the_rest(stand):
    """Свободный аккаунт не простаивает: разбирает общую очередь дальше."""
    s = stand(lambda acc_id, refs, dry=False: _ok(len(refs)), limits={1: 5})
    _run(_Pool(), TARGETS)

    by_acc = {}
    for acc_id, refs in s.calls:
        by_acc[acc_id] = by_acc.get(acc_id, 0) + len(refs)
    assert by_acc.get(1) == 5, "аккаунт с лимитом 5 не должен его превышать"
    assert by_acc.get(2) == 15, "остальное обязан добрать свободный аккаунт"


# ── честность счётчиков ──────────────────────────────────────────────────────

def test_progress_counts_only_attempted_targets(stand):
    """Оборванный батч прибавлял в done_items ВЕСЬ размер батча — это враньё."""
    def responder(acc_id, refs, dry=False):
        if acc_id == 1:
            return _ok(0, 1, peer_flood=True)
        return _ok(len(refs))

    s = stand(responder)
    pool = _Pool()
    _run(pool, TARGETS)

    assert pool.done_items == len(s.attempted), (
        f"прогресс {pool.done_items} не сходится с фактически обработанными "
        f"{len(s.attempted)} целями"
    )


def test_leftover_is_reported_honestly(stand):
    """Если лимиты не дали разобрать очередь — это должно быть видно в итоге."""
    s = stand(lambda acc_id, refs, dry=False: _ok(len(refs)), limits={1: 3, 2: 3})
    res = _run(_Pool(), TARGETS)

    assert res["left"] == len(TARGETS) - 6, "остаток очереди обязан считаться"
    assert "Осталось в очереди" in res["summary"], "молчаливый недобор — тихий провал"


# ── закрытая группа ──────────────────────────────────────────────────────────

def test_broken_group_stops_the_whole_fleet(stand):
    """Нет прав на группу — это про цель, а не про аккаунт.

    С общей очередью цели возвращаются в неё, поэтому без остановки флот
    разбивался бы об закрытую группу кругами, сжигая аккаунты один за другим.
    """
    def responder(acc_id, refs, dry=False):
        return {"ok": 0, "failed": 1, "errors": ["group error: ChannelPrivateError"]}

    s = stand(responder)
    res = _run(_Pool(), TARGETS)

    assert len(s.calls) == 1, f"после отказа группы попыток быть не должно: {s.calls}"
    # Итог называет причину остановки: либо «Причина: <текст движка>»,
    # либо общий «Группа недоступна» — молчаливой остановки быть не должно.
    assert ("Причина" in res["summary"] or "недоступ" in res["summary"]), \
        "причина остановки должна быть названа"


# ── отсутствие вечного круга ─────────────────────────────────────────────────

def test_dead_accounts_terminate_the_loop(stand):
    """Все аккаунты не подключаются — цикл обязан завершиться, а не крутиться."""
    s = stand(lambda acc_id, refs, dry=False: _ok(0, 0, errors=["connect: timeout"]))
    res = _run(_Pool(), TARGETS)

    assert len(s.calls) == len(ACCOUNTS), "каждый аккаунт получает ровно одну попытку"
    assert res["left"] == len(TARGETS), "непройденные цели остаются в очереди, а не теряются"


def test_engine_exception_returns_targets_to_queue(stand):
    """Исключение движка = батч не отработан; цели возвращаются, а не сгорают."""
    def responder(acc_id, refs, dry=False):
        if dry:
            return _ok(0, 0)
        if acc_id == 1:
            raise RuntimeError("boom")
        return _ok(len(refs))

    s = stand(responder)
    res = _run(_Pool(), TARGETS)

    handled = set()
    for acc_id, refs in s.calls:
        if acc_id == 2:
            handled.update(refs)
    assert handled == set(TARGETS), "упавший аккаунт не должен уносить цели с собой"
    assert res["left"] == 0


# ── стоп-кран по флудам на весь флот ─────────────────────────────────────────

def test_flood_storm_stops_operation(stand, monkeypatch):
    """N подряд флудов без успеха → операция встаёт, чтобы не жечь аккаунты."""
    monkeypatch.setenv("INVITE_FLOOD_STOP_STREAK", "2")
    s = stand(lambda acc_id, refs, dry=False: _ok(0, 1, peer_flood=True))
    res = _run(_Pool(), TARGETS)

    assert res.get("flood_storm") is True, "серия флудов должна остановить операцию"
    assert "перегрет" in res["summary"], "причина остановки должна быть названа в итоге"
    assert res["left"] > 0, "остановка на пороге — очередь НЕ должна быть разобрана до конца"


def test_success_resets_flood_streak(stand, monkeypatch):
    """Успех между флудами разрывает серию — ложной остановки быть не должно."""
    monkeypatch.setenv("INVITE_FLOOD_STOP_STREAK", "2")

    def responder(acc_id, refs, dry=False):
        if acc_id == 1:
            return _ok(0, 1, peer_flood=True)
        return _ok(len(refs))  # аккаунт 2 всегда успешно

    s = stand(responder)
    res = _run(_Pool(), TARGETS)

    assert not res.get("flood_storm"), "успех сбрасывает серию — операция не должна вставать"
    assert res["left"] == 0, "живой аккаунт обязан разобрать очередь до конца"


def test_flood_stop_disabled_by_env(stand, monkeypatch):
    """INVITE_FLOOD_STOP_STREAK=0 отключает стоп-кран (обратная совместимость)."""
    monkeypatch.setenv("INVITE_FLOOD_STOP_STREAK", "0")
    s = stand(lambda acc_id, refs, dry=False: _ok(0, 1, peer_flood=True))
    res = _run(_Pool(), TARGETS)

    assert not res.get("flood_storm"), "при пороге 0 стоп-кран не срабатывает"


# ── дедуп уже-приглашённых ─────────────────────────────────────────────────────

class _LogPool:
    """Пул с реальным invite_target_log — проверяем дедуп МЕЖДУ прогонами."""

    def __init__(self):
        self.done_items = 0
        self.log: set = set()

    async def execute(self, q, *a):
        if "done_items=done_items+" in q:
            self.done_items += int(a[1])
        elif "INSERT INTO invite_target_log" in q:
            # Исполнитель пишет цели ОДНИМ запросом через unnest($3::text[]) —
            # раньше это был INSERT на цель, и прогон на 2000 успехов давал
            # 2000 round-trip'ов. Стенд принимает обе формы.
            targets = a[2] if isinstance(a[2], (list, tuple, set)) else [a[2]]
            for t in targets:
                self.log.add((a[0], a[1], t))
        return "OK"

    async def fetch(self, q, *a):
        if "FROM invite_target_log" in q:
            return [{"target": t} for (o, g, t) in self.log if o == a[0] and g == a[1]]
        return []

    async def fetchrow(self, q, *a):
        return None


def test_invite_dedup_skips_already_invited_on_rerun(stand):
    """Повторный прогон по той же группе не тычет уже обработанные цели."""
    s = stand(lambda acc_id, refs, dry=False: _ok(len(refs)))
    pool = _LogPool()

    r1 = _run(pool, list(TARGETS))
    assert r1["ok"] == len(TARGETS), "первый прогон приглашает всех"
    assert len(pool.log) == len(TARGETS), "обработанные цели должны записаться в лог"
    first_calls = len(s.calls)

    r2 = _run(pool, list(TARGETS))  # та же группа @g, та же аудитория
    assert s.calls[first_calls:] == [], "второй прогон не должен приглашать заново"
    assert r2["ok"] == 0
    assert "уже приглашались" in r2["summary"], "причина пустого прогона должна быть названа"


def test_invite_dedup_off_reinvites(stand):
    """skip_invited=False отключает дедуп — полный контроль у пользователя."""
    s = stand(lambda acc_id, refs, dry=False: _ok(len(refs)))
    pool = _LogPool()
    _run(pool, list(TARGETS))
    before = len(s.calls)
    r2 = _run(pool, list(TARGETS), skip_invited=False)
    assert len(s.calls) > before, "без дедупа второй прогон снова приглашает"
    assert r2["ok"] == len(TARGETS)


# ── Аудитория: фильтры ДО усечения источника ─────────────────────────────────
# ЧТО БЫЛО СЛОМАНО. Источник читался одним `LIMIT 2000`, и лишь потом
# применялись дедуп и реестр «не приглашать». Для второй кампании в ту же группу
# это давало ЛОЖНЫЙ ТУПИК: свежие 2000 целиком состояли из уже приглашённых,
# операция отвечала «все цели уже приглашались, соберите свежую аудиторию» — при
# том, что в базе лежали десятки тысяч нетронутых. Пользователь шёл собирать
# аудиторию, которая у него уже была.

class _SourcePool(_LogPool):
    """Пул с источником `parsed_audiences`, честно уважающим LIMIT/OFFSET."""

    def __init__(self, usernames):
        super().__init__()
        self.rows = [{"username": u, "tg_user_id": None,
                      "source_username": "src", "source_id": None} for u in usernames]
        self.pages = 0

    async def fetch(self, q, *a):
        if "FROM parsed_audiences" in q:
            self.pages += 1
            lim = int(q.split("LIMIT ")[1].split()[0])
            off = int(q.split("OFFSET ")[1].split()[0])
            return self.rows[off:off + lim]
        return await super().fetch(q, *a)


def _run_source(pool, **params):
    p = {"group": "@g", "account_ids": [1, 2], "source": "parsed", "batch_size": 5}
    p.update(params)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(op_worker._exec_mass_invite(pool, None, 43, 100, p))
    finally:
        loop.run_until_complete(op_worker.release_operation_accounts(43))
        loop.close()


@pytest.fixture
def source_stand(stand, monkeypatch):
    """Стенд, где _safe_fetch отдаёт и аккаунты, и страницы источника."""
    def _install(pool, responder):
        s = stand(responder)

        async def _fetch(_pool, q, *a):
            if "LEFT JOIN user_proxies" in q or "FROM tg_accounts" in q:
                return ACCOUNTS
            return await pool.fetch(q, *a)

        monkeypatch.setattr(op_worker, "_safe_fetch", _fetch)
        return s
    return _install


def test_source_paging_finds_fresh_targets_behind_already_invited(source_stand):
    """Уже приглашённые не должны съедать всю выборку: за ними есть свежие."""
    invited = [f"old{i}" for i in range(1, 2501)]     # больше одной страницы (2000)
    fresh = [f"new{i}" for i in range(1, 11)]
    pool = _SourcePool(invited + fresh)
    # все `old*` уже приглашены в эту группу. Ключи — ровно те, что пишет
    # исполнитель: '@username' и нормализованная ссылка на группу.
    from services.mass_inviter_engine import parse_group_ref
    gkey = parse_group_ref("@g")
    for u in invited:
        pool.log.add((100, gkey, "@" + u))

    s = source_stand(pool, lambda acc_id, refs, dry=False: _ok(len(refs)))
    res = _run_source(pool)

    assert res["ok"] == len(fresh), (
        "исполнитель обязан долистать источник до свежих целей, а не упереться "
        f"в первую страницу уже приглашённых (ok={res['ok']}, summary={res['summary']})"
    )
    assert set(s.attempted) == {"@" + u for u in fresh}
    assert pool.pages >= 2, "одна страница — значит долистывания не было"


def test_opted_out_never_invited_from_source(source_stand):
    """Реестр «не приглашать» действует и на цели, пришедшие из источника."""
    pool = _SourcePool(["keep1", "banned1", "keep2"])

    async def _fetch_oo(_pool, q, *a):
        return [{"target": "@banned1"}]
    import services.contact_opt_out as coo
    _orig = coo.load_opted_out

    async def _loader(pool_, owner_id):
        return {"@banned1"}
    coo.load_opted_out = _loader
    try:
        s = source_stand(pool, lambda acc_id, refs, dry=False: _ok(len(refs)))
        res = _run_source(pool)
    finally:
        coo.load_opted_out = _orig

    assert "@banned1" not in set(s.attempted), "opted-out цель ушла в инвайт"
    assert res["ok"] == 2


def test_dedup_is_case_insensitive_for_usernames(stand):
    """'@Ivan' и '@ivan' — один человек: второй инвайт в ту же группу не идёт.

    Telegram матчит username без учёта регистра, а аудитория собирается из
    разных парсингов, где регистр может отличаться. Сырое сравнение строк
    приглашало одного и того же человека дважды."""
    s = stand(lambda acc_id, refs, dry=False: _ok(len(refs)))
    pool = _LogPool()

    r1 = _run(pool, ["@Ivan", "@Petr"])
    assert r1["ok"] == 2
    before = len(s.calls)

    r2 = _run(pool, ["@ivan", "@PETR"])
    assert s.calls[before:] == [], "тот же человек в другом регистре получил второй инвайт"
    assert r2["ok"] == 0


def test_phones_dedup_between_runs(stand):
    """Успешно приглашённый номер не импортируется повторно."""
    s = stand(lambda acc_id, refs, dry=False: _ok(len(refs),
                                                  invited_phones=list(refs)))
    pool = _LogPool()
    phones = ["+79990000001", "+79990000002"]

    r1 = _run(pool, [], phones=list(phones))
    assert r1["ok"] == 2
    before = len(s.calls)

    r2 = _run(pool, [], phones=list(phones))
    assert s.calls[before:] == [], "номера приглашены повторно"
    assert r2["ok"] == 0


def test_invited_targets_written_in_one_query(stand):
    """Запись дедупа — один запрос, а не по запросу на цель."""
    s = stand(lambda acc_id, refs, dry=False: _ok(len(refs)))

    class _CountingPool(_LogPool):
        def __init__(self):
            super().__init__()
            self.log_writes = 0

        async def execute(self, q, *a):
            if "INSERT INTO invite_target_log" in q:
                self.log_writes += 1
            return await super().execute(q, *a)

    pool = _CountingPool()
    _run(pool, list(TARGETS))
    assert len(pool.log) == len(TARGETS)
    assert pool.log_writes == 1, (
        f"на {len(TARGETS)} целей ушло {pool.log_writes} запросов — должен быть один"
    )


# ── Темп: настройка обязана доходить до движка ───────────────────────────────

def test_pace_reaches_the_engine(stand):
    """Режим темпа должен влиять на паузы ВНУТРИ батча, а не только между ними.

    Раньше движок спал фиксированные 2–4с на каждую цель, и именно это было
    основной задержкой прогона: пользователь выбирал «быстро», исполнитель
    послушно уменьшал паузу МЕЖДУ батчами, а суммарное время почти не менялось.
    Настройка существовала и ни на что не влияла.
    """
    s = stand(lambda acc_id, refs, dry=False: _ok(len(refs)))
    _run(_Pool(), TARGETS[:5], pace="fast")
    assert s.pace_seen, "движок не получил множитель темпа"
    assert all(p < 1.0 for p in s.pace_seen), (
        f"режим «быстро» должен уменьшать паузы внутри батча, получено {s.pace_seen}"
    )

    s2 = stand(lambda acc_id, refs, dry=False: _ok(len(refs)))
    _run(_Pool(), TARGETS[:5], pace="slow")
    assert all(p > 1.0 for p in s2.pace_seen), (
        f"режим «медленно» должен увеличивать паузы, получено {s2.pace_seen}"
    )


def test_failure_reasons_come_from_engine_structure(stand):
    """Причины отказов берутся из fail_kinds, а не угадываются по тексту."""
    from services import mass_inviter_engine as mie

    def responder(acc_id, refs, dry=False):
        return {"ok": 0, "failed": len(refs), "errors": [],
                "fail_kinds": {mie.FAIL_DEAD: len(refs)}}

    stand(responder)
    res = _run(_Pool(), TARGETS[:5])
    assert mie.FAIL_LABELS[mie.FAIL_DEAD] in res["summary"], (
        f"корзина отказа не названа в итоге: {res['summary']}"
    )
    assert mie.FAIL_ADVICE[mie.FAIL_DEAD][:30] in res["summary"], (
        "к крупнейшей корзине отказов должна прилагаться подсказка «что делать»"
    )
    assert "прочее" not in res["summary"], (
        "известная причина не должна попадать в корзину «прочее»"
    )


def test_bulk_flag_defaults_to_environment_not_forced_off(stand):
    """Отсутствие настройки у операции означает «как решит окружение»: иначе
    глобальный INVITE_BULK_API нельзя было бы включить вообще."""
    s = stand(lambda acc_id, refs, dry=False: _ok(len(refs)))
    _run(_Pool(), TARGETS[:5])
    assert s.bulk_seen and all(b is None for b in s.bulk_seen), (
        f"операция без настройки не должна навязывать режим: {s.bulk_seen}"
    )


def test_bulk_flag_of_the_operation_reaches_the_engine(stand):
    s = stand(lambda acc_id, refs, dry=False: _ok(len(refs)))
    _run(_Pool(), TARGETS[:5], bulk_api=True)
    assert all(b is True for b in s.bulk_seen), f"флаг операции потерян: {s.bulk_seen}"
