"""
Fórmulas en LaTeX escritas por la IA:  \\( a^2 + b^2 \\)  dentro del texto  y  \\[ … \\]  (o $$ … $$) en línea aparte.

Se dibujan con MathJax en Chromium a PNG (alta resolución) antes de armar el documento; Word/PDF/tríptico/
infografía insertan la imagen. Donde no se puede insertar imagen (PowerPoint, títulos) se usa una versión en
texto Unicode legible (b^{T}y ≥ c  →  bᵀy ≥ c).
"""
import hashlib
import json
import re
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

from ..db import DATA_DIR
from ..logger import logger

_MATHJAX_URL = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"
_MATHJAX_JS = DATA_DIR / "cache" / "mathjax-tex-svg.js"
RENDER_FONT_PX = 20  # tamaño de letra con el que se dibuja (para escalar al tamaño del documento)

# \( … \)  o  $…$ (este último solo si parece LaTeX, para no confundir precios como $100)
_INLINE = re.compile(r"\\\((.+?)\\\)|(?<![\w$\\])\$(?!\s)([^$\n]+?)(?<!\s)\$(?![\w$])")
_LATEXISH = re.compile(r"[\\^_{}=<>]")


class MathChunk(str):
    """Fragmento de fórmula dentro del texto: su valor como str es la versión Unicode; .tex es el LaTeX."""
    tex: str

    def __new__(cls, tex: str):
        obj = super().__new__(cls, tex_to_unicode(tex))
        obj.tex = tex
        return obj


def split_inline(text: str) -> list:
    """Divide el texto en str normales y MathChunk."""
    parts, pos = [], 0
    for m in _INLINE.finditer(text or ""):
        tex = m.group(1) if m.group(1) is not None else m.group(2)
        if m.group(2) is not None and not _LATEXISH.search(tex):
            continue  # "$100 y $200": no es fórmula
        if m.start() > pos:
            parts.append(text[pos:m.start()])
        parts.append(MathChunk(tex.strip()))
        pos = m.end()
    if pos < len(text or ""):
        parts.append(text[pos:])
    return parts


# ─── Versión en texto (PowerPoint, títulos, búsqueda) ─────────────────────────

_SUP = str.maketrans("0123456789+-=()nTiaxyk", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿᵀⁱᵃˣʸᵏ")
_SUB = str.maketrans("0123456789+-=()aeijknmox", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑᵢⱼₖₙₘₒₓ")
_SYMBOLS = {
    r"\geq": "≥", r"\ge": "≥", r"\leq": "≤", r"\le": "≤", r"\neq": "≠", r"\ne": "≠", r"\approx": "≈",
    r"\times": "×", r"\cdot": "·", r"\div": "÷", r"\pm": "±", r"\mp": "∓", r"\infty": "∞",
    r"\sum": "∑", r"\prod": "∏", r"\int": "∫", r"\partial": "∂", r"\nabla": "∇", r"\sqrt": "√",
    r"\in": "∈", r"\notin": "∉", r"\subseteq": "⊆", r"\subset": "⊂", r"\cup": "∪", r"\cap": "∩",
    r"\forall": "∀", r"\exists": "∃", r"\rightarrow": "→", r"\to": "→", r"\leftarrow": "←",
    r"\Rightarrow": "⇒", r"\Leftrightarrow": "⇔", r"\alpha": "α", r"\beta": "β", r"\gamma": "γ",
    r"\delta": "δ", r"\Delta": "Δ", r"\epsilon": "ε", r"\theta": "θ", r"\lambda": "λ", r"\mu": "μ",
    r"\pi": "π", r"\rho": "ρ", r"\sigma": "σ", r"\Sigma": "Σ", r"\phi": "φ", r"\omega": "ω",
    r"\ldots": "…", r"\dots": "…", r"\cdots": "⋯", r"\quad": "  ", r"\qquad": "    ",
    r"\,": " ", r"\;": " ", r"\:": " ", r"\!": "", r"\ ": " ",
}


def tex_to_unicode(tex: str) -> str:
    s = tex or ""
    s = re.sub(r"\\(?:text|mathrm|mathbf|operatorname|textbf|mathit)\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", s)
    s = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", s)
    for k in sorted(_SYMBOLS, key=len, reverse=True):
        s = s.replace(k, _SYMBOLS[k])
    s = re.sub(r"\^\{([^{}]*)\}", lambda m: _to(m.group(1), _SUP, "^"), s)
    s = re.sub(r"\^(\w)", lambda m: _to(m.group(1), _SUP, "^"), s)
    s = re.sub(r"_\{([^{}]*)\}", lambda m: _to(m.group(1), _SUB, "_"), s)
    s = re.sub(r"_(\w)", lambda m: _to(m.group(1), _SUB, "_"), s)
    s = s.replace(r"\left", "").replace(r"\right", "").replace("{", "").replace("}", "")
    s = re.sub(r"\\([A-Za-z]+)", r"\1", s)
    return re.sub(r"[ \t]+", " ", s).strip()


def _to(text: str, table: dict, fallback: str) -> str:
    converted = text.translate(table)
    # Si algún carácter no tiene versión super/subíndice, dejarlo legible con ^ o _
    return converted if all(c != o or not c.isalnum() for c, o in zip(converted, text)) else f"{fallback}({text})"


# ─── Dibujo con MathJax ───────────────────────────────────────────────────────

def _key(tex: str, display: bool) -> str:
    return hashlib.sha1(f"{display}|{tex.strip()}".encode()).hexdigest()[:14]


def math_image(tex: str, display: bool, work_dir: Path) -> tuple[Path, float, float] | None:
    """(png, ancho_px, alto_px) en px CSS a RENDER_FONT_PX, o None si no se dibujó."""
    png = work_dir / f"math_{_key(tex, display)}.png"
    meta = png.with_suffix(".json")
    if png.exists() and meta.exists():
        m = json.loads(meta.read_text())
        return png, m["w"], m["h"]
    return None


def collect(blocks: list[dict]) -> list[tuple[str, bool]]:
    """Todas las fórmulas del documento: bloques 'math' (display) y fórmulas dentro del texto."""
    found = []

    def scan(text):
        for part in split_inline(text or ""):
            if isinstance(part, MathChunk):
                found.append((part.tex, False))

    for b in blocks:
        if b["type"] == "math":
            found.append((b["tex"], True))
        for key in ("text",):
            if key in b:
                scan(b[key])
        for item in b.get("items", []):
            scan(item)
        for row in [b.get("header", [])] + b.get("rows", []):
            for cell in row:
                scan(cell)
    return found


def _ensure_mathjax() -> Path:
    if not _MATHJAX_JS.exists():
        _MATHJAX_JS.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Descargando MathJax (solo la primera vez)…")
        with urllib.request.urlopen(_MATHJAX_URL, timeout=60) as r:
            _MATHJAX_JS.write_bytes(r.read())
    return _MATHJAX_JS


async def render_math(formulas: list[tuple[str, bool]], work_dir: Path) -> None:
    pending = [(t, d) for t, d in dict.fromkeys(formulas) if math_image(t, d, work_dir) is None]
    if not pending:
        return
    work_dir.mkdir(parents=True, exist_ok=True)
    js = _ensure_mathjax()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(device_scale_factor=3)
            await page.set_content(
                f"<html><body style='margin:0;background:#fff;font-size:{RENDER_FONT_PX}px'>"
                "<div id='m' style='display:inline-block;padding:2px;line-height:0'></div></body></html>"
            )
            await page.add_script_tag(content="window.MathJax={svg:{fontCache:'none'},startup:{typeset:false}};")
            await page.add_script_tag(path=str(js))
            await page.wait_for_function("() => !!(window.MathJax && MathJax.startup && MathJax.startup.promise)")
            await page.evaluate("() => MathJax.startup.promise")
            for tex, display in pending:
                res = await page.evaluate(
                    """([tex, d]) => {
                        const el = document.getElementById('m'); el.innerHTML = '';
                        const node = MathJax.tex2svg(tex, {display: d});
                        const err = node.querySelector('[data-mjx-error]');
                        el.appendChild(node.querySelector('svg'));
                        const r = el.getBoundingClientRect();
                        return {w: r.width, h: r.height, err: err ? err.getAttribute('data-mjx-error') : null};
                    }""",
                    [tex, display],
                )
                if res["err"]:
                    logger.warning(f"Fórmula con error de LaTeX ({res['err']}): {tex[:80]} — se deja como texto")
                    continue
                png = work_dir / f"math_{_key(tex, display)}.png"
                await page.locator("#m").screenshot(path=str(png))
                png.with_suffix(".json").write_text(json.dumps({"w": res["w"], "h": res["h"]}))
        finally:
            await browser.close()
