"""
main.py — Entry point for the Message Forwarding Mother Bot platform.
"""

import asyncio
import logging
import os
import signal

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import database as db
from handlers import (
    build_mother_router,
    build_child_router,
    setup_mother_bot_commands,
    setup_child_bot_commands,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
PORT: int = int(os.environ.get("PORT", 10000))

mother_bot: Bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

child_bots: dict[str, Bot] = {}
child_tasks: dict[str, asyncio.Task] = {}


async def launch_child_bot(token: str, m_bot: Bot, c_bots: dict[str, Bot]) -> None:
    """
    Spin up a child bot polling loop. Safe to call multiple times.
    Registers owner-scoped menu commands before starting polling.
    """
    if token in c_bots:
        logger.info("Child bot already running for token …%s", token[-8:])
        return

    bot_doc = await db.get_bot_by_token(token)
    if not bot_doc or bot_doc.get("is_banned"):
        logger.info("Skipping banned/missing child bot token …%s", token[-8:])
        return

    child_bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    c_bots[token] = child_bot

    owner_id: int = bot_doc["owner_id"]
    await setup_child_bot_commands(child_bot, owner_id)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_child_router(child_bot, m_bot))

    async def _polling_task() -> None:
        logger.info("Polling started: @%s", bot_doc.get("bot_username", "?"))
        try:
            await dp.start_polling(child_bot, handle_signals=False, drop_pending_updates=True)
        except asyncio.CancelledError:
            logger.info("Polling cancelled: token …%s", token[-8:])
        except Exception as exc:
            logger.exception("Child bot error (token …%s): %s", token[-8:], exc)
        finally:
            c_bots.pop(token, None)
            child_tasks.pop(token, None)
            await child_bot.session.close()

    task = asyncio.create_task(_polling_task(), name=f"child_{token[-8:]}")
    child_tasks[token] = task


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(
        text=f"OK | Active child bots: {len(child_bots)}",
        content_type="text/plain",
    )


def build_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    return app


async def main() -> None:
    logger.info("=== Message Forwarding Mother Bot starting ===")

    await db.setup_indexes()
    await setup_mother_bot_commands(mother_bot)

    mother_dp = Dispatcher(storage=MemoryStorage())
    mother_dp.include_router(build_mother_router(mother_bot, child_bots))

    active_bots = await db.list_all_bots(banned=False)
    logger.info("Loading %d active child bot(s)…", len(active_bots))
    await asyncio.gather(
        *[launch_child_bot(b["bot_token"], mother_bot, child_bots) for b in active_bots]
    )

    web_app = build_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("Health-check server on port %d", PORT)

    logger.info("Mother Bot polling started.")
    try:
        await mother_dp.start_polling(mother_bot, handle_signals=False, drop_pending_updates=True)
    finally:
        logger.info("Shutting down %d child bot(s)…", len(child_tasks))
        for task in list(child_tasks.values()):
            task.cancel()
        await asyncio.gather(*child_tasks.values(), return_exceptions=True)
        await runner.cleanup()
        await mother_bot.session.close()
        logger.info("Shutdown complete.")


def _handle_signal(sig: signal.Signals) -> None:
    logger.info("Signal %s — shutting down.", sig.name)
    for task in asyncio.all_tasks():
        task.cancel()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Platform stopped.")
    finally:
        loop.close()
