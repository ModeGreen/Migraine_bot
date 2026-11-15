"""Настройки из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_timezone() -> ZoneInfo:
    # Пустая переменная TZ в glibc означает UTC — убираем её, чтобы взять пояс хоста.
    if os.environ.get("TZ", None) == "":
        del os.environ["TZ"]
    tz_name = os.environ.get("TZ")
    if tz_name:
        return ZoneInfo(tz_name)
    try:
        import tzlocal

        return tzlocal.get_localzone()
    except Exception:  # noqa: BLE001 — не смогли определить, работаем в UTC
        return ZoneInfo("UTC")


def _int_list(value: str) -> set[int]:
    return {int(x) for x in value.replace(";", ",").split(",") if x.strip()}


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_user_ids: set[int]
    data_dir: Path
    questions_file: Path
    tz: ZoneInfo
    whisper_model: str          # tiny / base / small / medium / large-v3 / off
    whisper_unload_minutes: int
    whisper_language: str
    notes_autoclose_minutes: int
    snooze_minutes: int

    @property
    def custom_emoji(self) -> bool:
        """Иконки кнопок из пака кастомных эмодзи (нужен Telegram Premium у владельца бота)."""
        return os.environ.get("CUSTOM_EMOJI", "1").strip().lower() not in ("0", "false", "no", "off", "")

    @property
    def menu_dir(self) -> Path:
        return Path(os.environ.get("MENU_DIR", "menu")).resolve()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "diary.sqlite3"

    @property
    def voice_dir(self) -> Path:
        return self.data_dir / "voice"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"


def load_config() -> Config:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN не задан. Заполни .env (см. .env.example).")
    data_dir = Path(os.environ.get("DATA_DIR", "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        bot_token=token,
        allowed_user_ids=_int_list(os.environ.get("ALLOWED_USER_IDS", "")),
        data_dir=data_dir,
        questions_file=Path(os.environ.get("QUESTIONS_FILE", "questions.yaml")).resolve(),
        tz=_load_timezone(),
        whisper_model=os.environ.get("WHISPER_MODEL", "medium").strip() or "off",
        whisper_unload_minutes=int(os.environ.get("WHISPER_UNLOAD_MINUTES", "10")),
        whisper_language=os.environ.get("WHISPER_LANGUAGE", "ru"),
        notes_autoclose_minutes=int(os.environ.get("NOTES_AUTOCLOSE_MINUTES", "60")),
        snooze_minutes=int(os.environ.get("SNOOZE_MINUTES", "120")),
    )
    cfg.voice_dir.mkdir(exist_ok=True)
    cfg.models_dir.mkdir(exist_ok=True)
    # Медицинские данные — доступ только владельцу
    for path in (cfg.data_dir, cfg.voice_dir):
        try:
            path.chmod(0o700)
        except OSError:
            pass
    return cfg
