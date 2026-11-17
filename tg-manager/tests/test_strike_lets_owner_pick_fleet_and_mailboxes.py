"""Strike даёт владельцу ВИДЕТЬ и ВЫБИРАТЬ аккаунты флота и почтовые ящики.

Было: экран запуска Strike показывал «Аккаунты: все доступные активные» (строка-
заглушка), почтовые ящики не показывались вовсе, а launch слал только target+
category+настройки интенсивности — выбора не было ни в UI, ни в API. Владелец не
видел, ЧЕМ бьёт, и не мог исключить аккаунт или ящик.

Стало сквозная цепочка выбора:
  • strike_status отдаёт реальные списки `fleet` и `mailboxes`;
  • strike_launch принимает `account_ids`/`mailbox_ids`, пересекает их с
    активными ресурсами ВЛАДЕЛЬЦА прямо в запросе (owner_id скоупит, чужой id
    отсеивается) и кладёт валидные id в params;
  • StrikePlan несёт `mailbox_ids`, `_exec_strike` их читает и прокидывает;
  • `_run_email_escalation` принимает `mailbox_ids` и сужает выборку ящиков.

Этот тест сторожит каждый стык: отвалится любой — выбор снова станет невидимым
или необязательным, и экран вернётся к лжи «все доступные».
"""
from __future__ import annotations

import inspect
from pathlib import Path

from services import op_worker, strike_engine

_SVC = Path(__file__).resolve().parent.parent / "services"
_PROT = (_SVC / "mini_app_protection.py").read_text(encoding="utf-8")
_HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_status_exposes_fleet_and_mailboxes():
    # Без этих ключей UI нечего показать в списках выбора.
    assert '"fleet": fleet' in _PROT, "strike_status обязан отдавать список аккаунтов флота"
    assert '"mailboxes": mailboxes' in _PROT, "strike_status обязан отдавать список ящиков"
    # Фильтр аккаунтов статуса == фильтру launch (активные и не мёртвые), иначе
    # выбор на экране разойдётся с тем, что реально уйдёт в операцию.
    assert _PROT.count("sql_not_dead()") >= 2


def test_launch_accepts_and_scopes_account_and_mailbox_selection():
    # launch читает ОБА выбора из тела запроса.
    assert 'body.get("account_ids")' in _PROT
    assert 'body.get("mailbox_ids")' in _PROT
    # Выбор пересекается с ресурсами владельца В ЗАПРОСЕ (скоуп owner_id + ANY),
    # а не доверяется клиенту: чужой/битый id не должен подхватиться.
    assert "id = ANY($2::bigint[])" in _PROT
    assert 'WHERE owner_id=$1 AND is_active=TRUE AND id = ANY($2::bigint[])' in _PROT
    # В params уходят ТОЛЬКО провалидированные id ящиков.
    assert '"mailbox_ids": mailbox_ids' in _PROT


def test_strike_plan_carries_mailbox_ids():
    src = inspect.getsource(strike_engine.StrikePlan)
    assert "mailbox_ids" in src, "StrikePlan обязан нести выбор ящиков до email-эскалации"


def test_exec_strike_reads_and_forwards_mailbox_ids():
    src = inspect.getsource(op_worker._exec_strike)
    assert 'params.get("mailbox_ids")' in src, "_exec_strike обязан читать выбор ящиков"
    assert "mailbox_ids=mailbox_ids" in src, "_exec_strike обязан прокинуть выбор в StrikePlan"


def test_email_escalation_filters_by_selected_mailboxes():
    src = inspect.getsource(strike_engine._run_email_escalation)
    assert "mailbox_ids" in src, "email-эскалация обязана принимать выбор ящиков"
    # Сужение идёт в SQL с owner-скоупом, не в Python-фильтре по чужим строкам.
    assert "id = ANY($2::bigint[])" in src


def test_ui_renders_pickers_and_sends_selection():
    # Контейнеры выбора существуют на экране.
    assert 'id="strikeFleetPick"' in _HTML
    assert 'id="strikeMailboxPick"' in _HTML
    # Старая заглушка «все доступные активные» в превью убрана.
    assert "все доступные активные" not in _HTML, (
        "превью не должно утверждать «все доступные» — теперь это выбор"
    )
    # Запуск собирает отмеченные id и кладёт в payload.
    assert "strikeCollectPicks" in _HTML
    assert "account_ids: _picks.account_ids" in _HTML
    assert "mailbox_ids: _picks.mailbox_ids" in _HTML
