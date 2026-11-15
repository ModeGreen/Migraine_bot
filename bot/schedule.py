"""Расписание опросов и фоновые задачи (без внешних планировщиков)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TICK_SECONDS = 30
QUESTIONS_AUTOCLOSE = timedelta(hours=24)


def compute_next(user: dict, tz: ZoneInfo, after: datetime, extra_days: int = 0) -> datetime:
    """Ближайший момент poll_time (по местному времени) строго после `after`, плюс extra_days."""
    hh, mm = (int(x) for x in user["poll_time"].split(":"))
    local = after.astimezone(tz)
    candidate = datetime.combine(local.date(), time(hh, mm), tzinfo=tz)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate + timedelta(days=extra_days)


async def run_background(bot, db, cfg, qn, transcriber) -> None:
    # импорт здесь, чтобы избежать циклического импорта
    from .db import from_iso, now_utc, to_iso
    from .handlers import finish_entry, send_poll

    last_backup_day = None
    while True:
        try:
            now = now_utc()
            # 0. Ежедневный бэкап базы
            if now.date() != last_backup_day:
                path = db.backup(cfg.data_dir / "backups")
                last_backup_day = now.date()
                log.info("Бэкап базы: %s", path)

            # 1. Регулярные опросы
            for user in db.all_users():
                if user["paused"] or not user["next_poll_at"]:
                    continue
                if now >= from_iso(user["next_poll_at"]):
                    nxt = compute_next(user, cfg.tz, now, extra_days=user["interval_days"] - 1)
                    db.update_user(user["user_id"], next_poll_at=to_iso(nxt))
                    try:
                        await send_poll(bot, db, cfg, user)
                    except Exception:  # noqa: BLE001
                        log.exception("Не удалось отправить опрос пользователю %s", user["user_id"])

            # 2. «Спроси позже»
            for poll in db.due_reminders(now):
                user = db.get_user(poll["user_id"])
                try:
                    await send_poll(bot, db, cfg, user, poll_id=poll["id"])
                except Exception:  # noqa: BLE001
                    log.exception("Не удалось повторить опрос %s", poll["id"])
                    db.update_poll(poll["id"], remind_at=None)

            # 3. Автозакрытие забытых записей
            stale = db.stale_entries("notes", now - timedelta(minutes=cfg.notes_autoclose_minutes))
            stale += db.stale_entries("questions", now - QUESTIONS_AUTOCLOSE)
            for entry in stale:
                user = db.get_user(entry["user_id"])
                try:
                    await finish_entry(bot, db, qn, cfg, user["chat_id"], entry["id"], auto=True)
                except Exception:  # noqa: BLE001
                    log.exception("Не удалось закрыть запись %s", entry["id"])
                    db.close_entry(entry["id"])

            # 4. Выгрузка Whisper из памяти
            await transcriber.unload_if_idle()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в фоновом цикле")
        await asyncio.sleep(TICK_SECONDS)
