import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Literal
import uuid

from . import db
from .logger import logger


TaskType = Literal["assignment", "exam"]
TaskStatus = Literal[
    "pending_approval",  # activa, esperando que el usuario apruebe
    "approved",          # aprobada, entregando...
    "rejected",          # rechazada por el usuario
    "submitted",         # entregada exitosamente por el bot
    "failed",            # error al entregar
    "expired",           # vencida sin entregar
    "future",            # aún no disponible
    "done",              # ya estaba entregada/completada antes del bot
]


@dataclass
class ExamQuestion:
    question: str
    options: list[str]
    question_type: Literal["single", "multiple"]
    ai_selected: list[int]


@dataclass
class PendingTask:
    id: str
    user_id: int
    course_name: str
    task_title: str
    task_description: str
    task_type: TaskType
    task_url: str
    status: TaskStatus
    created_at: datetime

    due_date: str | None = None
    available_from: str | None = None
    ai_response: str | None = None
    exam_questions: list[ExamQuestion] = field(default_factory=list)
    output_format: str = "docx"  # docx | pptx | pdf | triptico


# ─── Persistence ──────────────────────────────────────────────────────────────

def _from_row(row) -> PendingTask | None:
    if not row:
        return None
    return PendingTask(
        id=row["id"],
        user_id=row["user_id"],
        course_name=row["course_name"],
        task_title=row["task_title"],
        task_description=row["task_description"],
        task_type=row["task_type"],
        task_url=row["task_url"],
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        due_date=row["due_date"],
        available_from=row["available_from"],
        ai_response=row["ai_response"],
        exam_questions=[ExamQuestion(**q) for q in json.loads(row["exam_questions"] or "[]")],
        output_format=row["output_format"],
    )


def _update(task_id: str, **fields) -> bool:
    if "exam_questions" in fields:
        fields["exam_questions"] = json.dumps([asdict(q) for q in fields["exam_questions"]], ensure_ascii=False)
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    cur = db.execute(f"UPDATE tasks SET {sets} WHERE id = :id", {**fields, "id": task_id})
    return cur.rowcount > 0


def import_legacy_json(path: Path, user_id: int) -> int:
    """Importa el antiguo data/tasks.json (versión de un solo usuario) al usuario indicado."""
    raw = json.loads(path.read_text())
    count = 0
    for item in raw:
        if get_task(item["id"]):
            continue
        db.execute(
            "INSERT INTO tasks (id, user_id, course_name, task_title, task_description, task_type, task_url, "
            "status, created_at, due_date, available_from, ai_response, exam_questions, output_format) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item["id"], user_id, item["course_name"], item["task_title"], item["task_description"],
                item["task_type"], item["task_url"], item["status"], item["created_at"],
                item.get("due_date"), item.get("available_from"), item.get("ai_response"),
                json.dumps(item.get("exam_questions", []), ensure_ascii=False),
                item.get("output_format", "docx"),
            ),
        )
        count += 1
    logger.info(f"Importadas {count} tareas de {path.name} al usuario {user_id}")
    return count


# ─── Public API ───────────────────────────────────────────────────────────────

def add_task(
    user_id: int,
    course_name: str,
    task_title: str,
    task_description: str,
    task_type: TaskType,
    task_url: str,
    status: TaskStatus = "pending_approval",
    due_date: str | None = None,
    available_from: str | None = None,
    ai_response: str | None = None,
    exam_questions: list[ExamQuestion] | None = None,
    output_format: str = "docx",
) -> PendingTask:
    task_id = str(uuid.uuid4())[:8]
    db.execute(
        "INSERT INTO tasks (id, user_id, course_name, task_title, task_description, task_type, task_url, "
        "status, created_at, due_date, available_from, ai_response, exam_questions, output_format) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id, user_id, course_name, task_title, task_description, task_type, task_url, status,
            datetime.now().isoformat(), due_date, available_from, ai_response,
            json.dumps([asdict(q) for q in exam_questions or []], ensure_ascii=False), output_format,
        ),
    )
    return get_task(task_id)


def get_task(task_id: str, user_id: int | None = None) -> PendingTask | None:
    """Con user_id, solo devuelve la tarea si pertenece a ese usuario."""
    if user_id is None:
        return _from_row(db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,)))
    return _from_row(db.query_one("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)))


def get_pending_tasks(user_id: int) -> list[PendingTask]:
    rows = db.query(
        "SELECT * FROM tasks WHERE user_id = ? AND status = 'pending_approval' ORDER BY created_at",
        (user_id,),
    )
    return [_from_row(r) for r in rows]


def get_all_tasks(user_id: int) -> list[PendingTask]:
    rows = db.query("SELECT * FROM tasks WHERE user_id = ? ORDER BY created_at", (user_id,))
    return [_from_row(r) for r in rows]


def update_status(task_id: str, status: TaskStatus) -> bool:
    return _update(task_id, status=status)


def update_response(task_id: str, new_response: str) -> bool:
    return _update(task_id, ai_response=new_response)


def update_format(task_id: str, output_format: str) -> bool:
    return _update(task_id, output_format=output_format)


def update_description(task_id: str, description: str) -> bool:
    return _update(task_id, task_description=description)


def update_exam_questions(task_id: str, questions: list[ExamQuestion], description: str) -> bool:
    return _update(
        task_id,
        exam_questions=questions,
        task_description=description,
        status="pending_approval",
        available_from=None,
    )


def is_duplicate(user_id: int, course_name: str, task_title: str, task_url: str = "") -> bool:
    row = db.query_one(
        "SELECT 1 FROM tasks WHERE user_id = ? AND ((? != '' AND task_url = ?) OR (course_name = ? AND task_title = ?))",
        (user_id, task_url, task_url, course_name, task_title),
    )
    return row is not None
