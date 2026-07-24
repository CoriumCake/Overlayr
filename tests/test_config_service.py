"""Tests for ConfigService — settings persistence."""
import pytest
import json
import os
import tempfile
from overlayr.services.config_service import ConfigService


@pytest.fixture
def config_path(tmp_path):
    """Temporary config file path."""
    return str(tmp_path / "test_settings.json")


@pytest.fixture
def config(config_path):
    """ConfigService with a temporary file."""
    return ConfigService(config_path=config_path)


class TestConfigDefaults:
    def test_default_values(self, config):
        assert config.get("appearance_mode") == "Dark"
        assert config.get("source_lang") == "Japanese"
        assert config.get("target_lang") == "Thai"
        assert config.get("grouping_mode") == "Group (Manga)"
        assert config.get("save_debug_output") is False

    def test_get_missing_key_returns_default(self, config):
        assert config.get("nonexistent") is None
        assert config.get("nonexistent", "fallback") == "fallback"


class TestConfigPersistence:
    def test_set_and_get(self, config):
        config.set("source_lang", "Korean")
        assert config.get("source_lang") == "Korean"

    def test_save_creates_file(self, config, config_path):
        config.set("test_key", "test_value")
        assert os.path.exists(config_path)

    def test_reload_persists_values(self, config_path):
        c1 = ConfigService(config_path=config_path)
        c1.set("source_lang", "French")

        c2 = ConfigService(config_path=config_path)
        assert c2.get("source_lang") == "French"

    def test_corrupt_json_uses_defaults(self, config_path):
        with open(config_path, "w") as f:
            f.write("{invalid json!!!")

        config = ConfigService(config_path=config_path)
        assert config.get("appearance_mode") == "Dark"

    def test_missing_file_uses_defaults(self, tmp_path):
        config = ConfigService(config_path=str(tmp_path / "nonexistent.json"))
        assert config.get("appearance_mode") == "Dark"
