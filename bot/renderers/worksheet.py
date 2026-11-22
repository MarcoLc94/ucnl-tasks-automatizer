"""
Actividades que se entregan contestando un Word adjunto por el profesor.

1. outline(): convierte el Word en texto con marcas de ubicación para la IA:
       [P7] ________________
       [T0 R0 C2] (vacía)
2. La IA responde con líneas en el mismo formato (editables en el panel):
       [P7] My name is Linda and I am from Monterrey.
       [T0 R0 C2] She
       [P43] SUBRAYAR: I am shocked by Alice’s opinion.
       [DESPUÉS P5] Texto que va en un párrafo nuevo después de P5
       [P3] MARCAR                       → casilla ☐ → ☒
       [P37] REEMPLAZAR: nuevo texto     → sustituye el texto del párrafo
       [INICIO] / [FINAL]                → secciones nuevas al principio / al final del documento
                                           (líneas "## Título" = subtítulo)
   Las líneas que no empiezan con "[" continúan la respuesta anterior (saltos de línea).
   Los párrafos de marcador ("Haga clic o pulse aquí para escribir texto.", "Borra esto y coloca aquí…")
   se reemplazan por la respuesta.
3. fill(): escribe las respuestas en una copia del Word original, conservando su formato.
"""
import copy
import re
from pathlib import Path

from docx import Document
from docx.text.paragraph import Paragraph

_BLANK = re.compile(r"_{3,}")
_LINE = re.compile(r"^\[(DESPU[EÉ]S\s+)?(P\d+|T\d+\s+R\d+\s+C\d+|INICIO|FINAL)\]\s*(.*)$", re.I)
_UNDERLINE = re.compile(r"^(SUBRAYAR|SELECCIONAR|MARCAR|CIRCULAR)\s*:\s*", re.I)
_CHECK = re.compile(r"^(MARCAR|X|✓|☒)\s*$", re.I)
_REPLACE = re.compile(r"^REEMPLAZAR\s*:\s*", re.I)
_PLACEHOLDER = re.compile(
    r"(haga clic o pulse aqu[ií]|click or tap here|borra esto|escrib[ae] aqu[ií]|coloca aqu[ií]|escriba su respuesta)",
    re.I,
)
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _full_text(p: Paragraph) -> str:
    """Texto del párrafo incluyendo controles de contenido (casillas ☐, campos), que p.text omite."""
    return "".join(t.text or "" for t in p._p.iter(f"{_W}t"))


def outline(path: Path, max_chars: int = 12000) -> str:
    """Texto del Word con marcas [P#] y [T# R# C#] para que la IA sepa dónde contestar."""
    doc = Document(str(path))
    lines = []
    # Mantener las listas vivas: así cada elemento XML conserva su identidad al comparar
    paragraphs, tables = list(doc.paragraphs), list(doc.tables)
    p_index = {p._p: n for n, p in enumerate(paragraphs)}
    t_index = {t._tbl: n for n, t in enumerate(tables)}
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            n = p_index.get(child)
            text = _full_text(paragraphs[n]).strip() if n is not None else ""
            if text:
                lines.append(f"[P{n}] {text}")
        elif tag == "tbl":
            t = t_index.get(child)
            if t is None:
                continue
            table = tables[t]
            lines.append(f"[T{t}] Tabla de {len(table.rows)} filas x {len(table.columns)} columnas:")
            for r, row in enumerate(table.rows):
                seen = set()
                cells = []
                for c, cell in enumerate(row.cells):
                    if id(cell._tc) in seen:  # celdas combinadas
                        continue
                    seen.add(id(cell._tc))
                    cells.append(f"[T{t} R{r} C{c}] {cell.text.strip() or '(vacía)'}")
                lines.append("   " + " | ".join(cells))
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars] + "\n[… documento recortado …]"


def parse_answers(text: str) -> list[dict]:
    answers: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        m = _LINE.match(line.strip())
        if m:
            after, loc, value = bool(m.group(1)), m.group(2).upper(), m.group(3).strip()
            item = {"after": after, "value": value}
            if loc in ("INICIO", "FINAL"):
                item["where"] = loc
            elif loc.startswith("P"):
                item["p"] = int(loc[1:])
            else:
                t, r, c = map(int, re.findall(r"\d+", loc))
                item.update(t=t, r=r, c=c)
            answers.append(item)
        elif answers and line.strip():
            answers[-1]["value"] += "\n" + line.strip()
    return answers


def _first_run_format(p: Paragraph):
    for r in p.runs:
        if r.text.strip():
            return r
    return p.runs[0] if p.runs else None


def _rebuild(p: Paragraph, segments: list[tuple[str, dict]]):
    """Reemplaza el texto del párrafo por segmentos (texto, formato extra) conservando la fuente del original."""
    base = _first_run_format(p)
    base_rpr = copy.deepcopy(base._r.rPr) if base is not None and base._r.rPr is not None else None
    for r in list(p.runs):
        r._r.getparent().remove(r._r)
    for text, fmt in segments:
        if not text:
            continue
        for i, chunk in enumerate(text.split("\n")):
            if i:
                p.add_run().add_break()
            run = p.add_run(chunk)
            if base_rpr is not None:
                run._r.insert(0, copy.deepcopy(base_rpr))
            if fmt.get("underline"):
                run.underline = True
            if fmt.get("bold"):
                run.bold = True
            if fmt.get("no_underline"):
                run.underline = False


def _norm_quotes(s: str) -> str:
    return s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def _check_box(p: Paragraph) -> bool:
    for t in p._p.iter(f"{_W}t"):
        if t.text and "☐" in t.text:
            t.text = t.text.replace("☐", "☒", 1)
            return True
    return False


def _replace_all(p: Paragraph, value: str):
    """Sustituye TODO el contenido del párrafo (incluidos controles de contenido) conservando el formato."""
    base_rpr = None
    first_r = next(p._p.iter(f"{_W}r"), None)
    if first_r is not None and first_r.find(f"{_W}rPr") is not None:
        base_rpr = copy.deepcopy(first_r.find(f"{_W}rPr"))
        # Los marcadores suelen ir en gris: quitar el color para que la respuesta se vea normal
        for tag in ("color", "shd", "highlight"):
            el = base_rpr.find(f"{_W}{tag}")
            if el is not None:
                base_rpr.remove(el)
    for child in list(p._p):
        if child.tag != f"{_W}pPr":
            p._p.remove(child)
    for i, chunk in enumerate(value.split("\n")):
        if i:
            p.add_run().add_break()
        run = p.add_run(chunk)
        if base_rpr is not None:
            run._r.insert(0, copy.deepcopy(base_rpr))


def _fill_paragraph(p: Paragraph, value: str) -> bool:
    if _CHECK.match(value):
        return _check_box(p)
    rm = _REPLACE.match(value)
    if rm:
        _replace_all(p, value[rm.end():].strip())
        return True
    if _PLACEHOLDER.search(_full_text(p)):
        _replace_all(p, value)
        return True
    text = p.text
    um = _UNDERLINE.match(value)
    if um:
        option = value[um.end():].strip().strip('"')
        idx = _norm_quotes(text).find(_norm_quotes(option))
        if idx < 0:
            return False
        _rebuild(p, [(text[:idx], {}), (text[idx:idx + len(option)], {"underline": True, "bold": True}),
                     (text[idx + len(option):], {})])
        return True

    blanks = list(_BLANK.finditer(text))
    if blanks:
        # Varias líneas en blanco en el mismo párrafo: respuestas separadas por " | "
        parts = [v.strip() for v in value.split(" | ")] if len(blanks) > 1 else [value]
        segments, pos = [], 0
        for i, m in enumerate(blanks):
            segments.append((text[pos:m.start()], {}))
            answer = parts[i] if i < len(parts) else ""
            if answer:
                # Evitar que la respuesta quede pegada a la palabra de al lado ("_____video" → "her video")
                if m.start() > 0 and text[m.start() - 1].isalnum():
                    answer = " " + answer
                if m.end() < len(text) and text[m.end()].isalnum():
                    answer = answer + " "
            segments.append((answer or m.group(0), {"underline": bool(answer.strip())}))
            pos = m.end()
        segments.append((text[pos:], {}))
        _rebuild(p, segments)
        return True
    return False


def _insert_after(p: Paragraph, value: str) -> Paragraph:
    new_p = copy.deepcopy(p._p)
    for child in list(new_p):
        if child.tag.rsplit("}", 1)[-1] != "pPr":
            new_p.remove(child)
    p._p.addnext(new_p)
    para = Paragraph(new_p, p._parent)
    for i, chunk in enumerate(value.split("\n")):
        if i:
            para.add_run().add_break()
        para.add_run(chunk)
    return para


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9ñ ]+", " ", s).split())


def _check_listed_options(paragraphs: list, start: int, value: str) -> bool:
    """
    Si la respuesta a una pregunta enumera opciones que están abajo como casillas (☐ Opción),
    marca esas casillas. Devuelve True si marcó alguna (y entonces no se escribe el texto).
    """
    answer = _norm(value)
    marked = False
    for p in paragraphs[start + 1:start + 16]:
        text = _full_text(p).strip()
        if not text:
            continue
        if "☐" not in text and "☒" not in text:
            break  # terminó la lista de casillas de esta pregunta
        label = _norm(text.replace("☐", "").replace("☒", ""))
        if label and label in answer:
            if "☐" in text:
                _check_box(p)
            marked = True  # ya marcada (p. ej. por una línea MARCAR) también cuenta: el texto sobra
    return marked


def _add_section(doc, value: str) -> list:
    """Agrega párrafos al final del documento; '## Título' se vuelve subtítulo. Devuelve los elementos XML creados."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt
    created = []
    for line in value.split("\n"):
        line = line.strip()
        if not line:
            continue
        p = doc.add_paragraph()
        if line.startswith("#"):
            r = p.add_run(line.lstrip("#").strip())
            r.bold = True
            r.font.size = Pt(14)
            p.paragraph_format.space_before = Pt(12)
        else:
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            p.add_run(line)
        created.append(p._p)
    return created


def _move_to_start(doc, elements: list):
    body = doc.element.body
    for i, el in enumerate(elements):
        body.remove(el)
        body.insert(i, el)


def _page_break_element(doc):
    from docx.enum.text import WD_BREAK
    p = doc.add_paragraph()
    p.add_run().add_break(WD_BREAK.PAGE)
    return p._p


def fill(original: Path, answers_text: str, out_path: Path, cover=None) -> tuple[Path, list[str]]:
    """
    Escribe las respuestas en una copia del Word. Si se da `cover` (CoverInfo), antepone la portada.
    Devuelve (ruta, avisos de respuestas que no se pudieron colocar).
    """
    doc = Document(str(original))
    paragraphs = list(doc.paragraphs)
    warnings = []
    last_inserted: dict[int, Paragraph] = {}
    redirected: set[int] = set()  # campos "Haga clic aquí" llenados desde la respuesta a su pregunta
    start_blocks, end_blocks = [], []

    for a in parse_answers(answers_text):
        value = a["value"]
        if "where" in a:
            (start_blocks if a["where"] == "INICIO" else end_blocks).append(value)
            continue
        if "p" in a:
            if a["p"] >= len(paragraphs):
                warnings.append(f"[P{a['p']}] no existe en el documento")
                continue
            p = paragraphs[a["p"]]
            # Respuesta explícita a un campo que ya se llenó desde su pregunta: la explícita manda
            if a["p"] in redirected and not a["after"]:
                _replace_all(p, value)
                continue
            plain_answer = not a["after"] and not _BLANK.search(p.text) and not _PLACEHOLDER.search(_full_text(p)) \
                and not _CHECK.match(value) and not _REPLACE.match(value) and not _UNDERLINE.match(value)
            # La respuesta enumera opciones que abajo son casillas → marcarlas en vez de escribir texto
            if plain_answer and _check_listed_options(paragraphs, a["p"], value):
                continue
            # Si respondieron sobre la pregunta y justo debajo hay un campo "Haga clic aquí…", usar ese campo
            if plain_answer:
                for offset, nxt in enumerate(paragraphs[a["p"] + 1:a["p"] + 4], start=a["p"] + 1):
                    nxt_text = _full_text(nxt).strip()
                    if _PLACEHOLDER.search(nxt_text):
                        p = nxt
                        redirected.add(offset)
                        break
                    if nxt_text:  # hay otro contenido antes: no es el campo de esta pregunta
                        break
            if a["after"] or not _fill_paragraph(p, value):
                if _UNDERLINE.match(value):
                    warnings.append(f"[P{a['p']}] no se encontró la opción a subrayar")
                    continue
                anchor = last_inserted.get(a["p"], p)
                last_inserted[a["p"]] = _insert_after(anchor, value)
        else:
            try:
                cell = doc.tables[a["t"]].cell(a["r"], a["c"])
            except IndexError:
                warnings.append(f"[T{a['t']} R{a['r']} C{a['c']}] no existe en el documento")
                continue
            target = cell.paragraphs[-1]
            if cell.text.strip() and not _BLANK.search(cell.text):
                target = _insert_after(target, value)
            elif not _fill_paragraph(target, value):
                target.add_run(value)

    for value in end_blocks:
        _add_section(doc, value)

    # Principio del documento: portada (si se pidió) + secciones [INICIO], luego el formato original
    head = []
    if cover is not None:
        from .docx_renderer import _add_cover
        body = doc.element.body
        keep = list(body)  # mantener vivas las referencias para que id() sea estable
        existing = {id(el) for el in keep}
        _add_cover(doc, cover)  # se agrega al final…
        head += [el for el in body if id(el) not in existing and el.tag != f"{_W}sectPr"]
        del keep
    for value in start_blocks:
        head += _add_section(doc, value)
    if start_blocks:
        head.append(_page_break_element(doc))
    if head:
        _move_to_start(doc, head)  # …y se mueve al principio

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path, warnings
