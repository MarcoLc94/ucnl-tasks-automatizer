import os
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from bot import config as cfg_module
from bot.scheduler import scheduler
from bot.logger import logger
from bot import state as st
from bot.ai import generate_assignment_response, analyze_exam
from bot.browser import run_scan, submit_assignment, submit_quiz

# ─── Scan logic ───────────────────────────────────────────────────────────────

_scan_running = False


async def do_scan():
    global _scan_running
    if _scan_running:
        logger.warning("Escaneo ya en curso, saltando")
        return

    _scan_running = True
    logger.info("Iniciando escaneo de tareas...")
    try:
        raw_items = await run_scan()
        new_count = 0

        for item in raw_items:
            activity = item["activity"]
            course_name = activity["course"]
            task_title = activity["title"]
            task_url = activity["url"]

            if st.is_duplicate(course_name, task_title):
                logger.info(f"Ya registrada: {task_title}")
                continue

            if activity["type"] == "assignment":
                details = item["details"]
                description = details["description"]
                already_submitted = details.get("already_submitted", False)
                is_past_due = details.get("is_past_due", False)
                due_date = details.get("due_date")

                if already_submitted:
                    status = "done"
                    ai_response = None
                elif is_past_due:
                    status = "expired"
                    ai_response = None
                else:
                    status = "pending_approval"
                    ai_response = generate_assignment_response(course_name, task_title, description)

                st.add_task(
                    course_name=course_name,
                    task_title=task_title,
                    task_description=description,
                    task_type="assignment",
                    task_url=task_url,
                    status=status,
                    due_date=due_date,
                    ai_response=ai_response,
                )
                new_count += 1
                logger.info(f"Tarea registrada [{status}]: {task_title}")

            elif activity["type"] == "quiz":
                quiz = item["quiz"]
                already_completed = quiz.get("already_completed", False)
                available_from = quiz.get("available_from")
                questions_raw = quiz.get("questions")

                if already_completed:
                    status = "done"
                    exam_questions = []
                    description = "Examen ya completado"
                elif available_from:
                    status = "future"
                    exam_questions = []
                    description = f"Disponible: {available_from}"
                elif questions_raw:
                    answered = analyze_exam(course_name, questions_raw)
                    from bot.state import ExamQuestion
                    exam_questions = [
                        ExamQuestion(
                            question=q["question"],
                            options=q["options"],
                            question_type=q["type"],
                            ai_selected=q.get("selected_indices", [0]),
                        )
                        for q in answered
                    ]
                    status = "pending_approval"
                    description = f"Examen con {len(answered)} preguntas"
                else:
                    status = "expired"
                    exam_questions = []
                    description = "Examen no disponible"

                st.add_task(
                    course_name=course_name,
                    task_title=task_title,
                    task_description=description,
                    task_type="exam",
                    task_url=task_url,
                    status=status,
                    available_from=available_from,
                    exam_questions=exam_questions,
                )
                new_count += 1
                logger.info(f"Examen registrado [{status}]: {task_title}")

        logger.info(f"Escaneo completado — {new_count} nuevas tareas pendientes de aprobación")
    except Exception as e:
        logger.error(f"Error en escaneo: {e}")
    finally:
        _scan_running = False


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg_module.load()
    st.load_state()
    cfg = cfg_module.get()
    sched_cfg = cfg["scheduler"]

    scheduler.add_job(
        do_scan,
        trigger="cron",
        hour=int(os.getenv("SCAN_HOUR", sched_cfg["scan_hour"])),
        minute=int(os.getenv("SCAN_MINUTE", sched_cfg["scan_minute"])),
        timezone=sched_cfg["timezone"],
        id="daily_scan",
    )
    scheduler.start()
    logger.info(f"Bot iniciado — escaneo diario a las {sched_cfg['scan_hour']:02d}:{sched_cfg['scan_minute']:02d}")
    yield
    scheduler.shutdown()


app = FastAPI(title="UCNL Task Bot", lifespan=lifespan)


# ─── REST API ──────────────────────────────────────────────────────────────────

class ApproveRequest(BaseModel):
    edited_response: str | None = None


@app.get("/api/pending")
def get_pending():
    tasks = st.get_pending_tasks()
    return [_task_to_dict(t) for t in tasks]


@app.get("/api/tasks")
def get_all():
    return [_task_to_dict(t) for t in st.get_all_tasks()]


@app.post("/api/approve/{task_id}")
async def approve_task(task_id: str, body: ApproveRequest, background_tasks: BackgroundTasks):
    task = st.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    if task.status != "pending_approval":
        raise HTTPException(status_code=400, detail=f"La tarea ya está en estado: {task.status}")

    if body.edited_response and task.task_type == "assignment":
        st.update_response(task_id, body.edited_response)
        task = st.get_task(task_id)

    st.update_status(task_id, "approved")
    background_tasks.add_task(_submit_task, task_id)
    return {"status": "approved", "task_id": task_id}


@app.post("/api/reject/{task_id}")
def reject_task(task_id: str):
    task = st.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    st.update_status(task_id, "rejected")
    return {"status": "rejected", "task_id": task_id}


@app.post("/api/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    if _scan_running:
        return {"status": "already_running"}
    background_tasks.add_task(do_scan)
    return {"status": "scan_started"}


@app.get("/api/status")
def status():
    pending = st.get_pending_tasks()
    all_tasks = st.get_all_tasks()
    return {
        "scan_running": _scan_running,
        "pending_approval": len(pending),
        "total_tasks": len(all_tasks),
        "timestamp": datetime.now().isoformat(),
    }


# ─── Submit helper ─────────────────────────────────────────────────────────────

async def _submit_task(task_id: str):
    task = st.get_task(task_id)
    if not task:
        return

    try:
        if task.task_type == "assignment":
            ok = await submit_assignment(
                task.task_url,
                task.ai_response or "",
                course_name=task.course_name,
                task_title=task.task_title,
            )
        else:
            questions = [
                {
                    "question": q.question,
                    "options": q.options,
                    "type": q.question_type,
                    "selected_indices": q.ai_selected,
                }
                for q in task.exam_questions
            ]
            ok = await submit_quiz(task.task_url, questions)

        st.update_status(task_id, "submitted" if ok else "failed")
        logger.info(f"Tarea {task_id} {'entregada' if ok else 'FALLÓ al entregar'}")
    except Exception as e:
        logger.error(f"Error al entregar tarea {task_id}: {e}")
        st.update_status(task_id, "failed")


# ─── HTML UI ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def ui():
    return HTMLResponse(_render_ui())


def _task_to_dict(task: st.PendingTask) -> dict:
    return {
        "id": task.id,
        "course_name": task.course_name,
        "task_title": task.task_title,
        "task_description": task.task_description,
        "task_type": task.task_type,
        "task_url": task.task_url,
        "status": task.status,
        "created_at": task.created_at.strftime("%d/%m/%Y %H:%M"),
        "due_date": task.due_date,
        "available_from": task.available_from,
        "ai_response": task.ai_response,
        "exam_questions": [
            {
                "question": q.question,
                "options": q.options,
                "question_type": q.question_type,
                "ai_selected": q.ai_selected,
            }
            for q in task.exam_questions
        ],
    }


def _render_ui() -> str:
    return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>UCNL Task Bot</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  .status-pending { @apply bg-yellow-100 text-yellow-800; }
  textarea { font-family: inherit; }
</style>
</head>
<body class="bg-gray-50 min-h-screen p-6">
<div class="max-w-4xl mx-auto">

  <div class="flex items-center justify-between mb-6">
    <h1 class="text-2xl font-bold text-gray-800">UCNL Task Bot</h1>
    <div class="flex gap-3">
      <span id="status-badge" class="text-sm bg-gray-200 text-gray-700 px-3 py-1 rounded-full">Cargando...</span>
      <button onclick="triggerScan()" class="bg-blue-600 text-white text-sm px-4 py-1.5 rounded-lg hover:bg-blue-700">
        Escanear ahora
      </button>
      <button onclick="loadTasks()" class="bg-gray-200 text-gray-700 text-sm px-4 py-1.5 rounded-lg hover:bg-gray-300">
        Actualizar
      </button>
    </div>
  </div>

  <div id="tasks-container" class="space-y-4">
    <p class="text-gray-500 text-center py-8">Cargando tareas...</p>
  </div>

</div>

<script>
async function loadStatus() {
  const r = await fetch('/api/status');
  const data = await r.json();
  const badge = document.getElementById('status-badge');
  badge.textContent = data.scan_running
    ? 'Escaneando...'
    : `${data.pending_approval} pendientes | ${data.total_tasks} total`;
  badge.className = data.scan_running
    ? 'text-sm bg-blue-100 text-blue-700 px-3 py-1 rounded-full animate-pulse'
    : 'text-sm bg-gray-200 text-gray-700 px-3 py-1 rounded-full';
}

async function loadTasks() {
  const r = await fetch('/api/tasks');
  const tasks = await r.json();
  const container = document.getElementById('tasks-container');

  if (!tasks.length) {
    container.innerHTML = '<p class="text-gray-400 text-center py-10">No hay tareas registradas. Usa "Escanear ahora" para buscar actividades.</p>';
    return;
  }

  // Group by course
  const courses = {};
  for (const t of tasks) {
    if (!courses[t.course_name]) courses[t.course_name] = [];
    courses[t.course_name].push(t);
  }

  container.innerHTML = Object.entries(courses).map(([course, items]) => {
    const pending = items.filter(t => t.status === 'pending_approval').length;
    const badge = pending > 0
      ? `<span class="ml-2 text-xs bg-yellow-100 text-yellow-800 border border-yellow-300 px-2 py-0.5 rounded-full">${pending} pendiente${pending > 1 ? 's' : ''}</span>`
      : '';
    return `
      <div class="bg-white rounded-xl border shadow-sm overflow-hidden">
        <button onclick="toggleCourse(this)" class="w-full flex items-center justify-between px-5 py-4 hover:bg-gray-50 transition-colors">
          <div class="flex items-center gap-2">
            <span class="text-base font-semibold text-gray-800">${course}</span>
            ${badge}
          </div>
          <div class="flex items-center gap-3">
            <span class="text-xs text-gray-400">${items.length} actividad${items.length > 1 ? 'es' : ''}</span>
            <svg class="chevron w-4 h-4 text-gray-400 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
            </svg>
          </div>
        </button>
        <div class="course-body border-t divide-y">
          ${items.map(renderTask).join('')}
        </div>
      </div>
    `;
  }).join('');
}

function toggleCourse(btn) {
  const body = btn.nextElementSibling;
  const chevron = btn.querySelector('.chevron');
  const hidden = body.style.display === 'none';
  body.style.display = hidden ? 'block' : 'none';
  chevron.style.transform = hidden ? 'rotate(0deg)' : 'rotate(-90deg)';
}

function renderTask(t) {
  const statusColors = {
    pending_approval: 'bg-yellow-100 border-yellow-300 text-yellow-800',
    approved:         'bg-blue-50 border-blue-300 text-blue-800',
    submitted:        'bg-green-50 border-green-300 text-green-800',
    rejected:         'bg-red-50 border-red-300 text-red-700',
    failed:           'bg-red-100 border-red-400 text-red-900',
    expired:          'bg-gray-100 border-gray-300 text-gray-500',
    future:           'bg-purple-50 border-purple-300 text-purple-700',
    done:             'bg-green-50 border-green-200 text-green-700',
  };
  const statusLabel = {
    pending_approval: 'Pendiente de aprobación',
    approved:         'Aprobada — entregando...',
    submitted:        'Entregada por el bot',
    rejected:         'Rechazada',
    failed:           'Error al entregar',
    expired:          'Vencida',
    future:           'Próximamente',
    done:             'Ya entregada',
  };
  const typeLabel = { assignment: 'Tarea', exam: 'Examen' };
  const typeColor = { assignment: 'bg-indigo-50 text-indigo-700', exam: 'bg-purple-50 text-purple-700' };
  const color = statusColors[t.status] || 'bg-gray-100';

  let body = '';

  if (t.task_type === 'assignment') {
    body = `
      <div class="mt-3">
        <p class="text-xs font-semibold text-gray-500 mb-1">DESCRIPCIÓN</p>
        <p class="text-sm text-gray-700 bg-gray-50 rounded p-2 whitespace-pre-wrap">${t.task_description}</p>
      </div>
      <div class="mt-3">
        <p class="text-xs font-semibold text-gray-500 mb-1">RESPUESTA GENERADA POR IA</p>
        <textarea id="resp-${t.id}" rows="6"
          class="w-full text-sm border rounded p-2 text-gray-800 ${t.status !== 'pending_approval' ? 'bg-gray-100' : ''}"
          ${t.status !== 'pending_approval' ? 'disabled' : ''}>${t.ai_response || ''}</textarea>
      </div>
    `;
  } else {
    const questions = t.exam_questions.map((q, i) => {
      const opts = q.options.map((opt, j) => {
        const sel = q.ai_selected.includes(j) ? 'font-semibold text-blue-700' : '';
        const mark = q.ai_selected.includes(j) ? '✓' : '○';
        return `<li class="text-sm ${sel}">${mark} ${opt}</li>`;
      }).join('');
      return `
        <div class="mb-3">
          <p class="text-sm font-medium text-gray-800">${i+1}. ${q.question}</p>
          <ul class="mt-1 ml-4 space-y-0.5">${opts}</ul>
        </div>
      `;
    }).join('');
    body = `
      <div class="mt-3">
        <p class="text-xs font-semibold text-gray-500 mb-2">PREGUNTAS (respuestas marcadas con ✓)</p>
        <div class="bg-gray-50 rounded p-3 max-h-64 overflow-y-auto">${questions}</div>
      </div>
    `;
  }

  const canAct = t.status === 'pending_approval';
  const actions = canAct ? `
    <div class="mt-4 flex gap-3">
      <button onclick="approve('${t.id}', '${t.task_type}')"
        class="bg-green-600 text-white text-sm px-5 py-2 rounded-lg hover:bg-green-700 font-medium">
        Aprobar y entregar
      </button>
      <button onclick="reject('${t.id}')"
        class="bg-red-100 text-red-700 text-sm px-5 py-2 rounded-lg hover:bg-red-200 font-medium">
        Rechazar
      </button>
    </div>
  ` : '';

  return `
    <div class="px-5 py-4">
      <div class="flex items-start justify-between gap-4">
        <div>
          <div class="flex items-center gap-2 mb-1">
            <span class="text-xs px-2 py-0.5 rounded ${typeColor[t.task_type] || 'bg-gray-100 text-gray-600'}">${typeLabel[t.task_type] || t.task_type}</span>
            <span class="text-xs text-gray-400">${t.created_at}</span>
          </div>
          <h3 class="text-sm font-semibold text-gray-900">${t.task_title}</h3>
          <div class="flex flex-wrap items-center gap-2 mt-1">
            <span class="text-xs px-2 py-0.5 rounded-full border ${color}">
              ${statusLabel[t.status] || t.status}
            </span>
            ${t.due_date ? `<span class="text-xs text-gray-500">Fecha límite: <strong>${t.due_date}</strong></span>` : ''}
            ${t.available_from ? `<span class="text-xs text-purple-600">Disponible: ${t.available_from}</span>` : ''}
          </div>
        </div>
        <a href="${t.task_url}" target="_blank" class="text-xs text-blue-600 hover:underline shrink-0">Ver en UCNL ↗</a>
      </div>
      ${body}
      ${actions}
    </div>
  `;
}

async function approve(taskId, type) {
  let body = {};
  if (type === 'assignment') {
    const ta = document.getElementById(`resp-${taskId}`);
    body.edited_response = ta ? ta.value : null;
  }
  await fetch(`/api/approve/${taskId}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  loadTasks();
  loadStatus();
}

async function reject(taskId) {
  await fetch(`/api/reject/${taskId}`, { method: 'POST' });
  loadTasks();
  loadStatus();
}

async function triggerScan() {
  const r = await fetch('/api/scan', { method: 'POST' });
  const d = await r.json();
  alert(d.status === 'scan_started' ? 'Escaneo iniciado. Actualiza en unos minutos.' : 'Ya hay un escaneo en curso.');
  loadStatus();
}

loadStatus();
loadTasks();
setInterval(() => { loadStatus(); }, 10000);
</script>
</body>
</html>"""
