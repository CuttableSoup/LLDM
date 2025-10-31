import json
import os
import sys
from typing import Any

class ConfigManager:
    """Handles loading and saving a simple JSON configuration file."""
    
    def __init__(self, config_path: str):
        """
        Initializes the config manager.
        
        Args:
            config_path: The path to the config.json file.
        """
        self.config_path = config_path
        self.config: dict = {}
        self.load_config()

    def load_config(self):
        """Loads the configuration from the file."""
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
        """
        Gets a value from the loaded config.
        
        Args:
            key: The config key to retrieve.
            default: The value to return if the key is not found.
            
        Returns:
            The configuration value or the default.
        """
        return self.config.get(key, default)

    def set(self, key: str, value: Any):
        """
        Sets a value in the config and saves it to the file.
        
        Args:
            key: The config key to set.
            value: The value to store.
        """
        self.config[key] = value
        self.save_config()

    def save_config(self):
        """Saves the current config dictionary to the file."""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4)
        except IOError as e:
            print(f"Error: Could not save config file to {self.config_path}: {e}", file=sys.stderr)