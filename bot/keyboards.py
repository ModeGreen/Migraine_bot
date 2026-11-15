"""Inline-клавиатуры. Весь интерфейс — одно сообщение-«панель», которое меняется
при навигации; reply-клавиатур нет, чтобы в чате не копились сообщения.

Иконки кнопок — кастомные эмодзи из пака «Зазеркалья» (t.me/addemoji/zazer_kalye_emoji),
поле icon_custom_emoji_id. Telegram показывает их, только если у владельца бота есть
Telegram Premium (или бот куплен на Fragment). Без этого — CUSTOM_EMOJI=0 в .env, и на
кнопках будут обычные эмодзи. Если Telegram отклонит иконки, бот сам переключится на
обычные эмодзи (см. strip_icons и handlers.show).
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton as IB, InlineKeyboardMarkup

from .questions import Question

FREQ_OPTIONS = {1: "Каждый день", 2: "Через день", 3: "Раз в 3 дня", 7: "Раз в неделю"}

# Кастомные эмодзи пака zazer_kalye_emoji: имя -> (custom_emoji_id, обычный эмодзи-замена)
ICONS: dict[str, tuple[str, str]] = {
    "record":   ("5323394002391703885", "🤕"),  # мужик в папахе — «Если тебя кокнули»
    "export":   ("5471936769173788340", "📜"),  # Истукан — хранитель древних времён
    "settings": ("5199882554118808525", "⚙️"),  # Игорь Диод (лампочка)
    "resume":   ("5469860886925583398", "▶️"),  # мальчик «на свободе»
    "tz":       ("5471880603886461876", "🌍"),  # Грозный Эдгар (солнце)
    "tz_other": ("5472363757642491575", "🕐"),  # луна
    "poll_no":  ("5321161744679220991", "🙂"),  # улыбака
    "poll_yes": ("5472167778284774460", "🤕"),  # орущий бобыль
    "later":    ("5472363757642491575", "⏰"),  # луна
    "home":     ("5323388543488271373", "🏠"),  # гриб Акакий
    "cancel":   ("5472299646665663359", "✖️"),  # тётка в бигудях
    "other":    ("5323567961452095030", "✍️"),  # пирамидка
    "done":     ("5474427532377897270", "✅"),  # зелёный — «запоминай и делись»
    "freq":     ("5472391262613053593", "🔁"),  # гриб (время от времени)
    "time":     ("5323388543488271373", "🕘"),  # гриб
    "pause":    ("5309938707371959760", "⏸"),   # Толстомясочка
    "resume_polls": ("5309938707371959760", "▶️"),
    "ask_now":  ("5201775371975959915", "🔔"),  # кокша — «Постой!»
    "last":     ("5204416703848555282", "🗂"),  # Дед Ченя
    "bury":     ("5204416703848555282", "⛏"),  # Дед Ченя
    "csv":      ("5474257163910159644", "📊"),  # мешок
}

_icons_enabled = True


def set_icons_enabled(value: bool) -> None:
    global _icons_enabled
    _icons_enabled = value


def icons_enabled() -> bool:
    return _icons_enabled


_FALLBACK_BY_ID = {cid: emoji for cid, emoji in ICONS.values()}


def ib(label: str, callback_data: str, icon: str | None = None, emoji_after: bool = False) -> IB:
    """Кнопка с иконкой-эмодзи из пака (или обычным эмодзи, если иконки выключены)."""
    if icon:
        custom_id, plain = ICONS[icon]
        if _icons_enabled:
            return IB(text=label, callback_data=callback_data, icon_custom_emoji_id=custom_id)
        label = f"{label} {plain}" if emoji_after else f"{plain} {label}"
    return IB(text=label, callback_data=callback_data)


def strip_icons(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """Та же клавиатура, но с обычными эмодзи вместо иконок из пака."""
    if markup is None:
        return None
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            if b.icon_custom_emoji_id:
                plain = _FALLBACK_BY_ID.get(b.icon_custom_emoji_id, "")
                b = b.model_copy(update={"icon_custom_emoji_id": None,
                                         "text": f"{plain} {b.text}".strip()})
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def back(to: str = "main") -> IB:
    return IB(text="⬅️ Назад", callback_data=f"go:{to}")


def home() -> IB:
    return ib("Меню", "go:main", "home")


# ---------------------------------------------------------------- главное меню
def main_kb(has_open_entry: bool, need_tz: bool) -> InlineKeyboardMarkup:
    rows = []
    if need_tz:
        rows.append([ib("Указать часовой пояс", "go:tz", "tz")])
    if has_open_entry:
        rows.append([ib("Продолжить запись", "go:entry", "resume")])
    rows.append([ib("Кокнуло — записать приступ", "rec", "record")])
    rows.append([ib("Летопись", "go:export", "export"),
                 ib("Настройки", "go:settings", "settings")])
    return _kb(rows)


def help_kb() -> InlineKeyboardMarkup:
    return _kb([[back()]])


# ---------------------------------------------------------------- опрос
def poll_kb(poll_id: int) -> InlineKeyboardMarkup:
    return _kb([
        [ib("Нет, не кокало", f"p:{poll_id}:no", "poll_no", emoji_after=True),
         ib("Да, кокнуло", f"p:{poll_id}:yes", "poll_yes", emoji_after=True)],
        [ib("Спроси позже", f"p:{poll_id}:later", "later"), home()],
    ])


# ---------------------------------------------------------------- запись приступа
def _entry_nav(entry_id: int) -> list[IB]:
    return [IB(text="⬅️ Назад", callback_data=f"qb:{entry_id}"), home(),
            ib("Отменить", f"x:{entry_id}", "cancel")]


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
        extra.append(ib("Другое", f"o:{entry_id}:{step}", "other"))
    extra.append(IB(text="Пропустить", callback_data=f"s:{entry_id}:{step}"))
    rows.append(extra)
    rows.append(_entry_nav(entry_id))
    return _kb(rows)


def notes_kb(entry_id: int) -> InlineKeyboardMarkup:
    return _kb([[ib("Готово — в летопись", f"d:{entry_id}", "done")],
                _entry_nav(entry_id)])


# ---------------------------------------------------------------- выгрузка
def export_kb() -> InlineKeyboardMarkup:
    return _kb([
        [IB(text="За неделю", callback_data="e:week"),
         IB(text="За месяц", callback_data="e:month"),
         IB(text="За всё время", callback_data="e:all")],
        [ib("Таблица CSV за всё время", "ec:all", "csv")],
        [back()],
    ])


# ---------------------------------------------------------------- настройки
def settings_kb(paused: bool) -> InlineKeyboardMarkup:
    return _kb([
        [ib("Частота", "go:freq", "freq"), ib("Время", "go:time", "time")],
        [ib("Часовой пояс", "go:tz", "tz"),
         ib("Возобновить", "set:pause", "resume_polls") if paused else ib("Пауза", "set:pause", "pause")],
        [ib("Спросить сейчас", "set:now", "ask_now"), ib("Последняя запись", "go:last", "last")],
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
    rows.append([ib("Другой — укажу, сколько у меня времени", "tz:other", "tz_other")])
    rows.append([back(back_to)])
    return _kb(rows)


def tz_time_kb() -> InlineKeyboardMarkup:
    return _kb([[back("tz")]])


def last_entry_kb(entry_id: int | None, confirm: bool = False) -> InlineKeyboardMarkup:
    if entry_id is None:
        return _kb([[back("settings")]])
    if confirm:
        return _kb([[ib("Да, закопать", f"del!:{entry_id}", "bury"),
                     IB(text="Нет", callback_data="go:last")]])
    return _kb([[ib("Закопать запись (удалить)", f"del:{entry_id}", "bury")],
                [back("settings")]])
