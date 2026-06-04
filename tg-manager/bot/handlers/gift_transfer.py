"""Gift Transfer handlers - Telegram UI for gift transfer management."""

from __future__ import annotations

import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters.state import State, StatesGroup

from services.gift_inventory import GiftInventoryService
from services.gift_transfer import GiftTransferService
from services.gift_report import GiftTransferReportService
from database import db

log = logging.getLogger(__name__)
router = Router(name="gift_transfer")


# ─── FSM States ────────────────────────────────────────────────────────────────

class GiftTransferFSM(StatesGroup):
    main_menu = State()
    selecting_accounts = State()
    scanning_gifts = State()
    viewing_inventory = State()
    selecting_recipient = State()
    selecting_payment = State()
    preview = State()
    executing = State()
    report = State()


# ─── Callbacks ─────────────────────────────────────────────────────────────────

class GiftTransferCb:
    """Callback data namespace for gift transfer."""
    def __init__(self, action: str, **kwargs):
        self.action = action
        self.data = kwargs
    
    def __str__(self):
        parts = [f"action={self.action}"]
        for k, v in self.data.items():
            parts.append(f"{k}={v}")
        return "gt:" + ":".join(parts)
    
    @classmethod
    def parse(cls, data: str):
        parts = data.replace("gt:", "").split(":")
        kwargs = {}
        action = parts[0] if parts else ""
        for part in parts[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                kwargs[k] = v
        return cls(action=action, **kwargs)


def make_gt_button(text: str, action: str, **kwargs) -> InlineKeyboardButton:
    """Create a callback button for gift transfer."""
    cb = GiftTransferCb(action=action, **kwargs)
    return InlineKeyboardButton(text=text, callback_data=str(cb))


def make_gt_kb(*rows) -> InlineKeyboardMarkup:
    """Create inline keyboard from rows of buttons."""
    return InlineKeyboardMarkup(inline_keyboard=[[make_gt_button(**btn) if isinstance(btn, dict) else btn 
                                                   for btn in row] for row in rows])


# ─── Main Menu ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "gt:main")
async def cb_gift_transfer_main(callback: CallbackQuery, state: FSMContext, pool):
    """Show gift transfer main menu."""
    await callback.answer()
    
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Scan Gifts", callback_data="gt:scan")
    kb.button(text="📦 Transfer Gifts", callback_data="gt:transfer")
    kb.button(text="👥 Saved Recipients", callback_data="gt:recipients")
    kb.button(text="📊 Reports", callback_data="gt:reports")
    kb.button(text="❓ Help", callback_data="gt:help")
    kb.button(text="◀️ Back to BotMother", callback_data="main_menu")
    kb.adjust(1)
    
    await callback.message.edit_text(
        "🎁 <b>Gift Transfer Manager</b>\n\n"
        "Transfer Telegram star gifts from multiple accounts to one recipient with one click.\n\n"
        "• Scan gifts from your accounts\n"
        "• Select recipient (saved or custom)\n"
        "• Review plan & cost\n"
        "• One final confirmation\n"
        "• Watch automatic transfer progress",
        reply_markup=kb.as_markup()
    )
    await state.set_state(GiftTransferFSM.main_menu)


# ─── Scan Gifts ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("gt:scan"), GiftTransferFSM.main_menu)
async def cb_scan_gifts(callback: CallbackQuery, state: FSMContext, pool):
    """Show account selection for scanning."""
    await callback.answer()
    
    user_id = callback.from_user.id
    
    # Get accounts with gifts capability
    accounts = await pool.fetch("""
        SELECT id, phone, status FROM tg_accounts
        WHERE owner_id=$1 AND session_str IS NOT NULL
        ORDER BY phone
    """, user_id)
    
    if not accounts:
        await callback.answer("No accounts found", show_alert=True)
        return
    
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Select All", callback_data="gt:scan_all")
    kb.button(text="❌ Deselect All", callback_data="gt:scan_none")
    kb.row()
    
    for acc in accounts:
        status_emoji = "🟢" if acc["status"] == "active" else "🔴"
        kb.button(
            text=f"{status_emoji} {acc['phone']}", 
            callback_data=f"gt:toggle_acc:{acc['id']}"
        )
    
    kb.row()
    kb.button(text="▶️ Start Scan", callback_data="gt:start_scan")
    kb.button(text="◀️ Back", callback_data="gt:main")
    kb.adjust(2, 1)
    
    await state.update_data(scan_accounts=[])
    await callback.message.edit_text(
        "🔍 <b>Scan Accounts for Gifts</b>\n\n"
        "Select accounts to scan for Telegram star gifts:\n\n"
        "<i>Scanning retrieves all star gifts from selected accounts.</i>",
        reply_markup=kb.as_markup()
    )
    await state.set_state(GiftTransferFSM.selecting_accounts)


@router.callback_query(F.data.startswith("gt:toggle_acc:"), GiftTransferFSM.selecting_accounts)
async def cb_toggle_account(callback: CallbackQuery, state: FSMContext):
    """Toggle account selection."""
    await callback.answer()
    
    data = await state.get_data()
    accounts = data.get("scan_accounts", [])
    acc_id = int(callback.data.split(":")[2])
    
    if acc_id in accounts:
        accounts.remove(acc_id)
    else:
        accounts.append(acc_id)
    
    await state.update_data(scan_accounts=accounts)
    
    # Refresh button states (just acknowledge)
    await callback.answer(f"Account toggled. Selected: {len(accounts)}")


@router.callback_query(F.data == "gt:scan_all", GiftTransferFSM.selecting_accounts)
async def cb_scan_all(callback: CallbackQuery, state: FSMContext, pool):
    """Select all accounts."""
    await callback.answer()
    
    user_id = callback.from_user.id
    accounts = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND session_str IS NOT NULL",
        user_id
    )
    await state.update_data(scan_accounts=[a["id"] for a in accounts])
    await callback.answer(f"All accounts selected ({len(accounts)})")


@router.callback_query(F.data == "gt:scan_none", GiftTransferFSM.selecting_accounts)
async def cb_scan_none(callback: CallbackQuery, state: FSMContext):
    """Deselect all accounts."""
    await callback.answer()
    await state.update_data(scan_accounts=[])
    await callback.answer("All deselected")


@router.callback_query(F.data == "gt:start_scan", GiftTransferFSM.selecting_accounts)
async def cb_start_scan(callback: CallbackQuery, state: FSMContext, pool):
    """Start scanning selected accounts."""
    await callback.answer("⏳ Scanning...")
    
    user_id = callback.from_user.id
    data = await state.get_data()
    account_ids = data.get("scan_accounts", [])
    
    if not account_ids:
        await callback.answer("Select at least one account", show_alert=True)
        return
    
    # Show scanning message
    await callback.message.edit_text(
        "⏳ <b>Scanning for Gifts...</b>\n\n"
        "Please wait while we scan your accounts for Telegram star gifts.\n\n"
        "This may take a few moments...",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏳ Processing...", callback_data="gt:ignore")]
        ])
    )
    
    # Perform scan
    gifts = await GiftInventoryService.scan_multiple_accounts(pool, user_id, account_ids)
    
    # Sync to DB
    synced = await GiftInventoryService.sync_inventory_to_db(pool, user_id, gifts)
    
    # Show results
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 View Inventory", callback_data="gt:inventory")
    kb.button(text="🔄 Scan Again", callback_data="gt:scan")
    kb.button(text="◀️ Back", callback_data="gt:main")
    kb.adjust(1)
    
    await callback.message.edit_text(
        f"✅ <b>Scan Complete!</b>\n\n"
        f"📊 <b>Results:</b>\n"
        f"• Accounts scanned: {len(account_ids)}\n"
        f"• Gifts found: {len(gifts)}\n"
        f"• Gift records synced: {synced}\n\n"
        f"<i>Transferable gifts can be sent to another user.</i>",
        reply_markup=kb.as_markup()
    )
    await state.set_state(GiftTransferFSM.scanning_gifts)


# ─── Inventory View ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "gt:inventory", GiftTransferFSM.main_menu)
async def cb_view_inventory(callback: CallbackQuery, state: FSMContext, pool):
    """View gift inventory."""
    await callback.answer()
    
    user_id = callback.from_user.id
    summary = await GiftInventoryService.get_inventory_summary(pool, user_id)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Start Transfer", callback_data="gt:transfer")
    kb.button(text="🔄 Refresh", callback_data="gt:inventory")
    kb.button(text="◀️ Back", callback_data="gt:main")
    kb.adjust(1)
    
    # Build account list
    account_lines = []
    for acc in summary.get("by_account", []):
        account_lines.append(
            f"• {acc['phone']}: {acc['total_gifts']} gifts "
            f"({acc['transferable']} transferable, {acc['total_stars_cost']}⭐)"
        )
    
    accounts_text = "\n".join(account_lines) if account_lines else "No gifts found."
    
    await callback.message.edit_text(
        f"📦 <b>Gift Inventory</b>\n\n"
        f"📊 <b>Summary:</b>\n"
        f"• Total gifts: {summary['total_gifts']}\n"
        f"• Transferable: {summary['transferable_gifts']} ⭐\n"
        f"• Non-transferable: {summary['non_transferable_gifts']}\n\n"
        f"<b>By Account:</b>\n{accounts_text}",
        reply_markup=kb.as_markup()
    )
    await state.set_state(GiftTransferFSM.viewing_inventory)


# ─── Transfer Flow ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "gt:transfer", GiftTransferFSM.main_menu)
async def cb_start_transfer(callback: CallbackQuery, state: FSMContext, pool):
    """Start transfer flow - select accounts with gifts."""
    await callback.answer()
    
    user_id = callback.from_user.id
    
    # Get accounts that have gifts
    accounts_with_gifts = await pool.fetch("""
        SELECT a.id, a.phone, COUNT(g.id) as gift_count,
               SUM(CASE WHEN g.is_transferable THEN 1 ELSE 0 END) as transfer_count
        FROM tg_accounts a
        LEFT JOIN gift_inventory g ON g.account_id = a.id
        WHERE a.owner_id=$1 AND a.session_str IS NOT NULL
        GROUP BY a.id, a.phone
        HAVING COUNT(g.id) > 0
        ORDER BY gift_count DESC
    """, user_id)
    
    if not accounts_with_gifts:
        await callback.answer("No gifts found. Scan accounts first.", show_alert=True)
        return
    
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Select All with Gifts", callback_data="gt:transfer_all")
    kb.button(text="❌ Clear Selection", callback_data="gt:transfer_none")
    kb.row()
    
    for acc in accounts_with_gifts:
        kb.button(
            text=f"📱 {acc['phone']} ({acc['transfer_count']}⭐)",
            callback_data=f"gt:transfer_toggle:{acc['id']}"
        )
    
    kb.row()
    kb.button(text="▶️ Continue to Recipient", callback_data="gt:select_recipient")
    kb.button(text="◀️ Back", callback_data="gt:main")
    kb.adjust(2, 1)
    
    await state.update_data(transfer_accounts=[])
    await callback.message.edit_text(
        "📤 <b>Transfer Gifts</b>\n\n"
        "Select accounts containing transferable gifts:\n\n"
        "Only accounts with ⭐ (transferable) gifts are shown.",
        reply_markup=kb.as_markup()
    )
    await state.set_state(GiftTransferFSM.selecting_accounts)


@router.callback_query(F.data.startswith("gt:transfer_toggle:"), GiftTransferFSM.selecting_accounts)
async def cb_transfer_toggle(callback: CallbackQuery, state: FSMContext):
    """Toggle transfer account selection."""
    await callback.answer()
    
    data = await state.get_data()
    accounts = data.get("transfer_accounts", [])
    acc_id = int(callback.data.split(":")[2])
    
    if acc_id in accounts:
        accounts.remove(acc_id)
    else:
        accounts.append(acc_id)
    
    await state.update_data(transfer_accounts=accounts)


@router.callback_query(F.data == "gt:transfer_all", GiftTransferFSM.selecting_accounts)
async def cb_transfer_all(callback: CallbackQuery, state: FSMContext, pool):
    """Select all accounts with gifts."""
    await callback.answer()
    
    user_id = callback.from_user.id
    accounts = await pool.fetch("""
        SELECT a.id FROM tg_accounts a
        JOIN gift_inventory g ON g.account_id = a.id
        WHERE a.owner_id=$1 AND g.is_transferable=true
        GROUP BY a.id
    """, user_id)
    
    await state.update_data(transfer_accounts=[a["id"] for a in accounts])
    await callback.answer(f"Selected {len(accounts)} accounts")


@router.callback_query(F.data == "gt:transfer_none", GiftTransferFSM.selecting_accounts)
async def cb_transfer_none(callback: CallbackQuery, state: FSMContext):
    """Clear selection."""
    await callback.answer()
    await state.update_data(transfer_accounts=[])
    await callback.answer("Cleared")


@router.callback_query(F.data == "gt:select_recipient", GiftTransferFSM.selecting_accounts)
async def cb_select_recipient(callback: CallbackQuery, state: FSMContext, pool):
    """Select recipient for transfer."""
    await callback.answer()
    
    user_id = callback.from_user.id
    data = await state.get_data()
    
    if not data.get("transfer_accounts"):
        await callback.answer("Select at least one account", show_alert=True)
        return
    
    # Get saved recipients
    recipients = await db.get_gift_recipients(pool, user_id)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="🔹 Enter @username", callback_data="gt:enter_username")
    
    if recipients:
        kb.row()
        for r in recipients[:5]:  # Show up to 5 saved
            kb.button(
                text=f"👤 {r['name']} ({r['username'] or 'no @'})",
                callback_data=f"gt:use_recipient:{r['id']}"
            )
    
    kb.row()
    kb.button(text="💾 Save as New Recipient", callback_data="gt:save_recipient")
    kb.button(text="◀️ Back", callback_data="gt:transfer")
    kb.adjust(1)
    
    await callback.message.edit_text(
        "👥 <b>Select Recipient</b>\n\n"
        "Choose who will receive the transferred gifts:\n\n"
        "• Enter a @username manually\n"
        "• Use a saved recipient\n"
        "• Save current recipient for future use",
        reply_markup=kb.as_markup()
    )
    await state.set_state(GiftTransferFSM.selecting_recipient)


@router.callback_query(F.data == "gt:enter_username", GiftTransferFSM.selecting_recipient)
async def cb_enter_username(callback: CallbackQuery, state: FSMContext):
    """Ask user to enter username."""
    await callback.answer()
    
    await callback.message.edit_text(
        "📝 <b>Enter Recipient</b>\n\n"
        "Send me the @username or profile link of the recipient.\n\n"
        "Example: @username or https://t.me/username",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Cancel", callback_data="gt:select_recipient")]
        ])
    )
    await state.set_state(GiftTransferFSM.selecting_recipient)


@router.message(F.text & ~F.text.startswith("/"), GiftTransferFSM.selecting_recipient)
async def msg_handle_username(message: Message, state: FSMContext, pool):
    """Handle username input."""
    user_id = message.from_user.id
    text = message.text.strip()
    
    # Parse username
    username = text.lstrip("@").split("/")[-1].split("?")[0]
    
    # Validate format
    if not username or len(username) < 5:
        await message.answer("❌ Invalid username format. Try again:")
        return
    
    # Save to state and continue
    await state.update_data(
        recipient_username=username,
        recipient_user_id=None,  # Will be resolved during transfer
        recipient_name=f"@{username}"
    )
    
    # Ask for payment source
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ Telegram Stars", callback_data="gt:payment_stars")
    kb.button(text="💼 @wallet", callback_data="gt:payment_wallet")
    kb.button(text="🔄 Auto-detect", callback_data="gt:payment_auto")
    kb.row()
    kb.button(text="◀️ Back", callback_data="gt:select_recipient")
    kb.adjust(1)
    
    await message.answer(
        "💳 <b>Payment Source</b>\n\n"
        "How should transfer costs be paid?\n\n"
        "• <b>⭐ Stars</b> — Pay from Telegram Stars balance\n"
        "• <b>💼 @wallet</b> — Use connected Wallet bot\n"
        "• <b>🔄 Auto</b> — We'll try to detect the best option",
        reply_markup=kb.as_markup()
    )
    await state.set_state(GiftTransferFSM.selecting_payment)


@router.callback_query(F.data.startswith("gt:use_recipient:"), GiftTransferFSM.selecting_recipient)
async def cb_use_recipient(callback: CallbackQuery, state: FSMContext, pool):
    """Use a saved recipient."""
    await callback.answer()
    
    recipient_id = int(callback.data.split(":")[2])
    recipient = await pool.fetchrow(
        "SELECT * FROM gift_recipients WHERE id=$1", recipient_id
    )
    
    if not recipient:
        await callback.answer("Recipient not found", show_alert=True)
        return
    
    await state.update_data(
        recipient_username=recipient["username"],
        recipient_user_id=recipient["user_id"],
        recipient_name=recipient["name"]
    )
    
    # Continue to payment selection
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ Stars", callback_data="gt:payment_stars")
    kb.button(text="💼 @wallet", callback_data="gt:payment_wallet")
    kb.button(text="🔄 Auto", callback_data="gt:payment_auto")
    kb.adjust(1)
    
    await callback.message.edit_text(
        "💳 <b>Payment Source</b>\n\n"
        f"Recipient: <b>{recipient['name']}</b>\n\n"
        "Select payment method:",
        reply_markup=kb.as_markup()
    )
    await state.set_state(GiftTransferFSM.selecting_payment)


@router.callback_query(F.data.startswith("gt:payment_"), GiftTransferFSM.selecting_payment)
async def cb_select_payment(callback: CallbackQuery, state: FSMContext):
    """Handle payment source selection."""
    await callback.answer()
    
    payment_map = {
        "gt:payment_stars": "stars",
        "gt:payment_wallet": "wallet",
        "gt:payment_auto": "auto"
    }
    
    payment_source = payment_map.get(callback.data, "stars")
    await state.update_data(payment_source=payment_source)
    
    # Build and show preview
    await _show_transfer_preview(callback.message, state, callback.from_user.id)


@router.callback_query(F.data == "gt:preview_confirm", GiftTransferFSM.preview)
async def cb_confirm_transfer(callback: CallbackQuery, state: FSMContext, pool):
    """Confirm and queue the transfer."""
    await callback.answer("⏳ Starting transfer...")
    
    user_id = callback.from_user.id
    data = await state.get_data()
    
    # Get selected gifts
    account_ids = data.get("transfer_accounts", [])
    gifts = await pool.fetch("""
        SELECT g.*, a.phone FROM gift_inventory g
        JOIN tg_accounts a ON a.id = g.account_id
        WHERE g.owner_id=$1 AND g.account_id = ANY($2) AND g.is_transferable=true
    """, user_id, account_ids)
    
    if not gifts:
        await callback.answer("No transferable gifts found", show_alert=True)
        return
    
    # Create plan
    plan_id = await GiftTransferService.create_plan(
        pool, user_id,
        recipient_username=data.get("recipient_username", ""),
        recipient_user_id=data.get("recipient_user_id", 0),
        recipient_name=data.get("recipient_name", "Unknown"),
        payment_source=data.get("payment_source", "stars")
    )
    
    # Add items to plan
    items = [
        {
            "inventory_id": g["id"],
            "account_id": g["account_id"],
            "gift_id": g["gift_id"],
            "gift_type": g["gift_type"],
            "stars_cost": g.get("stars_cost", 0)
        }
        for g in gifts
    ]
    await GiftTransferService.add_items_to_plan(pool, plan_id, items)
    
    # Validate plan
    validation = await GiftTransferService.validate_plan(pool, plan_id)
    
    if not validation["valid"]:
        await callback.answer(f"Validation failed: {validation['errors']}", show_alert=True)
        return
    
    # Queue operation
    from services.operation_bus import OperationBus
    op_bus = OperationBus()
    
    op_id = await op_bus.submit(
        pool, user_id, "gift_transfer",
        {"plan_id": plan_id},
        total_items=len(items)
    )
    
    # Link plan to operation
    await db.update_gift_transfer_plan(pool, plan_id, status="queued")
    await pool.execute(
        "UPDATE gift_transfer_plans SET status='queued' WHERE id=$1", plan_id
    )
    
    # Update state
    await state.update_data(plan_id=plan_id, op_id=op_id)
    
    # Show progress
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Check Progress", callback_data="gt:check_progress")
    kb.button(text="◀️ Back to Menu", callback_data="gt:main")
    kb.adjust(1)
    
    await callback.message.edit_text(
        f"🚀 <b>Transfer Started!</b>\n\n"
        f"📦 Plan ID: {plan_id}\n"
        f"🎁 Gifts: {len(items)}\n"
        f"👤 Recipient: {data.get('recipient_name')}\n\n"
        f"Transfer is running in background.\n"
        f"Use the button below to check progress.",
        reply_markup=kb.as_markup()
    )
    await state.set_state(GiftTransferFSM.executing)


async def _show_transfer_preview(message, state, user_id):
    """Build and show transfer preview."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    
    data = await state.get_data()
    account_ids = data.get("transfer_accounts", [])
    
    # Get gift stats
    gifts = await pool.fetch("""
        SELECT g.* FROM gift_inventory g
        WHERE g.owner_id=$1 AND g.account_id = ANY($2) AND g.is_transferable=true
    """, user_id, account_ids)
    
    total_cost = sum(g.get("stars_cost", 0) or 0 for g in gifts)
    total_gifts = len(gifts)
    unique_accounts = len(set(g["account_id"] for g in gifts))
    
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ OK, Transfer Selected Gifts", callback_data="gt:preview_confirm")
    kb.row()
    kb.button(text="❌ Cancel", callback_data="gt:main")
    kb.adjust(1)
    
    preview_text = (
        f"📋 <b>Transfer Preview</b>\n\n"
        f"👤 <b>Recipient:</b> {data.get('recipient_name', 'Unknown')}\n"
        f"💳 <b>Payment:</b> {data.get('payment_source', 'Stars').upper()}\n\n"
        f"📊 <b>Summary:</b>\n"
        f"• Accounts: {unique_accounts}\n"
        f"• Transferable gifts: {total_gifts}\n"
        f"• Estimated cost: {total_cost}⭐\n\n"
    )
    
    if data.get("payment_source") == "auto":
        preview_text += "⚠️ <i>Payment source will be auto-detected. May require confirmation.</i>\n\n"
    
    preview_text += (
        "⏳ <b>Warning:</b>\n"
        "• Some transfers may fail if balance is insufficient\n"
        "• Non-retryable errors will be marked separately\n"
        "• You can retry failed items later\n\n"
        "Press <b>Confirm</b> to start the transfer."
    )
    
    await message.edit_text(preview_text, reply_markup=kb.as_markup())
    await state.set_state(GiftTransferFSM.preview)


# ─── Progress & Reports ────────────────────────────────────────────────────────

@router.callback_query(F.data == "gt:check_progress", GiftTransferFSM.executing)
async def cb_check_progress(callback: CallbackQuery, state: FSMContext, pool):
    """Check transfer progress."""
    await callback.answer()
    
    data = await state.get_data()
    plan_id = data.get("plan_id")
    
    if not plan_id:
        await callback.answer("No active transfer", show_alert=True)
        return
    
    stats = await db.get_gift_transfer_stats(pool, plan_id)
    plan = await db.get_gift_transfer_plan(pool, plan_id, callback.from_user.id)
    
    kb = InlineKeyboardBuilder()
    
    if stats["remaining"] > 0:
        kb.button(text="🔄 Refresh", callback_data="gt:check_progress")
    
    if stats["failed"] > 0:
        retryable = await GiftTransferService.get_retryable_items(pool, plan_id)
        if retryable:
            kb.button(text=f"🔁 Retry ({len(retryable)} failed)", callback_data="gt:retry_failed")
    
    kb.button(text="📊 View Report", callback_data="gt:view_report")
    kb.button(text="◀️ Back", callback_data="gt:main")
    kb.adjust(2, 1)
    
    status = plan["status"] if plan else "unknown"
    status_emoji = {"queued": "⏳", "running": "🔄", "done": "✅", "cancelled": "❌"}.get(status, "❓")
    
    progress_text = (
        f"{status_emoji} <b>Transfer Progress</b>\n\n"
        f"Plan ID: {plan_id}\n"
        f"Status: {status.upper()}\n\n"
        f"📦 <b>Progress:</b>\n"
        f"• Total: {stats['total'] or 0}\n"
        f"• Transferred: ✅ {stats['transferred'] or 0}\n"
        f"• Failed: ❌ {stats['failed'] or 0}\n"
        f"• Skipped: ⏭️ {stats['skipped'] or 0}\n"
        f"• Pending: ⏳ {stats['pending'] or 0}\n"
        f"• Remaining: {stats['remaining'] or 0}\n\n"
        f"💰 <b>Cost:</b> {stats['actual_cost'] or 0}⭐"
    )
    
    if plan["status"] == "done":
        progress_text += "\n\n✅ <b>Transfer Complete!</b> View report for details."
    
    await callback.message.edit_text(progress_text, reply_markup=kb.as_markup())


@router.callback_query(F.data == "gt:retry_failed", GiftTransferFSM.executing)
async def cb_retry_failed(callback: CallbackQuery, state: FSMContext, pool):
    """Retry failed transfers."""
    await callback.answer()
    
    data = await state.get_data()
    plan_id = data.get("plan_id")
    
    if not plan_id:
        return
    
    # Reset failed items
    reset_count = await GiftTransferService.reset_failed_for_retry(pool, plan_id)
    
    # Re-queue operation
    from services.operation_bus import OperationBus
    op_bus = OperationBus()
    
    op_id = await op_bus.submit(
        pool, callback.from_user.id, "gift_transfer",
        {"plan_id": plan_id, "retry": True},
        total_items=reset_count
    )
    
    await state.update_data(op_id=op_id)
    
    await callback.message.edit_text(
        f"🔄 <b>Retry Started</b>\n\n"
        f"{reset_count} failed items have been reset for retry.\n\n"
        "Processing in background. Check progress shortly.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Check Progress", callback_data="gt:check_progress")],
            [InlineKeyboardButton(text="◀️ Back to Menu", callback_data="gt:main")]
        ])
    )


@router.callback_query(F.data == "gt:view_report", GiftTransferFSM.executing)
async def cb_view_report(callback: CallbackQuery, state: FSMContext, pool):
    """View final report."""
    await callback.answer()
    
    data = await state.get_data()
    plan_id = data.get("plan_id")
    
    # Check if report exists
    existing = await GiftTransferReportService.get_report_for_plan(pool, plan_id)
    
    if not existing:
        # Generate report
        await GiftTransferReportService.generate_report(pool, plan_id)
    
    report = await GiftTransferReportService.get_report_for_plan(pool, plan_id)
    
    if not report:
        await callback.answer("Report not available", show_alert=True)
        return
    
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Back to Menu", callback_data="gt:main")
    kb.adjust(1)
    
    report_text = (
        f"📊 <b>Transfer Report</b>\n\n"
        f"👤 <b>Recipient:</b> {report.get('recipient_name', 'Unknown')}\n\n"
        f"📦 <b>Gifts:</b>\n"
        f"• Found: {report.get('total_gifts_found', 0)}\n"
        f"• Selected: {report.get('total_selected', 0)}\n"
        f"• Transferred: ✅ {report.get('transferred', 0)}\n"
        f"• Failed: ❌ {report.get('failed', 0)}\n"
        f"• Skipped: ⏭️ {report.get('skipped', 0)}\n"
        f"• Pending confirmation: ⏳ {report.get('pending_confirmation', 0)}\n\n"
        f"💰 <b>Total Cost:</b> {report.get('total_cost', 0)}⭐\n\n"
    )
    
    if report.get('retryable_failures'):
        report_text += f"⚠️ <b>Retryable failures:</b> {len(report['retryable_failures'])}\n"
    
    if report.get('next_actions'):
        report_text += "\n<b>📋 Suggested Actions:</b>\n"
        for action in report['next_actions']:
            report_text += f"• {action['description']}\n"
    
    await callback.message.edit_text(report_text, reply_markup=kb.as_markup())
    await state.set_state(GiftTransferFSM.report)


# ─── Saved Recipients ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "gt:recipients", GiftTransferFSM.main_menu)
async def cb_manage_recipients(callback: CallbackQuery, state: FSMContext, pool):
    """Manage saved recipients."""
    await callback.answer()
    
    user_id = callback.from_user.id
    recipients = await db.get_gift_recipients(pool, user_id)
    
    kb = InlineKeyboardBuilder()
    
    if recipients:
        for r in recipients:
            kb.button(
                text=f"👤 {r['name']} ({r['username'] or 'no @'})",
                callback_data=f"gt:edit_recipient:{r['id']}"
            )
        kb.row()
    
    kb.button(text="➕ Add New Recipient", callback_data="gt:add_recipient")
    kb.button(text="◀️ Back", callback_data="gt:main")
    kb.adjust(1)
    
    recipients_text = "\n".join(
        f"• {r['name']} — {r['username'] or 'no @'}"
        for r in recipients
    ) if recipients else "No saved recipients."
    
    await callback.message.edit_text(
        "👥 <b>Saved Recipients</b>\n\n"
        f"{recipients_text}",
        reply_markup=kb.as_markup()
    )


# ─── Reports ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "gt:reports", GiftTransferFSM.main_menu)
async def cb_view_reports(callback: CallbackQuery, state: FSMContext, pool):
    """View transfer reports."""
    await callback.answer()
    
    user_id = callback.from_user.id
    reports = await GiftTransferReportService.get_reports_for_user(pool, user_id)
    
    kb = InlineKeyboardBuilder()
    
    for r in reports[:10]:
        date = r.get("created_at", "")[:10]
        kb.button(
            text=f"📋 {date} — {r.get('transferred', 0)}✅ {r.get('failed', 0)}❌",
            callback_data=f"gt:report_detail:{r['id']}"
        )
    
    kb.button(text="◀️ Back", callback_data="gt:main")
    kb.adjust(1)
    
    await callback.message.edit_text(
        "📊 <b>Transfer Reports</b>\n\n"
        "Recent transfer reports:",
        reply_markup=kb.as_markup()
    )


# ─── Help ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "gt:help", GiftTransferFSM.main_menu)
async def cb_help(callback: CallbackQuery, state: FSMContext):
    """Show help information."""
    await callback.answer()
    
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Back", callback_data="gt:main")
    kb.adjust(1)
    
    await callback.message.edit_text(
        "❓ <b>Gift Transfer Help</b>\n\n"
        "<b>How it works:</b>\n"
        "1. Scan your accounts for Telegram star gifts\n"
        "2. Select which accounts' gifts to transfer\n"
        "3. Choose a recipient (@username or saved)\n"
        "4. Review the plan and total cost\n"
        "5. Press <b>Confirm</b> once\n"
        "6. Transfers happen automatically\n\n"
        "<b>Payment:</b>\n"
        "• Telegram Stars — pay from Stars balance\n"
        "• @wallet — use connected Wallet bot\n"
        "• Auto — system chooses best option\n\n"
        "<b>Failed transfers:</b>\n"
        "• Insufficient balance — retry after adding Stars\n"
        "• Rate limits — wait and retry\n"
        "• External confirmation — confirm in Telegram app\n\n"
        "<b>Non-transferable gifts:</b>\n"
        "• Some gifts cannot be transferred\n"
        "• These will be automatically skipped",
        reply_markup=kb.as_markup()
    )