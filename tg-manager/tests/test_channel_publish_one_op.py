"""Публикацию в СВОИ каналы ставит одна операция — `mass_publish`.

ЧТО БЫЛО. Три типа на одно действие «опубликовать текст в моих каналах»:

  * `mass_publish` — единая публикация (мини-апп, бот);
  * `bulk_post_chans` — «Пост во все каналы аккаунта» в массовых действиях
    мини-аппа (`/channels/bulk_post`) и в боте (`channel_ops`);
  * `bulk_post_to_channel` — карточка канала (`/channel/{id}/post`).

Расходились они не только видом: гейт запрещённого контента
(`content_safety`) и правила виртуального администратора
(`editorial_review.autonomous_block`) стоят только в `_exec_mass_publish`.
Через два других входа пост, который единая публикация остановила бы,
уходил в канал.

Теперь все входы «в свои каналы» ставят `mass_publish` (`account_ids` +
`channel_ids` + `target='all'`). `bulk_post_to_channel` остаётся у бота как
отдельная функция — «несколько аккаунтов в один чужой чат», и это не
публикация в свой канал. Исполнители старых типов живы: они доделывают
стоящие в очереди и рекуррентные операции.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

_SURFACES = sorted((ROOT / "services").glob("mini_app*.py")) + sorted(
    (ROOT / "bot" / "handlers").glob("*.py"))


def _submitted_types(src: str) -> list[str]:
    """Тип операции из каждого вызова `submit(pool, uid, "<тип>", ...)`."""
    out = []
    for n in ast.walk(ast.parse(src)):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name != "submit" or len(n.args) < 3:
            continue
        a = n.args[2]
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            out.append(a.value)
    return out


def _by_type() -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for p in _SURFACES:
        for t in _submitted_types(p.read_text(encoding="utf-8")):
            seen.setdefault(t, []).append(str(p.relative_to(ROOT)))
    return seen


def test_measure_sees_live_submits():
    # Мерка, не видящая заведомо живого mass_publish, слепа, а не код чист.
    assert "mass_publish" in _by_type()


def test_nobody_submits_bulk_post_chans():
    assert "bulk_post_chans" not in _by_type(), _by_type().get("bulk_post_chans")


def test_bulk_post_to_channel_only_for_multi_account_bot_flow():
    assert _by_type().get("bulk_post_to_channel") == ["bot/handlers/channel_ops.py"]


def test_own_channel_entries_post_via_mass_publish():
    for rel in ("services/mini_app_channels.py", "bot/handlers/channel_ops.py"):
        assert "mass_publish" in _submitted_types(
            (ROOT / rel).read_text(encoding="utf-8")), rel


def test_gates_live_in_the_one_executor():
    src = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
    start = src.index("async def _exec_mass_publish(")
    body = src[start:src.index("\nasync def ", start + 10)]
    assert "content_safety.scan_text" in body
    assert "autonomous_block" in body
