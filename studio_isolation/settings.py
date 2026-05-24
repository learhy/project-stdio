"""Settings loading and validation."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .models import Settings

logger = logging.getLogger(__name__)


def get_settings_path() -> str | None:
    """Auto-detect the settings.json path. Returns None if no file found.

    Checks in order:
      1. STUDIO_SETTINGS_PATH env var
      2. /etc/studio/settings.json (system install)
      3. ~/.config/studio/settings.json (user install)
      4. memory/settings.json (dev/repo-relative fallback)
    """
    env_path = os.environ.get("STUDIO_SETTINGS_PATH")
    if env_path:
        return env_path

    candidates = [
        "/etc/studio/settings.json",
        os.path.expanduser("~/.config/studio/settings.json"),
        os.path.join("memory", "settings.json"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def load_settings(path: str | None = None) -> Settings:
    """Load and validate settings from a JSON file. Auto-detects path if not given."""
    resolved = path or get_settings_path()
    if resolved is None:
        logger.info("No settings.json found — using defaults")
        return Settings()
    logger.info("Loading settings from %s", resolved)
    raw = Path(resolved).read_text()
    data = json.loads(raw)
    return Settings.model_validate(data)
