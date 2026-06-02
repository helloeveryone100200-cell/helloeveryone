"""
database.py — Motor async MongoDB client and index setup for Livegram Mother Bot.
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
    Idempotently create all required MongoDB indexes on startup.

    - bot_customers: unique composite index on (bot_id, customer_id)
      to prevent duplicate tracking entries and storage bloat.
    - message_pairs: TTL index on created_at set to 48 hours (172800 s)
      so old routing records are automatically deleted by MongoDB.
    """
    logger.info("Setting up MongoDB indexes…")

    # Unique composite index: prevents duplicate (bot_id, customer_id) entries.
    await col_bot_customers().create_index(
        [("bot_id", 1), ("customer_id", 1)],
        unique=True,
        name="bot_customer_unique",
    )
    logger.info("Index 'bot_customer_unique' ensured on bot_customers.")

    # TTL index: MongoDB removes documents 48 h after created_at.
    await col_message_pairs().create_index(
        [("created_at", 1)],
        expireAfterSeconds=172800,
        name="message_pairs_ttl_48h",
    )
    logger.info("Index 'message_pairs_ttl_48h' ensured on message_pairs.")

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
        }
    )


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


# ---------------------------------------------------------------------------
# bot_customers helpers
# ---------------------------------------------------------------------------

async def register_customer(bot_id: str, customer_id: int) -> None:
    """Insert silently; ignore duplicate key errors (index enforces uniqueness)."""
    try:
        await col_bot_customers().insert_one(
            {"bot_id": bot_id, "customer_id": customer_id}
        )
    except Exception:
        pass  # Duplicate — already tracked.


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
