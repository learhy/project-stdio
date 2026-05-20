"""Tests for settings.py — auto-detection and loading."""
import json
import os
import tempfile
from unittest.mock import patch

import pytest

from studio.orchestrator.settings import get_settings_path, load_settings
from studio.orchestrator.models import Settings


class TestGetSettingsPath:
    def test_env_var_priority(self, monkeypatch, tmp_path):
        """STUDIO_SETTINGS_PATH env var takes priority."""
        f = tmp_path / "custom.json"
        f.write_text('{"kernel": {"mode": true}}')
        monkeypatch.setenv("STUDIO_SETTINGS_PATH", str(f))
        result = get_settings_path()
        assert result == str(f)

    def test_env_var_always_respected(self, monkeypatch):
        """Env var is always returned — even if file doesn't exist yet (may be created later)."""
        monkeypatch.setenv("STUDIO_SETTINGS_PATH", "/nonexistent/settings.json")
        monkeypatch.setattr("os.path.exists", lambda p: False)
        result = get_settings_path()
        assert result == "/nonexistent/settings.json"

    def test_system_path_first(self, monkeypatch):
        """When /etc/studio/settings.json exists, it's preferred."""
        monkeypatch.delenv("STUDIO_SETTINGS_PATH", raising=False)
        monkeypatch.setattr("os.path.exists", lambda p: p == "/etc/studio/settings.json")
        result = get_settings_path()
        assert result == "/etc/studio/settings.json"

    def test_user_path_fallback(self, monkeypatch):
        """When system path doesn't exist, fall back to user path."""
        monkeypatch.delenv("STUDIO_SETTINGS_PATH", raising=False)
        user_path = os.path.expanduser("~/.config/studio/settings.json")

        def exists(p):
            return p == user_path

        monkeypatch.setattr("os.path.exists", exists)
        result = get_settings_path()
        assert result == user_path

    def test_memory_fallback_last(self, monkeypatch):
        """memory/settings.json is the last resort."""
        monkeypatch.delenv("STUDIO_SETTINGS_PATH", raising=False)
        monkeypatch.setattr("os.path.exists", lambda p: p == "memory/settings.json")
        result = get_settings_path()
        assert result == "memory/settings.json"

    def test_none_when_nothing_exists(self, monkeypatch):
        """Returns None when no settings file exists anywhere."""
        monkeypatch.delenv("STUDIO_SETTINGS_PATH", raising=False)
        monkeypatch.setattr("os.path.exists", lambda p: False)
        result = get_settings_path()
        assert result is None


class TestLoadSettings:
    def test_loads_from_file(self, tmp_path):
        """load_settings reads and validates a JSON file."""
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"kernel": {"mode": True}}))
        result = load_settings(str(f))
        assert isinstance(result, Settings)
        assert result.kernel.mode is True

    def test_returns_defaults_when_path_none(self, monkeypatch):
        """When no path given and auto-detect returns None, use defaults."""
        monkeypatch.delenv("STUDIO_SETTINGS_PATH", raising=False)
        monkeypatch.setattr("os.path.exists", lambda p: False)
        result = load_settings()
        assert isinstance(result, Settings)
        # Default value
        assert result.orchestrator.db_path == "/var/lib/studio/state.db"

    def test_auto_detects_when_no_path_given(self, monkeypatch, tmp_path):
        """load_settings auto-detects when no path argument is passed."""
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"kernel": {"mode": True}}))
        monkeypatch.setenv("STUDIO_SETTINGS_PATH", str(f))
        result = load_settings()
        assert isinstance(result, Settings)
        assert result.kernel.mode is True
