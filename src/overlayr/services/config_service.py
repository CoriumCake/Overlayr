import json
import os

class ConfigService:
    def __init__(self, config_path="settings.json"):
        self.config_path = config_path
        self.settings = {
            "appearance_mode": "Dark",
            "source_lang": "Japanese",
            "target_lang": "Thai",
            "grouping_mode": "Group (Manga)",
            "ocr_engine": "RapidOCR"
        }
        self.load()

    def load(self):
        """Load settings from JSON file"""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    self.settings.update(loaded)
            except Exception as e:
                print(f"Error loading config: {e}")
        else:
            # Create new config with defaults if not found
            self.save()

    def save(self):
        """Save settings to JSON file"""
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=4)
        except Exception as e:
            print(f"Error saving config: {e}")

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def set(self, key, value):
        self.settings[key] = value
        self.save()
