"""Reusable target selection utilities.

Provides paginated, consistent picker keyboards for bots, accounts, channels,
and groups. Used by handlers that need the user to select an entity.

Philosophy:
  - Stateless: the caller manages FSM state and callback routing.
  - Pagination built-in: no more unbounded lists of 50+ buttons.
  - Callback factory: decouple the selector from any specific CallbackData class.

Usage example (pick a bot):

    from bot.utils.target_selector import TargetSelector

    targets = await TargetSelector.fetch_bots(pool, owner_id)
    kb = TargetSelector.single_pick_kb(
        targets,
        callback_factory=lambda t: MyCb(action="chosen", target_id=t.id),
        back_callback=BmCb(action="menu"),
        page=0,
    )
    await message.answer("Выберите бота:", reply_markup=kb)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import asyncpg
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import db as _db
from bot.utils.op_helpers import _acc_label as _base_acc_label

# ── Constants ────────────────────────────────────────────────────────────────────

DEFAULT_PAGE_SIZE = 8
MAX_PAGE_SIZE = 20


# ── Target data class ─────────────────────────────────────────────────────────────

@dataclass
class Target:
    """Uniform representation of a selectable entity."""
    id: int
    label: str          # primary display name
    subtitle: str = ""  # secondary line (e.g. audience count, phone, cluster)
    meta: dict = field(default_factory=dict)  # arbitrary extra data (e.g. bot username)


# ── Helpers ───────────────────────────────────────────────────────────────────────

def _chunk(items: list, size: int) -> list[list]:
    """Split items into pages of `size`."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def _acc_label(acc: dict) -> str:
    """Account label with active/inactive status emoji for target pickers.

    Extends the base _acc_label from op_helpers with a status prefix.
    """
    prefix = "✅ " if acc.get("is_active") else "❌ "
    return prefix + _base_acc_label(acc)


# ── Main selector class ───────────────────────────────────────────────────────────

class TargetSelector:
    """Reusable target selection for bots, accounts, channels, groups."""

    # ── Data fetch methods ────────────────────────────────────────────────────────

    @staticmethod
    async def fetch_bots(
        pool: asyncpg.Pool, owner_id: int, active_only: bool = True
    ) -> list[Target]:
        """Fetch owner's bots as Target list."""
        rows = await _db.get_bots(pool, owner_id)
        if active_only:
            rows = [r for r in (rows or []) if r.get("is_active", True)]
        targets = []
        for bot in (rows or []):
            label = bot.get("username") or bot.get("first_name") or f"bot_{bot['bot_id']}"
            if bot.get("username"):
                label = f"@{label}"
            subtitle_parts = []
            if bot.get("first_name"):
                if bot.get("username") and bot["first_name"] != bot["username"]:
                    subtitle_parts.append(bot["first_name"])
            audience = bot.get("audience_count", 0)
            if audience:
                subtitle_parts.append(f"{audience} подп.")
            targets.append(Target(
                id=bot["bot_id"],
                label=label,
                subtitle=" | ".join(subtitle_parts),
                meta={"username": bot.get("username", ""), "first_name": bot.get("first_name", "")},
            ))
        return targets

    @staticmethod
    async def fetch_accounts(
        pool: asyncpg.Pool, owner_id: int, active_only: bool = True
    ) -> list[Target]:
        """Fetch owner's Telegram accounts as Target list."""
        from bot.utils.op_helpers import _get_active_accounts
        rows = await _get_active_accounts(pool, owner_id)
        if active_only:
            rows = [r for r in (rows or []) if r.get("is_active", True)]
        targets = []
        for acc in (rows or []):
            label = _acc_label(acc)
            targets.append(Target(
                id=acc["id"],
                label=label,
                subtitle=f"Trust: {acc.get('trust_score', '?')}" if acc.get("trust_score") else "",
                meta={
                    "phone": acc.get("phone", ""),
                    "session_str": acc.get("session_str", ""),
                    "cluster": acc.get("cluster", ""),
                    "is_active": acc.get("is_active", False),
                },
            ))
        return targets

    @staticmethod
    async def fetch_channels(
        pool: asyncpg.Pool, owner_id: int, acc_id: int = None
    ) -> list[Target]:
        """Fetch managed channels as Target list (excludes megagroups)."""
        rows = await _db.get_managed_channels(pool, owner_id, acc_id=acc_id)
        rows = [r for r in (rows or []) if r.get("type") not in ("group", "megagroup", "supergroup")]
        targets = []
        for ch in (rows or []):
            label = ch.get("title") or f"channel_{ch['channel_id']}"
            username = ch.get("username", "")
            if username:
                label = f"{label} (@{username})"
            targets.append(Target(
                id=ch["channel_id"],
                label=label,
                subtitle=f"Аккаунт #{ch.get('acc_id', '?')}",
                meta={"username": username, "acc_id": ch.get("acc_id", 0)},
            ))
        return targets

    @staticmethod
    async def fetch_groups(
        pool: asyncpg.Pool, owner_id: int, acc_id: int = None
    ) -> list[Target]:
        """Fetch managed groups (megagroups) as Target list."""
        rows = await _db.get_managed_channels(pool, owner_id, acc_id=acc_id)
        rows = [r for r in (rows or []) if r.get("type") in ("group", "megagroup", "supergroup")]
        targets = []
        for gr in (rows or []):
            label = gr.get("title") or f"group_{gr['channel_id']}"
            username = gr.get("username", "")
            if username:
                label = f"{label} (@{username})"
            targets.append(Target(
                id=gr["channel_id"],
                label=label,
                subtitle=f"Аккаунт #{gr.get('acc_id', '?')}",
                meta={"username": username, "acc_id": gr.get("acc_id", 0)},
            ))
        return targets

    # ── Keyboard builders ──────────────────────────────────────────────────────────

    @staticmethod
    def single_pick_kb(
        targets: list[Target],
        callback_factory: Callable[[Target], Any],
        back_callback: Any,
        page: int = 0,
        page_size: int = DEFAULT_PAGE_SIZE,
        show_subtitle: bool = True,
    ) -> InlineKeyboardMarkup:
        """Paginated single-select keyboard.

        Args:
            targets: List of Target objects to display.
            callback_factory: fn(target) -> callback data for selection.
            back_callback: callback data for the Back button.
            page: current page number (0-indexed).
            page_size: items per page (default 8).
            show_subtitle: show subtitle text under each label.
        """
        builder = InlineKeyboardBuilder()
        page_size = min(page_size, MAX_PAGE_SIZE)
        pages = _chunk(targets, page_size)
        total_pages = max(len(pages), 1)

        current_page = max(0, min(page, total_pages - 1))
        page_items = pages[current_page] if pages else []

        for t in page_items:
            text = t.label
            if show_subtitle and t.subtitle:
                text += f"  ─  {t.subtitle}"
            builder.button(text=text, callback_data=callback_factory(t))

        builder.adjust(1)

        # Navigation row
        nav_builder = InlineKeyboardBuilder()
        if current_page > 0:
            nav_builder.button(
                text=f"◀️ Стр. {current_page}/{total_pages}",
                callback_data=callback_factory(Target(
                    id=current_page - 1, label="__page__",
                    meta={"__nav__": "prev", "__page__": current_page - 1},
                )),
            )
        if current_page < total_pages - 1:
            nav_builder.button(
                text=f"Стр. {current_page + 2}/{total_pages} ▶️",
                callback_data=callback_factory(Target(
                    id=current_page + 1, label="__page__",
                    meta={"__nav__": "next", "__page__": current_page + 1},
                )),
            )
        if total_pages > 1:
            nav_row = InlineKeyboardBuilder()
            nav_row.button(
                text=f"📄 {current_page + 1}/{total_pages}",
                callback_data="bm:noop",
            )
            nav_row.adjust(1)

        # Back button
        back_row = InlineKeyboardBuilder()
        back_row.button(text="◀️ Назад", callback_data=back_callback)

        # Assemble
        for row in builder.export() or []:
            pass  # builder is already populated

        if total_pages > 1:
            for btn in nav_builder.buttons:
                builder.row(btn)
            for btn in nav_row.buttons:
                builder.row(btn)

        for btn in back_row.buttons:
            builder.row(btn)

        return builder.as_markup()

    @staticmethod
    def multi_pick_kb(
        targets: list[Target],
        selected_ids: set[int],
        toggle_callback_factory: Callable[[int], Any],
        confirm_callback: Any,
        back_callback: Any,
        page: int = 0,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> InlineKeyboardMarkup:
        """Multi-select keyboard with checkboxes.

        Args:
            targets: List of Target objects.
            selected_ids: set of currently selected target IDs.
            toggle_callback_factory: fn(target_id) -> callback for toggle.
            confirm_callback: callback for "✓ Confirm" button.
            back_callback: callback for Back.
            page: current page.
            page_size: items per page.
        """
        builder = InlineKeyboardBuilder()
        page_size = min(page_size, MAX_PAGE_SIZE)
        pages = _chunk(targets, page_size)
        total_pages = max(len(pages), 1)
        current_page = max(0, min(page, total_pages - 1))
        page_items = pages[current_page] if pages else []

        for t in page_items:
            checked = "✅" if t.id in selected_ids else "☐"
            text = f"{checked} {t.label}"
            builder.button(text=text, callback_data=toggle_callback_factory(t.id))

        builder.adjust(1)

        # Action row
        builder.row()
        builder.button(text="☑ Выбрать все", callback_data="__select_all__")
        builder.button(text="⬜ Снять все", callback_data="__deselect_all__")
        builder.row()
        builder.button(
            text=f"✓ Подтвердить ({len(selected_ids)})",
            callback_data=confirm_callback,
        )

        # Navigation
        if total_pages > 1:
            nav_builder = InlineKeyboardBuilder()
            if current_page > 0:
                nav_builder.button(text=f"◀️ Стр. {current_page}/{total_pages}", callback_data=toggle_callback_factory(-(current_page - 1) - 1))
            nav_builder.button(text=f"📄 {current_page + 1}/{total_pages}", callback_data="bm:noop")
            if current_page < total_pages - 1:
                nav_builder.button(text=f"Стр. {current_page + 2}/{total_pages} ▶️", callback_data=toggle_callback_factory(-(current_page + 1) - 1))
            builder.row(*nav_builder.buttons)

        # Back
        builder.row()
        builder.button(text="◀️ Назад", callback_data=back_callback)

        return builder.as_markup()

    @staticmethod
    def quick_pick_kb(
        targets: list[Target],
        callback_factory: Callable[[Target], Any],
        back_callback: Any,
        max_buttons: int = 20,
    ) -> InlineKeyboardMarkup:
        """Non-paginated picker for small lists (< max_buttons items).

        Falls back to single_pick_kb with pagination if list is too large.
        """
        if len(targets) > max_buttons:
            return TargetSelector.single_pick_kb(
                targets, callback_factory, back_callback,
                page=0, page_size=DEFAULT_PAGE_SIZE,
            )

        builder = InlineKeyboardBuilder()
        for t in targets:
            text = t.label
            if t.subtitle:
                text += f"  ─  {t.subtitle}"
            builder.button(text=text, callback_data=callback_factory(t))

        builder.adjust(1)
        builder.row()
        builder.button(text="◀️ Назад", callback_data=back_callback)
        return builder.as_markup()


# ── Convenience factory for simple cases ──────────────────────────────────────────

async def show_bot_picker(
    pool: asyncpg.Pool,
    owner_id: int,
    callback_factory: Callable[[Target], Any],
    back_callback: Any,
    title: str = "🤖 Выберите бота:",
    exclude_ids: set[int] = None,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """One-shot: fetch bots + build keyboard.

    Returns (message_text, keyboard) ready for message.answer().
    """
    targets = await TargetSelector.fetch_bots(pool, owner_id)
    if exclude_ids:
        targets = [t for t in targets if t.id not in exclude_ids]

    if not targets:
        return ("⚠️ У вас нет ботов. Добавьте бота через 📱 Активы → 🤖 Мои боты.",
                InlineKeyboardBuilder().button(text="◀️ Назад", callback_data=back_callback).as_markup())

    kb = TargetSelector.quick_pick_kb(targets, callback_factory, back_callback)
    return (title, kb)


async def show_account_picker(
    pool: asyncpg.Pool,
    owner_id: int,
    callback_factory: Callable[[Target], Any],
    back_callback: Any,
    title: str = "📱 Выберите аккаунт:",
    active_only: bool = True,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """One-shot: fetch accounts + build keyboard.

    Returns (message_text, keyboard) ready for message.answer().
    """
    targets = await TargetSelector.fetch_accounts(pool, owner_id, active_only=active_only)
    if not targets:
        tail = "" if active_only else ""
        msg = "⚠️ Нет активных аккаунтов. Добавьте аккаунт через ⚙️ Мониторинг → 📱 Аккаунты."
        if not active_only:
            msg = "⚠️ У вас нет аккаунтов."
        return (msg, InlineKeyboardBuilder().button(text="◀️ Назад", callback_data=back_callback).as_markup())

    kb = TargetSelector.quick_pick_kb(targets, callback_factory, back_callback)
    return (title, kb)
