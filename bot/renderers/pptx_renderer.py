from pathlib import Path

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Inches, Pt

from PIL import Image

from ..images import fetch_photo
from .charts import PALETTE, normalize_chart
from .common import CoverInfo, reference_entries, split_document
from .markdown import inline_runs, parse, plain, split_sections

PRIMARY = RGBColor(0x1F, 0x3A, 0x5F)
ACCENT = RGBColor(0x2E, 0x86, 0xAB)
TEXT = RGBColor(0x33, 0x33, 0x33)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

SLIDE_W, SLIDE_H = Inches(13.333), Inches(7.5)
MAX_BULLETS = 6
MAX_CHARS = 650

_CHART_TYPES = {
    "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "line": XL_CHART_TYPE.LINE_MARKERS,
    "pie": XL_CHART_TYPE.PIE,
}


def render_pptx(markdown_text: str, cover: CoverInfo, path: Path, work_dir: Path) -> Path:
    prs = Presentation()
    prs.slide_width, prs.slide_height = SLIDE_W, SLIDE_H
    blank = prs.slide_layouts[6]

    _title_slide(prs.slides.add_slide(blank), cover)

    for section in _ordered_sections(parse(markdown_text)):
        title = section["title"] or cover.title
        bullets: list[tuple[str, int]] = []  # (texto, nivel)
        photo = next(
            (p for b in section["blocks"] if b["type"] == "image"
             for p in [fetch_photo(b["query"], work_dir, "portrait")] if p),
            None,
        )

        def flush():
            nonlocal photo
            for chunk in _chunk_bullets(bullets, narrow=photo is not None):
                _bullet_slide(prs.slides.add_slide(blank), title, chunk, photo)
                photo = None  # solo en la primera diapositiva de la sección
            bullets.clear()

        for block in section["blocks"]:
            kind = block["type"]
            if kind == "heading":
                bullets.append((f"**{plain(block['text'])}**", 0))
            elif kind in ("paragraph", "callout"):
                bullets.append((block["text"], 0))
            elif kind == "list":
                nested = bool(bullets) and bullets[-1][0].startswith("**")
                bullets.extend((item, 1 if nested else 0) for item in block["items"])
            elif kind == "table":
                flush()
                _table_slide(prs.slides.add_slide(blank), title, block["header"], block["rows"])
            elif kind == "chart":
                flush()
                chart = normalize_chart(block["spec"])
                if chart:
                    _chart_slide(prs.slides.add_slide(blank), title, chart)
        flush()

    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))
    return path


def _ordered_sections(blocks: list[dict]) -> list[dict]:
    """Introducción → contenido → Conclusiones → Referencias (las referencias ordenadas alfabéticamente)."""
    parts = split_document(blocks)
    sections = []
    if parts["intro"]:
        sections.append({"title": "Introducción", "blocks": parts["intro"]})
    sections += split_sections(parts["body"])
    if parts["conclusion"]:
        sections.append({"title": "Conclusiones", "blocks": parts["conclusion"]})
    if parts["references"]:
        refs = sorted(reference_entries(parts["references"]), key=lambda t: plain(t).lower())
        sections.append({"title": "Referencias", "blocks": [{"type": "list", "ordered": False, "items": refs}]})
    return sections


def _chunk_bullets(bullets: list[tuple[str, int]], narrow: bool = False) -> list[list[tuple[str, int]]]:
    max_chars = MAX_CHARS * 0.6 if narrow else MAX_CHARS
    chunks, current, chars = [], [], 0
    for b in bullets:
        if current and (len(current) >= MAX_BULLETS or chars + len(b[0]) > max_chars):
            chunks.append(current)
            current, chars = [], 0
        current.append(b)
        chars += len(b[0])
    if current:
        chunks.append(current)
    return chunks


def _rect(slide, left, top, width, height, color):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


def _text(slide, left, top, width, height, text, size, bold=False, color=TEXT, align=PP_ALIGN.LEFT):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.alignment = align
    r = p.add_run()
    r.text = text
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.color.rgb = color
    return tb


def _title_slide(slide, cover: CoverInfo):
    _rect(slide, 0, 0, SLIDE_W, SLIDE_H, PRIMARY)
    _rect(slide, Inches(0.8), Inches(3.55), Inches(1.5), Inches(0.08), ACCENT)
    _text(slide, Inches(0.8), Inches(0.6), Inches(11.5), Inches(0.5), cover.university.upper(), 14, True, WHITE)
    _text(slide, Inches(0.8), Inches(1.0), Inches(11.5), Inches(0.4), cover.career, 12, color=RGBColor(0xC8, 0xD6, 0xE5))
    tb = _text(slide, Inches(0.8), Inches(1.9), Inches(11.5), Inches(1.6), cover.title, 40, True, WHITE)
    tb.text_frame.vertical_anchor = MSO_ANCHOR.BOTTOM

    # Datos de la portada: licenciatura, materia, grado, alumno, docente, fecha
    box = slide.shapes.add_textbox(Inches(0.8), Inches(3.9), Inches(11.5), Inches(3.2))
    tf = box.text_frame
    tf.word_wrap = True
    for i, (label, value) in enumerate(f for f in cover.fields() if f[0] != "Licenciatura"):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(6)
        r1 = p.add_run()
        r1.text = f"{label}: "
        r1.font.size = Pt(16)
        r1.font.bold = True
        r1.font.color.rgb = RGBColor(0xC8, 0xD6, 0xE5)
        r2 = p.add_run()
        r2.text = value
        r2.font.size = Pt(16)
        r2.font.color.rgb = WHITE


def _header(slide, title: str):
    _rect(slide, 0, 0, SLIDE_W, Inches(1.2), PRIMARY)
    _rect(slide, 0, Inches(1.2), SLIDE_W, Inches(0.06), ACCENT)
    tb = _text(slide, Inches(0.6), Inches(0.2), Inches(12.1), Inches(0.8), title, 28, True, WHITE)
    tb.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE


def _add_picture_fill(slide, path, left, top, width, height):
    """Inserta la imagen recortada (tipo 'cover') para llenar exactamente el área dada."""
    with Image.open(path) as im:
        img_ratio = im.width / im.height
    box_ratio = width / height
    pic = slide.shapes.add_picture(str(path), left, top, width, height)
    if img_ratio > box_ratio:
        crop = (1 - box_ratio / img_ratio) / 2
        pic.crop_left = pic.crop_right = crop
    else:
        crop = (1 - img_ratio / box_ratio) / 2
        pic.crop_top = pic.crop_bottom = crop
    return pic


def _bullet_slide(slide, title: str, bullets: list[tuple[str, int]], photo: dict | None = None):
    _header(slide, title)
    total = sum(len(b[0]) for b in bullets)
    if photo:
        total = total / 0.6  # menos ancho disponible → letra más chica antes
    size = 22 if total < 300 else 20 if total < 450 else 18 if total < 550 else 16

    text_width = Inches(11.7)
    if photo:
        text_width = Inches(7.4)
        _add_picture_fill(slide, photo["path"], Inches(8.6), Inches(1.26), Inches(4.733), Inches(6.24))
        _text(slide, Inches(8.7), Inches(7.1), Inches(4.5), Inches(0.3), photo["credit"], 9, color=WHITE)

    tb = slide.shapes.add_textbox(Inches(0.8), Inches(1.6), text_width, Inches(5.4))
    tf = tb.text_frame
    tf.word_wrap = True
    for i, (text, level) in enumerate(bullets):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.level = level
        p.space_after = Pt(10)
        is_subtitle = text.startswith("**") and text.endswith("**")
        marker = p.add_run()
        marker.text = "" if is_subtitle else ("–  " if level else "•  ")
        marker.font.size = Pt(size)
        marker.font.color.rgb = ACCENT
        marker.font.bold = True
        for chunk, bold, italic in inline_runs(text):
            r = p.add_run()
            r.text = chunk
            r.font.size = Pt(size - 2 * level)
            r.font.bold = bold
            r.font.italic = italic
            r.font.color.rgb = PRIMARY if is_subtitle else TEXT


def _table_slide(slide, title: str, header: list[str], rows: list[list[str]]):
    _header(slide, title)
    n_rows, n_cols = len(rows) + 1, len(header)
    size = 16 if n_rows <= 6 else 13 if n_rows <= 10 else 11
    height = Inches(min(5.4, 0.5 * n_rows))
    shape = slide.shapes.add_table(n_rows, n_cols, Inches(0.8), Inches(1.7), Inches(11.7), height)
    table = shape.table
    for r_idx, row in enumerate([header] + rows):
        for c_idx, text in enumerate(row):
            cell = table.cell(r_idx, c_idx)
            cell.text = plain(text)
            para = cell.text_frame.paragraphs[0]
            for run in para.runs:
                run.font.size = Pt(size)
                run.font.bold = r_idx == 0
                run.font.color.rgb = WHITE if r_idx == 0 else TEXT
            cell.fill.solid()
            cell.fill.fore_color.rgb = PRIMARY if r_idx == 0 else (
                RGBColor(0xEE, 0xF3, 0xF8) if r_idx % 2 else WHITE
            )


def _chart_slide(slide, title: str, chart: dict):
    _header(slide, chart["title"] or title)
    data = CategoryChartData()
    data.categories = chart["labels"]
    for name, values in chart["series"]:
        data.add_series(name or "Serie", values)

    frame = slide.shapes.add_chart(
        _CHART_TYPES[chart["kind"]], Inches(1), Inches(1.6), Inches(11.3), Inches(5.5), data
    )
    ch = frame.chart
    ch.font.size = Pt(14)
    ch.has_legend = chart["kind"] == "pie" or len(chart["series"]) > 1
    if ch.has_legend:
        ch.legend.position = XL_LEGEND_POSITION.BOTTOM
        ch.legend.include_in_layout = False

    plot = ch.plots[0]
    if chart["kind"] == "pie":
        plot.has_data_labels = True
        plot.data_labels.number_format = "0%"
        plot.data_labels.show_percentage = True
        plot.data_labels.show_value = False
        for idx, point in enumerate(plot.series[0].points):
            point.format.fill.solid()
            point.format.fill.fore_color.rgb = RGBColor.from_string(PALETTE[idx % len(PALETTE)][1:])
    else:
        for idx, series in enumerate(plot.series):
            color = RGBColor.from_string(PALETTE[idx % len(PALETTE)][1:])
            if chart["kind"] == "line":
                series.format.line.color.rgb = color
            else:
                series.format.fill.solid()
                series.format.fill.fore_color.rgb = color
