"""
Parser mínimo del Markdown que genera la IA.

Soporta:
  # / ## / ###        títulos
  - item / 1. item    listas
  | a | b |           tablas (la primera fila es el encabezado)
  ```grafica {json}```  gráficas (ver charts.normalize_chart)
  > **75%** de …      cifra clave / cita destacada
  ![descripción](imagen)  foto buscada en Pexels por su descripción
  **negrita** / *cursiva* dentro del texto
"""
import json
import re

from ..logger import logger

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_FENCE = re.compile(r"^\s*```\s*(\w*)\s*$")
_IMAGE = re.compile(r"^!\[([^\]]+)\]\(([^)]*)\)$")
_INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*)")

DIAGRAM_FENCES = {"diagrama", "diagram", "mermaid"}
_MERMAID_STARTS = ("flowchart", "graph ", "graph\t", "mindmap", "sequencediagram", "timeline", "classdiagram")
CHART_FENCES = {"grafica", "gráfica", "chart", "grafico", "gráfico", "json"}


def parse(text: str) -> list[dict]:
    blocks: list[dict] = []
    lines = (text or "").replace("\r\n", "\n").split("\n")
    paragraph: list[str] = []
    i = 0

    def flush_paragraph():
        if paragraph:
            blocks.append({"type": "paragraph", "text": " ".join(paragraph).strip()})
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            i += 1
            continue

        fence = _FENCE.match(line)
        if fence:
            flush_paragraph()
            lang = fence.group(1).lower()
            body = []
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # cerrar fence
            raw = "\n".join(body)
            first = raw.strip().split("\n", 1)[0].strip().lower()
            if lang in DIAGRAM_FENCES or (lang == "" and first.startswith(_MERMAID_STARTS)):
                blocks.append({"type": "diagram", "code": raw.strip()})
                continue
            if lang in CHART_FENCES or lang == "":
                try:
                    blocks.append({"type": "chart", "spec": json.loads(raw)})
                    continue
                except json.JSONDecodeError:
                    if lang in CHART_FENCES and lang != "json":
                        logger.warning("Bloque de gráfica con JSON inválido — se omite")
                        continue
            blocks.append({"type": "paragraph", "text": raw})
            continue

        heading = _HEADING.match(stripped)
        if heading:
            flush_paragraph()
            level = min(len(heading.group(1)), 3)
            blocks.append({"type": "heading", "level": level, "text": heading.group(2).strip(" #")})
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush_paragraph()
            header = _split_row(stripped)
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i].strip()))
                i += 1
            width = len(header)
            rows = [(r + [""] * width)[:width] for r in rows]
            blocks.append({"type": "table", "header": header, "rows": rows})
            continue

        image = _IMAGE.match(stripped)
        if image:
            flush_paragraph()
            blocks.append({"type": "image", "query": image.group(1).strip()})
            i += 1
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            # Cada línea que empieza con "> **dato**" es una cifra clave distinta;
            # las demás líneas ">" continúan la anterior
            quotes: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                text = lines[i].strip().lstrip(">").strip()
                if text and (not quotes or text.startswith("**")):
                    quotes.append([text])
                elif text:
                    quotes[-1].append(text)
                i += 1
            blocks.extend({"type": "callout", "text": " ".join(q)} for q in quotes)
            continue

        bullet = _BULLET.match(line)
        numbered = _NUMBERED.match(line)
        if bullet or numbered:
            flush_paragraph()
            ordered = bool(numbered) and not bullet
            pattern = _NUMBERED if ordered else _BULLET
            items = []
            while i < len(lines) and pattern.match(lines[i]):
                items.append(pattern.match(lines[i]).group(1).strip())
                i += 1
            blocks.append({"type": "list", "ordered": ordered, "items": items})
            continue

        paragraph.append(stripped)
        i += 1

    flush_paragraph()
    return blocks


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def inline_runs(text: str) -> list[tuple[str, bool, bool]]:
    """Divide el texto en (fragmento, negrita, cursiva)."""
    runs = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            runs.append((part[2:-2], True, False))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            runs.append((part[1:-1], False, True))
        else:
            runs.append((part, False, False))
    return runs


def plain(text: str) -> str:
    return "".join(r[0] for r in inline_runs(text))


def split_sections(blocks: list[dict]) -> list[dict]:
    """
    Agrupa los bloques en secciones usando el nivel de título más alto
    que aparezca más de una vez (normalmente ##).
    Devuelve [{"title": str | None, "blocks": [...]}].
    """
    levels = [b["level"] for b in blocks if b["type"] == "heading"]
    split_level = next((lvl for lvl in (1, 2, 3) if levels.count(lvl) > 1), None)
    if split_level is None:
        split_level = min(levels) if levels else 99

    sections: list[dict] = []
    current = {"title": None, "blocks": []}
    for b in blocks:
        if b["type"] == "heading" and b["level"] <= split_level:
            if current["title"] or current["blocks"]:
                sections.append(current)
            current = {"title": plain(b["text"]), "blocks": []}
        else:
            current["blocks"].append(b)
    if current["title"] or current["blocks"]:
        sections.append(current)
    return sections
