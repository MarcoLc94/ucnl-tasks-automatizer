import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Literal
import uuid

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

_DB_PATH = Path(__file__).parent.parent / "data" / "tasks.json"


@dataclass
class ExamQuestion:
    question: str
    options: list[str]
    question_type: Literal["single", "multiple"]
    ai_selected: list[int]


@dataclass
class PendingTask:
    id: str
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


_tasks: dict[str, PendingTask] = {}


# ─── Persistence ──────────────────────────────────────────────────────────────

def _load():
    if not _DB_PATH.exists():
        return
    try:
        raw = json.loads(_DB_PATH.read_text())
        for item in raw:
            item["created_at"] = datetime.fromisoformat(item["created_at"])
            item["exam_questions"] = [ExamQuestion(**q) for q in item.get("exam_questions", [])]
            t = PendingTask(**item)
            _tasks[t.id] = t
        logger.info(f"Estado cargado: {len(_tasks)} tareas")
    except Exception as e:
        logger.error(f"Error cargando estado: {e}")


def _persist():
    try:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for t in _tasks.values():
            d = asdict(t)
            d["created_at"] = t.created_at.isoformat()
            data.append(d)
        _DB_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.error(f"Error guardando estado: {e}")


# ─── Public API ───────────────────────────────────────────────────────────────

def load_state():
    _load()


def add_task(
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
) -> PendingTask:
    task_id = str(uuid.uuid4())[:8]
    task = PendingTask(
        id=task_id,
        course_name=course_name,
        task_title=task_title,
        task_description=task_description,
        task_type=task_type,
        task_url=task_url,
        status=status,
        created_at=datetime.now(),
        due_date=due_date,
        available_from=available_from,
        ai_response=ai_response,
        exam_questions=exam_questions or [],
    )
    _tasks[task_id] = task
    _persist()
    return task


def get_task(task_id: str) -> PendingTask | None:
    return _tasks.get(task_id)


def get_pending_tasks() -> list[PendingTask]:
    return [t for t in _tasks.values() if t.status == "pending_approval"]


def get_all_tasks() -> list[PendingTask]:
    return list(_tasks.values())


def update_status(task_id: str, status: TaskStatus) -> bool:
    task = _tasks.get(task_id)
    if not task:
        return False
    task.status = status
    _persist()
    return True


def update_response(task_id: str, new_response: str) -> bool:
    task = _tasks.get(task_id)
    if not task:
        return False
    task.ai_response = new_response
    _persist()
    return True


def is_duplicate(course_name: str, task_title: str) -> bool:
    for task in _tasks.values():
        if task.course_name == course_name and task.task_title == task_title:
            return True
    return False
