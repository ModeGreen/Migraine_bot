from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton as IB,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from .questions import Question

BTN_RECORD = "🤕 Записать приступ"
BTN_EXPORT = "📤 Выгрузка"
BTN_SETTINGS = "⚙️ Настройки"
BTN_DONE = "✅ Готово"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_RECORD)],
              [KeyboardButton(text=BTN_EXPORT), KeyboardButton(text=BTN_SETTINGS)]],
    resize_keyboard=True,
    is_persistent=True,
)

NOTES_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_DONE)]],
    resize_keyboard=True,
    is_persistent=True,
)


BTN_TZ_LIST = "🌐 Выбрать город из списка"
BTN_TZ_LATER = "⏭ Позже"

TZ_REQUEST_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📍 Отправить геопозицию", request_location=True)],
              [KeyboardButton(text=BTN_TZ_LIST)],
              [KeyboardButton(text=BTN_TZ_LATER)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def tz_list_kb() -> "InlineKeyboardMarkup":
    from .tz import TZ_CHOICES

    rows = _rows([IB(text=label, callback_data=f"tz:{name}") for label, name in TZ_CHOICES], 2)
    rows.append([IB(text="🕐 Другой — укажу, сколько у меня времени", callback_data="tz:other")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _rows(buttons: list[IB], columns: int) -> list[list[IB]]:
    columns = max(1, columns)
    return [buttons[i:i + columns] for i in range(0, len(buttons), columns)]


def poll_kb(poll_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="Нет 🙂", callback_data=f"p:{poll_id}:no"),
         IB(text="Да 🤕", callback_data=f"p:{poll_id}:yes")],
        [IB(text="⏰ Спроси позже", callback_data=f"p:{poll_id}:later")],
    ])


def question_kb(entry_id: int, step: int, q: Question, selected: list | None = None) -> InlineKeyboardMarkup:
    selected = selected or []
    prefix = "t" if q.type == "multi" else "a"
    buttons = []
    for i, opt in enumerate(q.options):
        mark = "✓ " if q.type == "multi" and opt in selected else ""
        buttons.append(IB(text=f"{mark}{opt}", callback_data=f"{prefix}:{entry_id}:{step}:{i}"))
    rows = _rows(buttons, q.columns)
    extra = []
    if q.type == "multi":
        extra.append(IB(text="Дальше ➡️", callback_data=f"n:{entry_id}:{step}"))
    if q.other:
        extra.append(IB(text="✍️ Другое", callback_data=f"o:{entry_id}:{step}"))
    extra.append(IB(text="Пропустить", callback_data=f"s:{entry_id}:{step}"))
    rows.append(extra)
    rows.append([IB(text="✖️ Отменить запись", callback_data=f"x:{entry_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def start_record_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[IB(text=BTN_RECORD, callback_data="rec")]])


def export_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="За неделю", callback_data="e:week"),
         IB(text="За месяц", callback_data="e:month"),
         IB(text="За всё время", callback_data="e:all")],
        [IB(text="📊 CSV-таблица за всё время", callback_data="ec:all")],
    ])


def settings_kb(paused: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="🔁 Частота", callback_data="set:freq"),
         IB(text="🕘 Время", callback_data="set:time")],
        [IB(text="🌍 Часовой пояс", callback_data="set:tz"),
         IB(text="▶️ Возобновить опросы" if paused else "⏸ Приостановить опросы",
            callback_data="set:pause")],
        [IB(text="🔔 Спросить сейчас", callback_data="set:now"),
         IB(text="🗂 Последняя запись", callback_data="set:last")],
    ])


FREQ_OPTIONS = {1: "Каждый день", 2: "Через день", 3: "Раз в 3 дня", 7: "Раз в неделю"}


def freq_kb() -> InlineKeyboardMarkup:
    rows = _rows([IB(text=t, callback_data=f"set:int:{d}") for d, t in FREQ_OPTIONS.items()], 2)
    rows.append([IB(text="⬅️ Назад", callback_data="set:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def time_kb() -> InlineKeyboardMarkup:
    buttons = [IB(text=f"{h:02d}:00", callback_data=f"set:t:{h:02d}:00") for h in range(7, 24)]
    rows = _rows(buttons, 4)
    rows.append([IB(text="⬅️ Назад", callback_data="set:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delete_kb(entry_id: int, confirm: bool = False) -> InlineKeyboardMarkup:
    if confirm:
        return InlineKeyboardMarkup(inline_keyboard=[[
            IB(text="Да, удалить", callback_data=f"del!:{entry_id}"),
            IB(text="Нет", callback_data="delno"),
        ]])
    return InlineKeyboardMarkup(inline_keyboard=[[IB(text="🗑 Удалить запись", callback_data=f"del:{entry_id}")]])
