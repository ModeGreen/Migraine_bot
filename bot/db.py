"""Хранилище на SQLite. Все времена хранятся в UTC в формате ISO."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    chat_id       INTEGER NOT NULL,
    interval_days INTEGER NOT NULL DEFAULT 1,
    poll_time     TEXT    NOT NULL DEFAULT '21:00',
    paused        INTEGER NOT NULL DEFAULT 0,
    next_poll_at  TEXT,
    tz            TEXT,                    -- «Europe/Moscow» или «UTC+03:00»; NULL — пояс по умолчанию
    awaiting      TEXT,                    -- чего бот ждёт текстом вне записи: 'tz_time'
    panel_msg_id  INTEGER,                 -- сообщение-«панель», которое бот редактирует
    kb_removed    INTEGER NOT NULL DEFAULT 0, -- убрана ли reply-клавиатура старой версии
    created_at    TEXT    NOT NULL
);

-- Регулярный опрос «болела ли голова с прошлого опроса».
-- status: pending (ждём ответа) | yes | no | missed (так и не ответила)
CREATE TABLE IF NOT EXISTS polls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    sent_at     TEXT    NOT NULL,
    message_id  INTEGER,
    status      TEXT    NOT NULL DEFAULT 'pending',
    answered_at TEXT,
    remind_at   TEXT
);

-- Запись о приступе. stage: questions | notes | done
CREATE TABLE IF NOT EXISTS entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    poll_id        INTEGER,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    stage          TEXT    NOT NULL DEFAULT 'questions',
    step           INTEGER NOT NULL DEFAULT 0,
    awaiting_other INTEGER,
    q_message_id   INTEGER,                -- сообщение с текущим вопросом
    answers        TEXT    NOT NULL DEFAULT '{}',
    closed_at      TEXT
);

-- Свободные заметки к записи: текст или расшифровка голосового.
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id   INTEGER NOT NULL,
    created_at TEXT    NOT NULL,
    kind       TEXT    NOT NULL,           -- text | voice
    text       TEXT,
    file_path  TEXT,
    status     TEXT    NOT NULL DEFAULT 'ok', -- ok | pending (расшифровывается) | queued (ждёт модель) | failed
    status_msg_id INTEGER                   -- сообщение «🎙 …», которое обновится расшифровкой
);

CREATE INDEX IF NOT EXISTS idx_polls_user   ON polls(user_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_entries_user ON entries(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notes_entry  ON notes(entry_id);
"""


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class DB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Добавляет колонки, появившиеся в новых версиях, в уже существующую базу."""
        wanted = {
            "users": {"tz": "TEXT", "awaiting": "TEXT", "panel_msg_id": "INTEGER",
                      "kb_removed": "INTEGER NOT NULL DEFAULT 0"},
            "entries": {"q_message_id": "INTEGER"},
            "notes": {"status_msg_id": "INTEGER"},
        }
        for table, cols in wanted.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def backup(self, backup_dir: Path, keep: int = 14) -> Path:
        """Копия базы в backup_dir/diary-ГГГГ-ММ-ДД.sqlite3, хранит последние `keep` штук."""
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"diary-{now_utc():%Y-%m-%d}.sqlite3"
        dest = sqlite3.connect(target)
        with dest:
            self.conn.backup(dest)
        dest.close()
        for old in sorted(backup_dir.glob("diary-*.sqlite3"))[:-keep]:
            old.unlink()
        return target

    # ---------- users ----------
    def get_user(self, user_id: int) -> dict | None:
        return self._one("SELECT * FROM users WHERE user_id=?", (user_id,))

    def ensure_user(self, user_id: int, chat_id: int) -> tuple[dict, bool]:
        user = self.get_user(user_id)
        if user:
            if user["chat_id"] != chat_id:
                self._exec("UPDATE users SET chat_id=? WHERE user_id=?", (chat_id, user_id))
            return self.get_user(user_id), False
        self._exec(
            "INSERT INTO users(user_id, chat_id, created_at) VALUES (?,?,?)",
            (user_id, chat_id, to_iso(now_utc())),
        )
        return self.get_user(user_id), True

    def update_user(self, user_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE users SET {cols} WHERE user_id=?", (*fields.values(), user_id))

    def all_users(self) -> list[dict]:
        return self._all("SELECT * FROM users")

    # ---------- polls ----------
    def create_poll(self, user_id: int) -> int:
        return self._exec(
            "INSERT INTO polls(user_id, sent_at) VALUES (?,?)", (user_id, to_iso(now_utc()))
        ).lastrowid

    def get_poll(self, poll_id: int) -> dict | None:
        return self._one("SELECT * FROM polls WHERE id=?", (poll_id,))

    def update_poll(self, poll_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE polls SET {cols} WHERE id=?", (*fields.values(), poll_id))

    def pending_polls(self, user_id: int) -> list[dict]:
        return self._all(
            "SELECT * FROM polls WHERE user_id=? AND status='pending' ORDER BY sent_at", (user_id,)
        )

    def due_reminders(self, now: datetime) -> list[dict]:
        return self._all(
            "SELECT * FROM polls WHERE status='pending' AND remind_at IS NOT NULL AND remind_at<=?",
            (to_iso(now),),
        )

    def previous_answered_poll(self, user_id: int, before_id: int) -> dict | None:
        return self._one(
            "SELECT * FROM polls WHERE user_id=? AND id<? AND status IN ('yes','no') "
            "ORDER BY id DESC LIMIT 1",
            (user_id, before_id),
        )

    def polls_between(self, user_id: int, start: datetime | None, end: datetime) -> list[dict]:
        return self._all(
            "SELECT * FROM polls WHERE user_id=? AND sent_at>=? AND sent_at<=? ORDER BY sent_at",
            (user_id, to_iso(start) if start else "", to_iso(end)),
        )

    # ---------- entries ----------
    def create_entry(self, user_id: int, poll_id: int | None) -> int:
        ts = to_iso(now_utc())
        return self._exec(
            "INSERT INTO entries(user_id, poll_id, created_at, updated_at) VALUES (?,?,?,?)",
            (user_id, poll_id, ts, ts),
        ).lastrowid

    def get_entry(self, entry_id: int) -> dict | None:
        e = self._one("SELECT * FROM entries WHERE id=?", (entry_id,))
        if e:
            e["answers"] = json.loads(e["answers"])
        return e

    def update_entry(self, entry_id: int, **fields: Any) -> None:
        if "answers" in fields:
            fields["answers"] = json.dumps(fields["answers"], ensure_ascii=False)
        fields["updated_at"] = to_iso(now_utc())
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE entries SET {cols} WHERE id=?", (*fields.values(), entry_id))

    def touch_entry(self, entry_id: int) -> None:
        self.update_entry(entry_id)

    def open_entry(self, user_id: int) -> dict | None:
        row = self._one(
            "SELECT id FROM entries WHERE user_id=? AND stage!='done' ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        return self.get_entry(row["id"]) if row else None

    def close_entry(self, entry_id: int) -> None:
        self.update_entry(entry_id, stage="done", awaiting_other=None, closed_at=to_iso(now_utc()))

    def stale_entries(self, stage: str, older_than: datetime) -> list[dict]:
        return self._all(
            "SELECT * FROM entries WHERE stage=? AND updated_at<?", (stage, to_iso(older_than))
        )

    def entries_between(self, user_id: int, start: datetime | None, end: datetime) -> list[dict]:
        rows = self._all(
            "SELECT * FROM entries WHERE user_id=? AND created_at>=? AND created_at<=? "
            "ORDER BY created_at",
            (user_id, to_iso(start) if start else "", to_iso(end)),
        )
        for r in rows:
            r["answers"] = json.loads(r["answers"])
            r["notes"] = self.notes_for(r["id"])
        return rows

    def last_entry(self, user_id: int) -> dict | None:
        row = self._one(
            "SELECT id FROM entries WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
        )
        return self.get_entry(row["id"]) if row else None

    def delete_entry(self, entry_id: int) -> list[str]:
        """Удаляет запись и её заметки, возвращает пути голосовых файлов."""
        files = [n["file_path"] for n in self.notes_for(entry_id) if n["file_path"]]
        self._exec("DELETE FROM notes WHERE entry_id=?", (entry_id,))
        self._exec("DELETE FROM entries WHERE id=?", (entry_id,))
        return files

    # ---------- notes ----------
    def add_note(
        self, entry_id: int, kind: str, text: str | None = None,
        file_path: str | None = None, status: str = "ok",
    ) -> int:
        note_id = self._exec(
            "INSERT INTO notes(entry_id, created_at, kind, text, file_path, status) "
            "VALUES (?,?,?,?,?,?)",
            (entry_id, to_iso(now_utc()), kind, text, file_path, status),
        ).lastrowid
        self.touch_entry(entry_id)
        return note_id

    def update_note(self, note_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE notes SET {cols} WHERE id=?", (*fields.values(), note_id))

    def notes_for(self, entry_id: int) -> list[dict]:
        return self._all("SELECT * FROM notes WHERE entry_id=? ORDER BY id", (entry_id,))

    def requeue_interrupted_voice(self) -> None:
        """Голосовые, расшифровка которых оборвалась перезапуском, — снова в очередь."""
        self._exec("UPDATE notes SET status='queued' WHERE kind='voice' AND status='pending'")

    def queued_voice_notes(self) -> list[dict]:
        return self._all("SELECT * FROM notes WHERE kind='voice' AND status='queued' ORDER BY id")
