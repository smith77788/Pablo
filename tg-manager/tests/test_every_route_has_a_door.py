"""Ручка без двери: бэкенд есть, а нажать его нечем.

Порода, которая в этом проекте повторялась не раз: ручку и экран добавляют
РАЗНЫМИ коммитами, бэкенд приезжает, экран — нет. Старый случай записан прямо
в докстринге `tests/test_ranking_screen_wired.py`: «HTML + бэкенд
/api/miniapp/ranking/* добавили одним коммитом, но JS-функции не написали →
клик по тайлу ничего не делал». Обратная половина той же беды — ручка, которую
не зовёт никто, — храповика не имела: она не падает, не краснеет и выглядит
работающей функцией в списке маршрутов.

Цена не в мёртвом коде, а в том, что он ВРЁТ. Я сам на этом попался в тот же
день: оправдал потолок в `tg_cloud.list_files` тем, что «с фронта
/api/miniapp/cloud/list не зовут» — такого пути нет вовсе, настоящий
/api/miniapp/cloud/files, и экран облака его зовёт. Двести первый файл облака
был недостижим ровно потому, что я поверил угаданному пути вместо проверки.

Правило держит число таких ручек невозрастающим и требует вычёркивать из
реестра ту, у которой дверь появилась: иначе реестр перестаёт что-либо
означать (ту же анти-гниль сосед поставил своему списку исключений потолка).

ЧТО СЧИТАЕТСЯ ДВЕРЬЮ. Упоминание пути (до первой `{`) в исходнике мини-аппа:
`mini_app/index.html` плюс `mini_app/screens/*.js`.

Сначала я обосновал это тем, что «фронт зовёт ручки через `api('<путь>'...)`, и
путь там написан буквально». Проверил — неправда: из 746 вызовов `api()` 38
получают путь не литералом, а из сборщиков (`_chQuery`, `_bcastQuery`,
`_opsQuery` и прочих). Мерка всё равно верна, но по другой причине: она ищет
путь по ВСЕМУ исходнику, а не по тексту вызова, и литерал внутри сборщика
находит так же. Настоящее условие — чтобы путь нигде не собирался из префикса и
переменной (`'/api/miniapp/' + kind`): такой вызов дверь бы открыл, а мерка его
не увидела бы. Этого в слое нет, и на это есть проверка ниже, а не обещание.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Ручки, у которых двери нет СЕЙЧАС. Список не оправдание, а опись: он
# фиксирует долг и не даёт ему расти. Группы — причины, которые видны из кода.
#
# `mp/*` (11) и `host_server/*` (9) — внешние API: витрина провайдеров по
# ключам и аренда хост-серверов. Экранов у них нет, и решение «строить, удалить
# или оставить» за владельцем; вопрос ему задан.
#
# `network/*` (7) и `ranking/*` (6) — старые двойники живых семейств:
# `networks/*` и голого `/api/miniapp/ranking` соответственно. Фронт ходит в
# живые. Дублирующий набор остался от переписывания экранов.
#
# `admin/*`, `cloud/set-paid`, `sys_health`, `circuit_breaker`, `audit`,
# `topology/nodes`, `analytics/realtime` — служебные/админские.
#
# Остальные — одиночные ручки без экрана: `broadcast/ab_test`, `channel_factory/submit`,
# `workflow/create`, `team/members`, `team/audit`, `my_activity`,
# `accounts/health`, `audience/analyze/{bot_id}`, `audience/segment/{bot_id}`.
_KNOWN_DOORLESS = frozenset({
    "/api/miniapp/accounts/health",
    "/api/miniapp/admin/audit",
    "/api/miniapp/admin/ops/stats",
    "/api/miniapp/analytics/realtime",
    "/api/miniapp/audience/analyze/{bot_id}",
    "/api/miniapp/audience/segment/{bot_id}",
    "/api/miniapp/audit",
    "/api/miniapp/broadcast/ab_test",
    "/api/miniapp/channel_factory/submit",
    "/api/miniapp/circuit_breaker",
    "/api/miniapp/cloud/set-paid",
    "/api/miniapp/host_server/buy",
    "/api/miniapp/host_server/market",
    "/api/miniapp/host_server/my",
    "/api/miniapp/host_server/offering",
    "/api/miniapp/host_server/offering/{offering_id}/toggle",
    "/api/miniapp/host_server/price",
    "/api/miniapp/host_server/rent",
    "/api/miniapp/host_server/rental/{rental_id}/status",
    "/api/miniapp/host_server/status",
    "/api/miniapp/mp/admin/provider/{pid}/status",
    "/api/miniapp/mp/admin/providers",
    "/api/miniapp/mp/catalog",
    "/api/miniapp/mp/mine",
    "/api/miniapp/mp/provider/{pid}",
    "/api/miniapp/mp/provider/{pid}/apikey",
    "/api/miniapp/mp/provider/{pid}/apikey/{kid}/revoke",
    "/api/miniapp/mp/provider/{pid}/service",
    "/api/miniapp/mp/register",
    "/api/miniapp/mp/service/{sid}",
    "/api/miniapp/mp/service/{sid}/toggle",
    "/api/miniapp/my_activity",
    "/api/miniapp/network/instance",
    "/api/miniapp/network/instance/{instance_id}",
    "/api/miniapp/network/instances",
    "/api/miniapp/network/stats",
    "/api/miniapp/network/template",
    "/api/miniapp/network/template/{template_id}",
    "/api/miniapp/network/templates",
    "/api/miniapp/ranking/alerts",
    "/api/miniapp/ranking/history/{keyword_id}",
    "/api/miniapp/ranking/positions",
    "/api/miniapp/ranking/record",
    "/api/miniapp/ranking/stats",
    "/api/miniapp/ranking/track",
    "/api/miniapp/sys_health",
    "/api/miniapp/team/audit",
    "/api/miniapp/team/members",
    "/api/miniapp/topology/nodes",
    "/api/miniapp/workflow/create",
})


def _front_source() -> str:
    return "\n".join(
        [(ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")]
        + [p.read_text(encoding="utf-8")
           for p in sorted((ROOT / "mini_app" / "screens").glob("*.js"))])


def _miniapp_paths() -> list[str]:
    snap = (ROOT / "tests" / "miniapp_routes_snapshot.txt").read_text(
        encoding="utf-8")
    out = set()
    for line in snap.split("\n"):
        if not line.startswith(("GET ", "POST ", "PUT ", "DELETE ", "PATCH ")):
            continue
        path = line.split(" ", 1)[1].strip()
        if path.split("{")[0].rstrip("/").startswith("/api/miniapp"):
            out.add(path)
    return sorted(out)


def _doorless(front: str | None = None) -> list[str]:
    front = _front_source() if front is None else front
    return [p for p in _miniapp_paths()
            if p.split("{")[0].rstrip("/") not in front]


# ── Мерка видит оба случая ──────────────────────────────────────────────────

def test_the_measure_sees_a_door_and_its_absence():
    paths = _miniapp_paths()
    assert len(paths) >= 300, f"путей мини-аппа найдено {len(paths)}"
    front = _front_source()
    assert len(front) > 500_000, "исходник фронта не прочитан целиком"
    # живой путь, который фронт точно зовёт
    assert "/api/miniapp/cloud/files" in front
    assert "/api/miniapp/cloud/files" not in _doorless()
    # и путь, которого на фронте нет
    assert "/api/miniapp/sys_health" in _doorless()


def test_a_guessed_path_is_not_a_door():
    """Ровно та ошибка, на которой я попался: путь, которого нет в роутере.

    `/api/miniapp/cloud/list` не зарегистрирован — значит и в описи его быть
    не может, а «двери нет» про него ничего не значит.
    """
    assert "/api/miniapp/cloud/list" not in _miniapp_paths()


def test_no_path_is_assembled_from_the_prefix():
    """Условие, на котором мерка держится, проверяется, а не обещается.

    Путь, собранный из префикса и переменной, был бы дверью, которой мерка не
    видит, — и тогда живая ручка попала бы в опись долга.
    """
    import re

    front = _front_source()
    bad = re.findall(r"""['"`]/api/miniapp/?['"`]\s*\+|`/api/miniapp/\$\{""", front)
    assert not bad, (
        f"путь собирается из префикса и переменной ({len(bad)} мест): мерка "
        "двери по такому вызову не найдёт")


def test_the_measure_reads_paths_built_inside_helpers():
    """Сборщик пути — такая же дверь: литерал лежит в его теле.

    Экран каналов зовёт `api(_chQuery(0))`, и `/api/miniapp/channels` написан
    внутри `_chQuery`. Если бы мерка смотрела только на текст вызова `api()`,
    живое семейство каналов оказалось бы «без двери».
    """
    front = _front_source()
    assert "_chQuery" in front
    assert "/api/miniapp/channels" in front
    assert "/api/miniapp/channels" not in [
        p.split("{")[0].rstrip("/") for p in _doorless()]


# ── Собственно правило ─────────────────────────────────────────────────────

def test_no_new_route_arrives_without_a_door():
    new = sorted(set(_doorless()) - _KNOWN_DOORLESS)
    assert not new, (
        "у этих ручек нет вызова с фронта — бэкенд приехал, а нажать его "
        "нечем. Либо достройте экран, либо внесите путь в _KNOWN_DOORLESS с "
        "причиной:\n  " + "\n  ".join(new))


def test_a_route_that_got_a_door_leaves_the_registry():
    """Анти-гниль: опись, в которой есть уже живые ручки, перестаёт означать
    долг — и следующая мёртвая ручка затеряется среди них."""
    alive = sorted(_KNOWN_DOORLESS - set(_doorless()))
    assert not alive, (
        "у этих ручек дверь уже есть — вычеркните их из _KNOWN_DOORLESS:\n  "
        + "\n  ".join(alive))


def test_the_registry_names_only_registered_routes():
    """Путь, которого нет в роутере, в описи долга не имеет смысла: он не
    ручка, а опечатка, и его «дверь» не появится никогда."""
    paths = set(_miniapp_paths())
    ghosts = sorted(p for p in _KNOWN_DOORLESS if p not in paths)
    assert not ghosts, (
        "в _KNOWN_DOORLESS пути, которых нет среди зарегистрированных "
        "маршрутов:\n  " + "\n  ".join(ghosts))
