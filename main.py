import json
import time
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, Request, Response
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

from bot import config as cfg_module
from bot.scheduler import scheduler
from bot.logger import logger
from bot import state as st
from bot import users
from bot.security import check_secret_key
from bot.ai import generate_assignment_response
from bot.browser import run_scan, submit_assignment, submit_quiz, browser_session, _login
from bot.renderers import FORMATS, detect_format, build_document, to_pdf_for_preview
from bot.dates import status_from_dates

SESSION_COOKIE = "ucnl_session"

# ─── Scan logic ───────────────────────────────────────────────────────────────

_scans_running: set[int] = set()


async def do_scan(user_id: int):
    user = users.get_user(user_id)
    if not user:
        return
    if user_id in _scans_running:
        logger.warning(f"[{user.username}] Escaneo ya en curso, saltando")
        return
    if not user.has_ucnl_credentials:
        logger.warning(f"[{user.username}] Sin credenciales de UCNL — escaneo omitido")
        return

    _scans_running.add(user_id)
    logger.info(f"[{user.username}] Iniciando escaneo de tareas...")
    try:
        scan = await run_scan(user)
        new_count = 0

        for item in scan["items"]:
            try:
                if await _register_item(user, item):
                    new_count += 1
            except Exception as e:
                # Un error en una actividad no debe tirar el resto del escaneo
                logger.error(f"[{user.username}] Error registrando '{item['activity']['title']}': {e}")

        if scan["courses"]:
            removed = st.delete_tasks_outside_courses(user_id, scan["courses"])
            if removed:
                logger.info(f"[{user.username}] {removed} tareas de materias anteriores eliminadas")

        logger.info(f"[{user.username}] Escaneo completado — {new_count} nuevas tareas")
    except Exception as e:
        logger.error(f"[{user.username}] Error en escaneo: {e}")
    finally:
        _scans_running.discard(user_id)


def _compute_status(kind: str, data: dict) -> str:
    """Estado de una actividad según lo que se vio en UCNL y sus fechas de apertura/cierre."""
    if kind == "assignment":
        if data.get("already_submitted"):
            return "done"
        status = status_from_dates(data.get("opens_at"), data.get("closes_at"))
        if status == "pending_approval" and data.get("is_past_due"):
            return "expired"
        return status

    if data.get("already_completed"):
        return "done"
    status = status_from_dates(data.get("opens_at"), data.get("closes_at"))
    if status == "pending_approval" and data.get("available_from"):
        return "future"  # Moodle dice "no disponible" aunque no tengamos la fecha
    return status


# Estados que un escaneo puede recalcular (los demás son decisiones o resultados definitivos)
_REFRESHABLE = {"future", "pending_approval", "expired"}

_QUIZ_DESCRIPTIONS = {
    "done": "Examen ya completado",
    "future": "Aún no abre",
    "expired": "Ya cerró",
    "pending_approval": "Examen disponible — el bot responderá cada pregunta en tiempo real al aprobar",
}


async def _register_item(user: users.User, item: dict) -> bool:
    """Registra una actividad escaneada o actualiza sus fechas/estado. Devuelve True si era nueva."""
    activity = item["activity"]
    course_name = activity["course"]
    task_title = activity["title"]
    task_url = activity["url"]
    kind = activity["type"]
    if kind not in ("assignment", "quiz"):
        return False

    data = item["details"] if kind == "assignment" else item["quiz"]
    status = _compute_status(kind, data)
    opens_at, closes_at = data.get("opens_at"), data.get("closes_at")

    course_grade, teacher = activity.get("course_grade", ""), activity.get("teacher", "")

    existing = st.find_existing(user.id, course_name, task_title, task_url)
    if existing:
        st.update_dates(existing.id, opens_at, closes_at)
        st.update_course_info(existing.id, course_grade, teacher)
        if existing.status in _REFRESHABLE and existing.status != status:
            st.update_status(existing.id, status)
            if kind == "quiz":
                st.update_description(existing.id, _QUIZ_DESCRIPTIONS[status])
            logger.info(f"[{user.username}] '{task_title}': {existing.status} → {status}")
        if (kind == "assignment" and status in ("pending_approval", "future")
                and not (existing.ai_response or "").strip()):
            await _generate_response(user, existing.id, course_name, task_title, existing.task_description, existing.output_format)
        return False

    if kind == "assignment":
        description = data["description"]
        output_format = detect_format(task_title, description)
        task = st.add_task(
            user_id=user.id,
            course_name=course_name,
            task_title=task_title,
            task_description=description,
            task_type="assignment",
            task_url=task_url,
            status=status,
            due_date=data.get("due_date"),
            output_format=output_format,
            opens_at=opens_at,
            closes_at=closes_at,
            course_grade=course_grade,
            teacher=teacher,
        )
        # Se genera también para las que aún no abren, así ya están listas para revisar
        if status in ("pending_approval", "future"):
            await _generate_response(user, task.id, course_name, task_title, description, output_format)
        logger.info(f"[{user.username}] Tarea registrada [{status}] ({output_format}): {task_title}")
        return True

    st.add_task(
        user_id=user.id,
        course_name=course_name,
        task_title=task_title,
        task_description=_QUIZ_DESCRIPTIONS[status],
        task_type="exam",
        task_url=task_url,
        status=status,
        opens_at=opens_at,
        closes_at=closes_at,
        course_grade=course_grade,
        teacher=teacher,
    )
    logger.info(f"[{user.username}] Examen registrado [{status}]: {task_title}")
    return True


async def _generate_response(user, task_id, course_name, task_title, description, output_format):
    try:
        response = await run_in_threadpool(
            generate_assignment_response, course_name, task_title, description, output_format
        )
        st.update_response(task_id, response)
    except Exception as e:
        # Se queda vacía; desde el panel se puede usar "Regenerar"
        logger.error(f"[{user.username}] No se pudo generar la respuesta de '{task_title}': {e}")


def schedule_user_scan(user: users.User):
    """Crea o reprograma el escaneo diario de un usuario."""
    scheduler.add_job(
        do_scan,
        trigger="cron",
        args=[user.id],
        hour=user.scan_hour,
        minute=user.scan_minute,
        timezone=cfg_module.get()["scheduler"]["timezone"],
        id=f"scan_{user.id}",
        replace_existing=True,
    )


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg_module.load()
    check_secret_key()
    all_users = users.list_users()
    for user in all_users:
        schedule_user_scan(user)
    scheduler.start()
    if not all_users:
        logger.warning("No hay usuarios — crea uno con: python manage.py add-user <usuario>")
    for user in all_users:
        logger.info(f"[{user.username}] Escaneo diario a las {user.scan_hour:02d}:{user.scan_minute:02d}")
    yield
    scheduler.shutdown()


app = FastAPI(title="UCNL Task Bot", lifespan=lifespan)


# ─── Auth ──────────────────────────────────────────────────────────────────────

_LOGIN_WINDOW = 15 * 60
_LOGIN_MAX_FAILS = 5
_login_fails: dict[str, list[float]] = {}


def current_user(request: Request) -> users.User:
    user = users.user_from_session(request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    return user


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginRequest, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    now = time.time()
    fails = [t for t in _login_fails.get(ip, []) if now - t < _LOGIN_WINDOW]
    if len(fails) >= _LOGIN_MAX_FAILS:
        raise HTTPException(status_code=429, detail="Demasiados intentos. Espera unos minutos.")

    user = users.authenticate(body.username, body.password)
    if not user:
        _login_fails[ip] = fails + [now]
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

    _login_fails.pop(ip, None)
    response.set_cookie(
        SESSION_COOKIE,
        users.create_session(user.id),
        max_age=users.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return {"username": user.username}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    users.delete_session(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "ok"}


def _user_to_dict(user: users.User) -> dict:
    return {
        "username": user.username,
        "display_name": user.display_name,
        "career": user.career,
        "ucnl_username": user.ucnl_username,
        "has_ucnl_password": bool(user.ucnl_password_enc),
        "scan_hour": user.scan_hour,
        "scan_minute": user.scan_minute,
    }


class ProfileRequest(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=120)
    career: str | None = Field(None, min_length=1, max_length=160)
    ucnl_username: str | None = Field(None, max_length=120)
    ucnl_password: str | None = Field(None, max_length=200)
    scan_hour: int | None = Field(None, ge=0, le=23)
    scan_minute: int | None = Field(None, ge=0, le=59)
    current_password: str | None = None
    new_password: str | None = Field(None, min_length=8, max_length=200)


@app.get("/api/me")
def get_me(user: users.User = Depends(current_user)):
    return _user_to_dict(user)


@app.put("/api/me")
def update_me(body: ProfileRequest, user: users.User = Depends(current_user)):
    if body.new_password:
        if not body.current_password or not users.authenticate(user.username, body.current_password):
            raise HTTPException(status_code=400, detail="La contraseña actual no es correcta")

    updated = users.update_profile(
        user.id,
        display_name=body.display_name,
        career=body.career,
        ucnl_username=body.ucnl_username,
        ucnl_password=body.ucnl_password or None,
        scan_hour=body.scan_hour,
        scan_minute=body.scan_minute,
    )
    if body.scan_hour is not None or body.scan_minute is not None:
        schedule_user_scan(updated)
    if body.new_password:
        users.set_password(user.id, body.new_password)  # cierra todas las sesiones
    return _user_to_dict(updated)


# ─── REST API ──────────────────────────────────────────────────────────────────

class ApproveRequest(BaseModel):
    edited_response: str | None = None
    output_format: str | None = None


class DocumentRequest(BaseModel):
    text: str | None = None
    output_format: str | None = None
    view: bool = False  # True: devolver PDF para verlo en el panel (convierte Word/PowerPoint si se puede)


def _validate_format(output_format: str | None) -> None:
    if output_format and output_format not in FORMATS:
        raise HTTPException(status_code=400, detail=f"Formato no válido: {output_format}")


def _own_task(task_id: str, user: users.User) -> st.PendingTask:
    task = st.get_task(task_id, user.id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    return task


@app.get("/api/pending")
def get_pending(user: users.User = Depends(current_user)):
    return [_task_to_dict(t) for t in st.get_pending_tasks(user.id)]


@app.get("/api/tasks")
def get_all(user: users.User = Depends(current_user)):
    return [_task_to_dict(t) for t in st.get_all_tasks(user.id)]


@app.post("/api/approve/{task_id}")
async def approve_task(
    task_id: str, body: ApproveRequest, background_tasks: BackgroundTasks,
    user: users.User = Depends(current_user),
):
    task = _own_task(task_id, user)
    if task.status != "pending_approval":
        raise HTTPException(status_code=400, detail=f"La tarea ya está en estado: {task.status}")
    window = status_from_dates(task.opens_at, task.closes_at)
    if window != "pending_approval":
        st.update_status(task_id, window)
        raise HTTPException(
            status_code=400,
            detail="Esta actividad todavía no abre" if window == "future" else "Esta actividad ya cerró",
        )

    _validate_format(body.output_format)
    if body.edited_response and task.task_type == "assignment":
        st.update_response(task_id, body.edited_response)
    if body.output_format and task.task_type == "assignment":
        st.update_format(task_id, body.output_format)

    if task.task_type == "assignment" and not (body.edited_response or task.ai_response or "").strip():
        raise HTTPException(status_code=400, detail="La respuesta está vacía — usa 'Regenerar' o escríbela")

    st.update_status(task_id, "approved")
    background_tasks.add_task(_submit_task, task_id)
    return {"status": "approved", "task_id": task_id}


@app.post("/api/preview/{task_id}")
async def preview_document(task_id: str, body: DocumentRequest, user: users.User = Depends(current_user)):
    """Genera el documento (con el texto y formato actuales de la UI) para revisarlo antes de entregar."""
    task = _own_task(task_id, user)
    if task.task_type != "assignment":
        raise HTTPException(status_code=400, detail="Solo aplica para tareas")
    _validate_format(body.output_format)
    try:
        path = await build_document(
            body.output_format or task.output_format,
            body.text if body.text is not None else (task.ai_response or ""),
            course_name=task.course_name,
            task_title=task.task_title,
            student_name=user.display_name,
            output_dir=user.docs_dir / "previews",
            career=user.career,
            grade=task.course_grade or "",
            teacher=task.teacher or "",
        )
    except Exception as e:
        logger.error(f"Error generando vista previa de {task_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error generando documento: {e}")
    if body.view and path.suffix != ".pdf":
        pdf = await to_pdf_for_preview(path)
        if pdf:
            return FileResponse(pdf, filename=pdf.name, content_disposition_type="inline")
    # Los PDF se muestran en el navegador; Word/PowerPoint se descargan
    disposition = "inline" if path.suffix == ".pdf" else "attachment"
    return FileResponse(path, filename=path.name, content_disposition_type=disposition)


@app.post("/api/regenerate/{task_id}")
async def regenerate_response(task_id: str, body: DocumentRequest, user: users.User = Depends(current_user)):
    """Vuelve a generar la respuesta de la IA adaptada al formato elegido."""
    task = _own_task(task_id, user)
    if task.task_type != "assignment":
        raise HTTPException(status_code=400, detail="Solo aplica para tareas")
    if task.status != "pending_approval":
        raise HTTPException(status_code=400, detail=f"La tarea ya está en estado: {task.status}")
    _validate_format(body.output_format)
    output_format = body.output_format or task.output_format
    response = await run_in_threadpool(
        generate_assignment_response,
        task.course_name, task.task_title, task.task_description, output_format,
    )
    st.update_response(task_id, response)
    st.update_format(task_id, output_format)
    return {"ai_response": response, "output_format": output_format}


@app.post("/api/reject/{task_id}")
def reject_task(task_id: str, user: users.User = Depends(current_user)):
    _own_task(task_id, user)
    st.update_status(task_id, "rejected")
    return {"status": "rejected", "task_id": task_id}


@app.post("/api/attempt/{task_id}")
async def attempt_task(task_id: str, background_tasks: BackgroundTasks, user: users.User = Depends(current_user)):
    task = _own_task(task_id, user)
    if task.task_type != "exam":
        raise HTTPException(status_code=400, detail="Solo aplica para exámenes")
    if task.status not in ("future", "failed", "rejected", "expired"):
        raise HTTPException(status_code=400, detail=f"No se puede intentar en estado: {task.status}")
    if status_from_dates(task.opens_at, None) == "future":
        raise HTTPException(status_code=400, detail="Este examen todavía no abre")
    background_tasks.add_task(_fetch_and_analyze_quiz, task_id)
    return {"status": "attempt_started", "task_id": task_id}


async def _fetch_and_analyze_quiz(task_id: str):
    """Re-check availability for exams in future/failed/rejected state."""
    from bot.browser import get_quiz_details
    task = st.get_task(task_id)
    user = users.get_user(task.user_id) if task else None
    if not task or not user:
        return
    try:
        activity = {"title": task.task_title, "url": task.task_url}
        async with browser_session(user) as (context, page):
            if not await _login(page, context, user):
                st.update_status(task_id, "failed")
                return
            quiz = await get_quiz_details(page, activity)
        st.update_dates(task_id, quiz.get("opens_at"), quiz.get("closes_at"))

        if quiz.get("already_completed"):
            grade = quiz.get("grade", "")
            desc = f"Examen completado — Calificación: {grade}" if grade else "Examen completado"
            st.update_description(task_id, desc)
            st.update_status(task_id, "done")
            logger.info(f"Examen {task_id} completado. {desc}")
        elif _compute_status("quiz", quiz) != "pending_approval":
            new_status = _compute_status("quiz", quiz)
            st.update_status(task_id, new_status)
            st.update_description(task_id, _QUIZ_DESCRIPTIONS[new_status])
            logger.info(f"Examen {task_id} no disponible: {new_status}")
        else:
            description = "Examen disponible — el bot responderá cada pregunta en tiempo real al aprobar"
            st.update_exam_questions(task_id, [], description)
            logger.info(f"Examen {task_id} disponible para aprobar")
    except Exception as e:
        logger.error(f"Error al verificar examen {task_id}: {e}")
        st.update_status(task_id, "failed")


@app.post("/api/scan")
async def trigger_scan(background_tasks: BackgroundTasks, user: users.User = Depends(current_user)):
    if user.id in _scans_running:
        return {"status": "already_running"}
    if not user.has_ucnl_credentials:
        raise HTTPException(status_code=400, detail="Primero configura tus credenciales de UCNL en 'Mi cuenta'")
    background_tasks.add_task(do_scan, user.id)
    return {"status": "scan_started"}


@app.get("/api/status")
def status(user: users.User = Depends(current_user)):
    return {
        "scan_running": user.id in _scans_running,
        "pending_approval": len(st.get_pending_tasks(user.id)),
        "total_tasks": len(st.get_all_tasks(user.id)),
        "has_ucnl_credentials": user.has_ucnl_credentials,
        "display_name": user.display_name,
        "timestamp": datetime.now().isoformat(),
    }


# ─── Submit helper ─────────────────────────────────────────────────────────────

async def _submit_task(task_id: str):
    task = st.get_task(task_id)
    user = users.get_user(task.user_id) if task else None
    if not task or not user:
        return

    try:
        if task.task_type == "assignment":
            ok = await submit_assignment(
                user,
                task.task_url,
                task.ai_response or "",
                course_name=task.course_name,
                task_title=task.task_title,
                output_format=task.output_format,
                course_grade=task.course_grade or "",
                teacher=task.teacher or "",
            )
        else:
            ok = await submit_quiz(user, task.task_url, course_name=task.course_name)

        st.update_status(task_id, "submitted" if ok else "failed")
        logger.info(f"[{user.username}] Tarea {task_id} {'entregada' if ok else 'FALLÓ al entregar'}")
    except Exception as e:
        logger.error(f"[{user.username}] Error al entregar tarea {task_id}: {e}")
        st.update_status(task_id, "failed")


# ─── HTML UI ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def ui(request: Request):
    if not users.user_from_session(request.cookies.get(SESSION_COOKIE)):
        return HTMLResponse(_render_login())
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
        "output_format": task.output_format,
        "course_grade": task.course_grade,
        "teacher": task.teacher,
        "opens_at": task.opens_at,
        "closes_at": task.closes_at,
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


def _render_login() -> str:
    return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>UCNL Task Bot</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-50 min-h-screen flex items-center justify-center p-6">
  <form id="login-form" class="bg-white border rounded-xl shadow-sm p-6 w-full max-w-sm space-y-4">
    <h1 class="text-xl font-bold text-gray-800">UCNL Task Bot</h1>
    <div>
      <label class="text-xs font-semibold text-gray-500">USUARIO</label>
      <input id="username" autocomplete="username" required class="w-full border rounded px-3 py-2 text-sm mt-1">
    </div>
    <div>
      <label class="text-xs font-semibold text-gray-500">CONTRASEÑA</label>
      <input id="password" type="password" autocomplete="current-password" required class="w-full border rounded px-3 py-2 text-sm mt-1">
    </div>
    <p id="error" class="text-sm text-red-600 hidden"></p>
    <button class="w-full bg-blue-600 text-white text-sm py-2 rounded-lg hover:bg-blue-700 font-medium">Entrar</button>
  </form>
<script>
document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const r = await fetch('/api/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      username: document.getElementById('username').value,
      password: document.getElementById('password').value,
    }),
  });
  if (r.ok) { location.reload(); return; }
  const err = document.getElementById('error');
  err.textContent = (await r.json()).detail || 'Error al iniciar sesión';
  err.classList.remove('hidden');
});
</script>
</body>
</html>"""


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
    <div>
      <h1 class="text-2xl font-bold text-gray-800">UCNL Task Bot</h1>
      <p id="user-name" class="text-sm text-gray-500"></p>
    </div>
    <div class="flex flex-wrap gap-3 justify-end">
      <span id="status-badge" class="text-sm bg-gray-200 text-gray-700 px-3 py-1 rounded-full">Cargando...</span>
      <button onclick="triggerScan()" class="bg-blue-600 text-white text-sm px-4 py-1.5 rounded-lg hover:bg-blue-700">
        Escanear ahora
      </button>
      <button onclick="loadTasks()" class="bg-gray-200 text-gray-700 text-sm px-4 py-1.5 rounded-lg hover:bg-gray-300">
        Actualizar
      </button>
      <button onclick="toggleSettings()" class="bg-gray-200 text-gray-700 text-sm px-4 py-1.5 rounded-lg hover:bg-gray-300">
        Mi cuenta
      </button>
      <button onclick="logout()" class="text-gray-500 text-sm px-2 py-1.5 hover:text-gray-800">
        Salir
      </button>
    </div>
  </div>

  <div id="creds-banner" class="hidden mb-4 bg-amber-50 border border-amber-300 text-amber-800 text-sm rounded-lg px-4 py-3">
    Aún no configuras tu usuario y contraseña de UCNL. Ve a <button onclick="toggleSettings(true)" class="underline font-medium">Mi cuenta</button> para que el bot pueda escanear tus tareas.
  </div>

  <form id="settings" class="hidden mb-6 bg-white rounded-xl border shadow-sm p-5 space-y-4" onsubmit="saveSettings(event)">
    <h2 class="text-base font-semibold text-gray-800">Mi cuenta</h2>
    <div class="grid sm:grid-cols-2 gap-4">
      <label class="block">
        <span class="text-xs font-semibold text-gray-500">NOMBRE (aparece en las portadas)</span>
        <input id="s-display" required class="w-full border rounded px-3 py-2 text-sm mt-1">
      </label>
      <label class="block">
        <span class="text-xs font-semibold text-gray-500">LICENCIATURA (aparece en las portadas)</span>
        <input id="s-career" required class="w-full border rounded px-3 py-2 text-sm mt-1">
      </label>
      <label class="block">
        <span class="text-xs font-semibold text-gray-500">HORA DEL ESCANEO DIARIO</span>
        <input id="s-time" type="time" required class="w-full border rounded px-3 py-2 text-sm mt-1">
      </label>
      <label class="block">
        <span class="text-xs font-semibold text-gray-500">USUARIO UCNL</span>
        <input id="s-ucnl-user" autocomplete="off" class="w-full border rounded px-3 py-2 text-sm mt-1">
      </label>
      <label class="block">
        <span class="text-xs font-semibold text-gray-500">CONTRASEÑA UCNL</span>
        <input id="s-ucnl-pass" type="password" autocomplete="new-password" class="w-full border rounded px-3 py-2 text-sm mt-1">
        <span id="s-ucnl-hint" class="text-xs text-gray-400"></span>
      </label>
    </div>
    <details class="text-sm">
      <summary class="cursor-pointer text-gray-600">Cambiar contraseña del panel</summary>
      <div class="grid sm:grid-cols-2 gap-4 mt-3">
        <input id="s-cur-pass" type="password" placeholder="Contraseña actual" autocomplete="current-password" class="border rounded px-3 py-2 text-sm">
        <input id="s-new-pass" type="password" placeholder="Nueva (mín. 8 caracteres)" autocomplete="new-password" class="border rounded px-3 py-2 text-sm">
      </div>
    </details>
    <p id="s-msg" class="text-sm hidden"></p>
    <div class="flex gap-3">
      <button class="bg-blue-600 text-white text-sm px-5 py-2 rounded-lg hover:bg-blue-700 font-medium">Guardar</button>
      <button type="button" onclick="toggleSettings(false)" class="bg-gray-100 text-gray-700 text-sm px-5 py-2 rounded-lg hover:bg-gray-200">Cerrar</button>
    </div>
  </form>

  <div id="tasks-container" class="space-y-4">
    <p class="text-gray-500 text-center py-8">Cargando tareas...</p>
  </div>

</div>

<script>
const FORMATS = """ + json.dumps(FORMATS, ensure_ascii=False) + """;

// Si la sesión expiró, volver al login
const _fetch = window.fetch;
window.fetch = async (...args) => {
  const r = await _fetch(...args);
  if (r.status === 401) location.reload();
  return r;
};

let _wasScanning = false;

async function loadStatus() {
  const r = await fetch('/api/status');
  const data = await r.json();
  // Al terminar un escaneo, recargar la lista para no mostrar datos a medias
  if (_wasScanning && !data.scan_running) loadTasks();
  _wasScanning = data.scan_running;
  document.getElementById('user-name').textContent = data.display_name;
  document.getElementById('creds-banner').classList.toggle('hidden', data.has_ucnl_credentials);
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

  // Ordenar por fecha de cierre (las que cierran antes primero; sin fecha al final)
  tasks.sort((a, b) => (a.closes_at || '9999').localeCompare(b.closes_at || '9999'));

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
            ${items[0].course_grade || items[0].teacher ? `<span class="text-xs text-gray-500">${[items[0].course_grade, items[0].teacher].filter(Boolean).join(' · ')}</span>` : ''}
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

function fmtDate(iso) {
  return new Date(iso).toLocaleString('es-MX', {
    weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}

function relative(iso) {
  const days = Math.round((new Date(iso) - new Date()) / 86400000);
  if (days === 0) return 'hoy';
  if (days === 1) return 'mañana';
  if (days === -1) return 'ayer';
  return days > 0 ? `en ${days} días` : `hace ${-days} días`;
}

function renderDates(t) {
  const parts = [];
  const now = new Date();
  if (t.opens_at && new Date(t.opens_at) > now) {
    parts.push(`<span class="text-xs text-purple-700">Abre: <strong>${fmtDate(t.opens_at)}</strong> (${relative(t.opens_at)})</span>`);
  }
  if (t.closes_at) {
    const soon = new Date(t.closes_at) - now < 3 * 86400000 && new Date(t.closes_at) > now;
    parts.push(`<span class="text-xs ${soon ? 'text-red-700' : 'text-gray-600'}">Cierra: <strong>${fmtDate(t.closes_at)}</strong> (${relative(t.closes_at)})</span>`);
  }
  if (!parts.length && t.due_date) {
    parts.push(`<span class="text-xs text-gray-500">Fecha límite: <strong>${t.due_date}</strong></span>`);
  }
  return parts.join('');
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
      <div class="mt-3 flex flex-wrap items-center gap-2">
        <p class="text-xs font-semibold text-gray-500">FORMATO DE ENTREGA</p>
        <select id="fmt-${t.id}" class="text-sm border rounded px-2 py-1"
          ${t.status !== 'pending_approval' ? 'disabled' : ''}>
          ${Object.entries(FORMATS).map(([k, v]) =>
            `<option value="${k}" ${k === (t.output_format || 'docx') ? 'selected' : ''}>${v}</option>`).join('')}
        </select>
        <button onclick="previewDoc('${t.id}')" id="prev-${t.id}"
          class="text-sm bg-gray-100 text-gray-700 px-3 py-1 rounded hover:bg-gray-200">Vista previa</button>
        ${t.status === 'pending_approval' ? `
        <button onclick="regenerate('${t.id}')" id="regen-${t.id}"
          class="text-sm bg-indigo-50 text-indigo-700 px-3 py-1 rounded hover:bg-indigo-100">Regenerar para este formato</button>` : ''}
      </div>
      <div class="mt-3">
        <p class="text-xs font-semibold text-gray-500 mb-1">RESPUESTA GENERADA POR IA
          <span class="font-normal normal-case text-gray-400">— Markdown: ## secciones, - viñetas, | tablas |, bloques de gráfica</span></p>
        <textarea id="resp-${t.id}" rows="12"
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
    body = questions ? `
      <div class="mt-3">
        <p class="text-xs font-semibold text-gray-500 mb-2">PREGUNTAS (respuestas marcadas con ✓)</p>
        <div class="bg-gray-50 rounded p-3 max-h-64 overflow-y-auto">${questions}</div>
      </div>
    ` : `
      <p class="mt-3 text-sm text-gray-600 bg-gray-50 rounded p-3">${t.task_description}</p>
      ${t.status === 'pending_approval' ? `
      <p class="mt-2 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2">
        Al aprobar, el bot abre el examen en UCNL, contesta cada pregunta con IA y <strong>envía el intento</strong>.
        Cuenta como tu calificación y no se puede deshacer.
      </p>` : ''}
    `;
  }

  const canAct = t.status === 'pending_approval';
  const notOpenYet = t.opens_at && new Date(t.opens_at) > new Date();
  const canAttempt = t.task_type === 'exam' && !notOpenYet && ['future', 'failed', 'rejected', 'expired'].includes(t.status);
  const actions = canAct ? `
    <div class="mt-4 flex gap-3">
      <button onclick="approve('${t.id}', '${t.task_type}')"
        class="bg-green-600 text-white text-sm px-5 py-2 rounded-lg hover:bg-green-700 font-medium">
        ${t.task_type === 'exam' ? 'Contestar y enviar examen' : 'Aprobar y entregar'}
      </button>
      <button onclick="reject('${t.id}')"
        class="bg-red-100 text-red-700 text-sm px-5 py-2 rounded-lg hover:bg-red-200 font-medium">
        Rechazar
      </button>
    </div>
  ` : canAttempt ? `
    <div class="mt-4 flex gap-3">
      <button onclick="attemptExam('${t.id}')"
        class="bg-purple-600 text-white text-sm px-5 py-2 rounded-lg hover:bg-purple-700 font-medium">
        Volver a verificar examen
      </button>
    </div>
  ` : '';

  return `
    <div class="px-5 py-4">
      <div class="flex items-start justify-between gap-4">
        <div>
          <div class="flex items-center gap-2 mb-1">
            <span class="text-xs px-2 py-0.5 rounded ${typeColor[t.task_type] || 'bg-gray-100 text-gray-600'}">${typeLabel[t.task_type] || t.task_type}</span>
            <span class="text-xs text-gray-400" title="Registrada por el bot">${t.created_at}</span>
          </div>
          <h3 class="text-sm font-semibold text-gray-900">${t.task_title}</h3>
          <div class="flex flex-wrap items-center gap-2 mt-1">
            <span class="text-xs px-2 py-0.5 rounded-full border ${color}">
              ${statusLabel[t.status] || t.status}
            </span>
            ${renderDates(t)}
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
  if (type === 'exam' && !confirm('El bot va a contestar y ENVIAR este examen en UCNL. Cuenta como tu calificación y no se puede deshacer. ¿Continuar?')) return;
  let body = {};
  if (type === 'assignment') {
    const ta = document.getElementById(`resp-${taskId}`);
    body.edited_response = ta ? ta.value : null;
    const fmt = document.getElementById(`fmt-${taskId}`);
    body.output_format = fmt ? fmt.value : null;
  }
  await fetch(`/api/approve/${taskId}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  loadTasks();
  loadStatus();
}

async function previewDoc(taskId, download = false) {
  const btn = document.getElementById(`prev-${taskId}`);
  const ta = document.getElementById(`resp-${taskId}`);
  const fmt = document.getElementById(`fmt-${taskId}`);
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Generando...';
  try {
    const r = await fetch(`/api/preview/${taskId}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        text: ta ? ta.value : null,
        output_format: fmt ? fmt.value : null,
        view: !download,
      }),
    });
    if (!r.ok) { alert('Error: ' + (await r.text())); return; }
    const blob = await r.blob();
    const name = (r.headers.get('content-disposition') || '').match(/filename[*]?=(?:UTF-8'')?"?([^";]+)/i);
    const filename = name ? decodeURIComponent(name[1]) : 'vista_previa';
    if (!download && blob.type === 'application/pdf') {
      openPreviewModal(taskId, URL.createObjectURL(blob), fmt ? fmt.options[fmt.selectedIndex].text : '');
      return;
    }
    if (!download) {
      alert('Para ver Word/PowerPoint aquí mismo instala LibreOffice. Por ahora se descargará el archivo.');
    }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

function openPreviewModal(taskId, url, formatName) {
  closePreviewModal();
  const modal = document.createElement('div');
  modal.id = 'preview-modal';
  modal.className = 'fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4';
  modal.innerHTML = `
    <div class="bg-white rounded-xl shadow-xl w-full max-w-5xl h-full flex flex-col overflow-hidden">
      <div class="flex items-center justify-between gap-3 px-4 py-2 border-b">
        <p class="text-sm font-semibold text-gray-700">Vista previa — ${formatName}</p>
        <div class="flex gap-2">
          <button onclick="previewDoc('${taskId}', true)" class="text-sm bg-gray-100 text-gray-700 px-3 py-1 rounded hover:bg-gray-200">Descargar archivo</button>
          <button onclick="closePreviewModal()" class="text-sm bg-gray-800 text-white px-3 py-1 rounded hover:bg-gray-700">Cerrar</button>
        </div>
      </div>
      <iframe src="${url}" class="flex-1 w-full" title="Vista previa"></iframe>
    </div>`;
  modal.addEventListener('click', (e) => { if (e.target === modal) closePreviewModal(); });
  modal.dataset.url = url;
  document.body.appendChild(modal);
}

function closePreviewModal() {
  const modal = document.getElementById('preview-modal');
  if (!modal) return;
  URL.revokeObjectURL(modal.dataset.url);
  modal.remove();
}

document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePreviewModal(); });

async function regenerate(taskId) {
  const btn = document.getElementById(`regen-${taskId}`);
  const fmt = document.getElementById(`fmt-${taskId}`);
  btn.disabled = true; btn.textContent = 'Regenerando...';
  try {
    const r = await fetch(`/api/regenerate/${taskId}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ output_format: fmt.value }),
    });
    if (!r.ok) { alert('Error: ' + (await r.text())); return; }
    const d = await r.json();
    document.getElementById(`resp-${taskId}`).value = d.ai_response;
  } finally {
    btn.disabled = false; btn.textContent = 'Regenerar para este formato';
  }
}

async function reject(taskId) {
  await fetch(`/api/reject/${taskId}`, { method: 'POST' });
  loadTasks();
  loadStatus();
}

async function attemptExam(taskId) {
  const r = await fetch(`/api/attempt/${taskId}`, { method: 'POST' });
  const d = await r.json();
  if (d.status === 'attempt_started') {
    alert('El bot está entrando al examen y generando respuestas. Actualiza en unos minutos.');
  } else {
    alert('Error: ' + JSON.stringify(d));
  }
  loadTasks();
  loadStatus();
}

async function triggerScan() {
  const r = await fetch('/api/scan', { method: 'POST' });
  const d = await r.json();
  if (!r.ok) { alert(d.detail); return; }
  _wasScanning = true;
  alert(d.status === 'scan_started' ? 'Escaneo iniciado. Actualiza en unos minutos.' : 'Ya hay un escaneo en curso.');
  loadStatus();
}

async function toggleSettings(show) {
  const form = document.getElementById('settings');
  const visible = show ?? form.classList.contains('hidden');
  form.classList.toggle('hidden', !visible);
  if (!visible) return;
  const me = await (await fetch('/api/me')).json();
  document.getElementById('s-display').value = me.display_name;
  document.getElementById('s-career').value = me.career;
  document.getElementById('s-time').value =
    `${String(me.scan_hour).padStart(2, '0')}:${String(me.scan_minute).padStart(2, '0')}`;
  document.getElementById('s-ucnl-user').value = me.ucnl_username;
  document.getElementById('s-ucnl-pass').value = '';
  document.getElementById('s-ucnl-hint').textContent = me.has_ucnl_password
    ? 'Guardada (cifrada). Déjala vacía para no cambiarla.' : 'Sin configurar';
  document.getElementById('s-msg').classList.add('hidden');
}

async function saveSettings(e) {
  e.preventDefault();
  const [h, m] = document.getElementById('s-time').value.split(':').map(Number);
  const body = {
    display_name: document.getElementById('s-display').value,
    career: document.getElementById('s-career').value,
    ucnl_username: document.getElementById('s-ucnl-user').value,
    ucnl_password: document.getElementById('s-ucnl-pass').value || null,
    scan_hour: h,
    scan_minute: m,
  };
  const newPass = document.getElementById('s-new-pass').value;
  if (newPass) {
    body.new_password = newPass;
    body.current_password = document.getElementById('s-cur-pass').value;
  }
  const r = await fetch('/api/me', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const msg = document.getElementById('s-msg');
  msg.classList.remove('hidden');
  if (r.ok) {
    if (newPass) { location.reload(); return; }
    msg.className = 'text-sm text-green-700';
    msg.textContent = 'Guardado';
    toggleSettings(true).then(() => { msg.classList.remove('hidden'); });
    loadStatus();
  } else {
    const d = await r.json();
    msg.className = 'text-sm text-red-600';
    msg.textContent = typeof d.detail === 'string' ? d.detail : 'Revisa los datos (contraseña nueva mín. 8 caracteres)';
  }
}

async function logout() {
  await fetch('/api/logout', { method: 'POST' });
  location.reload();
}

loadStatus();
loadTasks();
setInterval(() => { loadStatus(); }, 10000);
</script>
</body>
</html>"""
