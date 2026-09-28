"""
Fechas de apertura/cierre de actividades de Moodle y cálculo del estado de una tarea según ellas.
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import get

_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
_DATE_RE = re.compile(r"(\d{1,2})\s+de\s+([a-záéíóú]+)\s+de\s+(\d{4})(?:,?\s*(\d{1,2}):(\d{2}))?", re.I)

# Etiquetas que usa Moodle (en la traducción de la UCNL) en el bloque de fechas de la actividad
_OPEN_LABELS = ("abre", "abiert", "apertura", "disponible desde", "permitir entregas desde")
_CLOSE_LABELS = ("cierra", "cierre", "pendiente", "fecha de entrega", "vence", "fecha límite", "entrega")


def now() -> datetime:
    """Hora actual en la zona horaria de la UCNL (sin tzinfo, igual que las fechas guardadas)."""
    tz = get().get("scheduler", {}).get("timezone", "America/Monterrey")
    return datetime.now(ZoneInfo(tz)).replace(tzinfo=None)


def parse_spanish_date(text: str) -> datetime | None:
    """'lunes, 30 de noviembre de 2026, 00:00' → datetime(2026, 11, 30, 0, 0)."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    month = _MONTHS.get(m.group(2).lower())
    if not month:
        return None
    hour, minute = (int(m.group(4)), int(m.group(5))) if m.group(4) else (23, 59)
    try:
        return datetime(int(m.group(3)), month, int(m.group(1)), hour, minute)
    except ValueError:
        return None


def classify_dates(rows: list[tuple[str, str]]) -> dict:
    """
    rows: [(etiqueta, texto_fecha)] tal como aparecen en el bloque de fechas.
    Devuelve {"opens_at": iso | None, "closes_at": iso | None}.
    """
    result = {"opens_at": None, "closes_at": None}
    for label, value in rows:
        label = label.strip().lower().rstrip(":")
        dt = parse_spanish_date(value)
        if not dt:
            continue
        if label.startswith(_OPEN_LABELS) and not result["opens_at"]:
            result["opens_at"] = dt.isoformat()
        elif any(k in label for k in _CLOSE_LABELS) and not result["closes_at"]:
            result["closes_at"] = dt.isoformat()
    return result


def status_from_dates(opens_at: str | None, closes_at: str | None) -> str:
    """future si aún no abre, expired si ya cerró, pending_approval si está abierta (o sin fechas)."""
    current = now()
    if opens_at and datetime.fromisoformat(opens_at) > current:
        return "future"
    if closes_at and datetime.fromisoformat(closes_at) < current:
        return "expired"
    return "pending_approval"
