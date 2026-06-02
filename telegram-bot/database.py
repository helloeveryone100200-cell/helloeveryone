"""
database.py — Motor async MongoDB client and index setup.
"""

import os
import logging
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

MONGODB_URI: str = os.environ["MONGODB_URI"]

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(MONGODB_URI)
    return _client


def get_db():
    return get_client()["livegram_db"]


def col_user_bots():
    return get_db()["user_bots"]


def col_bot_customers():
    return get_db()["bot_customers"]


def col_message_pairs():
    return get_db()["message_pairs"]


async def setup_indexes() -> None:
    """
    Idempotently create all required indexes on startup.
    - bot_customers: unique (bot_id, customer_id) — prevents duplicate tracking.
    - message_pairs: TTL on created_at (48 h) — auto-deletes old routing records.
    """
    logger.info("Setting up MongoDB indexes…")

    await col_bot_customers().create_index(
        [("bot_id", 1), ("customer_id", 1)],
        unique=True,
        name="bot_customer_unique",
    )

    await col_message_pairs().create_index(
        [("created_at", 1)],
        expireAfterSeconds=172800,
        name="message_pairs_ttl_48h",
    )

    logger.info("All indexes are in place.")


# ---------------------------------------------------------------------------
# user_bots helpers
# ---------------------------------------------------------------------------

async def get_bot_by_token(bot_token: str) -> dict | None:
    return await col_user_bots().find_one({"bot_token": bot_token})


async def get_bot_by_username(bot_username: str) -> dict | None:
    return await col_user_bots().find_one({"bot_username": bot_username})


async def get_owner_bot(owner_id: int) -> dict | None:
    return await col_user_bots().find_one({"owner_id": owner_id})


async def list_owner_bots(owner_id: int) -> list[dict]:
    """Return all bots belonging to a given owner."""
    cursor = col_user_bots().find({"owner_id": owner_id})
    return await cursor.to_list(length=None)


async def insert_user_bot(
    owner_id: int,
    bot_token: str,
    bot_username: str,
    custom_welcome: str = "",
    is_banned: bool = False,
) -> None:
    await col_user_bots().insert_one(
        {
            "owner_id": owner_id,
            "bot_token": bot_token,
            "bot_username": bot_username,
            "custom_welcome": custom_welcome,
            "is_banned": is_banned,
            "incoming_count": 0,
            "outgoing_count": 0,
            "blocked_count": 0,
            "created_at": datetime.now(timezone.utc),
        }
    )


async def remove_bot(bot_token: str) -> bool:
    """Permanently delete a bot record from the database."""
    result = await col_user_bots().delete_one({"bot_token": bot_token})
    # Also remove its customer records to prevent data bloat.
    await col_bot_customers().delete_many({"bot_id": bot_token})
    return result.deleted_count > 0


async def set_bot_banned(bot_username: str, banned: bool) -> bool:
    result = await col_user_bots().update_one(
        {"bot_username": bot_username},
        {"$set": {"is_banned": banned}},
    )
    return result.modified_count > 0


async def update_welcome(bot_token: str, new_text: str) -> None:
    await col_user_bots().update_one(
        {"bot_token": bot_token},
        {"$set": {"custom_welcome": new_text}},
    )


async def list_all_bots(banned: bool = False) -> list[dict]:
    cursor = col_user_bots().find({"is_banned": banned})
    return await cursor.to_list(length=None)


async def list_all_owners() -> list[int]:
    cursor = col_user_bots().find({}, {"owner_id": 1, "_id": 0})
    docs = await cursor.to_list(length=None)
    return [d["owner_id"] for d in docs]


async def increment_incoming(bot_token: str) -> None:
    """Atomically increment the incoming message counter — no extra documents."""
    await col_user_bots().update_one(
        {"bot_token": bot_token},
        {"$inc": {"incoming_count": 1}},
    )


async def increment_outgoing(bot_token: str) -> None:
    """Atomically increment the outgoing message counter."""
    await col_user_bots().update_one(
        {"bot_token": bot_token},
        {"$inc": {"outgoing_count": 1}},
    )


async def increment_blocked(bot_token: str) -> None:
    """Atomically increment the blocked-by-customer counter."""
    await col_user_bots().update_one(
        {"bot_token": bot_token},
        {"$inc": {"blocked_count": 1}},
    )


# ---------------------------------------------------------------------------
# bot_customers helpers
# ---------------------------------------------------------------------------

async def register_customer(bot_id: str, customer_id: int) -> bool:
    """
    Register a customer for a bot. Returns True if newly added, False if duplicate.
    The unique index on (bot_id, customer_id) silently rejects duplicates.
    """
    try:
        await col_bot_customers().insert_one(
            {"bot_id": bot_id, "customer_id": customer_id}
        )
        return True
    except Exception:
        return False


async def list_customers(bot_id: str) -> list[int]:
    cursor = col_bot_customers().find({"bot_id": bot_id}, {"customer_id": 1, "_id": 0})
    docs = await cursor.to_list(length=None)
    return [d["customer_id"] for d in docs]


async def count_customers(bot_id: str) -> int:
    return await col_bot_customers().count_documents({"bot_id": bot_id})


# ---------------------------------------------------------------------------
# message_pairs helpers
# ---------------------------------------------------------------------------

async def save_message_pair(
    child_bot_id: str,
    owner_msg_id: int,
    customer_id: int,
    customer_msg_id: int,
) -> None:
    await col_message_pairs().insert_one(
        {
            "child_bot_id": child_bot_id,
            "owner_msg_id": owner_msg_id,
            "customer_id": customer_id,
            "customer_msg_id": customer_msg_id,
            "created_at": datetime.now(timezone.utc),
        }
    )


async def find_pair_by_owner_msg(
    child_bot_id: str, owner_msg_id: int
) -> dict | None:
    return await col_message_pairs().find_one(
        {"child_bot_id": child_bot_id, "owner_msg_id": owner_msg_id}
    )
