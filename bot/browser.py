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
from pathlib import Path
from playwright.async_api import async_playwright, Page, BrowserContext

from .config import get
from .logger import logger

try:
    from playwright_stealth import stealth_async
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
SEL_ATTEMPT_BTN      = "button:has-text('Intentar'), a:has-text('Intentar cuestionario'), .btn:has-text('Comenzar')"
SEL_QUESTION_BLOCKS  = ".que, .question, .formulation"
SEL_QUESTION_TEXT    = ".qtext, .question-text, p"
SEL_ANSWER_OPTIONS   = ".answer .r0, .answer .r1, .answer label, .answeroptions label"
SEL_ANSWER_CHECKBOX  = "input[type='checkbox'], input[type='radio']"
SEL_FINISH_BTN       = "input[value*='Terminar'], button:has-text('Terminar'), .btn:has-text('Enviar')"
SEL_CONFIRM_FINISH   = "button:has-text('Enviar'), input[value*='Enviar todo']"

# ─── Browser context ─────────────────────────────────────────────────────────

_AUTH_STATE_PATH = Path(__file__).parent.parent / get()["ucnl"]["auth_state_file"]


async def _new_context(playwright, save_auth: bool = False) -> tuple:
    cfg = get()["ucnl"]
    browser = await playwright.chromium.launch(
        headless=cfg.get("headless", True),
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )

    storage_state = str(_AUTH_STATE_PATH) if _AUTH_STATE_PATH.exists() else None
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


async def _login(page: Page, context: BrowserContext) -> bool:
    cfg = get()["ucnl"]
    base_url = cfg["base_url"]
    username = os.getenv("UCNL_USERNAME", "")
    password = os.getenv("UCNL_PASSWORD", "")

    if not username or not password:
        logger.error("UCNL_USERNAME o UCNL_PASSWORD no están configurados en .env")
        return False

    await page.goto(base_url, wait_until="networkidle")

    # Si ya hay sesión activa (auth_state guardado), no necesitamos login
    if await _is_logged_in(page):
        logger.info("Sesión activa reutilizada")
        return True

    logger.info("Iniciando sesión...")
    try:
        await page.fill(SEL_USERNAME, username)
        await page.fill(SEL_PASSWORD, password)
        await page.click(SEL_LOGIN_BTN)
        await page.wait_for_load_state("networkidle")

        if not await _is_logged_in(page):
            logger.error("Login fallido — verifica usuario y contraseña")
            return False

        # Guardar estado de autenticación para reutilizar
        await context.storage_state(path=str(_AUTH_STATE_PATH))
        logger.info("Login exitoso — sesión guardada")
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
        await page.goto(f"{base_url}/my/courses.php", wait_until="networkidle")
    except Exception as e:
        logger.error(f"Error navegando a Mis Cursos: {e}")
        return []

    course_links = await page.query_selector_all(SEL_COURSE_LINKS)
    courses = []
    for link in course_links:
        name = (await link.inner_text()).strip()
        href = await link.get_attribute("href")
        if name and href:
            courses.append({"name": name, "url": href})
            logger.info(f"Curso encontrado: {name}")

    return courses


async def get_course_activities(page: Page, course: dict) -> list[dict]:
    """
    Enter a course and return list of pending activities.
    Returns: list of {title, url, type: 'assignment'|'quiz'}
    """
    try:
        await page.goto(course["url"], wait_until="networkidle")
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

            title = (await link.inner_text()).strip()
            href = await link.get_attribute("href")
            if title and href:
                activities.append({
                    "title": title,
                    "url": href,
                    "type": activity_type,
                    "course": course["name"],
                })
        except Exception:
            continue

    logger.info(f"Curso '{course['name']}': {len(activities)} actividades encontradas")
    return activities


async def get_assignment_details(page: Page, activity: dict) -> dict | None:
    """
    Enter an assignment page and extract description, due date, and submission status.
    Returns: {description, already_submitted, is_past_due, due_date} or None on error.
    """
    try:
        await page.goto(activity["url"], wait_until="networkidle")
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
    }


async def get_quiz_details(page: Page, activity: dict) -> dict:
    """
    Enter a quiz page and extract state + questions.
    Returns: {
        already_completed: bool,
        available_from: str | None,  # si aún no está disponible
        questions: list[dict] | None,
    }
    """
    result = {"already_completed": False, "available_from": None, "questions": None}

    try:
        await page.goto(activity["url"], wait_until="networkidle")
    except Exception as e:
        logger.error(f"Error entrando a examen {activity['title']}: {e}")
        return result

    page_text = (await page.inner_text("body")).lower()

    # Detectar si ya fue completado
    if any(s in page_text for s in ["ya has completado", "calificación final", "tu calificación", "revisión del intento"]):
        logger.info(f"Examen '{activity['title']}' ya fue completado")
        result["already_completed"] = True
        return result

    # Detectar si aún no está disponible y extraer fecha de apertura
    if any(s in page_text for s in ["no disponible", "no está disponible", "este cuestionario no estará disponible"]):
        available_el = await page.query_selector(".quizinfo, .alert, .generalbox")
        if available_el:
            result["available_from"] = (await available_el.inner_text()).strip()
        else:
            result["available_from"] = "Fecha de apertura no disponible"
        return result

    # Intentar hacer click en botón de inicio
    try:
        attempt_btn = await page.query_selector(SEL_ATTEMPT_BTN)
        if attempt_btn:
            await attempt_btn.click()
            await page.wait_for_load_state("networkidle")
            confirm = await page.query_selector("button:has-text('Comenzar el intento')")
            if confirm:
                await confirm.click()
                await page.wait_for_load_state("networkidle")
    except Exception as e:
        logger.warning(f"No se pudo hacer clic en 'Intentar': {e}")

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

    logger.info(f"Examen '{activity['title']}': {len(questions)} preguntas extraídas")
    result["questions"] = questions if questions else None
    return result


# ─── Submit ───────────────────────────────────────────────────────────────────

async def submit_assignment(
    task_url: str,
    response_text: str,
    course_name: str = "",
    task_title: str = "",
) -> bool:
    """Generate DOCX and upload it to the Moodle assignment."""
    from pathlib import Path
    from .document import generate_docx

    cfg = get()
    student_name = cfg.get("student", {}).get("name", "Estudiante")
    docs_dir = Path(__file__).parent.parent / "data" / "docs"

    docx_path = generate_docx(
        course_name=course_name,
        task_title=task_title or task_url,
        response_text=response_text,
        student_name=student_name,
        output_dir=docs_dir,
    )
    logger.info(f"DOCX generado: {docx_path.name}")

    async with async_playwright() as p:
        browser, context, page = await _new_context(p)
        try:
            if not await _login(page, context):
                return False

            await page.goto(task_url, wait_until="networkidle")

            # Click 'Agregar entrega' o 'Editar entrega'
            submit_btn = await page.query_selector(SEL_SUBMIT_BTN)
            if not submit_btn:
                logger.error(f"No se encontró botón de entrega en: {task_url}")
                return False
            await submit_btn.click()
            await page.wait_for_load_state("networkidle")

            # Intentar subir el DOCX (Moodle file manager)
            uploaded = await _upload_file(page, docx_path)

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
                await page.wait_for_load_state("networkidle")
                logger.info(f"Tarea entregada exitosamente: {task_url}")
                await context.storage_state(path=str(_AUTH_STATE_PATH))
                return True
            else:
                logger.error("No se encontró botón 'Guardar'")
                return False

        except Exception as e:
            logger.error(f"Error al entregar tarea: {e}")
            return False
        finally:
            await browser.close()


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
                await page.wait_for_load_state("networkidle")
                return True

        return False
    except Exception as e:
        logger.warning(f"Error en _upload_file: {e}")
        return False


async def submit_quiz(task_url: str, questions: list[dict]) -> bool:
    """
    Navigate to quiz, fill in answers, and submit.
    questions: list with {question, options, type, selected_indices}
    """
    async with async_playwright() as p:
        browser, context, page = await _new_context(p)
        try:
            if not await _login(page, context):
                return False

            await page.goto(task_url, wait_until="networkidle")

            # Click attempt button
            attempt_btn = await page.query_selector(SEL_ATTEMPT_BTN)
            if attempt_btn:
                await attempt_btn.click()
                await page.wait_for_load_state("networkidle")
                confirm = await page.query_selector("button:has-text('Comenzar el intento')")
                if confirm:
                    await confirm.click()
                    await page.wait_for_load_state("networkidle")

            question_blocks = await page.query_selector_all(SEL_QUESTION_BLOCKS)

            for i, (block, q_data) in enumerate(zip(question_blocks, questions)):
                try:
                    selected = q_data.get("selected_indices", [0])
                    inputs = await block.query_selector_all(SEL_ANSWER_CHECKBOX)
                    for idx in selected:
                        if idx < len(inputs):
                            await inputs[idx].check()
                except Exception as e:
                    logger.warning(f"Error marcando respuesta {i}: {e}")

            # Finish quiz
            finish_btn = await page.query_selector(SEL_FINISH_BTN)
            if finish_btn:
                await finish_btn.click()
                await page.wait_for_load_state("networkidle")

            confirm_btn = await page.query_selector(SEL_CONFIRM_FINISH)
            if confirm_btn:
                await confirm_btn.click()
                await page.wait_for_load_state("networkidle")

            logger.info(f"Examen entregado exitosamente: {task_url}")
            await context.storage_state(path=str(_AUTH_STATE_PATH))
            return True

        except Exception as e:
            logger.error(f"Error al entregar examen: {e}")
            return False
        finally:
            await browser.close()


# ─── Main scan entry point ────────────────────────────────────────────────────

async def run_scan() -> list[dict]:
    """
    Full scan: login → get courses → check activities → return raw data.
    State updates are handled by the caller (main.py).
    Returns list of {course, activity, details} dicts for NEW pending items.
    """
    async with async_playwright() as p:
        browser, context, page = await _new_context(p)
        try:
            if not await _login(page, context):
                return []

            courses = await get_courses(page)
            if not courses:
                logger.warning("No se encontraron cursos")
                return []

            results = []
            for course in courses:
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

            return results

        except Exception as e:
            logger.error(f"Error durante el escaneo: {e}")
            return []
        finally:
            await browser.close()


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
                await page.wait_for_load_state("networkidle")
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
