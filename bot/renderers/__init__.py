"""
Generación de entregables con formato a partir del Markdown que produce la IA.

    path = await build_document("pptx", markdown, course_name, task_title, student_name, output_dir)
"""
import re
import shutil
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

from ..logger import logger
from .common import CoverInfo
from .docx_renderer import render_docx
from .pdf_renderer import render_pdf, render_triptico
from .pptx_renderer import render_pptx

FORMATS = {
    "docx": "Word (.docx)",
    "pptx": "PowerPoint (.pptx)",
    "pdf": "PDF",
    "triptico": "Tríptico (PDF)",
}
_EXTENSIONS = {"docx": ".docx", "pptx": ".pptx", "pdf": ".pdf", "triptico": ".pdf"}

_MONTHS = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

# Orden importa: lo más específico primero
_KEYWORDS = [
    ("triptico", ["triptico", "folleto", "diptico", "brochure"]),
    ("pptx", ["presentacion", "powerpoint", "power point", "diapositiva", "ppt", "slides"]),
    ("pdf", ["pdf"]),
    ("docx", ["word", "docx", "ensayo", "reporte", "informe"]),
]


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def detect_format(task_title: str, task_description: str) -> str:
    """Deduce el formato de entrega a partir del título y las instrucciones."""
    text = _normalize(f"{task_title}\n{task_description}")
    for fmt, words in _KEYWORDS:
        if any(re.search(rf"\b{re.escape(w)}", text) for w in words):
            return fmt
    return "docx"


def spanish_date(dt: datetime | None = None) -> str:
    dt = dt or datetime.now()
    return f"{dt.day} de {_MONTHS[dt.month - 1]} de {dt.year}"


def safe_filename(title: str) -> str:
    name = "".join(c for c in title if c.isalnum() or c in " _-")[:50].strip()
    return name or "entrega"


async def build_document(
    output_format: str,
    markdown_text: str,
    course_name: str,
    task_title: str,
    student_name: str,
    output_dir: Path,
) -> Path:
    fmt = output_format if output_format in FORMATS else "docx"
    cover = CoverInfo(
        university="Universidad Ciudadana de Nuevo León",
        career="Ingeniería en Desarrollo de Software",
        course=course_name,
        title=task_title,
        student=student_name,
        date=spanish_date(),
    )
    path = Path(output_dir) / f"{safe_filename(task_title)}{_EXTENSIONS[fmt]}"
    work_dir = Path(tempfile.mkdtemp(prefix="ucnl_render_"))
    try:
        if fmt == "docx":
            render_docx(markdown_text, cover, path, work_dir)
        elif fmt == "pptx":
            render_pptx(markdown_text, cover, path, work_dir)
        elif fmt == "pdf":
            await render_pdf(markdown_text, cover, path, work_dir)
        else:
            await render_triptico(markdown_text, cover, path, work_dir)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    logger.info(f"Documento generado ({FORMATS[fmt]}): {path.name}")
    return path
