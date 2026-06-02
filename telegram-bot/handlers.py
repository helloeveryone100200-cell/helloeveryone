"""
handlers.py — Complete handler logic for the Message Forwarding Mother Bot platform.

Mother Bot:
  - Inline navigation: Main Menu → My Bots → Per-Bot Management → Admin Panel
  - Full child bot monitoring and control from Mother Bot
  - /addbot command, /cancel, /start

Child Bot:
  - Owner: /panel /broadcast /welcome /stats /cancel + inline panel
  - Customer: Reply keyboard + message forwarding
  - Livegram-style reply routing engine
"""

import asyncio
import logging
import os

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import database as db

logger = logging.getLogger(__name__)

ADMIN_ID: int = int(os.environ["ADMIN_ID"])

# ============================================================================
# FSM States
# ============================================================================

class MotherStates(StatesGroup):
    awaiting_token = State()
    bot_broadcasting = State()   # broadcast to a child bot's customers from Mother Bot
    bot_welcome = State()        # edit child bot welcome from Mother Bot
    admin_broadcasting = State() # broadcast to all owners


class ChildOwnerStates(StatesGroup):
    broadcasting = State()
    changing_welcome = State()


# ============================================================================
# Keyboard / menu builders
# ============================================================================

def _ik(*rows) -> InlineKeyboardMarkup:
    """Shorthand to build InlineKeyboardMarkup from button rows."""
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# --- Mother Bot menus -------------------------------------------------------

def _main_menu_kb(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [_btn("My Bots", "mybots"), _btn("Add Bot", "addbot")],
        [_btn("Help", "help")],
    ]
    if is_admin:
        rows.append([_btn("Admin Panel", "adm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _my_bots_kb(bots: list[dict]) -> InlineKeyboardMarkup:
    rows = [[_btn(f"@{b['bot_username']}", f"b:{b['bot_username']}")] for b in bots]
    rows.append([_btn("Add Bot", "addbot"), _btn("Back", "home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _bot_manage_kb(username: str, is_banned: bool) -> InlineKeyboardMarkup:
    ban_label = "Unban Bot" if is_banned else "Ban Bot"
    ban_data = f"adm:uban:{username}" if is_banned else f"adm:ban:{username}"
    return _ik(
        [_btn("Broadcast", f"bb:{username}"), _btn("Statistics", f"bs:{username}")],
        [_btn("Change Welcome", f"bw:{username}"), _btn("View Customers", f"bc:{username}")],
        [_btn("Disconnect Bot", f"bd:{username}")],
        [_btn("Back to My Bots", "mybots")],
    )


def _bot_manage_admin_kb(username: str, is_banned: bool) -> InlineKeyboardMarkup:
    ban_label = "Unban Bot" if is_banned else "Ban Bot"
    ban_data = f"adm:uban:{username}" if is_banned else f"adm:ban:{username}"
    return _ik(
        [_btn("Statistics", f"bs:{username}"), _btn("View Customers", f"bc:{username}")],
        [_btn(ban_label, ban_data)],
        [_btn("Back to All Bots", "adm:bots")],
    )


def _disconnect_confirm_kb(username: str) -> InlineKeyboardMarkup:
    return _ik(
        [_btn("Yes, Disconnect", f"bdc:{username}"), _btn("Cancel", f"b:{username}")],
    )


def _admin_kb() -> InlineKeyboardMarkup:
    return _ik(
        [_btn("All Bots", "adm:bots"), _btn("System Stats", "adm:stats")],
        [_btn("Broadcast to Owners", "adm:bc")],
        [_btn("Back", "home")],
    )


def _admin_bots_kb(bots: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for b in bots:
        status = "BANNED" if b.get("is_banned") else "Active"
        rows.append([_btn(f"@{b['bot_username']}  [{status}]", f"adm:b:{b['bot_username']}")])
    rows.append([_btn("Back", "adm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_kb(target: str) -> InlineKeyboardMarkup:
    return _ik([_btn("Back", target)])


# --- Child Bot menus --------------------------------------------------------

def _owner_inline_kb() -> InlineKeyboardMarkup:
    return _ik(
        [_btn("Broadcast", "owner:broadcast"), _btn("Statistics", "owner:stats")],
        [_btn("Change Welcome", "owner:welcome")],
    )


def _customer_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="About Us"), KeyboardButton(text="Contact Support")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


# ============================================================================
# Shared helpers
# ============================================================================

async def _get_mother_username(bot: Bot) -> str:
    me = await bot.get_me()
    return me.username or "MotherBot"


def _build_welcome(custom_text: str, mother_username: str) -> str:
    base = custom_text.strip() if custom_text.strip() else (
        "Hello!\n\nYou can contact us using this bot."
    )
    return f"{base}\n\nPowered by @{mother_username}"


def _format_stats(doc: dict, customer_count: int) -> str:
    username = doc.get("bot_username", "?")
    incoming = doc.get("incoming_count", 0)
    outgoing = doc.get("outgoing_count", 0)
    blocked = doc.get("blocked_count", 0)
    total_msgs = incoming + outgoing
    return (
        f"Statistics for @{username}\n\n"
        f"Users:\n"
        f"  Total customers: {customer_count}\n"
        f"  Blocked the bot: {blocked}\n\n"
        f"Messages:\n"
        f"  All messages: {total_msgs}\n"
        f"  Incoming: {incoming}\n"
        f"  Outgoing: {outgoing}"
    )


# ============================================================================
# Command menu setup
# ============================================================================

async def setup_mother_bot_commands(bot: Bot) -> None:
    default_cmds = [
        BotCommand(command="start",  description="Main menu"),
        BotCommand(command="addbot", description="Connect a new bot"),
        BotCommand(command="mybots", description="Manage your bots"),
        BotCommand(command="help",   description="Help and instructions"),
    ]
    admin_cmds = default_cmds + [
        BotCommand(command="stats",       description="Platform statistics"),
        BotCommand(command="broadcast",   description="Broadcast to all owners"),
        BotCommand(command="ban_bot",     description="Ban a child bot"),
        BotCommand(command="unban_bot",   description="Unban a child bot"),
        BotCommand(command="force_clean", description="Re-verify database indexes"),
    ]
    try:
        await bot.set_my_commands(default_cmds, scope=BotCommandScopeDefault())
        await bot.set_my_commands(admin_cmds,   scope=BotCommandScopeChat(chat_id=ADMIN_ID))
        logger.info("Mother Bot command menus registered.")
    except Exception as exc:
        logger.warning("Could not set Mother Bot commands: %s", exc)


async def setup_child_bot_commands(bot: Bot, owner_id: int) -> None:
    owner_cmds = [
        BotCommand(command="panel",     description="Management panel"),
        BotCommand(command="broadcast", description="Broadcast to customers"),
        BotCommand(command="welcome",   description="Change welcome message"),
        BotCommand(command="stats",     description="Bot statistics"),
        BotCommand(command="cancel",    description="Cancel current operation"),
    ]
    try:
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
        await bot.set_my_commands(owner_cmds, scope=BotCommandScopeChat(chat_id=owner_id))
        logger.info("Child bot commands set for owner %d.", owner_id)
    except Exception as exc:
        logger.warning("Could not set child bot commands (owner %d): %s", owner_id, exc)


# ============================================================================
# MOTHER BOT ROUTER
# ============================================================================

def build_mother_router(mother_bot: Bot, child_bots: dict[str, Bot]) -> Router:
    router = Router(name="mother")

    # ── Helpers scoped to this router ────────────────────────────────────────

    async def _render_main(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
        bots = await db.list_owner_bots(user_id)
        is_admin = user_id == ADMIN_ID
        if bots:
            bot_list = ", ".join(f"@{b['bot_username']}" for b in bots)
            text = (
                "Message Forwarding Mother Bot\n\n"
                f"Connected bots: {bot_list}\n\n"
                "Select an option below."
            )
        else:
            text = (
                "Message Forwarding Mother Bot\n\n"
                "No bots connected yet.\n"
                "Tap Add Bot to connect your first bot."
            )
        return text, _main_menu_kb(is_admin)

    async def _answer_main(message: Message, state: FSMContext) -> None:
        await state.clear()
        text, kb = await _render_main(message.from_user.id)
        await message.answer(text, reply_markup=kb)

    async def _edit_main(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        text, kb = await _render_main(callback.from_user.id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()

    # ── /start ────────────────────────────────────────────────────────────────

    @router.message(CommandStart())
    async def mother_start(message: Message, state: FSMContext) -> None:
        await _answer_main(message, state)

    @router.message(Command("mybots"))
    async def mother_cmd_mybots(message: Message, state: FSMContext) -> None:
        await state.clear()
        bots = await db.list_owner_bots(message.from_user.id)
        if not bots:
            await message.answer(
                "You have no connected bots yet.",
                reply_markup=_ik([_btn("Add Bot", "addbot")], [_btn("Back", "home")]),
            )
            return
        await message.answer(
            f"Your connected bots ({len(bots)}):",
            reply_markup=_my_bots_kb(bots),
        )

    @router.message(Command("addbot"))
    async def mother_cmd_addbot(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "Add a New Bot\n\n"
            "Steps:\n"
            "1. Open @BotFather and send /newbot\n"
            "2. Follow the prompts to create your bot\n"
            "3. Copy the API token (e.g. 123456:ABCDEFabcdef)\n"
            "4. Paste the token here\n\n"
            "Warning: Do not connect bots already in use by other services.\n\n"
            "Send the token now, or /cancel to abort.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(MotherStates.awaiting_token)

    @router.message(Command("help"))
    async def mother_cmd_help(message: Message) -> None:
        await message.answer(
            "How to use this bot\n\n"
            "1. Tap Add Bot to connect a bot you created via @BotFather.\n"
            "2. Your bot will start forwarding customer messages to you here.\n"
            "3. Reply to any forwarded message to reply back to the customer.\n"
            "4. Use My Bots to manage welcome messages, broadcast, and view stats.\n\n"
            "Commands:\n"
            "/addbot — Connect a new bot\n"
            "/mybots — Manage your bots\n"
            "/cancel — Cancel current action\n\n"
            "How do I reply to a customer?\n"
            "Use Telegram's reply feature — swipe left on the forwarded message.",
            reply_markup=_back_kb("home"),
        )

    # ── /cancel ───────────────────────────────────────────────────────────────

    @router.message(Command("cancel"))
    async def mother_cancel(message: Message, state: FSMContext) -> None:
        current = await state.get_state()
        await state.clear()
        if current:
            await message.answer("Operation cancelled.", reply_markup=ReplyKeyboardRemove())
        await _answer_main(message, state)

    # ── Token submission (FSM) ────────────────────────────────────────────────

    @router.message(StateFilter(MotherStates.awaiting_token))
    async def mother_receive_token(message: Message, state: FSMContext) -> None:
        token = (message.text or "").strip()
        if not token:
            await message.answer("Please send the token as a text message.")
            return

        existing = await db.get_bot_by_token(token)
        if existing:
            await message.answer(
                "This token is already registered on the platform.",
                reply_markup=_back_kb("home"),
            )
            await state.clear()
            return

        status_msg = await message.answer("Validating token…")
        try:
            probe = Bot(token=token)
            me = await probe.get_me()
            bot_username: str = me.username or ""
            await probe.session.close()
        except Exception:
            await status_msg.edit_text(
                "Invalid token. Telegram rejected it.\n\n"
                "Make sure you copied the full token from @BotFather and try again, "
                "or send /cancel to abort."
            )
            return

        owner_id = message.from_user.id
        await db.insert_user_bot(
            owner_id=owner_id,
            bot_token=token,
            bot_username=bot_username,
        )

        mother_username = await _get_mother_username(mother_bot)
        asyncio.create_task(_set_child_descriptions(token, mother_username))

        from main import launch_child_bot
        asyncio.create_task(launch_child_bot(token, mother_bot, child_bots))

        await status_msg.edit_text(
            f"@{bot_username} has been connected successfully.\n\n"
            f"The bot will forward all incoming messages to you here.\n\n"
            f"How do I reply to a customer?\n"
            f"Use Telegram's reply feature — swipe left on the forwarded message.\n\n"
            f"Manage your bot from My Bots anytime."
        )
        await state.clear()
        await _answer_main(message, state)

    # ── Broadcast from Mother Bot to child bot's customers (FSM) ─────────────

    @router.message(StateFilter(MotherStates.bot_broadcasting))
    async def mother_do_broadcast(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        token = data.get("token")
        username = data.get("username", "?")
        if not token:
            await state.clear()
            return
        customers = await db.list_customers(token)
        bot_instance = child_bots.get(token)
        if not bot_instance:
            await message.answer("Bot is not currently running.")
            await state.clear()
            return
        sent = failed = 0
        for cid in customers:
            try:
                await bot_instance.copy_message(
                    chat_id=cid,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                await db.increment_outgoing(token)
                sent += 1
            except Exception:
                failed += 1
        await message.answer(
            f"Broadcast complete for @{username}\n\n"
            f"Delivered: {sent}\n"
            f"Failed: {failed}",
            reply_markup=_back_kb(f"b:{username}"),
        )
        await state.clear()

    # ── Change welcome from Mother Bot (FSM) ─────────────────────────────────

    @router.message(StateFilter(MotherStates.bot_welcome))
    async def mother_do_welcome(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        token = data.get("token")
        username = data.get("username", "?")
        new_text = (message.text or "").strip()
        if not new_text:
            await message.answer("Please send a text message.")
            return
        await db.update_welcome(token, new_text)
        mother_username = await _get_mother_username(mother_bot)
        preview = _build_welcome(new_text, mother_username)
        await message.answer(
            f"Welcome message updated for @{username}\n\nPreview:\n\n{preview}",
            reply_markup=_back_kb(f"b:{username}"),
        )
        await state.clear()

    # ── Admin broadcast to all owners (FSM) ───────────────────────────────────

    @router.message(StateFilter(MotherStates.admin_broadcasting))
    async def mother_admin_broadcast(message: Message, state: FSMContext) -> None:
        if message.from_user.id != ADMIN_ID:
            await state.clear()
            return
        owner_ids = await db.list_all_owners()
        sent = 0
        for uid in owner_ids:
            try:
                await mother_bot.copy_message(
                    chat_id=uid,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                sent += 1
            except Exception:
                pass
        await message.answer(
            f"Broadcast sent to {sent} / {len(owner_ids)} owners.",
            reply_markup=_back_kb("adm"),
        )
        await state.clear()

    # ── Admin commands ────────────────────────────────────────────────────────

    @router.message(Command("stats"))
    async def admin_stats(message: Message) -> None:
        if message.from_user.id != ADMIN_ID:
            return
        active = await db.list_all_bots(banned=False)
        banned = await db.list_all_bots(banned=True)
        total_owners = len(set(b["owner_id"] for b in active + banned))
        total_customers = await db.col_bot_customers().count_documents({})
        total_pairs = await db.col_message_pairs().count_documents({})
        total_in = sum(b.get("incoming_count", 0) for b in active + banned)
        total_out = sum(b.get("outgoing_count", 0) for b in active + banned)
        await message.answer(
            "Platform Statistics\n\n"
            f"Registered owners: {total_owners}\n"
            f"Active bots: {len(active)}\n"
            f"Banned bots: {len(banned)}\n"
            f"Total customers: {total_customers}\n"
            f"Total messages in: {total_in}\n"
            f"Total messages out: {total_out}\n"
            f"Active routing pairs (48h): {total_pairs}",
            reply_markup=_back_kb("adm"),
        )

    @router.message(Command("broadcast"))
    async def admin_broadcast_cmd(message: Message, state: FSMContext) -> None:
        if message.from_user.id != ADMIN_ID:
            return
        await state.clear()
        await message.answer(
            "Send the message to broadcast to all bot owners.\n\n/cancel to abort.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(MotherStates.admin_broadcasting)

    @router.message(Command("ban_bot"))
    async def admin_ban_bot(message: Message) -> None:
        if message.from_user.id != ADMIN_ID:
            return
        username = message.text.partition(" ")[2].strip().lstrip("@")
        if not username:
            await message.answer("Usage: /ban_bot <username>")
            return
        doc = await db.get_bot_by_username(username)
        if not doc:
            await message.answer(f"Bot @{username} not found.")
            return
        token = doc["bot_token"]
        await db.set_bot_banned(username, True)
        if token in child_bots:
            try:
                await child_bots[token].session.close()
            except Exception:
                pass
            child_bots.pop(token, None)
        await message.answer(f"Bot @{username} has been banned and stopped.")

    @router.message(Command("unban_bot"))
    async def admin_unban_bot(message: Message) -> None:
        if message.from_user.id != ADMIN_ID:
            return
        username = message.text.partition(" ")[2].strip().lstrip("@")
        if not username:
            await message.answer("Usage: /unban_bot <username>")
            return
        doc = await db.get_bot_by_username(username)
        if not doc:
            await message.answer(f"Bot @{username} not found.")
            return
        await db.set_bot_banned(username, False)
        from main import launch_child_bot
        asyncio.create_task(launch_child_bot(doc["bot_token"], mother_bot, child_bots))
        await message.answer(f"Bot @{username} has been unbanned and restarted.")

    @router.message(Command("force_clean"))
    async def admin_force_clean(message: Message) -> None:
        if message.from_user.id != ADMIN_ID:
            return
        await db.setup_indexes()
        await message.answer("Database indexes verified and re-applied.")

    # ── Callback query handler (navigation hub) ───────────────────────────────

    @router.callback_query()
    async def mother_callbacks(callback: CallbackQuery, state: FSMContext) -> None:
        data = callback.data or ""
        uid = callback.from_user.id

        # ── home ──────────────────────────────────────────────────────────────
        if data == "home":
            await _edit_main(callback, state)
            return

        # ── My Bots list ──────────────────────────────────────────────────────
        if data == "mybots":
            await state.clear()
            bots = await db.list_owner_bots(uid)
            if not bots:
                await callback.message.edit_text(
                    "No connected bots yet.",
                    reply_markup=_ik([_btn("Add Bot", "addbot")], [_btn("Back", "home")]),
                )
            else:
                await callback.message.edit_text(
                    f"Your connected bots ({len(bots)}):\nSelect one to manage.",
                    reply_markup=_my_bots_kb(bots),
                )
            await callback.answer()
            return

        # ── Add Bot ───────────────────────────────────────────────────────────
        if data == "addbot":
            await state.clear()
            await callback.message.edit_text(
                "Add a New Bot\n\n"
                "Steps:\n"
                "1. Open @BotFather and send /newbot\n"
                "2. Follow the prompts to create your bot\n"
                "3. Copy the API token (e.g. 123456:ABCDEFabcdef)\n"
                "4. Paste it here\n\n"
                "Warning: Do not connect bots already used by other services.\n\n"
                "Send the token now, or /cancel to abort.",
            )
            await state.set_state(MotherStates.awaiting_token)
            await callback.answer()
            return

        # ── Help ──────────────────────────────────────────────────────────────
        if data == "help":
            await callback.message.edit_text(
                "How to use this bot\n\n"
                "1. Add Bot — connect a bot from @BotFather.\n"
                "2. Your bot forwards customer messages to you here.\n"
                "3. Reply to any forwarded message to reply to the customer.\n"
                "4. Use My Bots to broadcast, edit welcome, and view stats.\n\n"
                "How do I reply to a customer?\n"
                "Use Telegram's reply feature — swipe left on the forwarded message.",
                reply_markup=_back_kb("home"),
            )
            await callback.answer()
            return

        # ── Per-bot management ────────────────────────────────────────────────
        if data.startswith("b:"):
            username = data[2:]
            await state.clear()
            doc = await db.get_bot_by_username(username)
            # Verify ownership
            if not doc or doc["owner_id"] != uid:
                await callback.answer("Bot not found or not yours.", show_alert=True)
                return
            customers = await db.count_customers(doc["bot_token"])
            status = "Banned" if doc.get("is_banned") else "Active"
            await callback.message.edit_text(
                f"Managing @{username}\n\n"
                f"Status: {status}\n"
                f"Customers: {customers}\n"
                f"Messages in: {doc.get('incoming_count', 0)}\n"
                f"Messages out: {doc.get('outgoing_count', 0)}",
                reply_markup=_bot_manage_kb(username, doc.get("is_banned", False)),
            )
            await callback.answer()
            return

        # ── Bot Broadcast (start FSM) ──────────────────────────────────────────
        if data.startswith("bb:"):
            username = data[3:]
            doc = await db.get_bot_by_username(username)
            if not doc or doc["owner_id"] != uid:
                await callback.answer("Not authorised.", show_alert=True)
                return
            await state.update_data(token=doc["bot_token"], username=username)
            await callback.message.edit_text(
                f"Broadcast to customers of @{username}\n\n"
                "Send the message you want to deliver.\n"
                "You can send text, photos, videos, voice notes, or documents.\n\n"
                "/cancel to abort."
            )
            await state.set_state(MotherStates.bot_broadcasting)
            await callback.answer()
            return

        # ── Bot Statistics ─────────────────────────────────────────────────────
        if data.startswith("bs:"):
            username = data[3:]
            doc = await db.get_bot_by_username(username)
            # Allow owner OR admin to view stats
            if not doc or (doc["owner_id"] != uid and uid != ADMIN_ID):
                await callback.answer("Not authorised.", show_alert=True)
                return
            customers = await db.count_customers(doc["bot_token"])
            back_target = f"adm:b:{username}" if uid == ADMIN_ID and doc["owner_id"] != uid else f"b:{username}"
            await callback.message.edit_text(
                _format_stats(doc, customers),
                reply_markup=_back_kb(back_target),
            )
            await callback.answer()
            return

        # ── Bot Change Welcome (start FSM) ────────────────────────────────────
        if data.startswith("bw:"):
            username = data[3:]
            doc = await db.get_bot_by_username(username)
            if not doc or doc["owner_id"] != uid:
                await callback.answer("Not authorised.", show_alert=True)
                return
            mother_username = await _get_mother_username(mother_bot)
            await state.update_data(token=doc["bot_token"], username=username)
            await callback.message.edit_text(
                f"Change Welcome Message for @{username}\n\n"
                f"Current message:\n{_build_welcome(doc.get('custom_welcome',''), mother_username)}\n\n"
                f"Send your new text below.\n"
                f"Note: the footer 'Powered by @{mother_username}' is always appended automatically.\n\n"
                "/cancel to abort."
            )
            await state.set_state(MotherStates.bot_welcome)
            await callback.answer()
            return

        # ── View Customers ────────────────────────────────────────────────────
        if data.startswith("bc:"):
            username = data[3:]
            doc = await db.get_bot_by_username(username)
            if not doc or (doc["owner_id"] != uid and uid != ADMIN_ID):
                await callback.answer("Not authorised.", show_alert=True)
                return
            customers = await db.list_customers(doc["bot_token"])
            back_target = f"adm:b:{username}" if uid == ADMIN_ID and doc["owner_id"] != uid else f"b:{username}"
            if not customers:
                text = f"No customers have messaged @{username} yet."
            else:
                lines = [f"Customers of @{username} ({len(customers)}):"]
                for cid in customers[:50]:  # cap display at 50
                    lines.append(f"  • {cid}")
                if len(customers) > 50:
                    lines.append(f"  … and {len(customers) - 50} more")
                text = "\n".join(lines)
            await callback.message.edit_text(text, reply_markup=_back_kb(back_target))
            await callback.answer()
            return

        # ── Disconnect Bot (confirm prompt) ───────────────────────────────────
        if data.startswith("bd:"):
            username = data[3:]
            doc = await db.get_bot_by_username(username)
            if not doc or doc["owner_id"] != uid:
                await callback.answer("Not authorised.", show_alert=True)
                return
            await callback.message.edit_text(
                f"Disconnect @{username}?\n\n"
                "This will remove the bot from the platform. "
                "Customer records will be deleted. This cannot be undone.",
                reply_markup=_disconnect_confirm_kb(username),
            )
            await callback.answer()
            return

        # ── Disconnect Bot (confirmed) ────────────────────────────────────────
        if data.startswith("bdc:"):
            username = data[4:]
            doc = await db.get_bot_by_username(username)
            if not doc or doc["owner_id"] != uid:
                await callback.answer("Not authorised.", show_alert=True)
                return
            token = doc["bot_token"]
            # Stop running instance
            if token in child_bots:
                try:
                    await child_bots[token].session.close()
                except Exception:
                    pass
                child_bots.pop(token, None)
            await db.remove_bot(token)
            bots = await db.list_owner_bots(uid)
            if bots:
                await callback.message.edit_text(
                    f"@{username} has been disconnected and removed.",
                    reply_markup=_my_bots_kb(bots),
                )
            else:
                await callback.message.edit_text(
                    f"@{username} has been disconnected.\n\nYou have no more connected bots.",
                    reply_markup=_ik([_btn("Add Bot", "addbot")], [_btn("Back", "home")]),
                )
            await callback.answer()
            return

        # ── Admin Panel ───────────────────────────────────────────────────────
        if data == "adm":
            if uid != ADMIN_ID:
                await callback.answer("Not authorised.", show_alert=True)
                return
            await state.clear()
            active = await db.list_all_bots(banned=False)
            banned = await db.list_all_bots(banned=True)
            await callback.message.edit_text(
                f"Admin Panel\n\n"
                f"Active bots: {len(active)}\n"
                f"Banned bots: {len(banned)}\n"
                f"Total owners: {len(set(b['owner_id'] for b in active + banned))}",
                reply_markup=_admin_kb(),
            )
            await callback.answer()
            return

        if data == "adm:bots":
            if uid != ADMIN_ID:
                await callback.answer("Not authorised.", show_alert=True)
                return
            all_bots = await db.list_all_bots(False) + await db.list_all_bots(True)
            if not all_bots:
                await callback.message.edit_text(
                    "No bots registered on the platform.",
                    reply_markup=_back_kb("adm"),
                )
            else:
                await callback.message.edit_text(
                    f"All registered bots ({len(all_bots)}):",
                    reply_markup=_admin_bots_kb(all_bots),
                )
            await callback.answer()
            return

        if data == "adm:stats":
            if uid != ADMIN_ID:
                await callback.answer("Not authorised.", show_alert=True)
                return
            active = await db.list_all_bots(False)
            banned = await db.list_all_bots(True)
            all_bots = active + banned
            total_in = sum(b.get("incoming_count", 0) for b in all_bots)
            total_out = sum(b.get("outgoing_count", 0) for b in all_bots)
            total_customers = await db.col_bot_customers().count_documents({})
            total_pairs = await db.col_message_pairs().count_documents({})
            await callback.message.edit_text(
                "Platform Statistics\n\n"
                f"Registered owners: {len(set(b['owner_id'] for b in all_bots))}\n"
                f"Active bots: {len(active)}\n"
                f"Banned bots: {len(banned)}\n"
                f"Total customers: {total_customers}\n"
                f"Messages in: {total_in}\n"
                f"Messages out: {total_out}\n"
                f"Active routing pairs (48h): {total_pairs}",
                reply_markup=_back_kb("adm"),
            )
            await callback.answer()
            return

        if data == "adm:bc":
            if uid != ADMIN_ID:
                await callback.answer("Not authorised.", show_alert=True)
                return
            await state.clear()
            await callback.message.edit_text(
                "Broadcast to All Owners\n\nSend the message to deliver to all bot owners.\n\n/cancel to abort."
            )
            await state.set_state(MotherStates.admin_broadcasting)
            await callback.answer()
            return

        if data.startswith("adm:b:"):
            if uid != ADMIN_ID:
                await callback.answer("Not authorised.", show_alert=True)
                return
            username = data[6:]
            doc = await db.get_bot_by_username(username)
            if not doc:
                await callback.answer("Bot not found.", show_alert=True)
                return
            customers = await db.count_customers(doc["bot_token"])
            is_banned = doc.get("is_banned", False)
            status = "Banned" if is_banned else "Active"
            await callback.message.edit_text(
                f"Admin: @{username}\n\n"
                f"Owner ID: {doc['owner_id']}\n"
                f"Status: {status}\n"
                f"Customers: {customers}\n"
                f"Messages in: {doc.get('incoming_count', 0)}\n"
                f"Messages out: {doc.get('outgoing_count', 0)}\n"
                f"Blocked by: {doc.get('blocked_count', 0)}",
                reply_markup=_bot_manage_admin_kb(username, is_banned),
            )
            await callback.answer()
            return

        if data.startswith("adm:ban:"):
            if uid != ADMIN_ID:
                await callback.answer("Not authorised.", show_alert=True)
                return
            username = data[8:]
            doc = await db.get_bot_by_username(username)
            if doc:
                token = doc["bot_token"]
                await db.set_bot_banned(username, True)
                if token in child_bots:
                    try:
                        await child_bots[token].session.close()
                    except Exception:
                        pass
                    child_bots.pop(token, None)
            await callback.answer(f"@{username} banned.", show_alert=True)
            # Refresh the bot view
            doc = await db.get_bot_by_username(username)
            if doc:
                customers = await db.count_customers(doc["bot_token"])
                await callback.message.edit_text(
                    f"Admin: @{username}\n\nStatus: Banned\n"
                    f"Customers: {customers}",
                    reply_markup=_bot_manage_admin_kb(username, True),
                )
            return

        if data.startswith("adm:uban:"):
            if uid != ADMIN_ID:
                await callback.answer("Not authorised.", show_alert=True)
                return
            username = data[9:]
            doc = await db.get_bot_by_username(username)
            if doc:
                await db.set_bot_banned(username, False)
                from main import launch_child_bot
                asyncio.create_task(launch_child_bot(doc["bot_token"], mother_bot, child_bots))
            await callback.answer(f"@{username} unbanned.", show_alert=True)
            doc = await db.get_bot_by_username(username)
            if doc:
                customers = await db.count_customers(doc["bot_token"])
                await callback.message.edit_text(
                    f"Admin: @{username}\n\nStatus: Active\n"
                    f"Customers: {customers}",
                    reply_markup=_bot_manage_admin_kb(username, False),
                )
            return

        await callback.answer()

    return router


# ============================================================================
# Child bot description setter
# ============================================================================

async def _set_child_descriptions(token: str, mother_username: str) -> None:
    try:
        child = Bot(token=token)
        desc = f"Message forwarding bot. Created via @{mother_username}"
        await child.set_my_description(desc)
        await child.set_my_short_description(desc[:120])
        await child.session.close()
    except Exception as exc:
        logger.warning("Could not set child bot descriptions: %s", exc)


# ============================================================================
# CHILD BOT ROUTER
# ============================================================================

def build_child_router(child_bot: Bot, mother_bot: Bot) -> Router:
    router = Router(name=f"child_{child_bot.token[:10]}")
    child_token = child_bot.token

    async def _doc() -> dict | None:
        return await db.get_bot_by_token(child_token)

    async def _is_owner(uid: int) -> tuple[bool, dict | None]:
        doc = await _doc()
        return (uid == doc["owner_id"], doc) if doc else (False, None)

    # ── /start ────────────────────────────────────────────────────────────────

    @router.message(CommandStart())
    async def child_start(message: Message) -> None:
        doc = await _doc()
        if not doc:
            return
        uid = message.from_user.id

        if uid == doc["owner_id"]:
            customers = await db.count_customers(child_token)
            await message.answer(
                f"Welcome back. You are the owner of this bot.\n\n"
                f"Customers: {customers}\n"
                f"Messages in: {doc.get('incoming_count', 0)}\n"
                f"Messages out: {doc.get('outgoing_count', 0)}\n\n"
                f"Use the menu button (/) or the panel below.",
                reply_markup=_owner_inline_kb(),
            )
            return

        await db.register_customer(child_token, uid)
        mother_username = await _get_mother_username(mother_bot)
        welcome_text = _build_welcome(doc.get("custom_welcome", ""), mother_username)
        await message.answer(welcome_text, reply_markup=_customer_kb())

    # ── Owner commands ────────────────────────────────────────────────────────

    @router.message(Command("cancel"))
    async def child_cancel(message: Message, state: FSMContext) -> None:
        current = await state.get_state()
        await state.clear()
        if current:
            await message.answer("Operation cancelled.", reply_markup=ReplyKeyboardRemove())
            await message.answer(
                "Back to panel:", reply_markup=_owner_inline_kb()
            )

    @router.message(Command("panel"))
    async def owner_panel(message: Message, state: FSMContext) -> None:
        is_owner, doc = await _is_owner(message.from_user.id)
        if not is_owner:
            return
        await state.clear()
        customers = await db.count_customers(child_token)
        await message.answer(
            f"Management Panel\n\n"
            f"Customers: {customers}\n"
            f"Messages in: {doc.get('incoming_count', 0)}\n"
            f"Messages out: {doc.get('outgoing_count', 0)}",
            reply_markup=_owner_inline_kb(),
        )

    @router.message(Command("stats"))
    async def owner_stats(message: Message) -> None:
        is_owner, doc = await _is_owner(message.from_user.id)
        if not is_owner:
            return
        customers = await db.count_customers(child_token)
        await message.answer(_format_stats(doc, customers))

    @router.message(Command("broadcast"))
    async def owner_broadcast_cmd(message: Message, state: FSMContext) -> None:
        is_owner, _ = await _is_owner(message.from_user.id)
        if not is_owner:
            return
        await state.clear()
        await message.answer(
            "Broadcast Mode\n\n"
            "Send the message to deliver to all your customers.\n"
            "You can send text, photos, videos, voice notes, or documents.\n\n"
            "/cancel to abort.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(ChildOwnerStates.broadcasting)

    @router.message(Command("welcome"))
    async def owner_welcome_cmd(message: Message, state: FSMContext) -> None:
        is_owner, doc = await _is_owner(message.from_user.id)
        if not is_owner:
            return
        await state.clear()
        mother_username = await _get_mother_username(mother_bot)
        current = _build_welcome(doc.get("custom_welcome", ""), mother_username)
        await message.answer(
            f"Change Welcome Message\n\n"
            f"Current:\n{current}\n\n"
            f"Send your new text below.\n"
            f"The footer 'Powered by @{mother_username}' is always appended.\n\n"
            "/cancel to abort.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(ChildOwnerStates.changing_welcome)

    # ── Owner inline callbacks ────────────────────────────────────────────────

    @router.callback_query(F.data == "owner:broadcast")
    async def owner_cb_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
        is_owner, _ = await _is_owner(callback.from_user.id)
        if not is_owner:
            await callback.answer("Not authorised.", show_alert=True)
            return
        await state.clear()
        await callback.message.answer(
            "Broadcast Mode\n\n"
            "Send the message to deliver to all your customers.\n\n"
            "/cancel to abort.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(ChildOwnerStates.broadcasting)
        await callback.answer()

    @router.callback_query(F.data == "owner:welcome")
    async def owner_cb_welcome(callback: CallbackQuery, state: FSMContext) -> None:
        is_owner, doc = await _is_owner(callback.from_user.id)
        if not is_owner:
            await callback.answer("Not authorised.", show_alert=True)
            return
        await state.clear()
        mother_username = await _get_mother_username(mother_bot)
        current = _build_welcome(doc.get("custom_welcome", ""), mother_username)
        await callback.message.answer(
            f"Change Welcome Message\n\n"
            f"Current:\n{current}\n\n"
            f"Send your new text. The footer is always appended.\n\n"
            "/cancel to abort.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(ChildOwnerStates.changing_welcome)
        await callback.answer()

    @router.callback_query(F.data == "owner:stats")
    async def owner_cb_stats(callback: CallbackQuery) -> None:
        is_owner, doc = await _is_owner(callback.from_user.id)
        if not is_owner:
            await callback.answer("Not authorised.", show_alert=True)
            return
        customers = await db.count_customers(child_token)
        await callback.message.answer(_format_stats(doc, customers))
        await callback.answer()

    # ── FSM: execute broadcast ────────────────────────────────────────────────

    @router.message(StateFilter(ChildOwnerStates.broadcasting))
    async def owner_do_broadcast(message: Message, state: FSMContext) -> None:
        is_owner, _ = await _is_owner(message.from_user.id)
        if not is_owner:
            await state.clear()
            return
        customers = await db.list_customers(child_token)
        sent = failed = 0
        for cid in customers:
            try:
                await child_bot.copy_message(
                    chat_id=cid,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                await db.increment_outgoing(child_token)
                sent += 1
            except Exception:
                failed += 1
        await message.answer(
            f"Broadcast complete\n\nDelivered: {sent}\nFailed: {failed}",
            reply_markup=_owner_inline_kb(),
        )
        await state.clear()

    # ── FSM: save new welcome ─────────────────────────────────────────────────

    @router.message(StateFilter(ChildOwnerStates.changing_welcome))
    async def owner_do_welcome(message: Message, state: FSMContext) -> None:
        is_owner, _ = await _is_owner(message.from_user.id)
        if not is_owner:
            await state.clear()
            return
        new_text = (message.text or "").strip()
        if not new_text:
            await message.answer("Please send a text message.")
            return
        await db.update_welcome(child_token, new_text)
        mother_username = await _get_mother_username(mother_bot)
        preview = _build_welcome(new_text, mother_username)
        await message.answer(
            f"Welcome message updated.\n\nPreview:\n\n{preview}",
            reply_markup=_owner_inline_kb(),
        )
        await state.clear()

    # ── Customer keyboard buttons ─────────────────────────────────────────────

    @router.message(F.text == "About Us")
    async def child_about(message: Message) -> None:
        is_owner, _ = await _is_owner(message.from_user.id)
        if is_owner:
            return
        await message.answer(
            "About Us\n\n"
            "We are here to assist you with any questions or concerns.\n"
            "Our team will get back to you as soon as possible."
        )

    @router.message(F.text == "Contact Support")
    async def child_contact(message: Message) -> None:
        is_owner, _ = await _is_owner(message.from_user.id)
        if is_owner:
            return
        await message.answer(
            "Please type your message and we will get back to you as soon as possible."
        )

    # ── Catch-all: routing engine ─────────────────────────────────────────────

    @router.message()
    async def child_catch_all(message: Message) -> None:
        doc = await _doc()
        if not doc:
            return
        owner_id = doc["owner_id"]
        uid = message.from_user.id

        # ── Owner side ────────────────────────────────────────────────────────
        if uid == owner_id:
            if message.reply_to_message:
                pair = await db.find_pair_by_owner_msg(child_token, message.reply_to_message.message_id)
                if pair:
                    customer_id = pair["customer_id"]
                    try:
                        await child_bot.copy_message(
                            chat_id=customer_id,
                            from_chat_id=message.chat.id,
                            message_id=message.message_id,
                        )
                        await db.increment_outgoing(child_token)
                    except Exception as exc:
                        err = str(exc).lower()
                        if "forbidden" in err or "blocked" in err:
                            await db.increment_blocked(child_token)
                        await message.answer(
                            "Could not deliver your reply. The customer may have blocked the bot."
                        )
                    return
            await message.answer(
                "Use the panel below or the menu button (/) to manage your bot.",
                reply_markup=_owner_inline_kb(),
            )
            return

        # ── Customer side ─────────────────────────────────────────────────────
        await db.register_customer(child_token, uid)
        await db.increment_incoming(child_token)

        username = message.from_user.username
        name = message.from_user.full_name
        header = (
            f"Message from {name}"
            + (f" (@{username})" if username else "")
            + f"\nID: {uid}"
        )
        try:
            await child_bot.send_message(owner_id, header)
            fwd = await child_bot.forward_message(
                chat_id=owner_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            await db.save_message_pair(
                child_bot_id=child_token,
                owner_msg_id=fwd.message_id,
                customer_id=uid,
                customer_msg_id=message.message_id,
            )
        except Exception as exc:
            logger.warning("Failed to forward to owner %d: %s", owner_id, exc)

    return router
