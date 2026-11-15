"""Диалог с пользователем.

Интерфейс — одно сообщение-«панель» с inline-кнопками. Переходы между разделами
редактируют панель, а не присылают новые сообщения; сообщения пользователя
(ответы, заметки, голосовые) удаляются после сохранения. В чате остаются только
панель и файлы выгрузки. Из любого раздела можно вернуться назад и в главное меню.
"""
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
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
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
# чтобы он не перехватывал команды и кнопки
fallback_router = Router()

Screen = tuple[str, InlineKeyboardMarkup]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


# ================================================================ доступ
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


# ================================================================ вспомогательное
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
    wd = WEEKDAYS[dt.weekday()] + " " if with_weekday else ""
    return f"{wd}{dt:%d.%m %H:%M}"


def trunc(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_schedule(user: dict) -> str:
    freq = kb.FREQ_OPTIONS.get(user["interval_days"], f"раз в {user['interval_days']} дн.")
    return f"{freq.lower()} в {user['poll_time']}"


def answers_lines(entry: dict, qn: Questionnaire, upto: int | None = None) -> list[str]:
    lines = []
    for i, q in enumerate(qn.questions):
        if upto is not None and i >= upto:
            break
        if q.id in entry["answers"]:
            lines.append(f"✓ {escape(q.label)}: {escape(q.answer_to_text(entry['answers'][q.id]))}")
    return lines


def entry_summary(entry: dict, db: DB, qn: Questionnaire, cfg: Config) -> str:
    tz = utz(db.get_user(entry["user_id"]), cfg)
    lines = [f"<b>🤕 Приступ, запись от {fmt_local(entry['created_at'], tz, True)}</b>"]
    for q in qn.questions:
        lines.append(f"• {escape(q.label)}: {escape(q.answer_to_text(entry['answers'].get(q.id)))}")
    notes = db.notes_for(entry["id"])
    voices = sum(1 for n in notes if n["kind"] == "voice")
    if notes:
        lines.append(f"• {escape(qn.final_label)}: {len(notes)} сообщ."
                     + (f" (голосовых: {voices})" if voices else ""))
    return "\n".join(lines)


# ================================================================ панель
async def drop(bot: Bot, chat_id: int, message_id: int | None) -> None:
    """Удаляет сообщение бота; если нельзя (старше 48 ч) — хотя бы убирает кнопки."""
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except TelegramBadRequest:
            pass


async def delete_incoming(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def show(bot: Bot, db: DB, uid: int, screen: Screen, *, new: bool = False,
               via: int | None = None) -> None:
    """Показывает экран в панели.

    По умолчанию редактирует текущую панель (или сообщение `via`, на кнопку которого
    нажали). С new=True присылает панель заново внизу чата — для опросов (нужно
    уведомление) и после файлов выгрузки. Старая панель удаляется.
    """
    text, markup = screen
    user = db.get_user(uid)
    chat = user["chat_id"]
    if not user["kb_removed"]:
        # Убираем reply-клавиатуру прошлой версии бота
        try:
            tmp = await bot.send_message(chat, "Обновляю меню…", reply_markup=ReplyKeyboardRemove())
            await drop(bot, chat, tmp.message_id)
        except TelegramBadRequest:
            pass
        db.update_user(uid, kb_removed=1)
    panel = user["panel_msg_id"]
    target = via or panel
    if target and not new:
        try:
            await bot.edit_message_text(text=text, chat_id=chat, message_id=target, reply_markup=markup)
            edited = True
        except TelegramBadRequest as exc:
            edited = "not modified" in str(exc)
        if edited:
            if panel and panel != target:
                await drop(bot, chat, panel)
            db.update_user(uid, panel_msg_id=target)
            return
    msg = await bot.send_message(chat, text, reply_markup=markup)
    db.update_user(uid, panel_msg_id=msg.message_id)
    for old in {panel, via} - {None, msg.message_id}:
        await drop(bot, chat, old)


# ================================================================ экраны
def main_screen(db: DB, cfg: Config, user: dict, note: str = "") -> Screen:
    user = db.get_user(user["user_id"])
    tz = utz(user, cfg)
    lines = [note, ""] if note else []
    lines.append("💜 <b>Дневник головной боли</b>")
    if user["paused"]:
        lines.append("Опросы на паузе ⏸")
    else:
        lines.append(f"Опрос {describe_schedule(user)}, следующий — {fmt_local(user['next_poll_at'], tz, True)}")
    month = db.entries_between(user["user_id"], now_utc() - timedelta(days=30), now_utc())
    lines.append(f"Приступов за 30 дней: {len(month)}")
    open_entry = db.open_entry(user["user_id"])
    if open_entry:
        lines.append("\n✏️ Есть незаконченная запись — её можно продолжить.")
    if not user.get("tz"):
        lines.append("\n🌍 Укажи часовой пояс, чтобы опросы приходили в нужное время.")
    return "\n".join(lines), kb.main_kb(bool(open_entry), not user.get("tz"))


def help_screen(user: dict) -> Screen:
    return (
        "❓ <b>Как пользоваться</b>\n\n"
        f"• Я регулярно спрашиваю, болела ли голова (сейчас — {describe_schedule(user)}).\n"
        "• Если болит прямо сейчас — «🤕 Записать приступ», не дожидаясь опроса.\n"
        "• На вопросы отвечай кнопками или просто напиши ответ. В конце можно написать "
        "или надиктовать голосом всё, что важно, — сколько угодно сообщений.\n"
        "• Твои сообщения я сохраняю в дневник и убираю из чата, чтобы не копился мусор.\n"
        "• «📤 Выгрузка» — файл с дневником, его можно отдать врачу или нейросети.\n"
        "• «⚙️ Настройки» — частота и время опросов, часовой пояс, пауза.\n"
        "• Кнопка «⬅️ Назад» возвращает на шаг назад, «🏠 Меню» — в главное меню.",
        kb.help_kb(),
    )


def export_screen() -> Screen:
    return "📤 <b>Выгрузка дневника</b>\n\nЗа какой период?", kb.export_kb()


def settings_screen(cfg: Config, user: dict, note: str = "") -> Screen:
    tz = utz(user, cfg)
    status = ("⏸ на паузе" if user["paused"]
              else f"следующий — {fmt_local(user['next_poll_at'], tz, True)}")
    tz_text = tz_label(tz) + ("" if user.get("tz") else " — по умолчанию, лучше указать свой")
    head = f"{note}\n\n" if note else ""
    return (
        f"{head}<b>⚙️ Настройки</b>\n\n"
        f"Опрос: {describe_schedule(user)}\n"
        f"Статус: {status}\n"
        f"Часовой пояс: {tz_text}",
        kb.settings_kb(bool(user["paused"])),
    )


def tz_screen(user: dict, welcome: bool = False) -> Screen:
    text = ""
    if welcome:
        text = ("Привет! Я веду дневник головной боли 💜\n"
                "Для начала — один вопрос, чтобы опросы приходили в правильное время.\n\n")
    text += ("🌍 <b>Какой у тебя часовой пояс?</b>\n\n"
             "Выбери город с тем же временем, что у тебя.\n"
             "Или отправь геопозицию: 📎 → «Геопозиция» — я определю по ней только пояс, "
             "координаты не сохраняются.")
    return text, kb.tz_kb("settings" if user.get("tz") else "main")


def tz_time_screen(note: str = "") -> Screen:
    head = f"{note}\n\n" if note else ""
    return (f"{head}🕐 Напиши, сколько у тебя сейчас времени, например <code>14:35</code> — "
            "я сама вычислю часовой пояс.", kb.tz_time_kb())


def last_entry_screen(db: DB, qn: Questionnaire, cfg: Config, user: dict, confirm: bool = False) -> Screen:
    entry = db.last_entry(user["user_id"])
    if not entry:
        return "🗂 Записей о приступах пока нет.", kb.last_entry_kb(None)
    tail = ("\n\n<b>Удалить эту запись?</b> Это нельзя отменить." if confirm
            else "\n\nЕсли запись ошибочная — удали её и запиши заново.")
    return entry_summary(entry, db, qn, cfg) + tail, kb.last_entry_kb(entry["id"], confirm)


def poll_screen(db: DB, cfg: Config, user: dict, poll_id: int) -> Screen:
    uid = user["user_id"]
    prev = db.previous_answered_poll(uid, poll_id)
    since = prev["sent_at"] if prev else user["created_at"]
    recorded = len(db.entries_between(uid, from_iso(since), now_utc()))
    since_txt = fmt_local(since, utz(user, cfg), True)
    if recorded:
        text = (f"🔔 С прошлого опроса ({since_txt}) записано приступов: {recorded}.\n"
                "<b>Был ли ещё приступ, который не записан?</b>")
    elif prev:
        text = f"🔔 <b>Болела ли голова с прошлого опроса ({since_txt})?</b>"
    else:
        text = f"🔔 <b>Болела ли голова с начала ведения дневника ({since_txt})?</b>"
    return text, kb.poll_kb(poll_id)


def entry_screen(db: DB, qn: Questionnaire, entry: dict, extra: str = "") -> Screen:
    entry = db.get_entry(entry["id"])
    tail = f"\n\n{extra}" if extra else ""
    if entry["stage"] == "questions" and entry["step"] < len(qn):
        step = entry["step"]
        q = qn.questions[step]
        lines = [f"🤕 <b>Запись приступа</b> · вопрос {step + 1} из {len(qn)}"]
        done = answers_lines(entry, qn, upto=step)
        if done:
            lines += ["", *done]
        lines += ["", f"<b>{escape(q.text)}</b>"]
        selected = entry["answers"].get(q.id) if q.type == "multi" else None
        awaiting = entry["awaiting_other"] == step
        if awaiting:
            lines.append("\n✍️ Напиши свой ответ текстом или запиши голосовое.")
        return "\n".join(lines) + tail, kb.question_kb(
            entry["id"], step, q, selected if isinstance(selected, list) else None, awaiting)

    lines = ["🤕 <b>Запись приступа</b> · заметки"]
    done = answers_lines(entry, qn)
    if done:
        lines += ["", *done]
    lines += ["", escape(qn.final_text)]
    notes = db.notes_for(entry["id"])
    if notes:
        lines += ["", f"<b>Сохранено ({len(notes)}):</b>"]
        shown = notes[-8:]
        if len(notes) > len(shown):
            lines.append(f"… и ещё {len(notes) - len(shown)}")
        for n in shown:
            if n["kind"] == "voice":
                body = {
                    "pending": "расшифровываю…",
                    "queued": "сохранено, расшифрую позже",
                    "failed": "не удалось расшифровать, голосовое сохранено",
                }.get(n["status"]) or trunc(n["text"] or "", 150)
                lines.append(f"🎙 {escape(body)}")
            else:
                lines.append(f"📝 {escape(trunc(n['text'] or '', 150))}")
    return "\n".join(lines) + tail, kb.notes_kb(entry["id"])


# ================================================================ навигация
@router.callback_query(F.data.startswith("go:"))
async def on_nav(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    where = cb.data[3:]
    await cb.answer()
    uid = user["user_id"]
    if user.get("awaiting"):
        db.update_user(uid, awaiting=None)
        user = db.get_user(uid)
    screens: dict[str, Callable[[], Screen]] = {
        "main": lambda: main_screen(db, cfg, user),
        "help": lambda: help_screen(user),
        "export": export_screen,
        "settings": lambda: settings_screen(cfg, user),
        "freq": lambda: ("🔁 <b>Как часто спрашивать?</b>", kb.freq_kb()),
        "time": lambda: ("🕘 <b>Во сколько спрашивать?</b>", kb.time_kb()),
        "tz": lambda: tz_screen(user),
        "last": lambda: last_entry_screen(db, qn, cfg, user),
    }
    if where == "entry":
        entry = db.open_entry(uid)
        screen = entry_screen(db, qn, entry) if entry else main_screen(db, cfg, user)
    elif where in screens:
        screen = screens[where]()
    else:
        screen = main_screen(db, cfg, user)
    await show(bot, db, uid, screen, via=cb.message.message_id)


async def open_main(message: Message, bot: Bot, db: DB, cfg: Config, user: dict, note: str = "") -> None:
    await delete_incoming(message)
    await show(bot, db, user["user_id"], main_screen(db, cfg, user, note), new=True)


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, db: DB, cfg: Config, user: dict):
    await delete_incoming(message)
    if not user.get("tz"):
        return await show(bot, db, user["user_id"], tz_screen(user, welcome=True), new=True)
    await show(bot, db, user["user_id"], main_screen(db, cfg, user), new=True)


@router.message(Command("menu", "help"))
async def cmd_menu(message: Message, bot: Bot, db: DB, cfg: Config, user: dict):
    await open_main(message, bot, db, cfg, user)


@router.message(Command("settings"))
async def cmd_settings(message: Message, bot: Bot, db: DB, cfg: Config, user: dict):
    await delete_incoming(message)
    await show(bot, db, user["user_id"], settings_screen(cfg, user), new=True)


@router.message(Command("export"))
async def cmd_export(message: Message, bot: Bot, db: DB, user: dict):
    await delete_incoming(message)
    await show(bot, db, user["user_id"], export_screen(), new=True)


@router.message(Command("record"))
async def cmd_record(message: Message, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    await delete_incoming(message)
    await start_entry(bot, db, qn, cfg, user, new=True)


# ================================================================ опросы
async def send_poll(bot: Bot, db: DB, cfg: Config, user: dict, poll_id: int | None = None,
                    via: int | None = None) -> None:
    """Показывает опрос. Без poll_id создаёт новый; с poll_id — повтор («спроси позже»/отмена).
    Без via присылает новым сообщением внизу чата, чтобы пришло уведомление."""
    uid = user["user_id"]
    if poll_id is None:
        for old in db.pending_polls(uid):
            db.update_poll(old["id"], status="missed", remind_at=None)
        poll_id = db.create_poll(uid)
    await show(bot, db, uid, poll_screen(db, cfg, db.get_user(uid), poll_id), new=via is None, via=via)
    db.update_poll(poll_id, message_id=db.get_user(uid)["panel_msg_id"], remind_at=None)


@router.callback_query(F.data.startswith("p:"))
async def on_poll_answer(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    try:
        _, pid, action = cb.data.split(":")
        poll = db.get_poll(int(pid))
    except ValueError:
        return await cb.answer()
    uid, via = user["user_id"], cb.message.message_id
    if not poll or poll["user_id"] != uid:
        return await cb.answer("Опрос не найден")
    if poll["status"] != "pending":
        await cb.answer("На этот опрос уже ответили")
        return await show(bot, db, uid, main_screen(db, cfg, user), via=via)
    await cb.answer()
    now = now_utc()
    if action == "no":
        db.update_poll(poll["id"], status="no", answered_at=to_iso(now), remind_at=None)
        await show(bot, db, uid, main_screen(db, cfg, user, "🙂 Записала: голова не болела. Хорошего дня!"),
                   via=via)
    elif action == "later":
        remind = now + timedelta(minutes=cfg.snooze_minutes)
        db.update_poll(poll["id"], remind_at=to_iso(remind))
        await show(bot, db, uid, main_screen(
            db, cfg, user, f"⏰ Хорошо, спрошу ещё раз в {fmt_local(to_iso(remind), utz(user, cfg))}."), via=via)
    elif action == "yes":
        db.update_poll(poll["id"], status="yes", answered_at=to_iso(now), remind_at=None)
        await start_entry(bot, db, qn, cfg, user, poll_id=poll["id"], via=via)


# ================================================================ запись приступа
async def start_entry(bot: Bot, db: DB, qn: Questionnaire, cfg: Config, user: dict,
                      poll_id: int | None = None, via: int | None = None, new: bool = False,
                      extra: str = "") -> int:
    uid = user["user_id"]
    old = db.open_entry(uid)
    if old:
        db.close_entry(old["id"])
        extra = (extra + "\n" if extra else "") + "Предыдущая незаконченная запись сохранена как есть."
    entry_id = db.create_entry(uid, poll_id)
    await show(bot, db, uid, entry_screen(db, qn, db.get_entry(entry_id), extra), via=via, new=new)
    return entry_id


async def finish_entry(bot: Bot, db: DB, qn: Questionnaire, cfg: Config, uid: int, entry_id: int,
                       auto: bool = False, via: int | None = None) -> None:
    entry = db.get_entry(entry_id)
    if not entry:
        return
    db.close_entry(entry_id)
    head = ("💾 Запись закрыта автоматически — давно не было новых сообщений. Сохранено ✅"
            if auto else "✅ Запись сохранена")
    note = f"{head}\n\n{entry_summary(db.get_entry(entry_id), db, qn, cfg)}"
    await show(bot, db, uid, main_screen(db, cfg, db.get_user(uid), note), via=via)


def _question_for(cb: CallbackQuery, db: DB, qn: Questionnaire, user: dict) -> tuple[dict, int, Question] | None:
    parts = cb.data.split(":")
    try:
        entry = db.get_entry(int(parts[1]))
        step = int(parts[2])
        opt = int(parts[3]) if len(parts) > 3 else None
    except (IndexError, ValueError):
        return None
    if not entry or entry["user_id"] != user["user_id"] or entry["stage"] != "questions":
        return None
    if step != entry["step"] or not 0 <= step < len(qn):
        return None
    q = qn.questions[step]
    if opt is not None and not 0 <= opt < len(q.options):
        return None
    return entry, step, q


def _entry_for(cb: CallbackQuery, db: DB, user: dict) -> dict | None:
    try:
        entry = db.get_entry(int(cb.data.split(":")[1]))
    except (IndexError, ValueError):
        return None
    if not entry or entry["user_id"] != user["user_id"] or entry["stage"] == "done":
        return None
    return entry


def save_answer(db: DB, qn: Questionnaire, entry: dict, q: Question, value: Any) -> None:
    """Сохраняет ответ на текущий вопрос и переходит к следующему (или к заметкам)."""
    answers = entry["answers"]
    if value is None:
        answers.pop(q.id, None)
    else:
        answers[q.id] = value
    nxt = entry["step"] + 1
    db.update_entry(entry["id"], answers=answers, awaiting_other=None, step=nxt,
                    stage="questions" if nxt < len(qn) else "notes")


_NUMBER_WORDS = {
    "ноль": 0, "нол": 0, "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3,
    "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
}


def parse_scale(text: str, q: Question) -> int | None:
    """Число для вопроса-шкалы из ответа «7», «где-то семь из десяти», «на 8»."""
    import re

    allowed = {int(o) for o in q.options}
    for token in re.findall(r"\d+|[а-яё]+", text.lower()):
        value = int(token) if token.isdigit() else _NUMBER_WORDS.get(token)
        if value in allowed:
            return value
    return None


def text_answer_value(entry: dict, q: Question, text: str) -> Any:
    if q.type == "scale":
        value = parse_scale(text, q)
        return value if value is not None else text
    if q.type == "multi":
        selected = entry["answers"].get(q.id)
        return (list(selected) if isinstance(selected, list) else []) + [text]
    return text


async def stale(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict) -> None:
    """Нажата кнопка устаревшего экрана — показываем актуальный."""
    await cb.answer("Этот экран устарел")
    entry = db.open_entry(user["user_id"])
    screen = entry_screen(db, qn, entry) if entry else main_screen(db, cfg, user)
    await show(bot, db, user["user_id"], screen, via=cb.message.message_id)


@router.callback_query(F.data == "rec")
async def on_record(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    await cb.answer()
    await start_entry(bot, db, qn, cfg, user, via=cb.message.message_id)


@router.callback_query(F.data.startswith("a:") | F.data.startswith("n:") | F.data.startswith("s:"))
async def on_answer(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    found = _question_for(cb, db, qn, user)
    if not found:
        return await stale(cb, bot, db, cfg, qn, user)
    entry, _step, q = found
    kind = cb.data[0]
    if kind == "a":
        opt = q.options[int(cb.data.split(":")[3])]
        value: Any = int(opt) if q.type == "scale" else opt
    elif kind == "n":
        value = entry["answers"].get(q.id) or None
    else:
        value = None
    await cb.answer()
    save_answer(db, qn, entry, q, value)
    await show(bot, db, user["user_id"], entry_screen(db, qn, entry), via=cb.message.message_id)


@router.callback_query(F.data.startswith("t:"))
async def on_toggle(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    found = _question_for(cb, db, qn, user)
    if not found:
        return await stale(cb, bot, db, cfg, qn, user)
    entry, _step, q = found
    opt = q.options[int(cb.data.split(":")[3])]
    selected = entry["answers"].get(q.id)
    selected = list(selected) if isinstance(selected, list) else []
    selected.remove(opt) if opt in selected else selected.append(opt)
    entry["answers"][q.id] = selected
    db.update_entry(entry["id"], answers=entry["answers"])
    await cb.answer()
    await show(bot, db, user["user_id"], entry_screen(db, qn, entry), via=cb.message.message_id)


@router.callback_query(F.data.startswith("o:"))
async def on_other(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    found = _question_for(cb, db, qn, user)
    if not found:
        return await stale(cb, bot, db, cfg, qn, user)
    entry, step, _q = found
    db.update_entry(entry["id"], awaiting_other=step)
    await cb.answer()
    await show(bot, db, user["user_id"], entry_screen(db, qn, entry), via=cb.message.message_id)


@router.callback_query(F.data.startswith("ob:"))
async def on_other_back(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    entry = _entry_for(cb, db, user)
    if not entry:
        return await stale(cb, bot, db, cfg, qn, user)
    db.update_entry(entry["id"], awaiting_other=None)
    await cb.answer()
    await show(bot, db, user["user_id"], entry_screen(db, qn, entry), via=cb.message.message_id)


@router.callback_query(F.data.startswith("qb:"))
async def on_entry_back(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    """«Назад» внутри записи: к предыдущему вопросу; с первого вопроса — отмена записи."""
    entry = _entry_for(cb, db, user)
    if not entry:
        return await stale(cb, bot, db, cfg, qn, user)
    await cb.answer()
    if entry["stage"] == "notes":
        db.update_entry(entry["id"], stage="questions", step=max(0, len(qn) - 1), awaiting_other=None)
    elif entry["awaiting_other"] is not None:
        db.update_entry(entry["id"], awaiting_other=None)
    elif entry["step"] > 0:
        db.update_entry(entry["id"], step=entry["step"] - 1)
    else:
        return await cancel_entry(bot, db, cfg, user, entry, cb.message.message_id)
    await show(bot, db, user["user_id"], entry_screen(db, qn, entry), via=cb.message.message_id)


async def cancel_entry(bot: Bot, db: DB, cfg: Config, user: dict, entry: dict, via: int) -> None:
    for f in db.delete_entry(entry["id"]):
        Path(f).unlink(missing_ok=True)
    poll = db.get_poll(entry["poll_id"]) if entry["poll_id"] else None
    if poll:
        # Запись начали из опроса — возвращаем к опросу, чтобы ответить заново
        db.update_poll(poll["id"], status="pending", answered_at=None)
        return await send_poll(bot, db, cfg, user, poll_id=poll["id"], via=via)
    await show(bot, db, user["user_id"], main_screen(db, cfg, user, "Запись отменена, ничего не сохранила."),
               via=via)


@router.callback_query(F.data.startswith("x:"))
async def on_cancel(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    entry = _entry_for(cb, db, user)
    if not entry:
        return await stale(cb, bot, db, cfg, qn, user)
    await cb.answer()
    await cancel_entry(bot, db, cfg, user, entry, cb.message.message_id)


@router.callback_query(F.data.startswith("d:"))
async def on_done(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    entry = _entry_for(cb, db, user)
    if not entry:
        return await stale(cb, bot, db, cfg, qn, user)
    await cb.answer()
    await finish_entry(bot, db, qn, cfg, user["user_id"], entry["id"], via=cb.message.message_id)


# ================================================================ выгрузка
@router.callback_query(F.data.in_({"e:week", "e:month", "e:all", "ec:all"}))
async def on_export(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    kind, period = cb.data.split(":")
    await cb.answer("Готовлю файл…")
    builder = build_csv if kind == "ec" else build_markdown
    filename, data = builder(db, qn, user["user_id"], period, utz(user, cfg))
    caption = ("Таблица для Excel / Google Sheets." if kind == "ec" else
               "Можно отправить этот файл нейросети с просьбой: «Проанализируй дневник головной боли: "
               "частота, сила, возможные триггеры, связь с циклом, эффективность обезболивающих. "
               "Что стоит обсудить с неврологом?»")
    await bot.send_document(user["chat_id"], BufferedInputFile(data, filename=filename), caption=caption)
    # Файл остаётся в чате, а панель переезжает под него
    await show(bot, db, user["user_id"], main_screen(db, cfg, user, "📎 Дневник — файлом выше."), new=True)


# ================================================================ настройки
@router.callback_query(F.data.startswith("set:"))
async def on_settings_cb(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, user: dict):
    parts = cb.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    uid, via = user["user_id"], cb.message.message_id
    await cb.answer()

    def reschedule() -> dict:
        fresh = db.get_user(uid)
        db.update_user(uid, next_poll_at=to_iso(compute_next(fresh, cfg.tz, now_utc())))
        return db.get_user(uid)

    # Принимаем только значения с кнопок — данные callback'а может подделать клиент
    if action == "int" and len(parts) == 3 and parts[2].isdigit() and int(parts[2]) in kb.FREQ_OPTIONS:
        db.update_user(uid, interval_days=int(parts[2]))
        user = reschedule()
    elif action == "t" and len(parts) == 4 and is_valid_time(f"{parts[2]}:{parts[3]}"):
        db.update_user(uid, poll_time=f"{parts[2]}:{parts[3]}")
        user = reschedule()
    elif action == "pause":
        db.update_user(uid, paused=0 if user["paused"] else 1)
        user = reschedule()
    elif action == "now":
        return await send_poll(bot, db, cfg, user, via=via)
    else:
        return
    await show(bot, db, uid, settings_screen(cfg, user, "✅ Сохранено"), via=via)


@router.callback_query(F.data.startswith("del"))
async def on_delete(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    try:
        action, eid = cb.data.split(":")
        entry = db.get_entry(int(eid))
    except ValueError:
        return await cb.answer()
    uid, via = user["user_id"], cb.message.message_id
    await cb.answer()
    if not entry or entry["user_id"] != uid:
        return await show(bot, db, uid, last_entry_screen(db, qn, cfg, user), via=via)
    if action == "del":
        return await show(bot, db, uid, last_entry_screen(db, qn, cfg, user, confirm=True), via=via)
    for f in db.delete_entry(entry["id"]):
        Path(f).unlink(missing_ok=True)
    await show(bot, db, uid, settings_screen(cfg, user, "🗑 Запись удалена."), via=via)


# ================================================================ часовой пояс
async def apply_timezone(bot: Bot, db: DB, cfg: Config, user: dict, tz_name: str, via: int | None = None) -> None:
    uid = user["user_id"]
    db.update_user(uid, tz=tz_name, awaiting=None)
    db.update_user(uid, next_poll_at=to_iso(compute_next(db.get_user(uid), cfg.tz, now_utc())))
    fresh = db.get_user(uid)
    tz = utz(fresh, cfg)
    note = (f"🌍 Часовой пояс: {tz_label(tz)}. Сейчас у тебя {now_utc().astimezone(tz):%H:%M}.")
    await show(bot, db, uid, main_screen(db, cfg, fresh, note), via=via)


@router.callback_query(F.data.startswith("tz:"))
async def on_tz_choice(cb: CallbackQuery, bot: Bot, db: DB, cfg: Config, user: dict):
    name = cb.data[3:]
    if name != "other" and name not in {n for _, n in TZ_CHOICES}:
        return await cb.answer("Неизвестный пояс")
    await cb.answer()
    if name == "other":
        db.update_user(user["user_id"], awaiting="tz_time")
        return await show(bot, db, user["user_id"], tz_time_screen(), via=cb.message.message_id)
    await apply_timezone(bot, db, cfg, user, name, via=cb.message.message_id)


@router.message(F.location)
async def on_location(message: Message, bot: Bot, db: DB, cfg: Config, user: dict):
    loc = message.location
    await delete_incoming(message)
    try:
        name = tz_from_location(loc.latitude, loc.longitude)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось определить пояс по геопозиции")
        name = None
    if not name:
        text, markup = tz_screen(user)
        return await show(bot, db, user["user_id"],
                          ("Не получилось определить пояс по геопозиции.\n\n" + text, markup))
    await apply_timezone(bot, db, cfg, user, name)


# ================================================================ голосовые
async def save_voice(message: Message, bot: Bot, cfg: Config, entry_id: int) -> Path:
    media = message.voice or message.audio or message.video_note
    ext = ".mp4" if message.video_note else ".ogg"
    path = cfg.voice_dir / f"{message.from_user.id}_{entry_id}_{message.message_id}{ext}"
    await bot.download(media, destination=path)
    return path


async def transcribe_note(transcriber: Transcriber, db: DB, note_id: int, path: Path) -> str | None:
    db.update_note(note_id, status="pending")
    try:
        text = await transcriber.transcribe(path)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось расшифровать %s", path)
        db.update_note(note_id, status="failed")
        return None
    db.update_note(note_id, text=text or "(пусто)", status="ok")
    return text


async def refresh_entry_panel(bot: Bot, db: DB, qn: Questionnaire, entry_id: int) -> None:
    """Перерисовывает панель, если пользователь всё ещё в этой записи на шаге заметок."""
    entry = db.get_entry(entry_id)
    if not entry or entry["stage"] != "notes":
        return
    open_entry = db.open_entry(entry["user_id"])
    if open_entry and open_entry["id"] == entry_id:
        await show(bot, db, entry["user_id"], entry_screen(db, qn, entry))


_queue_running = False


async def process_queued_voice(bot: Bot, db: DB, qn: Questionnaire, transcriber: Transcriber) -> None:
    """Расшифровывает голосовые, отложенные до готовности модели."""
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
            await transcribe_note(transcriber, db, note["id"], path)
            await refresh_entry_panel(bot, db, qn, note["entry_id"])
    finally:
        _queue_running = False


@router.message(F.voice | F.audio | F.video_note)
async def on_voice(message: Message, bot: Bot, db: DB, cfg: Config, qn: Questionnaire,
                   transcriber: Transcriber, user: dict):
    uid = user["user_id"]
    entry = db.open_entry(uid)
    started_here = entry is None
    if started_here:
        # Голосовое без открытой записи — начинаем запись и кладём его в заметки
        entry_id = await start_entry(bot, db, qn, cfg, user)
        entry = db.get_entry(entry_id)
    path = await save_voice(message, bot, cfg, entry["id"])
    await delete_incoming(message)

    if not transcriber.enabled or not transcriber.ready:
        db.add_note(entry["id"], "voice", file_path=str(path), status="ok" if not transcriber.enabled else "queued")
        why = ("расшифровка отключена" if not transcriber.enabled
               else "модель для расшифровки ещё готовится, расшифрую позже")
        extra = f"🎙 Голосовое сохранено в заметки ({why})."
        if entry["stage"] == "questions":
            extra += " На вопрос ответь кнопкой или текстом."
        return await show(bot, db, uid, entry_screen(db, qn, entry, extra))

    note_id = db.add_note(entry["id"], "voice", file_path=str(path), status="pending")
    entry = db.get_entry(entry["id"])
    if entry["stage"] == "questions" and not started_here:
        await show(bot, db, uid, entry_screen(db, qn, entry, "🎙 Расшифровываю голосовое…"))
        text = await transcribe_note(transcriber, db, note_id, path)
        entry = db.get_entry(entry["id"])
        if text and entry["stage"] == "questions":
            # Голосовое на этапе вопросов — ответ на текущий вопрос (в заметках оно тоже остаётся)
            q = qn.questions[entry["step"]]
            save_answer(db, qn, entry, q, text_answer_value(entry, q, text))
            return await show(bot, db, uid, entry_screen(db, qn, entry, f"🎙 «{escape(trunc(text, 200))}»"))
        extra = "" if text else "⚠️ Не получилось расшифровать голосовое, но оно сохранено."
        return await show(bot, db, uid, entry_screen(db, qn, entry, extra))

    extra = "🎙 Голосовое сохранено в заметки к этой записи." if started_here else ""
    await show(bot, db, uid, entry_screen(db, qn, entry, extra))
    await transcribe_note(transcriber, db, note_id, path)
    if entry["stage"] == "notes":
        await refresh_entry_panel(bot, db, qn, entry["id"])


# ================================================================ свободный текст
@fallback_router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot, db: DB, cfg: Config, qn: Questionnaire, user: dict):
    uid = user["user_id"]
    text = message.text

    if user.get("awaiting") == "tz_time":
        await delete_incoming(message)
        name = tz_from_local_time(text, now_utc())
        if name:
            return await apply_timezone(bot, db, cfg, user, name)
        return await show(bot, db, uid, tz_time_screen("Не поняла время 🤔"))

    legacy = kb.LEGACY_BUTTONS.get(kb._norm(text))
    entry = db.open_entry(uid)
    if legacy:
        await delete_incoming(message)
        if legacy == "rec":
            return await start_entry(bot, db, qn, cfg, user, new=True)
        if legacy == "done" and entry:
            return await finish_entry(bot, db, qn, cfg, uid, entry["id"])
        if legacy == "export":
            return await show(bot, db, uid, export_screen(), new=True)
        if legacy == "settings":
            return await show(bot, db, uid, settings_screen(cfg, user), new=True)
        return await show(bot, db, uid, main_screen(db, cfg, user), new=True)

    if not entry:
        return await open_main(message, bot, db, cfg, user,
                               "Сейчас нет открытой записи. Если болит голова — нажми «🤕 Записать приступ».")
    if entry["stage"] == "questions":
        q = qn.questions[entry["step"]]
        save_answer(db, qn, entry, q, text_answer_value(entry, q, text))
    else:
        db.add_note(entry["id"], "text", text=text)
    await delete_incoming(message)
    await show(bot, db, uid, entry_screen(db, qn, entry))


@fallback_router.message()
async def on_other_message(message: Message, bot: Bot, db: DB, cfg: Config, user: dict):
    """Стикеры, фото и прочее — убираем, показываем меню."""
    await open_main(message, bot, db, cfg, user)
