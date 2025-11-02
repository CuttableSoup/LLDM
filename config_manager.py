"""
This module provides a simple configuration manager for handling user settings.

It allows for loading, getting, setting, and saving configuration options
in a JSON file.
"""
import json
import os
import sys
from typing import Any

class ConfigManager:
    """Manages the configuration of the application."""
    def __init__(self, config_path: str):
        """
        Initializes the ConfigManager.

        Args:
            config_path: The path to the configuration file.
        """
        self.config_path = config_path
        self.config: dict = {}
        self.load_config()

    def load_config(self) -> None:
        """Loads the configuration from the specified file."""
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
        Gets a configuration value.

        Args:
            key: The configuration key.
            default: The default value to return if the key is not found.

        Returns:
            The configuration value.
        """
        return self.config.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """
        Sets a configuration value and saves the configuration.

        Args:
            key: The configuration key.
            value: The value to set.
        """
        self.config[key] = value
        self.save_config()

    def save_config(self) -> None:
        """Saves the current configuration to the file."""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4)
        except IOError as e:
            print(f"Error: Could not save config file to {self.config_path}: {e}", file=sys.stderr)
