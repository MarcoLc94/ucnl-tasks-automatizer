"""
Playwright browser automation for licenciatura.ucnl.edu.mx

SELECTORS NOTE:
  This file uses CSS selectors that match common LMS patterns.
  After running `scan_debug()` or inspecting the site manually, update the
  constants in the SELECTORS section below to match the actual DOM.
"""

import os
import json
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from playwright.async_api import async_playwright, Page, BrowserContext

from .config import get
from .logger import logger
from .users import User

try:
    from playwright_stealth import Stealth  # v2

    async def stealth_async(page):
        await Stealth().apply_stealth_async(page)

    _STEALTH_AVAILABLE = True
except ImportError:
    try:
        from playwright_stealth import stealth_async  # v1
        _STEALTH_AVAILABLE = True
    except ImportError:
        _STEALTH_AVAILABLE = False
        logger.warning("playwright-stealth no disponible — instalalo con: pip install playwright-stealth")


# ─── SELECTORS — Actualizar según la estructura real del sitio ───────────────

SEL_USERNAME     = "#username, input[name='username'], input[type='text']"
SEL_PASSWORD     = "#password, input[name='password'], input[type='password']"
SEL_LOGIN_BTN    = "#loginbtn, button[type='submit'], input[type='submit']"

# Enlace "Mis Cursos" en el navbar
SEL_MIS_CURSOS   = "a[href*='my/courses.php']"

# En la página de cursos: título de cada materia (Moodle siempre usa /course/view.php)
SEL_COURSE_LINKS = "a[href*='course/view.php']"

# En la página de un curso: actividades/tareas pendientes
SEL_ACTIVITY_ITEMS  = "li.activity, .activity-item, .activityinstance"
SEL_ACTIVITY_LINK   = "a.aalink, a.instancename, .instancename a"
SEL_ACTIVITY_NAME   = ".instancename, span.instancename"

# Tipos de actividad (clases CSS que identifica el tipo)
CSS_ASSIGNMENT   = "assign"   # substring en class del <li>
CSS_QUIZ         = "quiz"     # substring en class del <li>

# En la página de tarea (assignment)
SEL_TASK_DESCRIPTION = ".box.generalbox, #intro, .description, .assign-intro"
SEL_TASK_STATUS      = ".submissionstatustable, .submission-status"
SEL_SUBMITTED_TEXT   = "submitted, Entregado, entregado"  # texto a buscar en status

# Formulario de entrega de tarea
SEL_SUBMIT_BTN       = "input[value*='Agregar entrega'], button:has-text('Agregar entrega'), .btn:has-text('Editar')"
SEL_ONLINE_TEXT_AREA = ".editor_atto_content, .atto_content, div[contenteditable='true'], textarea#id_onlinetext_editor"
SEL_SAVE_BTN         = "input[value*='Guardar'], button:has-text('Guardar'), input[type='submit']"

# Examen (quiz)
SEL_ATTEMPT_BTN      = "button:has-text('Intentar examen'), button:has-text('Intentar cuestionario'), button:has-text('Intentar'), a:has-text('Intentar cuestionario'), .btn-primary:has-text('Intentar')"
SEL_RESUME_BTN       = "button:has-text('Continuar su intento'), button:has-text('Continuar el último intento'), button:has-text('Reanudar'), input[value*='Continuar'], input[value*='Reanudar']"
SEL_CONFIRM_ATTEMPT  = "input[name='submitbutton'], input[value*='Iniciar intento'], input[value*='Comenzar el intento'], button:has-text('Comenzar el intento')"
SEL_QUESTION_BLOCKS  = ".que"
SEL_QUESTION_TEXT    = ".qtext, .question-text, p"
SEL_ANSWER_OPTIONS   = ".answer .r0, .answer .r1, .answer label, .answeroptions label"
SEL_ANSWER_CHECKBOX  = "input[type='radio'], input[type='checkbox']"
SEL_NEXT_BTN         = "input[name='next'], button[name='next'], .mod_quiz-next-nav, input[value*='Siguiente'], button:has-text('Siguiente')"
SEL_PREV_BTN         = "input[name='previous'], button[name='previous'], .mod_quiz-prev-nav"
SEL_FINISH_ATTEMPT_BTN = "input[name='finishattempt'], button[name='finishattempt'], input[value*='Terminar intento'], button:has-text('Terminar intento')"
SEL_FINISH_BTN       = "input[value*='Terminar'], button:has-text('Terminar'), .btn:has-text('Enviar')"
SEL_CONFIRM_FINISH   = "button:has-text('Enviar todo y terminar'), input[value*='Enviar todo']"
SEL_CONFIRM_MODAL    = "button[data-action='save']"

# ─── Browser context ─────────────────────────────────────────────────────────

# Cada Chromium consume ~300-500 MB: limitar cuántos corren a la vez entre todos los usuarios
_BROWSER_SLOTS = asyncio.Semaphore(int(os.getenv("MAX_BROWSERS", "2")))


def _headless() -> bool:
    env = os.getenv("HEADLESS")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes")
    return get()["ucnl"].get("headless", True)


@asynccontextmanager
async def browser_session(user: User):
    """Abre un Chromium con la sesión guardada del usuario. Espera turno si ya hay MAX_BROWSERS abiertos."""
    async with _BROWSER_SLOTS:
        async with async_playwright() as p:
            browser, context, page = await _new_context(p, user)
            try:
                yield context, page
            finally:
                await browser.close()


async def _new_context(playwright, user: User) -> tuple:
    browser = await playwright.chromium.launch(
        headless=_headless(),
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )

    storage_state = str(user.auth_state_path) if user.auth_state_path.exists() else None
    context = await browser.new_context(
        storage_state=storage_state,
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="es-MX",
    )
    page = await context.new_page()

    if _STEALTH_AVAILABLE:
        await stealth_async(page)

    return browser, context, page


async def _login(page: Page, context: BrowserContext, user: User) -> bool:
    cfg = get()["ucnl"]
    base_url = cfg["base_url"]
    username = user.ucnl_username
    password = user.ucnl_password

    if not username or not password:
        logger.error(f"[{user.username}] No tiene credenciales de UCNL configuradas")
        return False

    await page.goto(base_url, wait_until="load")

    # Si ya hay sesión activa (auth_state guardado), no necesitamos login
    if await _is_logged_in(page):
        logger.info(f"[{user.username}] Sesión activa reutilizada")
        return True

    logger.info(f"[{user.username}] Iniciando sesión...")
    try:
        await page.fill(SEL_USERNAME, username)
        await page.fill(SEL_PASSWORD, password)
        await page.click(SEL_LOGIN_BTN)
        await page.wait_for_load_state("load")

        if not await _is_logged_in(page):
            logger.error(f"[{user.username}] Login fallido — verifica usuario y contraseña")
            return False

        # Guardar estado de autenticación para reutilizar
        await context.storage_state(path=str(user.auth_state_path))
        logger.info(f"[{user.username}] Login exitoso — sesión guardada")
        return True

    except Exception as e:
        logger.error(f"Error durante login: {e}")
        return False


async def _is_logged_in(page: Page) -> bool:
    # Heurística: si la URL ya no tiene "login" y hay algún elemento de usuario en la página
    url = page.url
    if "login" in url.lower():
        return False
    # Busca elementos típicos post-login
    try:
        user_menu = await page.query_selector(".usermenu, .userbutton, #user-menu, .dropdown-user")
        return user_menu is not None
    except Exception:
        return False


# ─── Scan courses ─────────────────────────────────────────────────────────────

async def get_courses(page: Page) -> list[dict]:
    """Navigate to 'Mis Cursos' and return list of {name, url} for each course."""
    cfg = get()["ucnl"]
    base_url = cfg["base_url"].rstrip("/")
    try:
        await page.goto(f"{base_url}/my/courses.php", wait_until="load")
    except Exception as e:
        logger.error(f"Error navegando a Mis Cursos: {e}")
        return []

    # Moodle carga la lista de cursos por AJAX después del evento "load"
    try:
        await page.wait_for_selector(SEL_COURSE_LINKS, timeout=20000)
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        logger.warning("La lista de cursos no terminó de cargar a tiempo")

    course_links = await page.query_selector_all(SEL_COURSE_LINKS)
    courses = []
    seen_urls = set()
    for link in course_links:
        href = await link.get_attribute("href")
        if not href or href in seen_urls:
            continue
        # Extraer texto ignorando imágenes y etiquetas genéricas
        name = await link.evaluate("""el => {
            const clone = el.cloneNode(true);
            clone.querySelectorAll('img, .sr-only, .visually-hidden').forEach(i => i.remove());
            const lines = clone.innerText.split('\\n')
                .map(l => l.trim())
                .filter(l => l && l !== 'Imagen del curso' && l !== 'Nombre del curso');
            return lines.join(' ').replace(/^El curso está destacado\\s*/, '').trim();
        }""")
        if name:
            seen_urls.add(href)
            # Grado: categoría del curso en la tarjeta (ej. "5to Tetramestre")
            grade = await link.evaluate("""el => {
                const card = el.closest("[data-region='course-content']");
                const cat = card && card.querySelector('.categoryname');
                return cat ? cat.innerText.replace('Categoría del curso', '').trim() : '';
            }""")
            courses.append({"name": name, "url": href, "grade": grade, "teacher": ""})
            logger.info(f"Curso encontrado: {name} ({grade or 'sin grado'})")

    return courses


async def get_course_teacher(page: Page, course: dict) -> str:
    """Nombre del (los) profesor(es) del curso, desde la lista de participantes."""
    import re
    m = re.search(r"[?&]id=(\d+)", course["url"])
    if not m:
        return ""
    base_url = get()["ucnl"]["base_url"].rstrip("/")
    try:
        await page.goto(f"{base_url}/user/index.php?id={m.group(1)}&perpage=5000", wait_until="load")
        names = await page.evaluate("""() => {
            const out = [];
            for (const row of document.querySelectorAll('table#participants tbody tr')) {
                const cells = [...row.querySelectorAll('td, th')].map(c => c.innerText.trim());
                if (cells.some(c => c === 'Profesor')) {
                    const name = cells.find(c => c && !c.startsWith('Seleccionar'));
                    if (name && !out.includes(name)) out.push(name);
                }
            }
            return out;
        }""")
        return ", ".join(names)
    except Exception as e:
        logger.warning(f"No se pudo obtener el profesor de {course['name']}: {e}")
        return ""


async def get_course_activities(page: Page, course: dict) -> list[dict]:
    """
    Enter a course and return list of pending activities.
    Returns: list of {title, url, type: 'assignment'|'quiz'}
    """
    try:
        await page.goto(course["url"], wait_until="load")
    except Exception as e:
        logger.error(f"Error entrando al curso {course['name']}: {e}")
        return []

    items = await page.query_selector_all(SEL_ACTIVITY_ITEMS)
    activities = []

    for item in items:
        try:
            class_attr = await item.get_attribute("class") or ""
            if CSS_ASSIGNMENT in class_attr:
                activity_type = "assignment"
            elif CSS_QUIZ in class_attr:
                activity_type = "quiz"
            else:
                continue

            link = await item.query_selector(SEL_ACTIVITY_LINK)
            if not link:
                continue

            # Moodle agrega el tipo ("Tarea", "Examen") en un span oculto dentro del nombre
            title = (await link.evaluate("""el => {
                const clone = el.cloneNode(true);
                clone.querySelectorAll('.accesshide, .sr-only, .visually-hidden').forEach(e => e.remove());
                return clone.innerText;
            }""")).strip().split("\n")[0].strip()
            href = await link.get_attribute("href")
            if title and href:
                activities.append({
                    "title": title,
                    "url": href,
                    "type": activity_type,
                    "course": course["name"],
                    "course_grade": course.get("grade", ""),
                    "teacher": course.get("teacher", ""),
                })
        except Exception:
            continue

    logger.info(f"Curso '{course['name']}': {len(activities)} actividades encontradas")
    return activities


async def _get_activity_dates(page: Page) -> dict:
    """Lee el bloque 'Abre: … / Cierra: …' de una actividad (Moodle 4)."""
    from .dates import classify_dates
    rows = []
    for div in await page.query_selector_all("[data-region='activity-dates'] div:has(> strong), .activity-dates div:has(> strong)"):
        try:
            strong = await div.query_selector("strong")
            if not strong:
                continue
            label = (await strong.inner_text()).strip()
            full = (await div.inner_text()).strip()
            rows.append((label, full[len(label):].strip() if full.startswith(label) else full))
        except Exception:
            continue
    return classify_dates(rows)


async def get_assignment_details(page: Page, activity: dict) -> dict | None:
    """
    Enter an assignment page and extract description, due date, and submission status.
    Returns: {description, already_submitted, is_past_due, due_date} or None on error.
    """
    try:
        await page.goto(activity["url"], wait_until="load")
    except Exception as e:
        logger.error(f"Error entrando a tarea {activity['title']}: {e}")
        return None

    already_submitted = False
    is_past_due = False
    due_date = None

    # Parse submission status table (Moodle generaltable)
    rows = await page.query_selector_all("table.generaltable tr, .submissionstatustable tr")
    for row in rows:
        try:
            cells = await row.query_selector_all("td")
            if len(cells) < 2:
                continue
            label = (await cells[0].inner_text()).strip().lower()
            value = (await cells[1].inner_text()).strip()
            if "fecha de entrega" in label or "due date" in label:
                due_date = value
            if "estado de entrega" in label or "submission status" in label:
                v = value.lower()
                if any(s in v for s in ["entregad", "submitted", "calificad"]):
                    already_submitted = True
            if "tiempo restante" in label or "time remaining" in label:
                v = value.lower()
                if any(s in v for s in ["vencido", "atrasado", "overdue", "late"]):
                    is_past_due = True
        except Exception:
            continue

    # Fallback: check body text for expiry indicators
    body_text = (await page.inner_text("body")).lower()
    if "ya no se aceptan" in body_text or "no longer accepting" in body_text:
        is_past_due = True

    desc_el = await page.query_selector(SEL_TASK_DESCRIPTION)
    description = (await desc_el.inner_text()).strip() if desc_el else ""
    if not description:
        content_el = await page.query_selector("#region-main, .course-content, main")
        description = (await content_el.inner_text()).strip() if content_el else ""

    return {
        "description": description,
        "already_submitted": already_submitted,
        "is_past_due": is_past_due,
        "due_date": due_date,
        **await _get_activity_dates(page),
    }


async def _extract_page_questions(page: Page) -> list[dict]:
    """Extract all question blocks visible on the current page."""
    question_blocks = await page.query_selector_all(SEL_QUESTION_BLOCKS)
    questions = []
    for block in question_blocks:
        try:
            q_el = await block.query_selector(SEL_QUESTION_TEXT)
            question_text = (await q_el.inner_text()).strip() if q_el else ""
            if not question_text:
                continue
            answer_labels = await block.query_selector_all(SEL_ANSWER_OPTIONS)
            options = [
                (await label.inner_text()).strip()
                for label in answer_labels
                if (await label.inner_text()).strip()
            ]
            radios = await block.query_selector_all("input[type='radio']")
            checkboxes = await block.query_selector_all("input[type='checkbox']")
            q_type = "single" if radios else ("multiple" if checkboxes else "single")
            questions.append({"question": question_text, "options": options, "type": q_type})
        except Exception:
            continue
    return questions


async def _start_or_resume_attempt(page: Page) -> None:
    """Click the attempt/resume button and confirm the start dialog if it appears."""
    attempt_btn = await page.query_selector(SEL_ATTEMPT_BTN)
    resume_btn = await page.query_selector(SEL_RESUME_BTN)
    btn = attempt_btn or resume_btn
    if btn:
        await btn.click()
        await page.wait_for_load_state("load")
    # Moodle muestra una página de confirmación antes de iniciar el intento
    confirm = await page.query_selector(SEL_CONFIRM_ATTEMPT)
    if confirm:
        await confirm.click()
        await page.wait_for_load_state("load")


async def _go_to_question_one(page: Page) -> None:
    """Navigate to question 1 via Moodle's quiz navigation block or by going back page by page."""
    try:
        # Opción 1: panel de navegación lateral (salto directo)
        nav_btns = await page.query_selector_all(".qnbutton")
        if nav_btns:
            await nav_btns[0].click()
            await page.wait_for_load_state("load")
            logger.info("Navegado a pregunta 1 via panel de navegación")
            return

        # Opción 2: links en el resumen del intento
        summary_links = await page.query_selector_all(".quizsummaryofattempt td a, .mod_quiz-attempt-summary td a")
        if summary_links:
            await summary_links[0].click()
            await page.wait_for_load_state("load")
            logger.info("Navegado a pregunta 1 desde resumen del intento")
            return

        # Opción 3: retroceder página por página con "Página anterior"
        for _ in range(100):
            prev_btn = await page.query_selector(SEL_PREV_BTN)
            if not prev_btn:
                break
            await prev_btn.click()
            await page.wait_for_load_state("load")
        logger.info("Navegado a pregunta 1 via botón 'Página anterior'")
    except Exception as e:
        logger.warning(f"No se pudo navegar a pregunta 1: {e}")


async def get_quiz_details(page: Page, activity: dict) -> dict:
    """
    Check a quiz page during scan — does NOT start an attempt.
    Returns: {already_completed, available_from, questions=None}
    questions is always None; answering happens in real-time during submit_quiz.
    """
    result = {"already_completed": False, "available_from": None, "questions": None,
              "opens_at": None, "closes_at": None}

    try:
        await page.goto(activity["url"], wait_until="load")
    except Exception as e:
        logger.error(f"Error entrando a examen {activity['title']}: {e}")
        return result

    result.update(await _get_activity_dates(page))

    page_text = (await page.inner_text("body")).lower()

    import re

    already_completed = False
    grade_text = ""

    # Señal fuerte: tabla de intentos con calificación numérica real (ej: "9.67 / 10.00")
    # Solo aparece cuando hay un intento efectivamente terminado y calificado
    summary_table = await page.query_selector(".quizattemptcounts, table.generaltable")
    if summary_table:
        table_text = await summary_table.inner_text()
        grade_match = re.search(r"(\d+[.,]\d+)\s*/\s*(\d+[.,]\d+)", table_text)
        if grade_match:
            already_completed = True
            grade_text = grade_match.group(0)

    # Señal de respaldo: frases que solo aparecen en página post-entrega, nunca en info del examen
    if not already_completed:
        strong_signals = ["ya has completado", "revisión del intento", "intento terminado"]
        if any(s in page_text for s in strong_signals):
            already_completed = True

    if already_completed:
        logger.info(f"Examen '{activity['title']}' ya completado. Calificación: {grade_text or 'N/A'}")
        result["already_completed"] = True
        result["grade"] = grade_text
        return result

    if any(s in page_text for s in ["no disponible", "no está disponible", "este cuestionario no estará disponible"]):
        result["available_from"] = result["opens_at"] or "no disponible"
        return result

    logger.info(f"Examen '{activity['title']}' disponible — se responderá en tiempo real al aprobar")
    return result


# ─── Submit ───────────────────────────────────────────────────────────────────

async def submit_assignment(
    user: User,
    task_url: str,
    response_text: str,
    course_name: str = "",
    task_title: str = "",
    output_format: str = "docx",
    course_grade: str = "",
    teacher: str = "",
) -> bool:
    """Generate the document in the chosen format and upload it to the Moodle assignment."""
    from .renderers import build_document

    doc_args = dict(
        markdown_text=response_text,
        course_name=course_name,
        task_title=task_title or task_url,
        student_name=user.display_name,
        output_dir=user.docs_dir,
        career=user.career,
        grade=course_grade,
        teacher=teacher,
    )
    try:
        doc_path = await build_document(output_format, **doc_args)
    except Exception as e:
        if output_format == "docx":
            raise
        logger.error(f"Error generando {output_format} ({e}) — se entregará como Word")
        doc_path = await build_document("docx", **doc_args)

    async with browser_session(user) as (context, page):
        try:
            if not await _login(page, context, user):
                return False

            await page.goto(task_url, wait_until="load")

            # Click 'Agregar entrega' o 'Editar entrega'
            submit_btn = await page.query_selector(SEL_SUBMIT_BTN)
            if not submit_btn:
                logger.error(f"No se encontró botón de entrega en: {task_url}")
                return False
            await submit_btn.click()
            await page.wait_for_load_state("load")

            # Intentar subir el documento (Moodle file manager)
            uploaded = await _upload_file(page, doc_path)

            if not uploaded:
                # Fallback: si la tarea acepta texto en línea, usar el editor
                logger.warning("No se encontró área de subida — intentando editor de texto")
                editor = await page.query_selector(SEL_ONLINE_TEXT_AREA)
                if editor:
                    tag = await editor.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "textarea":
                        await editor.fill(response_text)
                    else:
                        await editor.click()
                        await editor.evaluate(
                            "(el, text) => { el.innerHTML = ''; el.innerText = text; }",
                            response_text,
                        )
                else:
                    logger.error("No se encontró ni file manager ni editor de texto")
                    return False

            save_btn = await page.query_selector(SEL_SAVE_BTN)
            if save_btn:
                await save_btn.click()
                await page.wait_for_load_state("load")
                logger.info(f"Tarea entregada exitosamente: {task_url}")
                await context.storage_state(path=str(user.auth_state_path))
                return True
            else:
                logger.error("No se encontró botón 'Guardar'")
                return False

        except Exception as e:
            logger.error(f"Error al entregar tarea: {e}")
            return False


async def _upload_file(page: Page, file_path) -> bool:
    """Upload a file through Moodle's file manager widget."""
    from pathlib import Path
    file_path = Path(file_path)
    try:
        # Buscar input[type=file] directamente (a veces visible)
        file_input = await page.query_selector("input[type='file']")
        if file_input:
            await file_input.set_input_files(str(file_path))
            await page.wait_for_timeout(1000)
            return True

        # Moodle file picker: click en "Subir un archivo"
        add_btn = await page.query_selector(
            ".fp-btn-add, button:has-text('Subir un archivo'), a:has-text('Subir un archivo')"
        )
        if not add_btn:
            return False

        await add_btn.click()
        await page.wait_for_timeout(500)

        # Dentro del diálogo del file picker
        file_input = await page.query_selector(".fp-upload-form input[type='file'], input[name='repo_upload_file']")
        if file_input:
            await file_input.set_input_files(str(file_path))
            await page.wait_for_timeout(500)

            # Click "Subir este archivo"
            upload_btn = await page.query_selector(
                "button:has-text('Subir este archivo'), .fp-upload-btn, input[value*='Subir este archivo']"
            )
            if upload_btn:
                await upload_btn.click()
                await page.wait_for_load_state("load")
                return True

        return False
    except Exception as e:
        logger.warning(f"Error en _upload_file: {e}")
        return False


async def submit_quiz(user: User, task_url: str, course_name: str = "") -> bool:
    """
    Enter/resume a quiz and answer every question in real-time with AI.
    Navigates backwards to question 1 first, then forward answering each page.
    """
    from .ai import analyze_single_question

    async with browser_session(user) as (context, page):
        try:
            if not await _login(page, context, user):
                return False

            await page.goto(task_url, wait_until="load")
            await _start_or_resume_attempt(page)

            # Retroceder hasta la primera pregunta
            logger.info("Retrocediendo hasta la pregunta 1...")
            for _ in range(100):
                prev_btn = await page.query_selector(SEL_PREV_BTN)
                if not prev_btn:
                    break
                await prev_btn.click()
                await page.wait_for_load_state("load")
            logger.info("En la primera pregunta — comenzando respuestas")

            # Contestar pregunta por pregunta hacia adelante
            answered = 0
            for _ in range(100):
                question_blocks = await page.query_selector_all(SEL_QUESTION_BLOCKS)

                for block in question_blocks:
                    try:
                        q_el = await block.query_selector(SEL_QUESTION_TEXT)
                        question_text = (await q_el.inner_text()).strip() if q_el else ""
                        if not question_text:
                            continue

                        answer_labels = await block.query_selector_all(SEL_ANSWER_OPTIONS)
                        options = [
                            (await lbl.inner_text()).strip()
                            for lbl in answer_labels
                            if (await lbl.inner_text()).strip()
                        ]
                        if not options:
                            continue

                        selected_idx = analyze_single_question(course_name, question_text, options)
                        inputs = await block.query_selector_all(SEL_ANSWER_CHECKBOX)
                        target = inputs[selected_idx] if selected_idx < len(inputs) else (inputs[0] if inputs else None)
                        if target:
                            await target.check()
                        answered += 1
                        logger.info(f"Pregunta {answered} respondida — opción {selected_idx}: {options[selected_idx] if selected_idx < len(options) else '?'}")
                    except Exception as e:
                        logger.warning(f"Error respondiendo pregunta: {e}")

                next_btn = await page.query_selector(SEL_NEXT_BTN)
                if next_btn:
                    await next_btn.click()
                    await page.wait_for_load_state("load")
                else:
                    break

            logger.info(f"Total preguntas respondidas: {answered}")

            # Al llegar aquí ya estamos en la página de resumen del intento:
            # el botón "Terminar intento ..." de la última pregunta tiene name="next"
            # y el bot ya lo clickeó como parte del loop de "Siguiente".
            # En el resumen aparece directamente "Enviar todo y terminar".
            # Fallback: si por algún motivo aún no llegamos al resumen, buscar botón intermedio.
            confirm_btn = await page.query_selector(SEL_CONFIRM_FINISH)
            if not confirm_btn:
                intermediate = await page.query_selector(SEL_FINISH_ATTEMPT_BTN) or await page.query_selector(SEL_FINISH_BTN)
                if intermediate:
                    await intermediate.click()
                    await page.wait_for_load_state("load")
                    confirm_btn = await page.query_selector(SEL_CONFIRM_FINISH)

            if not confirm_btn:
                logger.error("No se encontró botón 'Enviar todo y terminar' en el resumen")
                return False

            await confirm_btn.click()
            # El modal se abre por JS — esperar a que se renderice completamente
            try:
                await page.wait_for_selector(SEL_CONFIRM_MODAL, timeout=8000)
                await page.wait_for_timeout(800)
                logger.info("Modal de confirmación detectado — enviando examen")
                await page.evaluate("document.querySelector('[data-action=\"save\"]').click()")
                await page.wait_for_load_state("load")
            except Exception as e:
                logger.error(f"Modal de confirmación no apareció: {e}")
                return False

            logger.info(f"Examen entregado exitosamente: {task_url}")
            await context.storage_state(path=str(user.auth_state_path))
            return True

        except Exception as e:
            logger.error(f"Error al entregar examen: {e}")
            return False


# ─── Main scan entry point ────────────────────────────────────────────────────

async def run_scan(user: User) -> dict:
    """
    Full scan: login → get courses → check activities → return raw data.
    State updates are handled by the caller (main.py).
    Returns {"courses": [nombres] | None, "items": [{course, activity, details|quiz}]}.
    "courses" es None si el escaneo no terminó bien (para no borrar nada por error).
    """
    failed = {"courses": None, "items": []}
    async with browser_session(user) as (context, page):
        try:
            if not await _login(page, context, user):
                return failed

            courses = await get_courses(page)
            if not courses:
                logger.warning("No se encontraron cursos")
                return failed

            results = []
            for course in courses:
                course["teacher"] = await get_course_teacher(page, course)
                activities = await get_course_activities(page, course)
                for activity in activities:
                    if activity["type"] == "assignment":
                        details = await get_assignment_details(page, activity)
                        if details:
                            results.append({
                                "course": course,
                                "activity": activity,
                                "details": details,
                            })
                    elif activity["type"] == "quiz":
                        quiz = await get_quiz_details(page, activity)
                        results.append({
                            "course": course,
                            "activity": activity,
                            "quiz": quiz,
                        })
                await asyncio.sleep(1)

            return {"courses": [c["name"] for c in courses], "items": results}

        except Exception as e:
            logger.error(f"[{user.username}] Error durante el escaneo: {e}")
            return failed


async def scan_debug() -> None:
    """
    Debug mode: login and print page structure to help identify correct selectors.
    Runs with headless=False so you can see the browser.
    """
    import yaml
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    cfg["ucnl"]["headless"] = False  # Override to show browser

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        if _STEALTH_AVAILABLE:
            await stealth_async(page)

        base_url = cfg["ucnl"]["base_url"]
        username = os.getenv("UCNL_USERNAME", "")
        password = os.getenv("UCNL_PASSWORD", "")

        await page.goto(base_url)
        print(f"\n[DEBUG] URL actual: {page.url}")
        print("[DEBUG] Ingresa credenciales manualmente o espera el auto-login...")

        if username and password:
            try:
                await page.fill(SEL_USERNAME, username)
                await page.fill(SEL_PASSWORD, password)
                await page.click(SEL_LOGIN_BTN)
                await page.wait_for_load_state("load")
                print(f"[DEBUG] Post-login URL: {page.url}")
            except Exception as e:
                print(f"[DEBUG] Error en login: {e}")

        print("\n[DEBUG] Presiona Ctrl+C cuando hayas inspeccionado el sitio")
        print("[DEBUG] Usa page.query_selector() en la consola del navegador para encontrar selectores")
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            pass
        finally:
            await browser.close()
