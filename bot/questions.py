"""Загрузка вопросов из questions.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Question:
    id: str
    label: str
    text: str
    type: str                      # single | multi | scale
    options: list[str] = field(default_factory=list)
    other: bool = False
    columns: int = 2

    def answer_to_text(self, answer) -> str:
        if answer is None:
            return "—"
        if isinstance(answer, list):
            return ", ".join(str(a) for a in answer) if answer else "—"
        return str(answer)


@dataclass
class Questionnaire:
    questions: list[Question]
    final_label: str
    final_text: str

    def __len__(self) -> int:
        return len(self.questions)


def load_questions(path: Path) -> Questionnaire:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    questions: list[Question] = []
    seen: set[str] = set()
    for i, q in enumerate(raw.get("questions") or []):
        qtype = q.get("type", "single")
        if qtype not in ("single", "multi", "scale"):
            raise ValueError(f"Вопрос #{i + 1}: неизвестный type={qtype!r}")
        if qtype == "scale":
            options = [str(v) for v in range(int(q.get("min", 1)), int(q.get("max", 10)) + 1)]
        else:
            options = [str(o) for o in q.get("options") or []]
            if not options:
                raise ValueError(f"Вопрос #{i + 1}: нет вариантов ответа (options)")
        qid = str(q.get("id") or f"q{i + 1}")
        if qid in seen:
            raise ValueError(f"Повторяющийся id вопроса: {qid}")
        seen.add(qid)
        questions.append(
            Question(
                id=qid,
                label=str(q.get("label") or q["text"]),
                text=str(q["text"]),
                type=qtype,
                options=options,
                other=bool(q.get("other", False)),
                columns=int(q.get("columns", 5 if qtype == "scale" else 2)),
            )
        )
    final = raw.get("final") or {}
    return Questionnaire(
        questions=questions,
        final_label=str(final.get("label", "Заметки")),
        final_text=str(final.get("text", "Добавь заметки и нажми «✅ Готово».")),
    )
