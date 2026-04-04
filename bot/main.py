import asyncio
import contextlib
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from .config import config
from .db import init_db
from .handlers.commands import router as commands_router
from .hde_webhook import hde_webhook_handler
from .scheduler import run_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def on_startup(app: web.Application) -> None:
    await init_db()
    bot: Bot = app["bot"]
    webhook_url = f"{config.webhook_host}{config.webhook_path_tg}"
    await bot.set_webhook(webhook_url)

    stop_event = asyncio.Event()
    app["scheduler_stop_event"] = stop_event
    app["scheduler_task"] = asyncio.create_task(run_scheduler(bot, stop_event))

    logger.info("Webhook set to %s", webhook_url)
    logger.info("Bot started")


async def on_shutdown(app: web.Application) -> None:
    stop_event: asyncio.Event = app["scheduler_stop_event"]
    scheduler_task: asyncio.Task = app["scheduler_task"]
    stop_event.set()
    scheduler_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await scheduler_task

    bot: Bot = app["bot"]
    await bot.delete_webhook()
    await bot.session.close()
    logger.info("Bot stopped")


def create_app() -> web.Application:
    bot = Bot(token=config.bot_token)
    dp = Dispatcher()
    dp.include_router(commands_router)

    app = web.Application()
    app["bot"] = bot

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(
        app, path=config.webhook_path_tg
    )
    app.router.add_post(config.webhook_path_hde, hde_webhook_handler)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    setup_application(app, dp, bot=bot)
    return app


def main() -> None:
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=config.app_port)


if __name__ == "__main__":
    main()
