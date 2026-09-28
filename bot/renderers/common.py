import unicodedata
from dataclasses import dataclass

from .markdown import split_sections


@dataclass
class CoverInfo:
    university: str
    career: str
    course: str
    title: str
    student: str
    date: str
    grade: str = ""
    teacher: str = ""

    def fields(self) -> list[tuple[str, str]]:
        """Datos de la portada en orden, omitiendo los vacíos."""
        rows = [
            ("Licenciatura", self.career),
            ("Materia", self.course),
            ("Grado", self.grade),
            ("Alumno", self.student),
            ("Docente", self.teacher),
            ("Fecha", self.date),
        ]
        return [(k, v) for k, v in rows if v]


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c)).strip(" #*:")


_INTRO = ("introduccion",)
_CONCLUSION = ("conclusion",)
_REFERENCES = ("referencia", "fuente", "bibliograf", "ficha", "webgraf")


def split_document(blocks: list[dict]) -> dict:
    """
    Separa el trabajo en sus partes según los títulos de sección:
    {"intro": [bloques], "body": [bloques], "conclusion": [bloques], "references": [bloques]}.
    Lo que no es introducción, conclusiones o referencias es el contenido (se conservan sus títulos).
    """
    parts = {"intro": [], "body": [], "conclusion": [], "references": []}
    for sec in split_sections(blocks):
        title = _norm(sec["title"] or "")
        if title.startswith(_INTRO):
            parts["intro"] += sec["blocks"]
        elif title.startswith(_CONCLUSION):
            parts["conclusion"] += sec["blocks"]
        elif title.startswith(_REFERENCES):
            parts["references"] += sec["blocks"]
        else:
            if sec["title"]:
                parts["body"].append({"type": "heading", "level": 2, "text": sec["title"]})
            parts["body"] += sec["blocks"]
    return parts


def reference_entries(blocks: list[dict]) -> list[str]:
    """Referencias como entradas sueltas (de listas o párrafos)."""
    entries = []
    for b in blocks:
        if b["type"] == "list":
            entries += b["items"]
        elif b["type"] in ("paragraph", "callout"):
            entries.append(b["text"])
    return entries
