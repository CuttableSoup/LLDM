"""!
@file Logger.py
@brief Subscribes to logging events and writes them to standard output or a file.
"""

import datetime

class Logger:
    """!
    @brief Handles logging messages received from the event bus.
    """
    def __init__(self, event_bus):
        """!
        @brief Initializes the logger and subscribes to log events.
        @param event_bus The central event bus instance.
        """
        self.event_bus = event_bus
        self.event_bus.subscribe("log_info", self.log_info)
        self.event_bus.subscribe("log_error", self.log_error)

    def log_info(self, message):
        """!
        @brief Logs informational messages.
        @param message The message payload.
        """
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] INFO: {message}")

    def log_error(self, message):
        """!
        @brief Logs error messages.
        @param message The message payload.
        """
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] ERROR: {message}")