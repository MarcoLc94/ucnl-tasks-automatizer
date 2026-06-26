import os
import json
import time
from groq import Groq, RateLimitError
from .logger import logger

_client: Groq | None = None
_last_call_time: float = 0.0
_MIN_INTERVAL = 3.0  # segundos mínimos entre llamadas a Groq


def _get_client() -> Groq:
    global _client
    if not _client:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _client


def _chat(system: str, user: str, max_tokens: int = 2000) -> str:
    global _last_call_time
    from .config import get
    cfg = get()["bot"]

    # Respetar intervalo mínimo entre llamadas
    elapsed = time.time() - _last_call_time
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)

    retries = 3
    wait = 15  # segundos de espera inicial en 429
    for attempt in range(retries):
        try:
            _last_call_time = time.time()
            response = _get_client().chat.completions.create(
                model=cfg["model"],
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=cfg["temperature"],
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content.strip()
        except RateLimitError:
            if attempt < retries - 1:
                logger.warning(f"Rate limit de Groq — esperando {wait}s antes de reintentar ({attempt + 1}/{retries})")
                time.sleep(wait)
                wait *= 2  # backoff exponencial
            else:
                logger.error("Rate limit de Groq agotado tras varios reintentos")
                raise


def generate_assignment_response(
    course_name: str,
    task_title: str,
    task_description: str,
) -> str:
    system = """Eres un estudiante universitario aplicado de la Universidad Ciudadana de Nuevo León (UCNL)
cursando Ingeniería en Desarrollo de Software. Debes redactar respuestas académicas completas,
bien estructuradas y en español. Usa un tono formal pero claro. Responde directamente al contenido
de la tarea sin agregar encabezados innecesarios."""

    user = f"""Materia: {course_name}
Tarea: {task_title}

Instrucciones de la tarea:
{task_description}

Redacta una respuesta completa y bien argumentada para esta tarea."""

    logger.info(f"Generando respuesta IA para tarea: {task_title}")
    return _chat(system, user)


def analyze_exam(
    course_name: str,
    questions_raw: list[dict],
) -> list[dict]:
    """
    questions_raw: list of {question: str, options: list[str], type: "single"|"multiple"}
    Returns: same list with "selected_indices" added to each item.
    """
    system = """Eres un estudiante universitario de Ingeniería en Desarrollo de Software en la UCNL.
Debes responder preguntas de examen correctamente. Analiza cada pregunta con cuidado y elige la(s)
respuesta(s) más correcta(s). Para preguntas de opción única elige solo una. Para múltiple opción
puedes elegir varias si corresponde.

Responde ÚNICAMENTE con un JSON array con el siguiente formato (sin texto adicional):
[
  {"question_index": 0, "selected_indices": [0]},
  {"question_index": 1, "selected_indices": [1, 3]},
  ...
]"""

    questions_text = json.dumps(questions_raw, ensure_ascii=False, indent=2)
    user = f"""Materia: {course_name}

Preguntas del examen:
{questions_text}

Responde el examen en formato JSON."""

    logger.info(f"Analizando examen de {course_name} con {len(questions_raw)} preguntas")
    raw = _chat(system, user, max_tokens=1000)

    try:
        start = raw.find("[")
        end = raw.rfind("]") + 1
        answers = json.loads(raw[start:end])
        for i, q in enumerate(questions_raw):
            matched = next((a for a in answers if a["question_index"] == i), None)
            q["selected_indices"] = matched["selected_indices"] if matched else [0]
        return questions_raw
    except Exception as e:
        logger.error(f"Error parseando respuesta del examen: {e}\nRaw: {raw}")
        for q in questions_raw:
            q["selected_indices"] = [0]
        return questions_raw
