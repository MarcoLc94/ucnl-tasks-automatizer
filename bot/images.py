"""
Fotos de Pexels (https://www.pexels.com/api/) para los bloques ![descripción](imagen) del Markdown.

Sin PEXELS_API_KEY o si la búsqueda falla, se devuelve None y el documento se genera sin esa imagen.
"""
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

from .logger import logger

_API = "https://api.pexels.com/v1/search"
_TIMEOUT = 15


def enabled() -> bool:
    return bool(os.getenv("PEXELS_API_KEY"))


def fetch_photo(query: str, work_dir: Path, orientation: str = "landscape") -> dict | None:
    """
    Busca una foto y la descarga. Devuelve {"path": Path, "credit": "Foto: Nombre / Pexels"} o None.
    Pexels acepta búsquedas en español con locale=es-ES.
    """
    key = os.getenv("PEXELS_API_KEY")
    query = (query or "").strip()
    if not key or not query:
        return None

    safe = "".join(c if c.isalnum() else "_" for c in query.lower())[:60]
    target = work_dir / f"img_{safe}_{orientation}.jpg"
    meta = target.with_suffix(".json")
    if target.exists() and meta.exists():
        return {"path": target, **json.loads(meta.read_text())}

    params = urllib.parse.urlencode({
        "query": query, "per_page": 1, "orientation": orientation, "locale": "es-ES",
    })
    try:
        req = urllib.request.Request(f"{_API}?{params}", headers={"Authorization": key, "User-Agent": "ucnl-task-bot"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            photos = json.load(r).get("photos", [])
        if not photos:
            logger.info(f"Pexels: sin resultados para '{query}'")
            return None
        photo = photos[0]
        img_req = urllib.request.Request(photo["src"]["large"], headers={"User-Agent": "ucnl-task-bot"})
        with urllib.request.urlopen(img_req, timeout=_TIMEOUT) as r:
            work_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(r.read())
        info = {"credit": f"Foto: {photo.get('photographer', 'Pexels')} / Pexels"}
        meta.write_text(json.dumps(info, ensure_ascii=False))
        return {"path": target, **info}
    except Exception as e:
        logger.warning(f"Pexels: no se pudo obtener imagen para '{query}': {e}")
        return None


def get_site_logo(base_url: str) -> Path | None:
    """Escudo/logo oficial de la plataforma (licenciatura o bachillerato), tomado de su página de login."""
    import re
    from .db import DATA_DIR

    host = urllib.parse.urlsplit(base_url).netloc or "ucnl"
    target = DATA_DIR / "cache" / f"logo_{host}.jpg"
    if target.exists():
        return target
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/login/index.php", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            html = r.read().decode("utf-8", "replace")
        m = re.search(r'<img[^>]+id="logoimage"[^>]+src="([^"]+)"', html) or re.search(r'<img[^>]+src="([^"]+logo[^"]+)"', html)
        if not m:
            return None
        src = m.group(1)
        src = "https:" + src if src.startswith("//") else urllib.parse.urljoin(base_url, src)
        with urllib.request.urlopen(urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"}), timeout=_TIMEOUT) as r:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(r.read())
        return target
    except Exception as e:
        logger.warning(f"No se pudo obtener el logo de {base_url}: {e}")
        return None
