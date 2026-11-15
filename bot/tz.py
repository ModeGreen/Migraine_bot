"""Часовые пояса пользователей.

Telegram не сообщает боту часовой пояс, поэтому при первом запуске бот
спрашивает его: по геопозиции (определяется офлайн, координаты не хранятся),
из списка городов или по текущему местному времени.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# (подпись на кнопке, IANA-имя)
TZ_CHOICES: list[tuple[str, str]] = [
    ("Калининград", "Europe/Kaliningrad"),
    ("Москва, СПб", "Europe/Moscow"),
    ("Самара", "Europe/Samara"),
    ("Екатеринбург", "Asia/Yekaterinburg"),
    ("Омск", "Asia/Omsk"),
    ("Новосибирск", "Asia/Novosibirsk"),
    ("Красноярск", "Asia/Krasnoyarsk"),
    ("Иркутск", "Asia/Irkutsk"),
    ("Якутск", "Asia/Yakutsk"),
    ("Владивосток", "Asia/Vladivostok"),
    ("Магадан", "Asia/Magadan"),
    ("Камчатка", "Asia/Kamchatka"),
    ("Минск", "Europe/Minsk"),
    ("Киев", "Europe/Kyiv"),
    ("Алматы", "Asia/Almaty"),
    ("Ташкент", "Asia/Tashkent"),
    ("Тбилиси", "Asia/Tbilisi"),
    ("Ереван", "Asia/Yerevan"),
    ("Стамбул", "Europe/Istanbul"),
    ("Берлин, Париж", "Europe/Berlin"),
]

_OFFSET_RE = re.compile(r"^UTC([+-])(\d{2}):(\d{2})$")
_TIME_RE = re.compile(r"^\s*(\d{1,2})[:.\s-]?(\d{2})?\s*$")


def parse_tz(name: str | None) -> tzinfo | None:
    """IANA-имя («Europe/Moscow») или фиксированное смещение («UTC+03:00»)."""
    if not name:
        return None
    m = _OFFSET_RE.match(name)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))), name)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def user_tz(user: dict | None, default: tzinfo) -> tzinfo:
    return (parse_tz(user.get("tz")) if user else None) or default


def tz_label(tz: tzinfo) -> str:
    """«Europe/Moscow (UTC+03:00)» — человекочитаемое название пояса."""
    name = getattr(tz, "key", None) or str(tz)
    offset = datetime.now(timezone.utc).astimezone(tz).strftime("%z")
    off = f"UTC{offset[:3]}:{offset[3:]}"
    return off if name == off else f"{name} ({off})"


def tz_from_location(lat: float, lon: float) -> str | None:
    from timezonefinder import TimezoneFinder

    global _finder
    if _finder is None:
        _finder = TimezoneFinder()
    return _finder.timezone_at(lat=lat, lng=lon)


_finder = None


def tz_from_local_time(text: str, now: datetime) -> str | None:
    """По присланному местному времени («14:35», «9») вычисляет смещение от UTC.

    Возвращает строку вида «UTC+03:00» или None, если не удалось разобрать.
    Смещение округляется до 30 минут (есть пояса вроде +05:30).
    """
    m = _TIME_RE.match(text)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2) or 0)
    if hh > 23 or mm > 59:
        return None
    now = now.astimezone(timezone.utc)
    local_minutes = hh * 60 + mm
    utc_minutes = now.hour * 60 + now.minute
    diff = local_minutes - utc_minutes
    # Приводим к диапазону −12:00…+14:00
    if diff > 14 * 60:
        diff -= 24 * 60
    elif diff < -12 * 60:
        diff += 24 * 60
    # Округляем до получаса (до часа, если минуты не указаны) — на случай, если
    # часы отстают или сообщение отправлено на пару минут позже
    step = 30 if m.group(2) else 60
    diff = round(diff / step) * step
    sign = "+" if diff >= 0 else "-"
    diff = abs(diff)
    return f"UTC{sign}{diff // 60:02d}:{diff % 60:02d}"
