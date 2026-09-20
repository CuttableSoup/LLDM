"""!
@file Logger.py
@brief Subscribes to logging events and writes them to standard output, and -- when DEBUG is
    enabled (see LLDM.py) -- to a timestamped file under Logs/ as well.
"""

import datetime
import os

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Logs")


class Logger:
    """!
    @brief Handles logging messages received from the event bus.
    """
    def __init__(self, event_bus, debug=False):
        """!
        @brief Initializes the logger and subscribes to log events.
        @param event_bus The central event bus instance.
        @param debug When True, mirrors every log line -- info/warning/error plus the raw
            LLM query/response pairs already published as "llm_debug_updated" for the GUI's own
            Debug tab (LLM_Core.py's _fetch_and_publish) -- into a single timestamped file under
            Logs/, gitignored, one per process run. False (the default) keeps this class's
            original console-only behavior exactly, so every other Logger(event_bus) call site
            (ex: gui/Textual_Core.py) is unaffected.
        """
        self.event_bus = event_bus
        self._log_file = None
        if debug:
            os.makedirs(LOG_DIR, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self._log_file = open(
                os.path.join(LOG_DIR, f"session_{timestamp}.log"), "a", encoding="utf-8",
            )
            self.event_bus.subscribe("llm_debug_updated", self.log_llm_debug)

        self.event_bus.subscribe("log_info", self.log_info)
        self.event_bus.subscribe("log_error", self.log_error)
        # Published from a dozen call sites (ex: DM_Combat.py, DM_Persistence.py) but never
        # actually consumed before now -- every warning was silently dropped rather than
        # reaching the console. Folded in here rather than left as a debug-only addition, since
        # a missing "load_requested with no slot name" warning is a console gap regardless of
        # whether file logging is on.
        self.event_bus.subscribe("log_warning", self.log_warning)

    def _write(self, line):
        """!
        @brief Appends a line to the debug log file, if one is open. Flushed immediately --
            this app has no clean-shutdown hook that would otherwise guarantee buffered lines
            reach disk before a hard exit.
        """
        if self._log_file:
            self._log_file.write(line + "\n")
            self._log_file.flush()

    def log_info(self, message):
        """!
        @brief Logs informational messages.
        @param message The message payload.
        """
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] INFO: {message}"
        print(line)
        self._write(line)

    def log_error(self, message):
        """!
        @brief Logs error messages.
        @param message The message payload.
        """
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] ERROR: {message}"
        print(line)
        self._write(line)

    def log_warning(self, message):
        """!
        @brief Logs warning messages.
        @param message The message payload.
        """
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] WARNING: {message}"
        print(line)
        self._write(line)

    def log_llm_debug(self, data):
        """!
        @brief Mirrors one complete LLM exchange (the full request messages and the raw reply,
            already assembled by LLM_Core.py's _fetch_and_publish for the GUI's own Debug tab)
            into the debug log file -- console output stays untouched, since this is the one
            log source that's file-only (too large/frequent to also print per turn).
        @param data {"query", "response", "label"?} as published by "llm_debug_updated" --
            "label" is the short trigger tag LLM_Core.py's own generate_*/_queue_* call sites
            attach (ex: "dialogue:town crier", "scenario_intro"), so two calls that finish
            close together (ex: a scene's own automatic intro landing right next to an NPC's
            reply, purely because Ollama serializes requests) can be told apart without having
            to read each query's own prompt text and cross-reference timestamps by hand.
        """
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        label = data.get("label")
        tag = f" ({label})" if label else ""
        self._write(
            f"[{timestamp}] LLM QUERY{tag}:\n{data.get('query', '')}\n"
            f"[{timestamp}] LLM RESPONSE{tag}:\n{data.get('response', '')}\n{'-' * 80}"
        )
