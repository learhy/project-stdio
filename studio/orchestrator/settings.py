"""Settings loading and validation."""
from __future__ import annotations

import json
from pathlib import Path

from .models import Settings


def load_settings(path: str | Path = "settings.json") -> Settings:
    """Load and validate settings from a JSON file."""
    raw = Path(path).read_text()
    data = json.loads(raw)
    return Settings.model_validate(data)
