"""Диалог с пользователем: опросы, вопросы о приступе, заметки, настройки, выгрузка."""
from __future__ import annotations

import logging
from datetime import timedelta, tzinfo
from html import escape
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    Message,
    ReactionTypeEmoji,
    TelegramObject,
)

from . import keyboards as kb
from .config import Config
from .db import DB, from_iso, now_utc, to_iso
from .export import build_csv, build_markdown
from .questions import Question, Questionnaire
from .schedule import compute_next
from .transcribe import Transcriber
from .tz import TZ_CHOICES, tz_from_local_time, tz_from_location, tz_label, user_tz

log = logging.getLogger(__name__)
router = Router()
# Свободный текст — в отдельном роутере, который подключается последним,
# чтобы он не перехватывал нажатия кнопок меню
fallback_router = Router()


# ---------------------------------------------------------------- доступ
class AccessMiddleware(BaseMiddleware):
    """Пускает только пользователей из ALLOWED_USER_IDS и только в личном чате,
    регистрирует их в базе."""

    def __init__(self) -> None:
        self._denied: dict[int, Any] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        cfg: Config = data["cfg"]
        if user is None:
            return None
        # Только личные сообщения: в группе дневник (медицинские данные) увидели бы другие люди
        if chat is None or chat.type != "private" or chat.id != user.id:
            if isinstance(event, CallbackQuery):
                await event.answer()
            return None
        if user.id not in cfg.allowed_user_ids:
            log.warning("Отказ в доступе: %s", user.id)
            # Отвечаем не чаще раза в час, чтобы бот нельзя было заставить спамить
            now = now_utc()
            last = self._denied.get(user.id)
            if isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            elif last is None or now - last > timedelta(hours=1):
                self._denied[user.id] = now
                await event.answer(
                    "Это личный бот, у тебя нет доступа.\n"
                    f"Твой Telegram ID: <code>{user.id}</code>\n"
                    "Если это твой бот — добавь ID в ALLOWED_USER_IDS в файле .env и перезапусти его."
                )
            return None
        db: DB = data["db"]
        row, created = db.ensure_user(user.id, chat.id)
        if created or row["next_poll_at"] is None:
            db.update_user(user.id, next_poll_at=to_iso(compute_next(row, cfg.tz, now_utc())))
            row = db.get_user(user.id)
        data["user"] = row
        return await handler(event, data)


@router.error()
async def on_error(event: ErrorEvent) -> bool:
    """Ошибка в одном обработчике (например, подделанные данные кнопки) не должна ронять бота."""
    log.error("Ошибка при обработке обновления: %r", event.exception, exc_info=event.exception)
    cq = event.update.callback_query
    if cq:
        try:
            await cq.answer("Что-то пошло не так 😔")
        except TelegramBadRequest:
            pass
    return True


fallback_router.error()(on_error)


# ---------------------------------------------------------------- вспомогательное
def is_valid_time(value: str) -> bool:
    """Строго «ЧЧ:ММ» в пределах суток."""
    if len(value) != 5 or value[2] != ":" or not (value[:2] + value[3:]).isdigit():
        return False
    return int(value[:2]) < 24 and int(value[3:]) < 60


def utz(user: dict | None, cfg: Config) -> tzinfo:
    """Часовой пояс пользователя (или пояс по умолчанию, пока он не указан)."""
    return user_tz(user, cfg.tz)


def fmt_local(ts: str, tz: tzinfo, with_weekday: bool = False) -> str:
    dt = from_iso(ts).astimezone(tz)
    wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][dt.weekday()] + " " if with_weekday else ""
    return f"{wd}{dt:%d.%m %H:%M}"


def entry_summary(entry: dict, db: DB, qn: Questionnaire, cfg: Config) -> str:
    tz = utz(db.get_user(entry["user_id"]), cfg)
    lines = [f"<b>🤕 Приступ, запись от {fmt_local(entry['created_at'], tz, True)}</b>"]
    for q in qn.questions:
        lines.append(f"• {escape(q.label)}: {escape(q.answer_to_text(entry['answers'].get(q.id)))}")
    notes = db.notes_for(entry["id"])
    voices = sum(1 for n in notes if n["kind"] == "voice")
    if notes:
        lines.append(f"• {escape(qn.final_label)}: {len(notes)} сообщ." + (f" (голосовых: {voices})" if voices else ""))
    return "\n".join(lines)


async def safe_edit(bot: Bot, chat_id: int, message_id: int | None, text: str, markup=None) -> None:
    if not message_id:
        return
    try:
        await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except TelegramBadRequest:
        pass  # сообщение не изменилось или слишком старое


async def react(message: Message, emoji: str = "👍") -> None:
    try:
        await message.react([ReactionTypeEmoji(emoji=emoji)])
    except TelegramBadRequest:
        pass


# ---------------------------------------------------------------- опросы
async def send_poll(bot: Bot, db: DB, cfg: Config, user: dict, poll_id: int | None = None) -> None:
    """Отправляет опрос. Без poll_id создаёт новый; с poll_id — повтор после «спроси позже»."""
    uid = user["user_id"]
    if poll_id is None:
        for old in db.pending_polls(uid):
            db.update_poll(old["id"], status="missed", remind_at=None)
            await safe_edit(bot, user["chat_id"], old["message_id"], "Опрос пропущен.")
        poll_id = db.create_poll(uid)

    prev = db.previous_answered_poll(uid, poll_id)
    since = prev["sent_at"] if prev else user["created_at"]
    recorded = len(db.entries_between(uid, from_iso(since), now_utc()))
    since_txt = fmt_local(since, utz(user, cfg), True)
    if recorded:
        text = (f"С прошлого опроса ({since_txt}) записано приступов: {recorded}.\n"
                "<b>Был ли ещё приступ, который не записан?</b>")
    elif prev:
        text = f"<b>Болела ли голова с прошлого опроса ({since_txt})?</b>"
    else:
        text = f"<b>Болела ли голова с начала ведения дневника ({since_txt})?</b>"
    msg = await bot.send_message(user["chat_id"], text, reply_markup=kb.poll_kb(poll_id))
    db.update_poll(poll_id, message_id=msg.message_id, remind_at=None)


@router.callback_query(F.data.startswith("p:"))
async def on_poll_answer(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    _, pid, action = cb.data.split(":")
    poll = db.get_poll(int(pid))
    if not poll or poll["user_id"] != user["user_id"]:
        return await cb.answer("Опрос не найден")
    if poll["status"] != "pending":
        await cb.message.edit_reply_markup(reply_markup=None)
        return await cb.answer("На этот опрос уже ответили")
    await cb.answer()
    now = now_utc()
    if action == "no":
        db.update_poll(poll["id"], status="no", answered_at=to_iso(now), remind_at=None)
        await cb.message.edit_text(cb.message.html_text + "\n\nНет 🙂 Записала, хорошего дня!")
    elif action == "later":
        remind = now + timedelta(minutes=cfg.snooze_minutes)
        db.update_poll(poll["id"], remind_at=to_iso(remind))
        await cb.message.edit_text(
            cb.message.html_text + f"\n\n⏰ Хорошо, спрошу ещё раз в {fmt_local(to_iso(remind), utz(user, cfg))}."
        )
    elif action == "yes":
        db.update_poll(poll["id"], status="yes", answered_at=to_iso(now), remind_at=None)
        await cb.message.edit_text(cb.message.html_text + "\n\nДа 🤕")
        await start_entry(bot, db, qn, user, poll_id=poll["id"])


# ---------------------------------------------------------------- запись приступа
async def start_entry(bot: Bot, db: DB, qn: Questionnaire, user: dict, poll_id: int | None = None) -> None:
    uid, chat = user["user_id"], user["chat_id"]
    old = db.open_entry(uid)
    if old:
        db.close_entry(old["id"])
        await bot.send_message(chat, "Предыдущая незаконченная запись сохранена как есть.")
    entry_id = db.create_entry(uid, poll_id)
    intro = "Записываю приступ. Отвечай кнопками или просто напиши ответ."
    if poll_id:
        intro += "\nЕсли приступов было несколько — заполни про один, потом нажми «🤕 Записать приступ» для следующего."
    await bot.send_message(chat, intro, reply_markup=kb.MAIN_KB)
    await send_question(bot, db, qn, chat, db.get_entry(entry_id))


async def send_question(bot: Bot, db: DB, qn: Questionnaire, chat_id: int, entry: dict) -> None:
    step = entry["step"]
    if step >= len(qn):
        return await start_notes(bot, db, qn, chat_id, entry)
    q = qn.questions[step]
    selected = entry["answers"].get(q.id) if q.type == "multi" else None
    msg = await bot.send_message(
        chat_id,
        f"<b>{step + 1}/{len(qn)}.</b> {escape(q.text)}",
        reply_markup=kb.question_kb(entry["id"], step, q, selected if isinstance(selected, list) else None),
    )
    db.update_entry(entry["id"], q_message_id=msg.message_id, awaiting_other=None)


async def start_notes(bot: Bot, db: DB, qn: Questionnaire, chat_id: int, entry: dict) -> None:
    db.update_entry(entry["id"], stage="notes", awaiting_other=None, q_message_id=None)
    await bot.send_message(chat_id, escape(qn.final_text), reply_markup=kb.NOTES_KB)


async def finish_entry(bot: Bot, db: DB, qn: Questionnaire, cfg: Config, chat_id: int,
                       entry_id: int, auto: bool = False) -> None:
    entry = db.get_entry(entry_id)
    if not entry:
        return
    if entry["stage"] == "questions" and entry["q_message_id"]:
        await safe_edit(bot, chat_id, entry["q_message_id"],
                        f"{escape(qn.questions[min(entry['step'], len(qn) - 1)].text)}\n\n— не отвечено")
    db.close_entry(entry_id)
    head = ("Запись закрыта автоматически — давно не было новых сообщений. Сохранено ✅"
            if auto else "Запись сохранена ✅")
    await bot.send_message(
        chat_id,
        f"{head}\n\n{entry_summary(db.get_entry(entry_id), db, qn, cfg)}\n\n"
        "Если был ещё приступ — нажми «🤕 Записать приступ». Поправить запись: ⚙️ Настройки → 🗂 Последняя запись.",
        reply_markup=kb.MAIN_KB,
    )


def _question_for(cb: CallbackQuery, db: DB, qn: Questionnaire, user: dict) -> tuple[dict, int, Question] | None:
    parts = cb.data.split(":")
    try:
        entry = db.get_entry(int(parts[1]))
        step = int(parts[2])
        opt = int(parts[3]) if len(parts) > 3 else None
    except (IndexError, ValueError):
        return None
    if not entry or entry["user_id"] != user["user_id"] or not 0 <= step < len(qn):
        return None
    q = qn.questions[step]
    if opt is not None and not 0 <= opt < len(q.options):
        return None
    return entry, step, q


async def _answered(bot: Bot, db: DB, qn: Questionnaire, cb_or_chat, entry: dict, step: int,
                    q: Question, value: Any, chat_id: int) -> None:
    """Сохраняет ответ, «закрывает» сообщение с вопросом и переходит к следующему."""
    answers = entry["answers"]
    if value is None:
        answers.pop(q.id, None)
    else:
        answers[q.id] = value
    db.update_entry(entry["id"], answers=answers, awaiting_other=None)
    shown = "пропущено" if value is None else escape(q.answer_to_text(value))
    msg_id = entry["q_message_id"] if entry["step"] == step else None
    if isinstance(cb_or_chat, CallbackQuery):
        msg_id = cb_or_chat.message.message_id
    await safe_edit(bot, chat_id, msg_id, f"{escape(q.text)}\n\n✅ {shown}")
    if entry["stage"] == "questions" and entry["step"] == step:
        db.update_entry(entry["id"], step=step + 1)
        await send_question(bot, db, qn, chat_id, db.get_entry(entry["id"]))


@router.callback_query(F.data.startswith("a:"))
async def on_choice(cb: CallbackQuery, bot: Bot, db: DB, qn: Questionnaire, user: dict):
    found = _question_for(cb, db, qn, user)
    if not found:
        return await cb.answer("Этот вопрос устарел")
    entry, step, q = found
    opt = q.options[int(cb.data.split(":")[3])]
    value: Any = int(opt) if q.type == "scale" else opt
    await cb.answer()
    await _answered(bot, db, qn, cb, entry, step, q, value, cb.message.chat.id)


@router.callback_query(F.data.startswith("t:"))
async def on_toggle(cb: CallbackQuery, db: DB, qn: Questionnaire, user: dict):
    found = _question_for(cb, db, qn, user)
    if not found:
        return await cb.answer("Этот вопрос устарел")
    entry, step, q = found
    opt = q.options[int(cb.data.split(":")[3])]
    selected = entry["answers"].get(q.id)
    selected = list(selected) if isinstance(selected, list) else []
    selected.remove(opt) if opt in selected else selected.append(opt)
    entry["answers"][q.id] = selected
    db.update_entry(entry["id"], answers=entry["answers"])
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=kb.question_kb(entry["id"], step, q, selected))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("n:"))
async def on_multi_next(cb: CallbackQuery, bot: Bot, db: DB, qn: Questionnaire, user: dict):
    found = _question_for(cb, db, qn, user)
    if not found:
        return await cb.answer("Этот вопрос устарел")
    entry, step, q = found
    selected = entry["answers"].get(q.id) or None
    await cb.answer()
    await _answered(bot, db, qn, cb, entry, step, q, selected, cb.message.chat.id)


@router.callback_query(F.data.startswith("s:"))
async def on_skip(cb: CallbackQuery, bot: Bot, db: DB, qn: Questionnaire, user: dict):
    found = _question_for(cb, db, qn, user)
    if not found:
        return await cb.answer("Этот вопрос устарел")
    entry, step, q = found
    await cb.answer()
    await _answered(bot, db, qn, cb, entry, step, q, None, cb.message.chat.id)


@router.callback_query(F.data.startswith("o:"))
async def on_other(cb: CallbackQuery, db: DB, qn: Questionnaire, user: dict):
    found = _question_for(cb, db, qn, user)
    if not found:
        return await cb.answer("Этот вопрос устарел")
    entry, step, _q = found
    if entry["stage"] != "questions" or entry["step"] != step:
        return await cb.answer("Этот вопрос уже пройден")
    db.update_entry(entry["id"], awaiting_other=step)
    await cb.answer()
    await cb.message.answer("Напиши ответ текстом или запиши голосовое 🎙")


@router.callback_query(F.data.startswith("x:"))
async def on_cancel(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, user: dict):
    entry = db.get_entry(int(cb.data.split(":")[1]))
    if not entry or entry["user_id"] != user["user_id"] or entry["stage"] == "done":
        return await cb.answer("Запись уже закрыта")
    await cb.answer()
    for f in db.delete_entry(entry["id"]):
        Path(f).unlink(missing_ok=True)
    await cb.message.edit_text("Запись отменена.")
    await cb.message.answer("Хорошо, ничего не записала.", reply_markup=kb.MAIN_KB)
    if entry["poll_id"]:
        poll = db.get_poll(entry["poll_id"])
        if poll:
            db.update_poll(poll["id"], status="pending", answered_at=None)
            await send_poll(bot, db, cfg, user, poll_id=poll["id"])


@router.callback_query(F.data == "rec")
async def on_record_inline(cb: CallbackQuery, bot: Bot, db: DB, qn: Questionnaire, user: dict):
    await cb.answer()
    await start_entry(bot, db, qn, user)


# ---------------------------------------------------------------- голосовые
async def save_voice(message: Message, bot: Bot, cfg: Config, entry_id: int) -> Path:
    media = message.voice or message.audio or message.video_note
    ext = ".mp4" if message.video_note else ".ogg"
    path = cfg.voice_dir / f"{message.from_user.id}_{entry_id}_{message.message_id}{ext}"
    await bot.download(media, destination=path)
    return path


async def transcribe_note(transcriber: Transcriber, db: DB, note_id: int, path: Path) -> str | None:
    try:
        text = await transcriber.transcribe(path)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось расшифровать %s", path)
        db.update_note(note_id, status="failed")
        return None
    db.update_note(note_id, text=text or "(пусто)", status="ok")
    return text


_queue_running = False


async def process_queued_voice(bot: Bot, db: DB, transcriber: Transcriber) -> None:
    """Расшифровывает голосовые, отложенные до готовности модели, и присылает текст."""
    global _queue_running
    if _queue_running or not transcriber.ready:
        return
    _queue_running = True
    try:
        for note in db.queued_voice_notes():
            path = Path(note["file_path"] or "")
            if not path.is_file():
                db.update_note(note["id"], status="failed")
                continue
            text = await transcribe_note(transcriber, db, note["id"], path)
            entry = db.get_entry(note["entry_id"])
            user = db.get_user(entry["user_id"]) if entry else None
            if not user:
                continue
            body = (f"🎙 Расшифровала голосовое: «{escape(text) or '…'}»" if text is not None
                    else "⚠️ Не получилось расшифровать голосовое, но оно сохранено.")
            try:
                await bot.edit_message_text(text=body, chat_id=user["chat_id"],
                                            message_id=note["status_msg_id"])
            except Exception:  # noqa: BLE001 — сообщение удалено/старое: шлём новое
                await bot.send_message(user["chat_id"], body)
    finally:
        _queue_running = False


@router.message(F.voice | F.audio | F.video_note)
async def on_voice(message: Message, bot: Bot, db: DB, cfg: Config, qn: Questionnaire,
                   transcriber: Transcriber, user: dict):
    entry = db.open_entry(user["user_id"])
    if not entry:
        return await message.answer(
            "Сейчас нет открытой записи. Чтобы записать приступ — нажми кнопку ниже, "
            "а потом можно будет надиктовать голосом.",
            reply_markup=kb.start_record_kb(),
        )
    path = await save_voice(message, bot, cfg, entry["id"])
    if not transcriber.enabled:
        db.add_note(entry["id"], "voice", file_path=str(path), status="ok")
        if entry["stage"] == "questions":
            return await message.answer("Голосовое сохранено в заметки, но расшифровка отключена — "
                                        "на вопрос ответь кнопкой или текстом.")
        return await react(message)

    if not transcriber.ready:
        # Модель ещё скачивается — не держим чат, расшифруем позже (process_queued_voice)
        note_id = db.add_note(entry["id"], "voice", file_path=str(path), status="queued")
        hint = "\nНа вопрос пока ответь кнопкой или текстом." if entry["stage"] == "questions" else ""
        status = await message.reply(
            "🎙 Голосовое сохранено. Модель для расшифровки ещё готовится — "
            f"пришлю текст сюда, как только она будет готова.{hint}"
        )
        db.update_note(note_id, status_msg_id=status.message_id)
        return

    note_id = db.add_note(entry["id"], "voice", file_path=str(path), status="pending")
    status = await message.reply("🎙 Расшифровываю…")
    text = await transcribe_note(transcriber, db, note_id, path)
    if text is None:
        await safe_edit(bot, message.chat.id, status.message_id,
                        "⚠️ Не получилось расшифровать, но голосовое сохранено.")
        return
    await safe_edit(bot, message.chat.id, status.message_id, f"🎙 «{escape(text) or '…'}»")

    entry = db.get_entry(entry["id"])
    if entry["stage"] == "questions" and text:
        # Голосовое на этапе вопросов — это ответ на текущий вопрос (в заметках оно тоже остаётся)
        await answer_with_text(bot, db, qn, entry, text, message.chat.id)


# ---------------------------------------------------------------- часовой пояс
async def ask_timezone(message: Message) -> None:
    await message.answer(
        "🌍 <b>Какой у тебя часовой пояс?</b>\n"
        "Он нужен, чтобы опросы приходили в правильное время.\n\n"
        "Проще всего — отправить геопозицию кнопкой ниже: я определю по ней только часовой пояс, "
        "сами координаты не сохраняются. Или выбери город из списка.",
        reply_markup=kb.TZ_REQUEST_KB,
    )


async def set_timezone(message: Message, db: DB, cfg: Config, user: dict, tz_name: str) -> None:
    uid = user["user_id"]
    db.update_user(uid, tz=tz_name, awaiting=None)
    fresh = db.get_user(uid)
    db.update_user(uid, next_poll_at=to_iso(compute_next(fresh, cfg.tz, now_utc())))
    fresh = db.get_user(uid)
    tz = utz(fresh, cfg)
    await message.answer(
        f"Готово ✅ Часовой пояс: {tz_label(tz)}.\n"
        f"Сейчас у тебя {now_utc().astimezone(tz):%H:%M}.\n"
        f"Опрос: {describe_schedule(fresh)}, следующий — {fmt_local(fresh['next_poll_at'], tz, True)}.\n\n"
        "Поменять можно в «⚙️ Настройки».",
        reply_markup=kb.MAIN_KB,
    )


@router.message(F.location)
async def on_location(message: Message, db: DB, cfg: Config, user: dict):
    loc = message.location
    try:
        name = tz_from_location(loc.latitude, loc.longitude)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось определить пояс по геопозиции")
        name = None
    if not name:
        return await message.answer("Не получилось определить пояс по геопозиции, выбери город:",
                                    reply_markup=kb.tz_list_kb())
    await set_timezone(message, db, cfg, user, name)


@router.message(kb.is_btn(kb.BTN_TZ_LIST))
async def on_tz_list(message: Message):
    await message.answer("Выбери город с тем же временем, что у тебя:", reply_markup=kb.tz_list_kb())


@router.message(kb.is_btn(kb.BTN_TZ_LATER))
async def on_tz_later(message: Message, cfg: Config, user: dict):
    await message.answer(
        f"Хорошо, пока буду считать время по поясу {tz_label(utz(user, cfg))}. "
        "Указать свой можно в «⚙️ Настройки» → «🌍 Часовой пояс».",
        reply_markup=kb.MAIN_KB,
    )


@router.callback_query(F.data.startswith("tz:"))
async def on_tz_choice(cb: CallbackQuery, db: DB, cfg: Config, user: dict):
    name = cb.data[3:]
    if name != "other" and name not in {n for _, n in TZ_CHOICES}:
        return await cb.answer("Неизвестный пояс")
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    if name == "other":
        db.update_user(user["user_id"], awaiting="tz_time")
        return await cb.message.answer(
            "Напиши, сколько у тебя сейчас времени, например <code>14:35</code> — "
            "я сама вычислю часовой пояс."
        )
    await set_timezone(cb.message, db, cfg, user, name)


# ---------------------------------------------------------------- команды и меню
@router.message(CommandStart())
async def cmd_start(message: Message, cfg: Config, user: dict):
    text = (
        "Привет! Я веду дневник головной боли 💜\n\n"
        f"• Буду регулярно спрашивать, болела ли голова (сейчас — {describe_schedule(user)}).\n"
        "• Если болит прямо сейчас — нажми «🤕 Записать приступ», не дожидаясь опроса.\n"
        "• На вопросы отвечай кнопками, а в конце можно написать или надиктовать всё, что важно.\n"
        "• «📤 Выгрузка» — файл с дневником за неделю, месяц или всё время, его можно отдать врачу "
        "или нейросети для анализа.\n"
        "• «⚙️ Настройки» — частота и время опросов, часовой пояс."
    )
    if not user.get("tz"):
        await message.answer(text)
        return await ask_timezone(message)
    await message.answer(
        text + f"\n\nСледующий опрос: {fmt_local(user['next_poll_at'], utz(user, cfg), True)}.",
        reply_markup=kb.MAIN_KB,
    )


@router.message(Command("help"))
async def cmd_help(message: Message, cfg: Config, user: dict):
    await cmd_start(message, cfg, user)


@router.message(Command("record"))
@router.message(kb.is_btn(kb.BTN_RECORD))
async def on_record(message: Message, bot: Bot, db: DB, qn: Questionnaire, user: dict):
    await start_entry(bot, db, qn, user)


@router.message(Command("export"))
@router.message(kb.is_btn(kb.BTN_EXPORT))
async def on_export_menu(message: Message):
    await message.answer("За какой период выгрузить дневник?", reply_markup=kb.export_kb())


@router.callback_query(F.data.startswith("e:") | F.data.startswith("ec:"))
async def on_export(cb: CallbackQuery, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    kind, period = cb.data.split(":")
    await cb.answer("Готовлю файл…")
    builder = build_csv if kind == "ec" else build_markdown
    filename, data = builder(db, qn, user["user_id"], period, utz(user, cfg))
    caption = ("Таблица для Excel / Google Sheets." if kind == "ec" else
               "Дневник готов. Можно отправить этот файл нейросети с просьбой: "
               "«Проанализируй дневник головной боли: частота, сила, возможные триггеры, "
               "связь с циклом, эффективность обезболивающих. Что стоит обсудить с неврологом?»")
    await cb.message.answer_document(BufferedInputFile(data, filename=filename), caption=caption)


@router.message(kb.is_btn(kb.BTN_DONE))
async def on_done(message: Message, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    entry = db.open_entry(user["user_id"])
    if not entry:
        return await message.answer("Открытой записи нет.", reply_markup=kb.MAIN_KB)
    await finish_entry(bot, db, qn, cfg, message.chat.id, entry["id"])


async def answer_with_text(bot: Bot, db: DB, qn: Questionnaire, entry: dict, text: str, chat_id: int) -> None:
    step = entry["step"]
    q = qn.questions[step]
    value: Any = text
    if q.type == "scale" and text.strip().isdigit() and text.strip() in q.options:
        value = int(text.strip())
    elif q.type == "multi":
        selected = entry["answers"].get(q.id)
        value = (list(selected) if isinstance(selected, list) else []) + [text]
    await _answered(bot, db, qn, None, entry, step, q, value, chat_id)


@fallback_router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    if user.get("awaiting") == "tz_time":
        name = tz_from_local_time(message.text, now_utc())
        if name:
            return await set_timezone(message, db, cfg, user, name)
        return await message.answer("Не поняла время 🤔 Напиши в формате <code>14:35</code>.")
    entry = db.open_entry(user["user_id"])
    if not entry:
        return await message.answer(
            "Сейчас нет открытой записи. Если болит голова — нажми «🤕 Записать приступ».",
            reply_markup=kb.MAIN_KB,
        )
    if entry["stage"] == "questions":
        return await answer_with_text(bot, db, qn, entry, message.text, message.chat.id)
    db.add_note(entry["id"], "text", text=message.text)
    await react(message)


# ---------------------------------------------------------------- настройки
def describe_schedule(user: dict) -> str:
    freq = kb.FREQ_OPTIONS.get(user["interval_days"], f"раз в {user['interval_days']} дн.")
    return f"{freq.lower()} в {user['poll_time']}"


def settings_text(user: dict, cfg: Config) -> str:
    tz = utz(user, cfg)
    status = "⏸ на паузе" if user["paused"] else f"следующий — {fmt_local(user['next_poll_at'], tz, True)}"
    tz_text = tz_label(tz) + ("" if user.get("tz") else " — по умолчанию, лучше указать свой")
    return (
        "<b>⚙️ Настройки</b>\n\n"
        f"Опрос: {describe_schedule(user)}\n"
        f"Статус: {status}\n"
        f"Часовой пояс: {tz_text}"
    )


@router.message(Command("settings"))
@router.message(kb.is_btn(kb.BTN_SETTINGS))
async def on_settings(message: Message, cfg: Config, user: dict):
    await message.answer(settings_text(user, cfg), reply_markup=kb.settings_kb(bool(user["paused"])))


@router.callback_query(F.data.startswith("set:"))
async def on_settings_cb(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    parts = cb.data.split(":")
    action = parts[1]
    uid = user["user_id"]
    await cb.answer()

    def reschedule() -> dict:
        fresh = db.get_user(uid)
        db.update_user(uid, next_poll_at=to_iso(compute_next(fresh, cfg.tz, now_utc())))
        return db.get_user(uid)

    if action == "tz":
        return await ask_timezone(cb.message)
    if action == "freq":
        return await cb.message.edit_text("Как часто спрашивать?", reply_markup=kb.freq_kb())
    if action == "time":
        return await cb.message.edit_text("Во сколько спрашивать?", reply_markup=kb.time_kb())
    if action == "int":
        # Принимаем только значения с кнопок — данные callback'а может подделать клиент
        if len(parts) != 3 or not parts[2].isdigit() or int(parts[2]) not in kb.FREQ_OPTIONS:
            return
        db.update_user(uid, interval_days=int(parts[2]))
        user = reschedule()
    elif action == "t":
        if len(parts) != 4 or not is_valid_time(f"{parts[2]}:{parts[3]}"):
            return
        db.update_user(uid, poll_time=f"{parts[2]}:{parts[3]}")
        user = reschedule()
    elif action == "pause":
        db.update_user(uid, paused=0 if user["paused"] else 1)
        user = reschedule()
    elif action == "now":
        await send_poll(bot, db, cfg, user)
        return
    elif action == "last":
        entry = db.last_entry(uid)
        if not entry:
            return await cb.message.answer("Записей о приступах пока нет.")
        return await cb.message.answer(
            entry_summary(entry, db, qn, cfg)
            + "\n\nЕсли запись ошибочная — удали её и запиши заново.",
            reply_markup=kb.delete_kb(entry["id"]),
        )
    await cb.message.edit_text(settings_text(user, cfg), reply_markup=kb.settings_kb(bool(user["paused"])))


@router.callback_query(F.data.startswith("del"))
async def on_delete(cb: CallbackQuery, db: DB, user: dict):
    if cb.data == "delno":
        await cb.answer("Оставила как есть")
        return await cb.message.edit_reply_markup(reply_markup=None)
    action, eid = cb.data.split(":")
    entry = db.get_entry(int(eid))
    if not entry or entry["user_id"] != user["user_id"]:
        await cb.answer("Запись не найдена")
        return await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    if action == "del":
        return await cb.message.edit_reply_markup(reply_markup=kb.delete_kb(entry["id"], confirm=True))
    for f in db.delete_entry(entry["id"]):
        Path(f).unlink(missing_ok=True)
    await cb.message.edit_text("🗑 Запись удалена.")
