"""Картинки разделов меню.

Файлы лежат в папке menu/ (MENU_DIR): <раздел>.jpg — main, welcome, poll, entry,
notes, export, settings, time, delete. Если картинки нет, раздел показывается
обычным текстом. После первой отправки Telegram выдаёт file_id — он кэшируется
в базе вместе с «подписью» файла (размер + время изменения), так что при замене
картинки кэш сбрасывается сам.
"""
from __future__ import annotations

from pathlib import Path

from aiogram.types import FSInputFile

from .db import DB

SECTIONS = ("main", "welcome", "poll", "entry", "notes", "export", "settings", "time", "delete")
EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


class MenuImages:
    def __init__(self, menu_dir: Path, db: DB):
        self.dir = menu_dir
        self.db = db

    def _file(self, name: str) -> Path | None:
        for ext in EXTENSIONS:
            p = self.dir / f"{name}{ext}"
            if p.is_file():
                return p
        return None

    @staticmethod
    def _signature(path: Path) -> str:
        st = path.stat()
        return f"{path.name}:{st.st_size}:{int(st.st_mtime)}"

    def has(self, name: str | None) -> bool:
        return bool(name) and self._file(name) is not None

    def get(self, name: str):
        """file_id из кэша, если файл не менялся, иначе файл для загрузки."""
        path = self._file(name)
        if path is None:
            return None
        cached = self.db.get_media(name)
        if cached and cached["sig"] == self._signature(path):
            return cached["file_id"]
        return FSInputFile(path)

    def remember(self, name: str, file_id: str) -> None:
        path = self._file(name)
        if path is not None:
            self.db.set_media(name, self._signature(path), file_id)
