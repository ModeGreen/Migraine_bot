"""Выгрузка дневника в Markdown (для нейросети/врача) и CSV (для таблиц)."""
from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import DB, from_iso, now_utc
from .questions import Questionnaire

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

PERIODS = {
    "week": (7, "последние 7 дней"),
    "month": (30, "последние 30 дней"),
    "all": (None, "всё время"),
}


def _fmt_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d:%d.%m.%Y}"


def _local(ts: str, tz: ZoneInfo) -> datetime:
    return from_iso(ts).astimezone(tz)


def _painkiller_taken(answer) -> bool:
    if answer is None:
        return False
    text = ", ".join(answer) if isinstance(answer, list) else str(answer)
    return not text.strip().lower().startswith("нет")


def _collect(db: DB, user_id: int, period: str):
    days, title = PERIODS[period]
    end = now_utc()
    start = end - timedelta(days=days) if days else None
    entries = db.entries_between(user_id, start, end)
    polls = db.polls_between(user_id, start, end)
    return start, end, title, entries, polls


def _summary(qn: Questionnaire, entries: list[dict], polls: list[dict], tz: ZoneInfo,
             start: datetime | None, end: datetime) -> list[str]:
    lines = ["## Сводка", ""]
    pain_days = {_local(e["created_at"], tz).date() for e in entries}
    if start is None:
        firsts = [from_iso(e["created_at"]) for e in entries] + [from_iso(p["sent_at"]) for p in polls]
        start = min(firsts) if firsts else end
    total_days = (end.astimezone(tz).date() - start.astimezone(tz).date()).days + 1
    lines.append(f"- Записано приступов: {len(entries)}")
    lines.append(f"- Дней с записанной головной болью: {len(pain_days)} из {total_days}")

    scale_q = next((q for q in qn.questions if q.type == "scale"), None)
    if scale_q:
        values = []
        for e in entries:
            try:
                values.append(float(e["answers"].get(scale_q.id)))
            except (TypeError, ValueError):
                pass
        if values:
            lines.append(
                f"- {scale_q.label}: средняя {sum(values) / len(values):.1f}, "
                f"от {min(values):g} до {max(values):g}"
            )

    pk_q = next((q for q in qn.questions if q.id == "painkillers"), None)
    if pk_q:
        taken = [e for e in entries if _painkiller_taken(e["answers"].get(pk_q.id))]
        taken_days = {_local(e["created_at"], tz).date() for e in taken}
        lines.append(
            f"- Обезболивающее принимала: в {len(taken)} приступах, дней с приёмом: {len(taken_days)}"
        )

    status = Counter(p["status"] for p in polls)
    if polls:
        lines.append(
            f"- Регулярных опросов: {len(polls)} — «не болела»: {status['no']}, "
            f"«болела»: {status['yes']}, без ответа: {status['missed']}"
            + (f", ждёт ответа: {status['pending']}" if status["pending"] else "")
        )

    # Распределение ответов по вопросам с вариантами
    dist_lines = []
    for q in qn.questions:
        if q.type == "scale":
            continue
        c: Counter = Counter()
        for e in entries:
            a = e["answers"].get(q.id)
            for item in (a if isinstance(a, list) else [a] if a is not None else []):
                c[str(item)] += 1
        if c:
            dist_lines.append(f"- {q.label}: " + ", ".join(f"{k} — {v}" for k, v in c.most_common()))
    if dist_lines:
        lines += ["", "### Ответы по вопросам (сколько раз)", "", *dist_lines]
    return lines


def _entry_block(qn: Questionnaire, e: dict, tz: ZoneInfo) -> list[str]:
    t = _local(e["created_at"], tz)
    source = "после регулярного опроса" if e["poll_id"] else "записан сразу"
    lines = [f"**🤕 Приступ — запись в {t:%H:%M}** ({source})"]
    for q in qn.questions:
        lines.append(f"- {q.label}: {q.answer_to_text(e['answers'].get(q.id))}")
    notes = [n for n in e["notes"] if n["text"] or n["kind"] == "voice"]
    if notes:
        lines.append(f"- {qn.final_label}:")
        for n in notes:
            nt = _local(n["created_at"], tz)
            if n["kind"] == "voice":
                body = n["text"] or {
                    "pending": "(голосовое ещё расшифровывается)",
                    "queued": "(голосовое ещё не расшифровано)",
                    "failed": "(голосовое не удалось расшифровать)",
                }.get(n["status"], "(голосовое без расшифровки)")
                lines.append(f"  - {nt:%H:%M} (голосом): {body}")
            else:
                lines.append(f"  - {nt:%H:%M}: {n['text']}")
    else:
        lines.append(f"- {qn.final_label}: —")
    if e["stage"] != "done":
        lines.append("- _(запись ещё не завершена)_")
    return lines + [""]


def build_markdown(db: DB, qn: Questionnaire, user_id: int, period: str, tz: ZoneInfo) -> tuple[str, bytes]:
    start, end, title, entries, polls = _collect(db, user_id, period)
    end_local = end.astimezone(tz)
    lines = [
        "# Дневник головной боли (мигрени)",
        "",
        f"Период: {title}"
        + (f" ({start.astimezone(tz):%d.%m.%Y} – {end_local:%d.%m.%Y})" if start else ""),
        f"Выгружено: {end_local:%d.%m.%Y %H:%M} ({tz.key if hasattr(tz, 'key') else tz})",
        "",
        "> Как читать: записи ведёт сама пациентка через Telegram-бота. "
        "«Опрос» — регулярный вопрос «болела ли голова с прошлого опроса». "
        "«Приступ» — запись с ответами на вопросы и свободными заметками "
        "(часть заметок — расшифровки голосовых, возможны ошибки распознавания). "
        "Время записи может не совпадать со временем начала боли.",
        "",
    ]
    lines += _summary(qn, entries, polls, tz, start, end)
    lines += ["", "## Записи по дням", ""]

    by_day: dict[date, list[tuple[datetime, str, dict]]] = {}
    for e in entries:
        t = _local(e["created_at"], tz)
        by_day.setdefault(t.date(), []).append((t, "entry", e))
    for p in polls:
        if p["status"] == "yes":
            continue  # приступ уже есть в записях
        t = _local(p["sent_at"], tz)
        by_day.setdefault(t.date(), []).append((t, "poll", p))

    if not by_day:
        lines.append("_Записей за этот период нет._")

    def poll_line(t: datetime, p: dict) -> str:
        text = {
            "no": "голова не болела (или новых приступов не было)",
            "missed": "без ответа",
            "pending": "ждёт ответа",
        }[p["status"]]
        return f"- Опрос в {t:%H:%M}: {text}"

    days = sorted(by_day)
    i = 0
    while i < len(days):
        d = days[i]
        events = sorted(by_day[d], key=lambda x: x[0])
        if all(kind == "poll" for _, kind, _ in events):
            # Сжимаем подряд идущие дни без приступов в одну строку
            j = i
            quiet: list[dict] = []
            while j < len(days) and all(k == "poll" for _, k, _ in by_day[days[j]]):
                quiet += [p for _, _, p in by_day[days[j]]]
                j += 1
            if j - i > 1:
                c = Counter(p["status"] for p in quiet)
                parts = [f"опросов «не болела»: {c['no']}"]
                if c["missed"]:
                    parts.append(f"без ответа: {c['missed']}")
                if c["pending"]:
                    parts.append(f"ждёт ответа: {c['pending']}")
                lines += [
                    f"### {days[i]:%d.%m} – {days[j - 1]:%d.%m.%Y}: приступов не записано",
                    f"- {', '.join(parts)}",
                    "",
                ]
                i = j
                continue
        lines.append(f"### {_fmt_date(d)}")
        for t, kind, obj in events:
            if kind == "entry":
                lines += _entry_block(qn, obj, tz)
            else:
                lines.append(poll_line(t, obj))
        lines.append("")
        i += 1

    filename = f"migraine_diary_{period}_{end_local:%Y-%m-%d}.md"
    return filename, "\n".join(lines).encode("utf-8")


def _csv_safe(value) -> str:
    """Защита от CSV-инъекций: Excel выполняет ячейки, начинающиеся с = + @, как формулы."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "@", "\t", "\r") else text


def build_csv(db: DB, qn: Questionnaire, user_id: int, period: str, tz: ZoneInfo) -> tuple[str, bytes]:
    _start, end, _title, entries, polls = _collect(db, user_id, period)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["дата", "время", "тип", "ответ опроса", *[q.label for q in qn.questions], qn.final_label])
    rows = []
    for e in entries:
        t = _local(e["created_at"], tz)
        notes = " | ".join(n["text"] for n in e["notes"] if n["text"])
        rows.append((t, [f"{t:%Y-%m-%d}", f"{t:%H:%M}", "приступ", "",
                         *[q.answer_to_text(e["answers"].get(q.id)) for q in qn.questions], notes]))
    for p in polls:
        if p["status"] == "yes":
            continue
        t = _local(p["sent_at"], tz)
        status = {"no": "не болела", "missed": "без ответа", "pending": "ждёт ответа"}[p["status"]]
        rows.append((t, [f"{t:%Y-%m-%d}", f"{t:%H:%M}", "опрос", status, *[""] * len(qn.questions), ""]))
    for _, row in sorted(rows, key=lambda r: r[0]):
        w.writerow([_csv_safe(c) for c in row])
    filename = f"migraine_diary_{period}_{end.astimezone(tz):%Y-%m-%d}.csv"
    # BOM, чтобы Excel правильно открыл кириллицу
    return filename, ("﻿" + buf.getvalue()).encode("utf-8")
