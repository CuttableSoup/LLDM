"""!
@file Textual_Core.py
@brief Mirrors the same event-bus output GUICore displays into a Textual interface, so the
    app's behavior can be driven and inspected headlessly (via Textual's Pilot) for tests,
    without needing a real Tkinter window.
"""

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Input, RichLog, TabbedContent, TabPane


class TextualCore(App):
    """!
    @brief A headless-testable mirror of GUICore's output, subscribed to the same event bus.
    """

    CSS = """
    Horizontal { height: 1fr; }
    #save_row { height: 3; }
    """

    def __init__(self, event_bus):
        """!
        @brief Subscribes to the same events GUICore displays.
        @param event_bus The central event bus instance.
        """
        super().__init__()
        self.event_bus = event_bus
        self._mirror_ready = False
        self._buffered_lines = {"history": [], "debug_log": [], "event_log": []}

        self.event_bus.subscribe("user_input_submitted", self._on_user_input_submitted)
        self.event_bus.subscribe("llm_response_ready", self._on_llm_response)
        self.event_bus.subscribe("rules_loaded", self._on_rules_loaded)
        self.event_bus.subscribe("log_info", self._on_log_info)
        self.event_bus.subscribe("log_error", self._on_log_error)
        self.event_bus.subscribe("game_saved", self._on_game_saved)
        self.event_bus.subscribe("game_loaded", self._on_game_loaded)
        self.event_bus.subscribe("game_load_failed", self._on_game_load_failed)

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield RichLog(id="history", wrap=True, highlight=False)
            with TabbedContent():
                with TabPane("Debug", id="debug_tab"):
                    yield RichLog(id="debug_log", wrap=True)
                with TabPane("Log", id="event_log_tab"):
                    yield RichLog(id="event_log", wrap=True)
        # A slot-name field plus Save/Load buttons -- just another way to publish the same
        # "save_requested"/"load_requested" events NLPCore's text intercept publishes (ex:
        # "save as arena-run-1"), so DMCore/LLMCore handle it identically either way.
        with Horizontal(id="save_row"):
            yield Input(placeholder="Save slot name...", id="slot_input")
            yield Button("Save", id="save_button")
            yield Button("Load", id="load_button")
        yield Input(placeholder="Type an action and press Enter...", id="input_box")

    def on_mount(self) -> None:
        """!
        @brief Flushes any output that arrived before the app finished mounting its widgets.
        """
        self._mirror_ready = True
        for widget_id, lines in self._buffered_lines.items():
            log_widget = self.query_one(f"#{widget_id}", RichLog)
            for line in lines:
                log_widget.write(line)
        self._buffered_lines = {key: [] for key in self._buffered_lines}

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """!
        @brief Mirrors GUICore.submit_input: publishes the typed text and clears the field.
            Only reacts to the main action input -- "slot_input" submits via the Save/Load
            buttons instead, not by pressing Enter.
        """
        if event.input.id != "input_box":
            return
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self.event_bus.publish("user_input_submitted", text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """!
        @brief Mirrors GUICore.request_save/request_load: publishes "save_requested"/
            "load_requested" with the slot-name input's current text.
        """
        if event.button.id not in ("save_button", "load_button"):
            return
        slot_name = self.query_one("#slot_input", Input).value.strip()
        if not slot_name:
            return
        event_type = "save_requested" if event.button.id == "save_button" else "load_requested"
        self.event_bus.publish(event_type, {"slot": slot_name})

    def call_safely(self, fn, *args):
        """!
        @brief Runs fn(*args) on the Textual app's own thread, whether called from that thread,
            a foreign thread (ex: LLMCore's background fetch), or before the app has started.
        """
        try:
            self.call_from_thread(fn, *args)
        except Exception:
            fn(*args)

    def _write(self, widget_id, line):
        if not self._mirror_ready:
            self._buffered_lines[widget_id].append(line)
            return
        try:
            self.query_one(f"#{widget_id}", RichLog).write(line)
        except Exception:
            pass

    def _on_user_input_submitted(self, text):
        self.call_safely(self._write, "history", f"> {text}")

    def _on_llm_response(self, text):
        self.call_safely(self._write, "history", text)

    def _on_rules_loaded(self, rules_data):
        lines = ["--- Skills ---"]
        lines += [str(item) for item in rules_data.get("skills", {}).items()]
        lines.append("--- Entities ---")
        lines += [str(item) for item in rules_data.get("entities", {}).items()]
        for line in lines:
            self.call_safely(self._write, "debug_log", line)

    def _on_log_info(self, message):
        self.call_safely(self._write, "event_log", f"INFO: {message}")

    def _on_log_error(self, message):
        self.call_safely(self._write, "event_log", f"ERROR: {message}")

    def _on_game_saved(self, data):
        self.call_safely(self._write, "history", f"[System] Game saved as '{data.get('slot')}'.")

    def _on_game_loaded(self, data):
        self.call_safely(self._write, "history", f"[System] Game loaded from '{data.get('slot')}'.")

    def _on_game_load_failed(self, data):
        self.call_safely(self._write, "history", f"[System] No save named '{data.get('slot')}' found.")


if __name__ == "__main__":
    from Event_Bus import EventBus
    from Logger import Logger
    from NLP_Core import NLPCore
    from DM_Core import DMCore
    from LLM_Core import LLMCore

    event_bus = EventBus()
    logger = Logger(event_bus)
    nlp_core = NLPCore(event_bus)
    llm_core = LLMCore(event_bus)
    textual_core = TextualCore(event_bus)
    dm_core = DMCore(event_bus)

    textual_core.run()
