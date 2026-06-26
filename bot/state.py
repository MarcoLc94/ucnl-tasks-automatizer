from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
import uuid


TaskType = Literal["assignment", "exam"]
TaskStatus = Literal["pending_approval", "approved", "rejected", "submitted", "failed"]


@dataclass
class ExamQuestion:
    question: str
    options: list[str]
    question_type: Literal["single", "multiple"]
    ai_selected: list[int]  # indices of selected options


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

    # assignment fields
    ai_response: str | None = None

    # exam fields
    exam_questions: list[ExamQuestion] = field(default_factory=list)


# in-memory store: task_id -> PendingTask
_tasks: dict[str, PendingTask] = {}


def add_task(
    course_name: str,
    task_title: str,
    task_description: str,
    task_type: TaskType,
    task_url: str,
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
        status="pending_approval",
        created_at=datetime.now(),
        ai_response=ai_response,
        exam_questions=exam_questions or [],
    )
    _tasks[task_id] = task
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
    return True


def update_response(task_id: str, new_response: str) -> bool:
    task = _tasks.get(task_id)
    if not task:
        return False
    task.ai_response = new_response
    return True


def is_duplicate(course_name: str, task_title: str) -> bool:
    for task in _tasks.values():
        if task.course_name == course_name and task.task_title == task_title:
            return True
    return False
