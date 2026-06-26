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
SEL_MIS_CURSOS   = "a.nav-link:has-text('Mis cursos'), a.nav-link:has-text('Mis Cursos'), a.nav-link:has-text('cursos')"

# En la página de cursos: título de cada materia
SEL_COURSE_LINKS = ".coursebox .coursename a, .course-title a, .course-card a, h3.coursename a, a.aalink"

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
    try:
        mis_cursos = await page.query_selector(SEL_MIS_CURSOS)
        if mis_cursos:
            await mis_cursos.click()
            await page.wait_for_load_state("networkidle")
        else:
            logger.warning("No se encontró enlace 'Mis Cursos' — verifica el selector SEL_MIS_CURSOS")
            return []
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
    Enter an assignment page and extract its description.
    Returns: {description, already_submitted} or None on error.
    """
    try:
        await page.goto(activity["url"], wait_until="networkidle")
    except Exception as e:
        logger.error(f"Error entrando a tarea {activity['title']}: {e}")
        return None

    # Check if already submitted
    status_el = await page.query_selector(SEL_TASK_STATUS)
    already_submitted = False
    if status_el:
        status_text = (await status_el.inner_text()).lower()
        already_submitted = any(s in status_text for s in ["submitted", "entregado", "calificado"])

    desc_el = await page.query_selector(SEL_TASK_DESCRIPTION)
    description = (await desc_el.inner_text()).strip() if desc_el else ""

    if not description:
        # fallback: take all visible paragraph text in main content
        content_el = await page.query_selector("#region-main, .course-content, main")
        description = (await content_el.inner_text()).strip() if content_el else ""

    return {"description": description, "already_submitted": already_submitted}


async def get_quiz_details(page: Page, activity: dict) -> list[dict] | None:
    """
    Enter a quiz page, click 'Intentar', and extract all questions and options.
    Returns: list of {question, options, type} or None if already submitted or error.
    """
    try:
        await page.goto(activity["url"], wait_until="networkidle")
    except Exception as e:
        logger.error(f"Error entrando a examen {activity['title']}: {e}")
        return None

    # Check if already finished
    page_text = (await page.inner_text("body")).lower()
    if any(s in page_text for s in ["ya has completado", "calificación final", "tu calificación"]):
        logger.info(f"Examen '{activity['title']}' ya fue completado")
        return None

    # Click 'Intentar' button
    try:
        attempt_btn = await page.query_selector(SEL_ATTEMPT_BTN)
        if attempt_btn:
            await attempt_btn.click()
            await page.wait_for_load_state("networkidle")
            # Confirm dialog if appears
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
            options = []
            for label in answer_labels:
                text = (await label.inner_text()).strip()
                if text:
                    options.append(text)

            # Detect question type by checking if inputs are radio or checkbox
            radios = await block.query_selector_all("input[type='radio']")
            checkboxes = await block.query_selector_all("input[type='checkbox']")
            q_type = "single" if radios else ("multiple" if checkboxes else "single")

            questions.append({
                "question": question_text,
                "options": options,
                "type": q_type,
            })
        except Exception:
            continue

    logger.info(f"Examen '{activity['title']}': {len(questions)} preguntas extraídas")
    return questions if questions else None


# ─── Submit ───────────────────────────────────────────────────────────────────

async def submit_assignment(task_url: str, response_text: str) -> bool:
    """Navigate to task URL, fill text editor, and submit."""
    async with async_playwright() as p:
        browser, context, page = await _new_context(p)
        try:
            if not await _login(page, context):
                return False

            await page.goto(task_url, wait_until="networkidle")

            # Click 'Agregar entrega' or 'Editar entrega'
            submit_btn = await page.query_selector(SEL_SUBMIT_BTN)
            if not submit_btn:
                logger.error(f"No se encontró botón de entrega en: {task_url}")
                return False
            await submit_btn.click()
            await page.wait_for_load_state("networkidle")

            # Try to fill online text editor (Atto or contenteditable)
            editor = await page.query_selector(SEL_ONLINE_TEXT_AREA)
            if editor:
                tag = await editor.evaluate("el => el.tagName.toLowerCase()")
                if tag == "textarea":
                    await editor.fill(response_text)
                else:
                    # contenteditable div (Atto editor)
                    await editor.click()
                    await editor.evaluate(
                        "(el, text) => { el.innerHTML = ''; el.innerText = text; }",
                        response_text,
                    )
            else:
                logger.error("No se encontró el editor de texto para la entrega")
                return False

            # Save submission
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
                        if details and not details["already_submitted"]:
                            results.append({
                                "course": course,
                                "activity": activity,
                                "details": details,
                            })
                    elif activity["type"] == "quiz":
                        questions = await get_quiz_details(page, activity)
                        if questions:
                            results.append({
                                "course": course,
                                "activity": activity,
                                "questions": questions,
                            })
                await asyncio.sleep(1)  # polite delay between courses

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
