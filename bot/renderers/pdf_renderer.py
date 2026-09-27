"""
PDF (reporte) y tríptico: se arma HTML + CSS y Chromium (Playwright) lo imprime a PDF.
"""
import base64
import html
from pathlib import Path

from playwright.async_api import async_playwright

from .charts import render_chart_png
from .common import CoverInfo
from .markdown import inline_runs, parse, split_sections

_BASE_CSS = """
:root { --primary: #1F3A5F; --accent: #2E86AB; --text: #2b2b2b; --soft: #EEF3F8; }
* { box-sizing: border-box; }
body { margin: 0; font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif; color: var(--text); }
h1, h2, h3 { color: var(--primary); margin: 0.9em 0 0.35em; line-height: 1.2; }
p { margin: 0 0 0.6em; }
ul, ol { margin: 0 0 0.6em; padding-left: 1.3em; }
li { margin-bottom: 0.25em; }
li::marker { color: var(--accent); }
table { border-collapse: collapse; width: 100%; margin: 0.4em 0 0.9em; font-size: 0.92em; }
th { background: var(--primary); color: #fff; text-align: left; }
th, td { padding: 0.35em 0.5em; border: 1px solid #d5dde6; }
tr:nth-child(even) td { background: var(--soft); }
figure { margin: 0.6em 0 1em; text-align: center; }
figure img { max-width: 100%; }
figcaption { font-size: 0.85em; color: #666; margin-top: 0.3em; }
"""


def _inline(text: str) -> str:
    out = []
    for chunk, bold, italic in inline_runs(text):
        s = html.escape(chunk)
        if bold:
            s = f"<strong>{s}</strong>"
        if italic:
            s = f"<em>{s}</em>"
        out.append(s)
    return "".join(out)


def _block_html(block: dict, work_dir: Path, counter: list[int]) -> str:
    kind = block["type"]
    if kind == "heading":
        lvl = max(2, block["level"])  # h1 queda para el título del documento
        return f"<h{lvl}>{_inline(block['text'])}</h{lvl}>"
    if kind == "paragraph":
        return f"<p>{_inline(block['text'])}</p>"
    if kind == "list":
        tag = "ol" if block["ordered"] else "ul"
        items = "".join(f"<li>{_inline(i)}</li>" for i in block["items"])
        return f"<{tag}>{items}</{tag}>"
    if kind == "table":
        head = "".join(f"<th>{_inline(h)}</th>" for h in block["header"])
        rows = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in block["rows"])
        return f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"
    if kind == "chart":
        counter[0] += 1
        png = render_chart_png(block["spec"], work_dir / f"chart_{counter[0]}.png")
        if not png:
            return ""
        b64 = base64.b64encode(png.read_bytes()).decode()
        title = block["spec"].get("titulo") or block["spec"].get("title") or ""
        caption = html.escape(f"Figura {counter[0]}. {title}".strip(". "))
        return f'<figure><img src="data:image/png;base64,{b64}"><figcaption>{caption}</figcaption></figure>'
    return ""


async def _print_pdf(html_doc: str, path: Path, fit_panels: bool = False, **pdf_opts) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html_doc, wait_until="load")
            if fit_panels:
                # Reduce la letra de cada panel hasta que su contenido quepa
                await page.evaluate("""() => {
                  for (const el of document.querySelectorAll('.panel-inner')) {
                    let size = 10.5;
                    el.style.fontSize = size + 'pt';
                    while (el.scrollHeight > el.clientHeight + 1 && size > 6.5) {
                      size -= 0.25;
                      el.style.fontSize = size + 'pt';
                    }
                  }
                }""")
            await page.pdf(path=str(path), print_background=True, prefer_css_page_size=True, **pdf_opts)
        finally:
            await browser.close()
    return path


# ─── Reporte PDF ──────────────────────────────────────────────────────────────

async def render_pdf(markdown_text: str, cover: CoverInfo, path: Path, work_dir: Path) -> Path:
    counter = [0]
    body = "\n".join(_block_html(b, work_dir, counter) for b in parse(markdown_text))
    e = html.escape
    doc = f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><style>
{_BASE_CSS}
@page {{ size: Letter; margin: 2.2cm 2.4cm; }}
body {{ font-size: 11pt; line-height: 1.5; text-align: justify; }}
h2 {{ font-size: 16pt; border-bottom: 2px solid var(--accent); padding-bottom: 0.15em; }}
h3 {{ font-size: 13pt; }}
h4 {{ font-size: 11.5pt; }}
h2, h3, h4 {{ break-after: avoid; }}
figure, table {{ break-inside: avoid; }}
.cover {{ height: 23cm; display: flex; flex-direction: column; justify-content: space-between;
          text-align: center; break-after: page; }}
.cover .uni {{ font-size: 18pt; font-weight: 700; color: var(--primary); }}
.cover .career {{ font-size: 12pt; color: #555; }}
.cover .course {{ font-size: 14pt; font-weight: 600; }}
.cover .title {{ font-size: 26pt; font-weight: 800; color: var(--primary); margin-top: 0.3em; }}
.cover .bar {{ width: 3cm; height: 5px; background: var(--accent); margin: 0.8em auto; }}
.cover .meta {{ font-size: 12pt; line-height: 1.8; }}
</style></head><body>
<section class="cover">
  <div><div class="uni">{e(cover.university)}</div><div class="career">{e(cover.career)}</div></div>
  <div><div class="course">{e(cover.course)}</div><div class="bar"></div><div class="title">{e(cover.title)}</div></div>
  <div class="meta">Alumno: {e(cover.student)}<br>Fecha: {e(cover.date)}</div>
</section>
<main>{body}</main>
</body></html>"""
    footer = (
        '<div style="width:100%;font-size:9px;color:#888;text-align:center;">'
        '<span class="pageNumber"></span></div>'
    )
    return await _print_pdf(
        doc, path,
        display_header_footer=True, header_template="<div></div>", footer_template=footer,
    )


# ─── Tríptico ─────────────────────────────────────────────────────────────────

_PANEL_COUNT = 5  # paneles de contenido: 3 interiores + solapa + contraportada


def _block_weight(block: dict) -> int:
    kind = block["type"]
    if kind == "chart":
        return 500
    if kind == "table":
        return 90 * (len(block["rows"]) + 1)
    if kind == "list":
        return sum(len(i) + 40 for i in block["items"])
    if kind == "heading":
        return 80
    return len(block.get("text", "")) + 40


def _distribute(blocks: list[dict], n: int) -> list[list[dict]]:
    """
    Reparte el contenido en n paneles en orden, balanceando por tamaño.
    Cada sección se mantiene junta salvo que sea demasiado grande para un panel.
    """
    total = sum(_block_weight(b) for b in blocks) or 1
    target = total / n

    units: list[list[dict]] = []
    for section in split_sections(blocks):
        unit = ([{"type": "heading", "level": 2, "text": section["title"]}] if section["title"] else []) + section["blocks"]
        if sum(_block_weight(b) for b in unit) > target * 1.3:
            # Sección muy larga: se reparte por bloques, sin dejar títulos huérfanos
            chunk: list[dict] = []
            for b in unit:
                chunk.append(b)
                if b["type"] != "heading":
                    units.append(chunk)
                    chunk = []
            if chunk:
                units.append(chunk)
        else:
            units.append(unit)

    panels: list[list[dict]] = [[] for _ in range(n)]
    idx, acc = 0, 0.0
    for unit in units:
        w = sum(_block_weight(b) for b in unit)
        if idx < n - 1 and panels[idx] and acc + w / 2 > target * (idx + 1):
            idx += 1
        panels[idx].extend(unit)
        acc += w
    return panels


async def render_triptico(markdown_text: str, cover: CoverInfo, path: Path, work_dir: Path) -> Path:
    blocks = parse(markdown_text)
    # Si la IA repitió el título de la tarea como "# Título" al inicio, se quita (ya va en la portada)
    if blocks and blocks[0]["type"] == "heading" and blocks[0]["level"] == 1:
        blocks = blocks[1:]

    counter = [0]
    panels = _distribute(blocks, _PANEL_COUNT)
    panel_html = ["".join(_block_html(b, work_dir, counter) for b in p) for p in panels]
    e = html.escape

    def panel(content: str, extra_class: str = "") -> str:
        return f'<div class="panel {extra_class}"><div class="panel-inner">{content}</div></div>'

    cover_panel = f"""<div class="panel cover"><div class="cover-inner">
      <div class="uni">{e(cover.university)}</div>
      <div class="career">{e(cover.career)}</div>
      <div class="bar"></div>
      <div class="title">{e(cover.title)}</div>
      <div class="course">{e(cover.course)}</div>
      <div class="meta">{e(cover.student)}<br>{e(cover.date)}</div>
    </div></div>"""

    # Hoja 1 (exterior): solapa interior | contraportada | portada
    # Hoja 2 (interior): paneles 1, 2, 3
    doc = f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><style>
{_BASE_CSS}
@page {{ size: 11in 8.5in; margin: 0; }}
.sheet {{ width: 11in; height: 8.5in; display: flex; break-after: page; overflow: hidden; }}
.sheet:last-child {{ break-after: auto; }}
.panel {{ width: calc(11in / 3); height: 8.5in; padding: 0.4in 0.35in; position: relative; }}
.panel + .panel {{ border-left: 1px dashed #d0d7df; }}
.panel-inner {{ height: 100%; overflow: hidden; font-size: 10.5pt; line-height: 1.4; }}
.panel-inner > :first-child {{ margin-top: 0; }}
.panel-inner h2 {{ font-size: 1.45em; border-bottom: 2px solid var(--accent); padding-bottom: 0.15em; }}
.panel-inner h3 {{ font-size: 1.2em; }}
.panel-inner h4 {{ font-size: 1.05em; }}
.panel-inner figure img {{ max-height: 2.4in; }}
.panel.back {{ background: var(--soft); }}
.panel.back .signature {{ position: absolute; bottom: 0.4in; left: 0.35in; right: 0.35in;
  font-size: 8pt; color: #666; border-top: 1px solid #c9d3de; padding-top: 0.5em; }}
.panel.back .panel-inner {{ height: calc(100% - 0.8in); }}
.panel.cover {{ background: var(--primary); color: #fff; padding: 0; border: none; }}
.cover-inner {{ height: 100%; padding: 0.6in 0.45in; display: flex; flex-direction: column; }}
.cover .uni {{ font-size: 13pt; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; }}
.cover .career {{ font-size: 9.5pt; opacity: 0.8; margin-top: 0.3em; }}
.cover .bar {{ width: 1.2in; height: 5px; background: var(--accent); margin: auto 0 0.3in; }}
.cover .title {{ font-size: 26pt; font-weight: 800; line-height: 1.1; }}
.cover .course {{ font-size: 12pt; opacity: 0.85; margin: 0.3in 0 auto; }}
.cover .meta {{ font-size: 10pt; opacity: 0.9; line-height: 1.6; }}
</style></head><body>
<div class="sheet">
  {panel(panel_html[3])}
  <div class="panel back"><div class="panel-inner">{panel_html[4]}</div>
    <div class="signature">{e(cover.course)} · {e(cover.student)}</div></div>
  {cover_panel}
</div>
<div class="sheet">
  {panel(panel_html[0])}
  {panel(panel_html[1])}
  {panel(panel_html[2])}
</div>
</body></html>"""
    return await _print_pdf(doc, path, fit_panels=True)
