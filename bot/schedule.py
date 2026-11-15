"""Расписание опросов и фоновые задачи (без внешних планировщиков)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from datetime import tzinfo as ZoneInfo  # любой tzinfo: ZoneInfo или фиксированное смещение

log = logging.getLogger(__name__)

TICK_SECONDS = 30
QUESTIONS_AUTOCLOSE = timedelta(hours=24)


def compute_next(user: dict, tz: ZoneInfo, after: datetime, extra_days: int = 0) -> datetime:
    """Ближайший момент poll_time (по местному времени пользователя) строго после `after`,
    плюс extra_days. `tz` — пояс по умолчанию, если пользователь свой ещё не указал."""
    from .tz import user_tz

    tz = user_tz(user, tz)
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
            # 0. Ежедневный бэкап базы (ошибка бэкапа не должна останавливать опросы)
            if now.date() != last_backup_day:
                last_backup_day = now.date()
                try:
                    log.info("Бэкап базы: %s", db.backup(cfg.data_dir / "backups"))
                except Exception:  # noqa: BLE001
                    log.exception("Не удалось сделать бэкап базы")

            # 1. Регулярные опросы — только тем, кто сейчас в белом списке
            for user in db.all_users():
                if user["user_id"] not in cfg.allowed_user_ids:
                    continue
                if user["paused"] or not user["next_poll_at"]:
                    continue
                try:
                    if now >= from_iso(user["next_poll_at"]):
                        nxt = compute_next(user, cfg.tz, now, extra_days=max(0, user["interval_days"] - 1))
                        db.update_user(user["user_id"], next_poll_at=to_iso(nxt))
                        await send_poll(bot, db, cfg, user)
                except Exception:  # noqa: BLE001
                    log.exception("Не удалось отправить опрос пользователю %s", user["user_id"])

            # 2. «Спроси позже»
            for poll in db.due_reminders(now):
                user = db.get_user(poll["user_id"])
                if not user or user["user_id"] not in cfg.allowed_user_ids:
                    db.update_poll(poll["id"], remind_at=None)
                    continue
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
                if not user or user["user_id"] not in cfg.allowed_user_ids:
                    db.close_entry(entry["id"])
                    continue
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
