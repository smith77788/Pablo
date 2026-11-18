"""Регресс: проверка ограничений открывает приватную сущность ЛЮБЫМ подключённым
аккаунтом-участником, а не только записанным управляющим.

Жалоба владельца: «аккаунты-владельцы каналов подключены, но проверка пишет нет
доступа». Приватную сущность (без @username) открывает аккаунт-участник. Код брал
ТОЛЬКО managed_channels.acc_id (preferred_acc); у таблицы UNIQUE(owner_id,
channel_id) — ровно один acc_id на канал. Если он занят другой операцией, в
карантине или устарел (аккаунт переподключён под новым id, либо участник — ДРУГОЙ
аккаунт владельца), приватная падала в «нет управляющего аккаунта», хотя доступ у
владельца есть.

Фикс: перебираем управляющего первым (с его записанным access_hash), затем
остальных живых наблюдателей (access_hash=0 — резолв по участию/скан диалогов).
Не-участник приватную не откроет и даст «нечем проверить» (probe_error), а не
ложное «ограничена/недоступна», поэтому перебор безопасен и только помогает.
"""
from services.op_worker import _restriction_access_candidates


def _obs(*ids):
    accs = [{"id": i} for i in ids]
    return {int(a["id"]): a for a in accs}, accs


def test_private_falls_back_to_other_accounts_when_preferred_stale():
    # preferred=99 больше не среди наблюдателей (переподключён под новым id /
    # занят / в карантине) — но живы 1 и 2. Раньше кандидатов было НОЛЬ → владелец
    # видел «нет управляющего аккаунта». Теперь пробуем живых.
    obs_by_id, observers = _obs(1, 2)
    ent = {"preferred_acc": 99, "access_hash": 555, "is_private": True}
    cands = _restriction_access_candidates(ent, obs_by_id, observers, dead=set())
    ids = [int(a["id"]) for a, _h in cands]
    assert ids == [1, 2]
    # записанный хэш валиден только для устаревшего preferred — живым даём 0
    assert all(h == 0 for _a, h in cands)


def test_private_prefers_manager_with_its_hash_then_others():
    obs_by_id, observers = _obs(1, 2, 3)
    ent = {"preferred_acc": 2, "access_hash": 777, "is_private": True}
    cands = _restriction_access_candidates(ent, obs_by_id, observers, dead=set())
    ids = [int(a["id"]) for a, _h in cands]
    assert ids[0] == 2                        # управляющий — первым
    assert sorted(ids) == [1, 2, 3]           # но остальных тоже пробуем (суть фикса)
    assert cands[0][1] == 777                 # у управляющего — его записанный хэш
    assert all(h == 0 for a, h in cands if int(a["id"]) != 2)


def test_dead_accounts_excluded_without_dupes():
    obs_by_id, observers = _obs(1, 2, 3)
    ent = {"preferred_acc": 1, "access_hash": 10, "is_private": True}
    cands = _restriction_access_candidates(ent, obs_by_id, observers, dead={2})
    ids = [int(a["id"]) for a, _h in cands]
    assert ids == [1, 3]                       # 2 мёртв — пропущен; preferred без дубля


def test_public_keeps_manager_first_then_others():
    obs_by_id, observers = _obs(5, 6)
    ent = {"preferred_acc": 6, "access_hash": 0, "is_private": False, "username": "pub"}
    cands = _restriction_access_candidates(ent, obs_by_id, observers, dead=set())
    ids = [int(a["id"]) for a, _h in cands]
    assert ids[0] == 6 and sorted(ids) == [5, 6]


def test_no_preferred_still_lists_all_live():
    # acc_id не записан (NULL) — приватную всё равно пробуем всеми живыми.
    obs_by_id, observers = _obs(1, 2)
    ent = {"preferred_acc": None, "access_hash": None, "is_private": True}
    cands = _restriction_access_candidates(ent, obs_by_id, observers, dead=set())
    ids = sorted(int(a["id"]) for a, _h in cands)
    assert ids == [1, 2]
