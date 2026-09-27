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


_FORMAT_GUIDE = {
    "docx": (
        "El resultado será un documento de Word. Redacta un trabajo académico completo: "
        "introducción, desarrollo en varias secciones con párrafos bien argumentados, y conclusión. "
        "Agrega referencias al final si aplica.",
        3500,
    ),
    "pdf": (
        "El resultado será un reporte en PDF. Redacta un trabajo académico completo: "
        "introducción, desarrollo en varias secciones con párrafos bien argumentados, y conclusión. "
        "Agrega referencias al final si aplica.",
        3500,
    ),
    "pptx": (
        "El resultado será una presentación de PowerPoint. Cada sección '##' se convierte en una "
        "diapositiva: usa entre 6 y 10 secciones, cada una con 3 a 5 viñetas breves (máximo 20 palabras "
        "cada una). Evita párrafos largos. Termina con una sección de conclusiones.",
        2500,
    ),
    "triptico": (
        "El resultado será un tríptico (folleto de 6 paneles). Usa de 5 a 7 secciones '##' cortas y "
        "llamativas, con frases concisas y viñetas. En total no más de 450 palabras. La última sección "
        "debe ser un cierre (conclusión, datos clave o recomendaciones).",
        2000,
    ),
}

_MARKDOWN_RULES = """Escribe la respuesta en Markdown usando SOLO estos elementos:
- "## Título de sección" y "### Subtítulo"
- Párrafos normales, **negritas** y *cursivas*
- Listas con "- " o "1. "
- Tablas Markdown (| col | col | con fila separadora |---|---|) cuando comparar información ayude
- Gráficas, SOLO si la tarea lo pide o si hay datos reales y conocidos que graficar (nunca inventes estadísticas), con este bloque exacto:
```grafica
{"tipo": "barras", "titulo": "Título", "etiquetas": ["A", "B", "C"], "series": [{"nombre": "Serie", "valores": [10, 20, 30]}]}
```
  (tipo puede ser "barras", "lineas" o "pastel")
No incluyas portada, nombre del alumno, fecha ni el título de la tarea: eso se agrega automáticamente.
No uses ningún otro tipo de bloque de código."""


def generate_assignment_response(
    course_name: str,
    task_title: str,
    task_description: str,
    output_format: str = "docx",
) -> str:
    guide, max_tokens = _FORMAT_GUIDE.get(output_format, _FORMAT_GUIDE["docx"])
    system = f"""Eres un estudiante universitario aplicado de la Universidad Ciudadana de Nuevo León (UCNL)
cursando Ingeniería en Desarrollo de Software. Debes redactar respuestas académicas completas,
bien estructuradas y en español. Usa un tono formal pero claro.

{guide}

{_MARKDOWN_RULES}"""

    user = f"""Materia: {course_name}
Tarea: {task_title}

Instrucciones de la tarea:
{task_description}

Redacta el contenido completo para esta tarea."""

    logger.info(f"Generando respuesta IA ({output_format}) para tarea: {task_title}")
    return _chat(system, user, max_tokens=max_tokens)


def analyze_single_question(course_name: str, question: str, options: list[str]) -> int:
    """Analyze one exam question in real-time and return the index of the best answer."""
    options_text = "\n".join(f"{i}. {opt}" for i, opt in enumerate(options))
    system = (
        "Eres un estudiante universitario de Ingeniería en Desarrollo de Software en la UCNL. "
        "Responde la pregunta eligiendo la opción más correcta. "
        "Responde ÚNICAMENTE con el número del índice (0, 1, 2…). Sin texto adicional."
    )
    user = f"Materia: {course_name}\n\nPregunta: {question}\n\nOpciones:\n{options_text}\n\n¿Índice de la respuesta correcta?"
    try:
        raw = _chat(system, user, max_tokens=10)
        idx = int("".join(c for c in raw.strip() if c.isdigit())[:1])
        return idx if 0 <= idx < len(options) else 0
    except Exception as e:
        logger.error(f"Error analizando pregunta individual: {e}")
        return 0


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
