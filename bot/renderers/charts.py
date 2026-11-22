"""
Gráficas a partir del bloque ```grafica de la IA.

Formato esperado:
{
  "tipo": "barras" | "lineas" | "pastel",
  "titulo": "…",
  "etiquetas": ["A", "B"],
  "series": [{"nombre": "2024", "valores": [1, 2]}]   # o "valores": [1, 2]
}
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

PALETTE = ["#1F3A5F", "#2E86AB", "#F18F01", "#6A994E", "#C73E1D", "#7B5EA7"]

_KIND_ALIASES = {
    "barras": "bar", "bar": "bar", "columnas": "bar", "barra": "bar",
    "lineas": "line", "líneas": "line", "linea": "line", "línea": "line", "line": "line",
    "pastel": "pie", "pie": "pie", "circular": "pie", "dona": "pie",
}


def _to_number(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except ValueError:
        return 0.0


def normalize_chart(spec: dict) -> dict | None:
    """Devuelve {kind, title, labels, series: [(name, values)]} o None si no es válida."""
    if not isinstance(spec, dict):
        return None
    kind = _KIND_ALIASES.get(str(spec.get("tipo", spec.get("type", "barras"))).lower(), "bar")
    labels = [str(x) for x in spec.get("etiquetas", spec.get("labels", []))]
    raw_series = spec.get("series")
    if not raw_series and "valores" in spec:
        raw_series = [{"nombre": spec.get("titulo", ""), "valores": spec["valores"]}]
    if not labels or not raw_series:
        return None

    series = []
    for s in raw_series:
        if isinstance(s, dict):
            name = str(s.get("nombre", s.get("name", "")))
            values = s.get("valores", s.get("values", []))
        else:
            name, values = "", s
        values = [_to_number(v) for v in values][: len(labels)]
        values += [0.0] * (len(labels) - len(values))
        series.append((name, values))

    if kind == "pie":
        series = series[:1]
    return {"kind": kind, "title": str(spec.get("titulo", spec.get("title", ""))), "labels": labels, "series": series}


def render_chart_png(spec: dict, path: Path, width_in: float = 7, height_in: float = 4) -> Path | None:
    chart = normalize_chart(spec)
    if not chart:
        return None

    fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=200)
    labels, series = chart["labels"], chart["series"]

    if chart["kind"] == "pie":
        ax.pie(
            series[0][1], labels=labels, autopct="%1.0f%%", startangle=90,
            colors=PALETTE[: len(labels)] * (len(labels) // len(PALETTE) + 1),
            wedgeprops={"edgecolor": "white", "linewidth": 1.5},
            textprops={"fontsize": 9},
        )
        ax.axis("equal")
    else:
        n = len(series)
        x = range(len(labels))
        width = 0.8 / n
        for idx, (name, values) in enumerate(series):
            color = PALETTE[idx % len(PALETTE)]
            if chart["kind"] == "line":
                ax.plot(list(x), values, marker="o", linewidth=2, color=color, label=name or None)
            else:
                offsets = [xi - 0.4 + width * (idx + 0.5) for xi in x]
                ax.bar(offsets, values, width=width * 0.9, color=color, label=name or None)
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, fontsize=9, rotation=20 if max(map(len, labels)) > 10 else 0, ha="right" if max(map(len, labels)) > 10 else "center")
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", alpha=0.3)
        ax.set_axisbelow(True)
        if n > 1 or any(name for name, _ in series):
            ax.legend(frameon=False, fontsize=9)

    if chart["title"]:
        ax.set_title(chart["title"], fontsize=12, fontweight="bold", color="#1F3A5F")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path
