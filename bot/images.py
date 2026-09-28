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
