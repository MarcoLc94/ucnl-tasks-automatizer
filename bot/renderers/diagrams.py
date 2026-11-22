"""
Diagramas (mapas conceptuales, flujos, mapas mentales…) escritos por la IA en Mermaid:

    ```diagrama
    flowchart TD
        A["Tema"] --> B["Subtema"]
    ```

Se dibujan con Chromium (Playwright) a PNG antes de armar el documento. Si un diagrama tiene un error
de sintaxis se lanza DiagramError: es mejor no entregar que entregar un trabajo sin su diagrama.
"""
import hashlib
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

from ..db import DATA_DIR
from ..logger import logger

_MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"
_MERMAID_JS = DATA_DIR / "cache" / "mermaid.min.js"

_THEME = {
    "primaryColor": "#EEF3F8",
    "primaryBorderColor": "#1F3A5F",
    "primaryTextColor": "#1F3A5F",
    "lineColor": "#2E86AB",
    "secondaryColor": "#FFF4E0",
    "tertiaryColor": "#EAF4E4",
    "fontFamily": "Segoe UI, Helvetica Neue, Arial, sans-serif",
    "fontSize": "16px",
}


class DiagramError(Exception):
    pass


def diagram_path(code: str, work_dir: Path) -> Path:
    digest = hashlib.sha1(code.strip().encode()).hexdigest()[:12]
    return work_dir / f"diagram_{digest}.png"


def _ensure_mermaid() -> Path:
    if not _MERMAID_JS.exists():
        _MERMAID_JS.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Descargando Mermaid (solo la primera vez)…")
        with urllib.request.urlopen(_MERMAID_URL, timeout=60) as r:
            _MERMAID_JS.write_bytes(r.read())
    return _MERMAID_JS


async def render_diagrams(blocks: list[dict], work_dir: Path) -> None:
    """Dibuja todos los bloques 'diagram' a PNG en work_dir (diagram_path)."""
    pending = [b for b in blocks if b["type"] == "diagram" and not diagram_path(b["code"], work_dir).exists()]
    if not pending:
        return
    work_dir.mkdir(parents=True, exist_ok=True)
    js = _ensure_mermaid()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1400, "height": 1000}, device_scale_factor=2)
            await page.set_content("<html><body style='margin:0;background:#fff'><div id='d'></div></body></html>")
            await page.add_script_tag(path=str(js))
            await page.evaluate(
                "(vars) => mermaid.initialize({startOnLoad: false, theme: 'base', themeVariables: vars,"
                " flowchart: {htmlLabels: true, curve: 'basis'}, securityLevel: 'strict'})",
                _THEME,
            )
            for i, block in enumerate(pending):
                try:
                    await page.evaluate(
                        """async ([id, code]) => {
                            const {svg} = await mermaid.render(id, code);
                            const d = document.getElementById('d');
                            d.innerHTML = svg;
                            d.style.display = 'inline-block';
                            d.style.padding = '16px';
                        }""",
                        [f"diagram{i}", block["code"]],
                    )
                except Exception as e:
                    detail = str(e).split("Error:", 1)[-1].strip().splitlines()[0][:200]
                    raise DiagramError(f"El diagrama tiene un error de sintaxis ({detail}). Usa 'Regenerar'.")
                await page.locator("#d").screenshot(path=str(diagram_path(block["code"], work_dir)))
        finally:
            await browser.close()
