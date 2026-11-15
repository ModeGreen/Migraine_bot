"""Inline-клавиатуры. Весь интерфейс — одно сообщение-«панель», которое меняется
при навигации; reply-клавиатур нет, чтобы в чате не копились сообщения."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton as IB, InlineKeyboardMarkup

from .questions import Question

BACK = "⬅️ Назад"
HOME = "🏠 Меню"
FREQ_OPTIONS = {1: "Каждый день", 2: "Через день", 3: "Раз в 3 дня", 7: "Раз в неделю"}


def _norm(text: str | None) -> str:
    """Убирает селекторы вариантов эмодзи (U+FE0E/U+FE0F) и лишние пробелы."""
    return " ".join((text or "").replace("️", "").replace("︎", "").split())


# Кнопки старой reply-клавиатуры: у пользователей прошлой версии они могут
# остаться на экране, пока бот её не уберёт, — понимаем их нажатия.
LEGACY_BUTTONS = {
    _norm("🤕 Записать приступ"): "rec",
    _norm("📤 Выгрузка"): "export",
    _norm("⚙️ Настройки"): "settings",
    _norm("✅ Готово"): "done",
}


def _rows(buttons: list[IB], columns: int) -> list[list[IB]]:
    columns = max(1, columns)
    return [buttons[i:i + columns] for i in range(0, len(buttons), columns)]


def _kb(rows: list[list[IB]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back(to: str = "main", text: str = BACK) -> IB:
    return IB(text=text, callback_data=f"go:{to}")


# ---------------------------------------------------------------- главное меню
def main_kb(has_open_entry: bool, need_tz: bool) -> InlineKeyboardMarkup:
    rows = []
    if need_tz:
        rows.append([IB(text="🌍 Указать часовой пояс", callback_data="go:tz")])
    if has_open_entry:
        rows.append([IB(text="▶️ Продолжить запись", callback_data="go:entry")])
    rows.append([IB(text="🤕 Кокнуло — записать приступ", callback_data="rec")])
    rows.append([IB(text="📜 Летопись", callback_data="go:export"),
                 IB(text="⚙️ Настройки", callback_data="go:settings")])
    return _kb(rows)


def help_kb() -> InlineKeyboardMarkup:
    return _kb([[back()]])


# ---------------------------------------------------------------- опрос
def poll_kb(poll_id: int) -> InlineKeyboardMarkup:
    return _kb([
        [IB(text="Нет, не кокало 🙂", callback_data=f"p:{poll_id}:no"),
         IB(text="Да, кокнуло 🤕", callback_data=f"p:{poll_id}:yes")],
        [IB(text="⏰ Спроси позже", callback_data=f"p:{poll_id}:later"), back(text=HOME)],
    ])


# ---------------------------------------------------------------- запись приступа
def _entry_nav(entry_id: int) -> list[IB]:
    return [IB(text=BACK, callback_data=f"qb:{entry_id}"), back(text=HOME),
            IB(text="✖️ Отменить", callback_data=f"x:{entry_id}")]


def question_kb(entry_id: int, step: int, q: Question, selected: list | None,
                awaiting_other: bool) -> InlineKeyboardMarkup:
    if awaiting_other:
        return _kb([[IB(text="↩️ К вариантам ответа", callback_data=f"ob:{entry_id}")],
                    _entry_nav(entry_id)])
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
    rows.append(_entry_nav(entry_id))
    return _kb(rows)


def notes_kb(entry_id: int) -> InlineKeyboardMarkup:
    return _kb([[IB(text="✅ Готово — в летопись", callback_data=f"d:{entry_id}")],
                _entry_nav(entry_id)])


# ---------------------------------------------------------------- выгрузка
def export_kb() -> InlineKeyboardMarkup:
    return _kb([
        [IB(text="За неделю", callback_data="e:week"),
         IB(text="За месяц", callback_data="e:month"),
         IB(text="За всё время", callback_data="e:all")],
        [IB(text="📊 Таблица CSV за всё время", callback_data="ec:all")],
        [back()],
    ])


# ---------------------------------------------------------------- настройки
def settings_kb(paused: bool) -> InlineKeyboardMarkup:
    return _kb([
        [IB(text="🔁 Частота", callback_data="go:freq"),
         IB(text="🕘 Время", callback_data="go:time")],
        [IB(text="🌍 Часовой пояс", callback_data="go:tz"),
         IB(text="▶️ Возобновить" if paused else "⏸ Пауза", callback_data="set:pause")],
        [IB(text="🔔 Спросить сейчас", callback_data="set:now"),
         IB(text="🗂 Последняя запись", callback_data="go:last")],
        [back()],
    ])


def freq_kb() -> InlineKeyboardMarkup:
    rows = _rows([IB(text=t, callback_data=f"set:int:{d}") for d, t in FREQ_OPTIONS.items()], 2)
    rows.append([back("settings")])
    return _kb(rows)


def time_kb() -> InlineKeyboardMarkup:
    buttons = [IB(text=f"{h:02d}:00", callback_data=f"set:t:{h:02d}:00") for h in range(7, 24)]
    rows = _rows(buttons, 4)
    rows.append([back("settings")])
    return _kb(rows)


def tz_kb(back_to: str) -> InlineKeyboardMarkup:
    from .tz import TZ_CHOICES

    rows = _rows([IB(text=label, callback_data=f"tz:{name}") for label, name in TZ_CHOICES], 2)
    rows.append([IB(text="🕐 Другой — укажу, сколько у меня времени", callback_data="tz:other")])
    rows.append([back(back_to)])
    return _kb(rows)


def tz_time_kb() -> InlineKeyboardMarkup:
    return _kb([[back("tz")]])


def last_entry_kb(entry_id: int | None, confirm: bool = False) -> InlineKeyboardMarkup:
    if entry_id is None:
        return _kb([[back("settings")]])
    if confirm:
        return _kb([[IB(text="Да, закопать", callback_data=f"del!:{entry_id}"),
                     IB(text="Нет", callback_data="go:last")]])
    return _kb([[IB(text="⛏ Закопать запись (удалить)", callback_data=f"del:{entry_id}")],
                [back("settings")]])
