"""Точка входа: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .config import load_config
from .db import DB
from . import handlers
from .handlers import AccessMiddleware, fallback_router, router
from .media import MenuImages
from .questions import load_questions
from .schedule import run_background
from .transcribe import Transcriber

log = logging.getLogger("bot")



async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    qn = load_questions(cfg.questions_file)
    db = DB(cfg.db_path)
    db.requeue_interrupted_voice()
    transcriber = Transcriber(cfg.whisper_model, cfg.models_dir, cfg.whisper_language,
                              cfg.whisper_unload_minutes)
    log.info("Часовой пояс: %s; вопросов: %d; Whisper: %s; пользователей в белом списке: %d",
             cfg.tz, len(qn), cfg.whisper_model, len(cfg.allowed_user_ids))
    if not cfg.allowed_user_ids:
        log.warning("ALLOWED_USER_IDS пуст — бот никого не пустит, но подскажет ID в ответ на сообщение.")

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    handlers.images = MenuImages(cfg.menu_dir, db)
    handlers.kb.set_icons_enabled(cfg.custom_emoji)
    log.info("Картинки меню: %s", cfg.menu_dir if cfg.menu_dir.is_dir() else "нет папки — меню будет текстом")
    dp = Dispatcher(db=db, cfg=cfg, qn=qn, transcriber=transcriber)
    dp.message.outer_middleware(AccessMiddleware())
    dp.callback_query.outer_middleware(AccessMiddleware())
    dp.include_router(router)
    dp.include_router(fallback_router)

    await bot.set_my_commands([
        BotCommand(command="menu", description="Главное меню"),
        BotCommand(command="record", description="Записать приступ"),
        BotCommand(command="export", description="Выгрузить дневник"),
        BotCommand(command="settings", description="Настройки опросов"),
    ])

    background = asyncio.create_task(run_background(bot, db, cfg, qn, transcriber))
    model_download = asyncio.create_task(transcriber.download_forever())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        background.cancel()
        model_download.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
