import yaml
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_cfg: dict = {}


def load() -> dict:
    global _cfg
    if not _cfg:
        with open(_ROOT / "config.yaml", encoding="utf-8") as f:
            _cfg = yaml.safe_load(f)
    return _cfg


def get() -> dict:
    return _cfg or load()
