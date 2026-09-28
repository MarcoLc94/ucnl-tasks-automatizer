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

from ..images import get_site_logo
from ..logger import logger
from .common import CoverInfo
from .diagrams import DiagramError, render_diagrams  # noqa: F401 (DiagramError se re-exporta)
from .markdown import parse
from .docx_renderer import render_docx
from .pdf_renderer import render_infografia_png, render_pdf, render_triptico
from .pptx_renderer import render_pptx

FORMATS = {
    "docx": "Word (.docx)",
    "pptx": "PowerPoint (.pptx)",
    "pdf": "PDF",
    "triptico": "Tríptico (PDF)",
    "infografia": "Infografía (Word)",
    "worksheet": "Contestar Word adjunto",
}
_EXTENSIONS = {"docx": ".docx", "pptx": ".pptx", "pdf": ".pdf", "triptico": ".pdf", "infografia": ".docx", "worksheet": ".docx"}

_MONTHS = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

# Orden importa: lo más específico primero
_KEYWORDS = [
    ("infografia", ["infografia", "infograma", "cartel", "poster", "afiche"]),
    ("triptico", ["triptico", "folleto", "diptico", "brochure"]),
    ("pptx", ["presentacion", "powerpoint", "power point", "diapositiva", "ppt", "slides"]),
    ("pdf", ["pdf"]),
    ("docx", ["word", "docx", "ensayo", "reporte", "informe"]),
]


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def wants_toc(task_description: str) -> bool:
    """¿Las instrucciones piden índice?"""
    return bool(re.search(r"\b(indice|tabla de contenido)", _normalize(task_description or "")))


def wants_worksheet(task_description: str, attachments: list[str] | None) -> bool:
    """Actividad que se contesta sobre un Word adjunto ("descarga el archivo…", "contesta el documento…")."""
    if not any(str(a).lower().endswith(".docx") for a in attachments or []):
        return False
    text = _normalize(task_description or "")
    return bool(re.search(r"\b(descarg|adjunt|contest|resuelve|completa|llena|realiza la actividad)", text))


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


def clean_course_name(name: str) -> str:
    """'Desarrollo Sustentable (A) (2026-3)' → 'Desarrollo Sustentable'. Solo quita grupo y periodo."""
    pattern = r"\s*\((?:[A-Z]{1,2}\d?|\d{4}-\d{1,2})\)\s*$"
    while True:
        cleaned = re.sub(pattern, "", name).strip()
        if cleaned == name or not cleaned:
            return name
        name = cleaned


def course_group(name: str) -> str:
    """'Química I (F) (2026-3)' → 'F'."""
    m = re.search(r"\(([A-Z]{1,2}\d?)\)", name or "")
    return m.group(1) if m else ""


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
    career: str = "",
    grade: str = "",
    teacher: str = "",
    include_toc: bool = False,
    level: str = "licenciatura",
    matricula: str = "",
    place: str = "",
    site_url: str = "",
    attachment: str | None = None,
) -> Path:
    fmt = output_format if output_format in FORMATS else "docx"
    if fmt == "worksheet":
        # El Word del profesor con las respuestas escritas en su lugar (sin portada propia)
        if not attachment or not Path(attachment).exists():
            raise RuntimeError("Esta tarea no tiene un Word adjunto para contestar")
        from .worksheet import fill
        out = Path(output_dir) / f"{safe_filename(task_title)}.docx"
        _, warnings = fill(Path(attachment), markdown_text, out)
        for w in warnings:
            logger.warning(f"Contestar Word: {w}")
        logger.info(f"Documento generado (Word adjunto contestado): {out.name}")
        return out
    cover = CoverInfo(
        university="Universidad Ciudadana de Nuevo León",
        career=career,
        course=clean_course_name(course_name),
        title=task_title,
        student=student_name,
        date=spanish_date(),
        grade=grade,
        teacher=teacher,
        career_label="Bachillerato" if level == "bachillerato" else "Licenciatura",
        group=course_group(course_name),
        matricula=matricula,
        place=place,
        logo=get_site_logo(site_url) if site_url else None,
    )
    path = Path(output_dir) / f"{safe_filename(task_title)}{_EXTENSIONS[fmt]}"
    work_dir = Path(tempfile.mkdtemp(prefix="ucnl_render_"))
    try:
        # Los diagramas se dibujan antes (es asíncrono); los renderizadores solo insertan el PNG
        await render_diagrams(parse(markdown_text), work_dir)
        if fmt == "docx":
            render_docx(markdown_text, cover, path, work_dir, include_toc=include_toc)
        elif fmt == "pptx":
            render_pptx(markdown_text, cover, path, work_dir)
        elif fmt == "pdf":
            await render_pdf(markdown_text, cover, path, work_dir)
        elif fmt == "infografia":
            # Word con portada/intro/conclusiones/referencias; el contenido es la infografía como imagen
            poster = await render_infografia_png(markdown_text, cover, work_dir / "infografia.png", work_dir)
            render_docx(markdown_text, cover, path, work_dir, body_image=poster, include_toc=include_toc)
        else:
            await render_triptico(markdown_text, cover, path, work_dir)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    logger.info(f"Documento generado ({FORMATS[fmt]}): {path.name}")
    return path


def _office_binary() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


async def to_pdf_for_preview(path: Path) -> Path | None:
    """Convierte un .docx/.pptx a PDF con LibreOffice (solo para verlo). None si no está instalado."""
    import asyncio

    binary = _office_binary()
    if not binary:
        return None
    out_dir = path.parent / "pdf_preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="ucnl_lo_"))  # perfil propio: no choca con un LibreOffice abierto
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, f"-env:UserInstallation=file://{profile}", "--headless",
            "--convert-to", "pdf", "--outdir", str(out_dir), str(path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=90)
    except Exception as e:
        logger.warning(f"No se pudo convertir {path.name} a PDF para vista previa: {e}")
        return None
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    pdf = out_dir / f"{path.stem}.pdf"
    return pdf if pdf.exists() else None
