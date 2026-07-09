import asyncio
import contextlib
import logging
import os
import socket

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent, Update

from . import metrics
from .command_menu import build_command_scopes
from .config import config
from .db import init_db, migrate_feedback_samples
from .handlers.commands import router as commands_router
from .handlers.ai_feedback import router as ai_feedback_router
from .hde_api import close_shared_connector
from .hde_webhook import hde_webhook_handler
from .scheduler import run_scheduler, _ALLOWED_UPDATES
from .tg_session import RetrySession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _build_dispatcher(bot: Bot) -> Dispatcher:
    dp = Dispatcher()

    @dp.update.outer_middleware()
    async def log_update(handler, event: Update, data: dict):
        logger.info("⬇ Update id=%s type=%s", event.update_id, event.event_type)
        result = await handler(event, data)
        logger.info("✓ Update id=%s done", event.update_id)
        return result

    @dp.errors()
    async def global_error_handler(event: ErrorEvent) -> bool:
        logger.exception(
            "Unhandled error for update %s: %s",
            event.update.update_id if event.update else "?",
            event.exception,
        )
        return True

    dp.include_router(ai_feedback_router)
    dp.include_router(commands_router)
    return dp


async def _health_handler(request: web.Request | None) -> web.Response:
    return web.json_response({"status": "ok", **metrics.snapshot()})


async def _run_hde_server(bot: Bot, stop_event: asyncio.Event) -> None:
    """aiohttp server — only handles HDE webhooks."""
    app = web.Application()
    app["bot"] = bot
    app.router.add_post(config.webhook_path_hde, hde_webhook_handler)
    app.router.add_get("/health", _health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.app_port)
    await site.start()
    logger.info("HDE webhook server on port %d", config.app_port)
    await stop_event.wait()
    await runner.cleanup()


def _sd_notify(state: str) -> None:
    """Send a notification to systemd (no-op outside a systemd unit).

    Used for WatchdogSec keep-alive pings: if the event loop hangs, pings
    stop and systemd restarts the service.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr or not hasattr(socket, "AF_UNIX"):
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except OSError as exc:
        logger.warning("sd_notify failed: %s", exc)


_WATCHDOG_INTERVAL_SECONDS = 30  # WatchdogSec=90 in the unit → 3x margin


async def _watchdog_loop(stop_event: asyncio.Event, interval: float = _WATCHDOG_INTERVAL_SECONDS) -> None:
    while not stop_event.is_set():
        _sd_notify("WATCHDOG=1")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval)


def _ensure_webhook_secret() -> None:
    """Fail closed: refuse to start with an unauthenticated webhook endpoint."""
    if not config.hde_webhook_secret:
        raise SystemExit(
            "HDE_WEBHOOK_SECRET is not set — the HDE webhook endpoint would be "
            "open to anyone. Set it in the environment and restart."
        )


async def _main_async() -> None:
    _ensure_webhook_secret()
    await init_db()
    migrated = await migrate_feedback_samples()
    if migrated:
        logger.info("Migration: backfilled %d past 👍 records into optimization_samples", migrated)

    bot = Bot(token=config.bot_token, session=RetrySession())
    dp = _build_dispatcher(bot)

    # Remove any leftover webhook so polling works
    await bot.delete_webhook(drop_pending_updates=False)

    await bot.delete_my_commands()
    for entry in build_command_scopes(config.group_chat_id):
        await bot.set_my_commands(entry["commands"], scope=entry["scope"])
    logger.info("Bot started (polling mode)")

    stop_event = asyncio.Event()

    scheduler_task = asyncio.create_task(run_scheduler(bot, stop_event))
    hde_task = asyncio.create_task(_run_hde_server(bot, stop_event))
    watchdog_task = asyncio.create_task(_watchdog_loop(stop_event))
    _sd_notify("READY=1")

    try:
        await dp.start_polling(bot, allowed_updates=_ALLOWED_UPDATES, handle_signals=True)
    finally:
        stop_event.set()
        for task in (scheduler_task, hde_task, watchdog_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await bot.session.close()
        await close_shared_connector()
        logger.info("Bot stopped")


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
