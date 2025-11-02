import json
import os
import sys
from typing import Any

class ConfigManager:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config: dict = {}
        self.load_config()

    def load_config(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self.config = json.load(f)
                print(f"Configuration loaded from {self.config_path}")
            except json.JSONDecodeError:
                print(f"Warning: {self.config_path} is corrupt. Using default config.", file=sys.stderr)
                self.config = {}
        else:
            print(f"No config file found. A new {self.config_path} will be created.")
            self.config = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set(self, key: str, value: Any):
        self.config[key] = value
        self.save_config()

    def save_config(self):
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4)
        except IOError as e:
            print(f"Error: Could not save config file to {self.config_path}: {e}", file=sys.stderr)