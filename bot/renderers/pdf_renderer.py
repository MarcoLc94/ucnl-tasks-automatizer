"""
PDF (reporte) y tríptico: se arma HTML + CSS y Chromium (Playwright) lo imprime a PDF.
"""
import base64
import html
from pathlib import Path

from playwright.async_api import async_playwright

from ..images import fetch_photo
from .charts import render_chart_png
from .diagrams import diagram_path
from .common import CoverInfo, reference_entries, split_document
from .markdown import inline_runs, parse, plain, split_sections

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
figure.photo img { width: 100%; max-height: 3.2in; object-fit: cover; border-radius: 6px; }
figure.diagram img { max-width: 100%; max-height: 8in; }
blockquote.callout { margin: 0.6em 0; padding: 0.5em 0.8em; border-left: 4px solid var(--accent);
  background: var(--soft); font-size: 1.05em; }
blockquote.callout strong { color: var(--accent); font-size: 1.4em; }
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
    if kind == "callout":
        return f'<blockquote class="callout">{_inline(block["text"])}</blockquote>'
    if kind == "list":
        tag = "ol" if block["ordered"] else "ul"
        items = "".join(f"<li>{_inline(i)}</li>" for i in block["items"])
        return f"<{tag}>{items}</{tag}>"
    if kind == "table":
        head = "".join(f"<th>{_inline(h)}</th>" for h in block["header"])
        rows = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in block["rows"])
        return f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"
    if kind == "image":
        photo = fetch_photo(block["query"], work_dir)
        if not photo:
            return ""
        b64 = base64.b64encode(photo["path"].read_bytes()).decode()
        return (f'<figure class="photo"><img src="data:image/jpeg;base64,{b64}">'
                f'<figcaption>{html.escape(photo["credit"])}</figcaption></figure>')
    if kind == "diagram":
        png = diagram_path(block["code"], work_dir)
        if not png.exists():
            return ""
        b64 = base64.b64encode(png.read_bytes()).decode()
        return f'<figure class="diagram"><img src="data:image/png;base64,{b64}"></figure>'
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


async def _print_pdf(
    html_doc: str, path: Path, fit_panels: bool = False, fit_poster: bool = False, as_png: bool = False, **pdf_opts,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            # Para PNG: página del tamaño del cartel (8.5 x 11 in a 96 dpi) y resolución x2.5
            page = await browser.new_page(
                viewport={"width": 816, "height": 1056}, device_scale_factor=2.5,
            ) if as_png else await browser.new_page()
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
            if fit_poster:
                # Ajusta la letra para que el cartel llene la página sin desbordarse
                await page.evaluate("""() => {
                  const poster = document.querySelector('.poster');
                  const main = document.querySelector('main');
                  const fits = () => poster.scrollHeight <= poster.clientHeight + 1
                                  && main.scrollHeight <= main.clientHeight + 1;
                  let size = 15;
                  poster.style.fontSize = size + 'pt';
                  while (!fits() && size > 7) {
                    size -= 0.25;
                    poster.style.fontSize = size + 'pt';
                  }
                }""")
            if as_png:
                await page.locator(".poster").screenshot(path=str(path))
            else:
                await page.pdf(path=str(path), print_background=True, prefer_css_page_size=True, **pdf_opts)
        finally:
            await browser.close()
    return path


# ─── Reporte PDF ──────────────────────────────────────────────────────────────

async def render_pdf(markdown_text: str, cover: CoverInfo, path: Path, work_dir: Path) -> Path:
    """Portada → Introducción → Contenido → Conclusiones → Referencias, cada parte en hoja nueva."""
    counter = [0]
    parts = split_document(parse(markdown_text))
    render = lambda blocks: "\n".join(_block_html(b, work_dir, counter) for b in blocks)
    e = html.escape

    sections = []
    if parts["intro"]:
        sections.append(f'<section class="part"><h2>Introducción</h2>{render(parts["intro"])}</section>')
    if parts["body"]:
        sections.append(f'<section class="part">{render(parts["body"])}</section>')
    if parts["conclusion"]:
        sections.append(f'<section class="part"><h2>Conclusiones</h2>{render(parts["conclusion"])}</section>')
    if parts["references"]:
        refs = "".join(
            f"<p>{_inline(r)}</p>"
            for r in sorted(reference_entries(parts["references"]), key=lambda t: plain(t).lower())
        )
        sections.append(f'<section class="part refs"><h2>Referencias</h2>{refs}</section>')

    cover_rows = "".join(f"<div><strong>{e(k)}:</strong> {e(v)}</div>" for k, v in cover.fields())
    logo_html = ""
    if cover.logo:
        logo_b64 = base64.b64encode(cover.logo.read_bytes()).decode()
        logo_html = f'<img class="logo" src="data:image/jpeg;base64,{logo_b64}">'
    doc = f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><style>
{_BASE_CSS}
@page {{ size: Letter; margin: 2.2cm 2.4cm; }}
body {{ font-size: 11pt; line-height: 1.5; text-align: justify; }}
h2 {{ font-size: 16pt; border-bottom: 2px solid var(--accent); padding-bottom: 0.15em; }}
h3 {{ font-size: 13pt; }}
h4 {{ font-size: 11.5pt; }}
h2, h3, h4 {{ break-after: avoid; }}
figure, table {{ break-inside: avoid; }}
.part {{ break-before: page; }}
.part > h2:first-child {{ margin-top: 0; }}
.refs p {{ padding-left: 1.3cm; text-indent: -1.3cm; text-align: left; }}
.cover {{ height: 23cm; display: flex; flex-direction: column; justify-content: space-between;
          text-align: center; }}
.cover .uni {{ font-size: 18pt; font-weight: 700; color: var(--primary); }}
.cover .logo {{ max-width: 11cm; max-height: 4.2cm; display: block; margin: 0 auto 0.4cm; }}
.cover .title {{ font-size: 26pt; font-weight: 800; color: var(--primary); }}
.cover .bar {{ width: 3cm; height: 5px; background: var(--accent); margin: 0.6em auto 0; }}
.cover .meta {{ font-size: 12pt; line-height: 2; }}
</style></head><body>
<section class="cover">
  <div>{logo_html}<div class="uni">{e(cover.university)}</div></div>
  <div><div class="title">{e(cover.title)}</div><div class="bar"></div></div>
  <div class="meta">{cover_rows}</div>
</section>
{"".join(sections)}
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
    if kind in ("chart", "image", "diagram"):
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


# ─── Infografía ───────────────────────────────────────────────────────────────

_CARD_COLORS = ["#2E86AB", "#F18F01", "#6A994E", "#C73E1D", "#7B5EA7", "#1F3A5F"]
_SOURCE_TITLES = ("fuente", "referencia", "bibliograf")


async def render_infografia_png(markdown_text: str, cover: CoverInfo, path: Path, work_dir: Path) -> Path:
    """
    Cartel de una sola página (PNG) con el CONTENIDO del trabajo: tarjetas por sección, cifras clave,
    gráficas y foto de encabezado. La introducción, conclusiones y referencias van en el Word.
    """
    import re
    parts = split_document(parse(markdown_text))
    blocks = parts["body"]

    counter = [0]
    e = html.escape
    sections = split_sections(blocks)
    if sections and not sections[0]["title"]:
        sections.pop(0)

    # Frase del encabezado: primera oración de la introducción
    intro_html = ""
    first_par = next((b["text"] for b in parts["intro"] if b["type"] == "paragraph"), "")
    if first_par:
        sentence = re.split(r"(?<=[.!?])\s", first_par, maxsplit=1)[0]
        if len(sentence) <= 180:
            intro_html = f"<p>{_inline(sentence)}</p>"

    # "Fuentes" / "Referencias" van al pie, en letra chica
    sources_html = ""
    body_sections = []
    for sec in sections:
        if sec["title"] and sec["title"].lower().startswith(_SOURCE_TITLES):
            sources_html = "".join(_block_html(b, work_dir, counter) for b in sec["blocks"])
        else:
            body_sections.append(sec)

    # Cifras clave y gráficas se sacan de las secciones: van en su propia franja / tarjeta ancha.
    # La primera foto se usa de fondo del encabezado.
    stats, charts, images = [], [], [b for b in blocks if b["type"] == "image"]
    for sec in body_sections:
        stats += [b for b in sec["blocks"] if b["type"] == "callout"]
        charts += [b for b in sec["blocks"] if b["type"] in ("chart", "diagram")]
        sec["blocks"] = [b for b in sec["blocks"] if b["type"] not in ("callout", "chart", "diagram", "image")]

    hero_style, hero_credit = "", ""
    for img in images:
        photo = fetch_photo(img["query"], work_dir)
        if photo:
            b64 = base64.b64encode(photo["path"].read_bytes()).decode()
            hero_style = (
                "background: linear-gradient(135deg, rgba(31,58,95,0.93), rgba(46,134,171,0.78)), "
                f"url(data:image/jpeg;base64,{b64}) center/cover;"
            )
            hero_credit = f'<div class="credit">{e(photo["credit"])}</div>'
            break

    cards = []
    for i, sec in enumerate(s for s in body_sections if s["blocks"]):
        color = _CARD_COLORS[i % len(_CARD_COLORS)]
        content = "".join(_block_html(b, work_dir, counter) for b in sec["blocks"])
        title = f"<h2>{_inline(sec['title'])}</h2>" if sec["title"] else ""
        cards.append(f'<section class="card" style="--c:{color}">{title}{content}</section>')
    if len(cards) % 2:
        cards[-1] = cards[-1].replace('class="card"', 'class="card wide"', 1)

    stats_html = ""
    if stats:
        items = "".join(
            f'<div class="stat" style="--c:{_CARD_COLORS[(i + 1) % len(_CARD_COLORS)]}">{_inline(b["text"])}</div>'
            for i, b in enumerate(stats[:4])
        )
        stats_html = f'<section class="stats">{items}</section>'
    for b in charts[:2]:
        cards.append(f'<section class="card wide chart" style="--c:var(--primary)">{_block_html(b, work_dir, counter)}</section>')

    doc = f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><style>
{_BASE_CSS}
@page {{ size: Letter; margin: 0; }}
html, body {{ width: 8.5in; height: 11in; }}
.poster {{ width: 8.5in; height: 11in; overflow: hidden; display: flex; flex-direction: column;
  background: #F4F7FA; font-size: 12pt; line-height: 1.35; }}
header {{ background: linear-gradient(135deg, var(--primary), var(--accent)); color: #fff;
  padding: 0.45in 0.5in 0.35in; }}
header .course {{ font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.08em; opacity: 0.85; }}
header h1 {{ color: #fff; font-size: 2.5em; margin: 0.15em 0 0.2em; line-height: 1.05; }}
header .intro {{ font-size: 1.05em; opacity: 0.95; }}
header .intro p {{ margin: 0; }}
header .intro figure {{ display: none; }}
header {{ position: relative; }}
header .credit {{ position: absolute; right: 0.2in; bottom: 0.08in; font-size: 0.5em; opacity: 0.7; }}
main {{ flex: 1; padding: 0.3in 0.45in 0.15in; display: grid; grid-template-columns: 1fr 1fr;
  gap: 0.18in; align-content: start; }}
.card {{ background: #fff; border-radius: 12px; padding: 0.16in 0.2in; border-top: 6px solid var(--c);
  box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
.card.wide {{ grid-column: 1 / -1; }}
.card h2 {{ color: var(--c); font-size: 1.2em; margin: 0 0 0.35em; border: none; }}
.card ul, .card ol {{ padding-left: 1.1em; margin: 0; }}
.card li::marker {{ color: var(--c); }}
.card p {{ margin: 0 0 0.35em; }}
.card figure {{ margin: 0.2em 0 0; }}
.card figure img {{ max-height: 2.6in; }}
.card blockquote.callout {{ background: none; border: none; padding: 0; margin: 0.3em 0; text-align: center; }}
.card blockquote.callout strong {{ display: block; color: var(--c); font-size: 2.4em; line-height: 1; }}
.card table {{ font-size: 0.85em; }}
.card.chart {{ border-top-color: var(--primary); }}
.card.chart figure img {{ max-height: 2.8in; }}
.stats {{ grid-column: 1 / -1; display: flex; gap: 0.18in; }}
.stat {{ flex: 1; background: var(--c); color: #fff; border-radius: 12px; padding: 0.14in 0.18in;
  font-size: 0.9em; line-height: 1.25; }}
.stat strong {{ display: block; font-size: 2.3em; line-height: 1.05; margin-bottom: 0.1em; }}
footer {{ padding: 0.12in 0.5in 0.3in; font-size: 0.62em; color: #667; display: flex;
  justify-content: space-between; gap: 0.3in; }}
footer .sources p, footer .sources ul {{ margin: 0; }}
footer .sources ul {{ padding-left: 1em; }}
footer .who {{ white-space: nowrap; text-align: right; }}
</style></head><body>
<div class="poster">
  <header style="{hero_style}">
    {hero_credit}
    <div class="course">{e(cover.course)}</div>
    <h1>{e(cover.title)}</h1>
    <div class="intro">{intro_html}</div>
  </header>
  <main>{stats_html}{"".join(cards)}</main>
  <footer>
    <div class="sources">{("<strong>Fuentes:</strong> " + sources_html) if sources_html else ""}</div>
    <div class="who">{e(cover.student)}<br>{e(cover.university)} · {e(cover.date)}</div>
  </footer>
</div>
</body></html>"""
    return await _print_pdf(doc, path, fit_poster=True, as_png=True)
