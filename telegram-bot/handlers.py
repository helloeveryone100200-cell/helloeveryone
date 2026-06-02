"""
handlers.py — All bot handlers for the Livegram Mother Bot platform.

Covers:
  • Mother Bot: user onboarding, token registration, admin commands.
  • Child Bot: customer flows, owner control panel, message routing (Livegram engine).
"""

import asyncio
import logging
import os
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ContentType, ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
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

# ---------------------------------------------------------------------------
# FSM States
# ---------------------------------------------------------------------------

class MotherStates(StatesGroup):
    awaiting_token = State()


class ChildOwnerStates(StatesGroup):
    broadcasting = State()
    changing_welcome = State()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _owner_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Broadcast to Customers", callback_data="owner:broadcast")],
            [InlineKeyboardButton(text="⚙️ Change Welcome Message", callback_data="owner:welcome")],
            [InlineKeyboardButton(text="📊 My Bot Statistics", callback_data="owner:stats")],
        ]
    )


def _customer_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="ℹ️ About Us"), KeyboardButton(text="📞 Contact Support")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


async def _get_mother_username(mother_bot: Bot) -> str:
    me = await mother_bot.get_me()
    return me.username or "MotherBot"


def _build_welcome(custom_text: str, mother_username: str) -> str:
    base = custom_text.strip() if custom_text.strip() else "Hello!\n\nYou can contact us using this bot."
    return f"{base}\n\n🤖 Powered by @{mother_username}"


# ---------------------------------------------------------------------------
# MOTHER BOT ROUTER
# ---------------------------------------------------------------------------

def build_mother_router(mother_bot: Bot, child_bots: dict[str, Bot]) -> Router:
    """
    Returns the router for the Mother Bot.
    child_bots is a live mutable dict: {bot_token: Bot instance}.
    It is shared with main.py so dynamically spawned bots are visible.
    """
    router = Router(name="mother")

    # --- /start ---
    @router.message(CommandStart())
    async def mother_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        text = (
            "👋 *Welcome to the Livegram Mother Bot!*\n\n"
            "This platform lets you create your own personal Telegram feedback/support bot "
            "in seconds — no coding required.\n\n"
            "*How to get started:*\n"
            "1️⃣ Open @BotFather and create a new bot.\n"
            "2️⃣ Copy the API token BotFather gives you.\n"
            "3️⃣ Send the token here and we'll set everything up automatically.\n\n"
            "Send your bot token now to begin ➡️"
        )
        await message.answer(text, parse_mode=ParseMode.MARKDOWN)
        await state.set_state(MotherStates.awaiting_token)

    # --- Token submission ---
    @router.message(StateFilter(MotherStates.awaiting_token))
    async def mother_receive_token(message: Message, state: FSMContext) -> None:
        token = message.text.strip() if message.text else ""
        if not token:
            await message.answer("❌ Please send a valid bot token string.")
            return

        # Check duplicate
        existing = await db.get_bot_by_token(token)
        if existing:
            await message.answer("⚠️ This bot token is already registered on the platform.")
            await state.clear()
            return

        # Validate token via Telegram API
        status_msg = await message.answer("🔄 Validating your token…")
        try:
            probe_bot = Bot(token=token)
            me = await probe_bot.get_me()
            bot_username: str = me.username or ""
            await probe_bot.session.close()
        except Exception as exc:
            logger.warning("Invalid token submitted by %s: %s", message.from_user.id, exc)
            await status_msg.edit_text(
                "❌ *Invalid token.* Telegram rejected it.\n\n"
                "Make sure you copied the full token from @BotFather and try again.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        owner_id: int = message.from_user.id

        # Persist to DB
        await db.insert_user_bot(
            owner_id=owner_id,
            bot_token=token,
            bot_username=bot_username,
            custom_welcome="",
            is_banned=False,
        )

        # Auto-set child bot description (best-effort, non-blocking)
        mother_username = await _get_mother_username(mother_bot)
        asyncio.create_task(
            _set_child_bot_descriptions(token, mother_username)
        )

        # Dynamically spin up the child bot
        from main import launch_child_bot  # imported here to avoid circular at module level
        asyncio.create_task(launch_child_bot(token, mother_bot, child_bots))

        await status_msg.edit_text(
            f"✅ *Success!* Your bot @{bot_username} is now live.\n\n"
            f"Customers can message it and you'll receive their messages here.\n"
            f"Reply to any forwarded message to reply back to the customer.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.clear()

    # --- Admin: /stats ---
    @router.message(Command("stats"))
    async def admin_stats(message: Message) -> None:
        if message.from_user.id != ADMIN_ID:
            return
        active_bots = await db.list_all_bots(banned=False)
        banned_bots = await db.list_all_bots(banned=True)
        total_owners = len(set(b["owner_id"] for b in active_bots + banned_bots))
        total_customers = await db.col_bot_customers().count_documents({})
        total_pairs = await db.col_message_pairs().count_documents({})
        text = (
            "📊 *System Statistics*\n\n"
            f"👥 Registered owners: `{total_owners}`\n"
            f"🤖 Active child bots: `{len(active_bots)}`\n"
            f"🚫 Banned bots: `{len(banned_bots)}`\n"
            f"📨 Tracked customers: `{total_customers}`\n"
            f"🔗 Active message pairs (≤48 h): `{total_pairs}`"
        )
        await message.answer(text, parse_mode=ParseMode.MARKDOWN)

    # --- Admin: /broadcast <message> ---
    @router.message(Command("broadcast"))
    async def admin_broadcast(message: Message) -> None:
        if message.from_user.id != ADMIN_ID:
            return
        text_body = message.text.partition(" ")[2].strip()
        if not text_body:
            await message.answer("Usage: /broadcast <your message>")
            return
        owner_ids = await db.list_all_owners()
        sent = 0
        for uid in owner_ids:
            try:
                await mother_bot.send_message(uid, f"📢 *Announcement*\n\n{text_body}", parse_mode=ParseMode.MARKDOWN)
                sent += 1
            except Exception:
                pass
        await message.answer(f"✅ Broadcast sent to {sent}/{len(owner_ids)} owners.")

    # --- Admin: /ban_bot <bot_username> ---
    @router.message(Command("ban_bot"))
    async def admin_ban_bot(message: Message) -> None:
        if message.from_user.id != ADMIN_ID:
            return
        username = message.text.partition(" ")[2].strip().lstrip("@")
        if not username:
            await message.answer("Usage: /ban_bot <bot_username>")
            return
        bot_doc = await db.get_bot_by_username(username)
        if not bot_doc:
            await message.answer(f"❌ Bot @{username} not found.")
            return
        token = bot_doc["bot_token"]
        modified = await db.set_bot_banned(username, True)
        # Stop running instance
        if token in child_bots:
            try:
                await child_bots[token].session.close()
            except Exception:
                pass
            child_bots.pop(token, None)
        if modified:
            await message.answer(f"🚫 Bot @{username} has been banned and stopped.")
        else:
            await message.answer(f"⚠️ Could not update ban status for @{username}.")

    # --- Admin: /unban_bot <bot_username> ---
    @router.message(Command("unban_bot"))
    async def admin_unban_bot(message: Message) -> None:
        if message.from_user.id != ADMIN_ID:
            return
        username = message.text.partition(" ")[2].strip().lstrip("@")
        if not username:
            await message.answer("Usage: /unban_bot <bot_username>")
            return
        bot_doc = await db.get_bot_by_username(username)
        if not bot_doc:
            await message.answer(f"❌ Bot @{username} not found.")
            return
        await db.set_bot_banned(username, False)
        token = bot_doc["bot_token"]
        from main import launch_child_bot
        asyncio.create_task(launch_child_bot(token, mother_bot, child_bots))
        await message.answer(f"✅ Bot @{username} has been unbanned and restarted.")

    # --- Admin: /force_clean ---
    @router.message(Command("force_clean"))
    async def admin_force_clean(message: Message) -> None:
        if message.from_user.id != ADMIN_ID:
            return
        await db.setup_indexes()
        await message.answer("✅ Database indexes verified and re-applied.")

    return router


async def _set_child_bot_descriptions(token: str, mother_username: str) -> None:
    """Best-effort: set child bot profile text to link back to the Mother Bot."""
    try:
        child = Bot(token=token)
        desc = f"Customer Support Bot 🔗 Created via @{mother_username}"
        await child.set_my_description(desc)
        await child.set_my_short_description(desc[:120])
        await child.session.close()
    except Exception as exc:
        logger.warning("Could not set child bot descriptions: %s", exc)


# ---------------------------------------------------------------------------
# CHILD BOT ROUTER
# ---------------------------------------------------------------------------

def build_child_router(child_bot: Bot, mother_bot: Bot) -> Router:
    """
    Returns a router for a single child bot instance.
    Each child bot gets its own Dispatcher with its own router.
    """
    router = Router(name=f"child_{child_bot.token[:10]}")
    child_token = child_bot.token

    async def _bot_doc() -> dict | None:
        return await db.get_bot_by_token(child_token)

    # --- /start for customers and owners ---
    @router.message(CommandStart())
    async def child_start(message: Message) -> None:
        doc = await _bot_doc()
        if not doc:
            return
        user_id: int = message.from_user.id
        owner_id: int = doc["owner_id"]

        if user_id == owner_id:
            await message.answer(
                "👋 Welcome back! You are the *owner* of this bot.\n\n"
                "Send any message here to manage your bot:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_owner_panel_keyboard(),
            )
            return

        # Register customer (silent duplicate guard via unique index)
        await db.register_customer(child_token, user_id)

        mother_username = await _get_mother_username(mother_bot)
        welcome_text = _build_welcome(doc.get("custom_welcome", ""), mother_username)
        await message.answer(welcome_text, reply_markup=_customer_keyboard())

    # --- Customer keyboard buttons ---
    @router.message(F.text == "ℹ️ About Us")
    async def child_about(message: Message) -> None:
        doc = await _bot_doc()
        if not doc or message.from_user.id == doc["owner_id"]:
            return
        await message.answer(
            "ℹ️ *About Us*\n\n"
            "We are here to assist you with any questions or concerns.\n"
            "Feel free to reach out to our team at any time — we're happy to help!",
            parse_mode=ParseMode.MARKDOWN,
        )

    @router.message(F.text == "📞 Contact Support")
    async def child_contact(message: Message) -> None:
        doc = await _bot_doc()
        if not doc or message.from_user.id == doc["owner_id"]:
            return
        await message.answer(
            "📞 Please type your message below, and our team will get back to you as soon as possible."
        )

    # --- Owner inline callback: Broadcast ---
    @router.callback_query(F.data == "owner:broadcast")
    async def owner_cb_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
        doc = await _bot_doc()
        if not doc or callback.from_user.id != doc["owner_id"]:
            await callback.answer("Not authorised.", show_alert=True)
            return
        await callback.message.answer(
            "📢 *Broadcast Mode*\n\nSend the message you want to broadcast to all your customers.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(ChildOwnerStates.broadcasting)
        await callback.answer()

    # --- Owner inline callback: Change Welcome ---
    @router.callback_query(F.data == "owner:welcome")
    async def owner_cb_welcome(callback: CallbackQuery, state: FSMContext) -> None:
        doc = await _bot_doc()
        if not doc or callback.from_user.id != doc["owner_id"]:
            await callback.answer("Not authorised.", show_alert=True)
            return
        mother_username = await _get_mother_username(mother_bot)
        await callback.message.answer(
            f"⚙️ *Change Welcome Message*\n\n"
            f"Send your new welcome text. Note: the branding footer is automatically appended:\n"
            f"`🤖 Powered by @{mother_username}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(ChildOwnerStates.changing_welcome)
        await callback.answer()

    # --- Owner inline callback: Stats ---
    @router.callback_query(F.data == "owner:stats")
    async def owner_cb_stats(callback: CallbackQuery) -> None:
        doc = await _bot_doc()
        if not doc or callback.from_user.id != doc["owner_id"]:
            await callback.answer("Not authorised.", show_alert=True)
            return
        total = await db.count_customers(child_token)
        await callback.message.answer(
            f"📊 *Your Bot Statistics*\n\n"
            f"👥 Unique customers: `{total}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        await callback.answer()

    # --- FSM: owner broadcast message ---
    @router.message(StateFilter(ChildOwnerStates.broadcasting))
    async def owner_do_broadcast(message: Message, state: FSMContext) -> None:
        doc = await _bot_doc()
        if not doc or message.from_user.id != doc["owner_id"]:
            await state.clear()
            return
        customers = await db.list_customers(child_token)
        sent = 0
        for cid in customers:
            try:
                await child_bot.forward_message(chat_id=cid, from_chat_id=message.chat.id, message_id=message.message_id)
                sent += 1
            except Exception:
                pass
        await message.answer(
            f"✅ Broadcast sent to {sent}/{len(customers)} customers.",
            reply_markup=_owner_panel_keyboard(),
        )
        await state.clear()

    # --- FSM: owner change welcome ---
    @router.message(StateFilter(ChildOwnerStates.changing_welcome))
    async def owner_do_change_welcome(message: Message, state: FSMContext) -> None:
        doc = await _bot_doc()
        if not doc or message.from_user.id != doc["owner_id"]:
            await state.clear()
            return
        new_text = message.text.strip() if message.text else ""
        if not new_text:
            await message.answer("❌ Please send a non-empty text message.")
            return
        await db.update_welcome(child_token, new_text)
        mother_username = await _get_mother_username(mother_bot)
        preview = _build_welcome(new_text, mother_username)
        await message.answer(
            f"✅ *Welcome message updated!*\n\nPreview:\n\n{preview}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_owner_panel_keyboard(),
        )
        await state.clear()

    # --- Catch-all: forward customer messages to owner, route owner replies ---
    @router.message()
    async def child_catch_all(message: Message) -> None:
        doc = await _bot_doc()
        if not doc:
            return
        owner_id: int = doc["owner_id"]
        user_id: int = message.from_user.id

        # ── OWNER SIDE: handle reply to a forwarded customer message ─────────
        if user_id == owner_id:
            # If the owner replied to a message, try to route it back to the customer.
            if message.reply_to_message:
                pair = await db.find_pair_by_owner_msg(child_token, message.reply_to_message.message_id)
                if pair:
                    customer_id: int = pair["customer_id"]
                    try:
                        await child_bot.copy_message(
                            chat_id=customer_id,
                            from_chat_id=message.chat.id,
                            message_id=message.message_id,
                        )
                    except Exception as exc:
                        logger.warning("Failed to route reply to customer %s: %s", customer_id, exc)
                        await message.answer("⚠️ Could not deliver reply — customer may have blocked the bot.")
                    return
            # Otherwise show control panel
            await message.answer(
                "👇 Use the control panel to manage your bot:",
                reply_markup=_owner_panel_keyboard(),
            )
            return

        # ── CUSTOMER SIDE: forward message to owner ───────────────────────────
        await db.register_customer(child_token, user_id)
        username = message.from_user.username
        name = message.from_user.full_name
        header = (
            f"📨 *Message from* [{name}](tg://user?id={user_id})"
            + (f" (@{username})" if username else "")
            + f"\n`ID: {user_id}`"
        )

        try:
            header_msg = await child_bot.send_message(
                owner_id,
                header,
                parse_mode=ParseMode.MARKDOWN,
            )
            # Forward the actual content
            fwd_msg = await child_bot.forward_message(
                chat_id=owner_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            # Save pair so owner can reply
            await db.save_message_pair(
                child_bot_id=child_token,
                owner_msg_id=fwd_msg.message_id,
                customer_id=user_id,
                customer_msg_id=message.message_id,
            )
        except Exception as exc:
            logger.warning("Failed to forward customer message to owner %s: %s", owner_id, exc)

    return router
