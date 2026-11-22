"""
Lectura de los textos enlazados en las instrucciones (cuentos, artículos…), para que la IA trabaje con el
contenido real y no con lo que "recuerda" (que puede ser inventado).
"""
import html
import re
import urllib.request

from .logger import logger

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_LINKS_HEADER = "Enlaces en las instrucciones:"
_LINK_LINE = re.compile(r"^- (.*?): (https?://\S+)$", re.M)
_READING_HINTS = re.compile(r"\b(lee|leer|lectura|texto|relato|cuento|art[ií]culo|poema|selecciona uno)", re.I)


def append_links(description: str, links: list[tuple[str, str]]) -> str:
    """Agrega a la descripción la lista de enlaces (contexto: url), una sola vez."""
    if not links or _LINKS_HEADER in (description or ""):
        return description
    lines = [f"- {ctx.strip()[:90]}: {url}" for ctx, url in links]
    return f"{description}\n\n{_LINKS_HEADER}\n" + "\n".join(lines)


def links_in(description: str) -> list[tuple[str, str]]:
    return _LINK_LINE.findall(description or "")


def _html_to_text(raw: str) -> tuple[str, str]:
    title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    title = html.unescape(re.sub(r"\s+", " ", title.group(1))).strip() if title else ""
    raw = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form|noscript)[^>]*>.*?</\1>", " ", raw)
    # Preferir el contenido principal si existe
    main = re.search(r"(?is)<(article|main)[^>]*>(.*?)</\1>", raw)
    body = main.group(2) if main else raw
    paragraphs = re.findall(r"(?is)<p[^>]*>(.*?)</p>", body)
    text_parts = []
    for p in paragraphs:
        t = html.unescape(re.sub(r"<[^>]+>", "", p))
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) > 30:
            text_parts.append(t)
    return title, "\n".join(text_parts)


def _fetch_with_browser(url: str) -> str | None:
    """HTML de la página cargada con Chromium (se llama desde un hilo, por eso la API síncrona)."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=_UA, locale="es-MX")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
                return page.content()
            finally:
                browser.close()
    except Exception as e:
        logger.info(f"Tampoco con navegador: {url}: {str(e).splitlines()[0][:100]}")
        return None


def fetch_readable(url: str, timeout: int = 20) -> tuple[str, str] | None:
    """Descarga una página y devuelve (título, texto). None si no se pudo o no es HTML legible."""
    if url.lower().split("?")[0].endswith((".pdf", ".doc", ".docx", ".ppt", ".pptx")):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "es-MX,es;q=0.9"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if "html" not in (r.headers.get("Content-Type") or "html"):
                return None
            raw = r.read(2_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
    except Exception as e:
        # Algunos sitios bloquean descargas simples (403): intentar con el navegador del bot
        raw = _fetch_with_browser(url)
        if raw is None:
            logger.info(f"No se pudo leer {url}: {e}")
            return None
    title, text = _html_to_text(raw)
    return (title, text) if len(text) > 600 else None


def reference_texts(description: str, max_texts: int = 2, max_chars: int = 7000) -> str:
    """
    Si la actividad pide leer/elegir un texto y trae enlaces, descarga los más cortos legibles y los devuelve
    listos para el prompt. Cadena vacía si no aplica.
    """
    links = links_in(description)
    if not links or not _READING_HINTS.search(description or ""):
        return ""
    fetched = []
    for ctx, url in links[:12]:
        res = fetch_readable(url)
        if res:
            title, text = res
            fetched.append((ctx, url, title, text))
    if not fetched:
        return ""
    # Los más cortos primero (micro-relatos): caben completos y la IA los analiza mejor
    fetched.sort(key=lambda f: len(f[3]))
    blocks = []
    for ctx, url, title, text in fetched[:max_texts]:
        body = text if len(text) <= max_chars else text[:max_chars] + " […]"
        blocks.append(f"### {ctx}\nFuente: {url}\n{body}")
    logger.info(f"Textos de referencia leídos: {[f[0][:40] for f in fetched[:max_texts]]}")
    return "\n\n".join(blocks)
