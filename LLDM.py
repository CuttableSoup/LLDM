import argparse
import atexit
import json
import os
import sys
import threading

from Event_Bus import EventBus
from Logger import Logger
from llm.LLM_Core import LLMCore
from dm.DM_Core import DMCore, scenario_file_path
from gui.GUI_Core import GUICore
from nlp.NLP_Core import NLPCore
from llm.Ollama_Launcher import ensure_ollama_running

DEFAULT_SCENARIO = "arena"


def _peek_saved_scenario_key(slot_name, fallback, fallback_setting="Fantasy"):
    """!
    @brief Reads just a save slot's own "scenario_key"/"setting" straight out of its
        dm_state.json -- without needing a live DMCore to ask (see DM_Persistence.py's
        save_game/load_game) -- so main()'s own cold-start "Load..." handler knows which
        scenario/setting to construct a brand new DMCore against *before* DMCore.load_game()
        itself has anything to run against. Both matter: DMCore.__init__ resolves scenario_key
        against Rules/<setting>/scenarios/ before load_game ever gets to overlay the save's
        own state, so a setting mismatch here would fail scenario lookup outright rather than
        just cosmetically narrating the wrong intro (see load_game's own "throwaway intro"
        note). Mirrors DM_Persistence.py's own _save_slot_dir sanitizing (os.path.basename,
        stripped) so this reads the exact same directory a real load_game would.
    @param slot_name The save slot name, as picked from GUICore's own load-slot picker.
    @param fallback Returned as the scenario key if the slot doesn't exist or its
        dm_state.json can't be read/parsed -- DMCore.load_game itself is what surfaces a real
        "no such slot" error to the player (via "game_load_failed"); this only ever has to
        pick *some* scenario to construct DMCore with in the first place.
    @param fallback_setting Returned as the setting under the same failure conditions.
    @return (scenario_key, setting) -- either or both may be the given fallbacks.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    safe_name = os.path.basename(slot_name.strip()) or "unnamed"
    path = os.path.join(base_dir, "Saves", safe_name, "dm_state.json")
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data.get("scenario_key", fallback), data.get("setting", fallback_setting)
    except (OSError, ValueError):
        return fallback, fallback_setting


def main():
    parser = argparse.ArgumentParser(description="LLDM - an autonomous dungeon master.")
    parser.add_argument(
        "scenario",
        nargs="?",
        default=None,
        help="Scenario to load, matching a file in Rules/Fantasy/scenarios/ (ex: 'arena'). "
             "Boots straight into it, skipping the Character menu entirely, the moment it's "
             "given -- omit it to open the window and pick Character -> Create/Load instead.",
    )
    parser.add_argument(
        "character_name",
        nargs="?",
        default=None,
        help="Optional player character name for a quick boot alongside 'scenario' -- renames "
             "the default player template (its own hand-authored skills untouched) instead of "
             "opening the race/point-buy dialog. Ignored if 'scenario' isn't also given.",
    )
    parser.add_argument(
        "--setting",
        default="Fantasy",
        help="Which Rules/ subdirectory to boot from (ex: 'Fantasy', 'Zombie') -- each is a "
             "self-contained TOML data pack (skills/entities/rules/scenarios). Only meaningful "
             "alongside 'scenario'. Defaults to 'Fantasy'.",
    )
    args = parser.parse_args()

    # Fail fast on a bad scenario name before spending ~15-20s loading NLPCore's
    # sentence-transformers model, rather than silently continuing with no scenario
    # data (which used to let the LLM hallucinate a scene from nothing).
    if args.scenario is not None and not os.path.exists(scenario_file_path(args.scenario, args.setting)):
        print(
            f"Error: scenario '{args.scenario}' not found in Rules/{args.setting}/scenarios/.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 1. Initialize Event Bus and Logger
    event_bus = EventBus()
    logger = Logger(event_bus)

    # 1.5. GUICore is constructed before everything else below it, specifically so its window
    # already exists for the Ollama bootstrap (next) to report progress into -- none of its own
    # subscriptions (llm_response_ready, rules_loaded, ...) can fire this early regardless of
    # construction order, since nothing publishes them until DMCore exists.
    gui_core = GUICore(event_bus)

    # 1.6. Best-effort local Ollama bootstrap -- installs a local copy if nothing's found
    # anywhere (one-time; see Ollama_Launcher.py's own module note) and makes sure the
    # configured model is pulled, then spawns the server fire-and-forget, not waiting on it
    # becoming ready. Run on a background thread rather than blocking here: a fresh machine has
    # to download the ~1.5GB Ollama binary plus, by default, a ~9.6GB model pull before this
    # call would otherwise return, which would freeze the window for however long that takes --
    # nothing but display_system_status's own manual root.update() pumps events before
    # mainloop() starts, so no menu is clickable until it finishes. Backgrounding it instead
    # lets gui_core.start() reach mainloop() immediately: the player can open Character ->
    # Create... and start a scenario right away, and narration simply degrades to "Could not
    # connect to the local LLM" (LLM_Core.py's own existing best-effort path) until the
    # bootstrap catches up -- the same posture this app already takes toward Ollama being
    # unavailable for any other reason. Status is relayed both to the console (Logger.py, via
    # log_info) and into the GUI's own History pane (display_system_status) exactly the way
    # LLM_Core.py's own background narration fetches already touch GUICore from a foreign
    # thread -- this isn't a new cross-thread risk, just the same existing one at another call
    # site.
    def _report_ollama_status(message):
        event_bus.publish("log_info", message)
        gui_core.display_system_status(message)

    # ollama_process stays None until the background thread actually assigns it -- a shutdown
    # that races the bootstrap (the window closed before install/pull finishes) simply has
    # nothing to clean up yet, the same "nothing was started" case already handled below when
    # ensure_ollama_running finds no executable to install.
    ollama_process = None

    def _bootstrap_ollama():
        nonlocal ollama_process
        ollama_process = ensure_ollama_running(log=_report_ollama_status)

    def _stop_ollama_if_started():
        # Only ever terminates a process this call itself started, never a pre-existing Ollama
        # instance -- terminate(), not kill(), gives the server a chance to shut down cleanly.
        if ollama_process is not None and ollama_process.poll() is None:
            ollama_process.terminate()

    atexit.register(_stop_ollama_if_started)
    threading.Thread(target=_bootstrap_ollama, daemon=True).start()

    # 2. Initialize cores that subscribe to events
    # NLPCore needs to hear 'rules_loaded' from DMCore
    nlp_core = NLPCore(event_bus)
    # LLMCore needs to hear 'action_resolved' from DMCore
    llm_core = LLMCore(event_bus)

    # 2.5. No scenario/character is loaded automatically -- DMCore (and the scenario it
    # publishes "scenario_loaded" for during its own __init__) is only ever constructed in
    # response to an actual choice: the "scenario" CLI argument below, GUICore's own Scenario ->
    # Load... menu entry ("scenario_selected" -- unlocked only after Character -> Create...
    # produces a character, see GUI_Core.py's request_character_creation/
    # _set_scenario_menu_enabled), or GUICore's File -> Load... picker ("load_requested").
    # DMCore doesn't exist yet for any of these to have anything to publish to, so this
    # closure -- not DMCore itself -- is what's subscribed to react to them the very first
    # time; DMCore's own _on_load_requested (subscribed inside its own __init__, once one
    # exists) transparently takes back over for every subsequent load from then on, since this
    # handler no-ops as soon as dm_core is no longer None.
    dm_core = None

    def start_game(scenario_name, character, setting="Fantasy"):
        nonlocal dm_core
        if dm_core is not None:
            return
        # 3. Constructed last, as it publishes 'rules_loaded' in its __init__ and needs to
        # hear 'turn_detected' from NLPCore -- both already subscribed above regardless of
        # when this actually fires.
        dm_core = DMCore(event_bus, scenario_name=scenario_name, character=character, setting=setting)

    def on_character_created(data):
        # Doesn't start a game itself -- GUICore holds the new character as its own
        # "pending" until Scenario -> Load... picks a scenario for it (on_scenario_selected,
        # below). This only ever logs the same "ignored" warning
        # request_character_creation's own docstring describes for the case where a game is
        # somehow already active by the time Create... is used again.
        if dm_core is not None:
            event_bus.publish(
                "log_warning",
                "character_created with a game already active; ignored (restart to create "
                "a different character).",
            )

    def on_scenario_selected(data):
        if dm_core is not None:
            return
        start_game(data.get("scenario_name"), data.get("character"))

    def on_load_requested(data):
        if dm_core is not None:
            return  # an active game already exists; DMCore's own handler owns this load now
        slot = data.get("slot")
        if not slot:
            event_bus.publish("log_warning", "load_requested with no slot name; ignored.")
            return
        scenario_name, setting = _peek_saved_scenario_key(
            slot, args.scenario or DEFAULT_SCENARIO, args.setting,
        )
        start_game(scenario_name, None, setting=setting)
        dm_core.load_game(slot)

    event_bus.subscribe("character_created", on_character_created)
    event_bus.subscribe("scenario_selected", on_scenario_selected)
    event_bus.subscribe("load_requested", on_load_requested)

    if args.scenario is not None:
        # Quick-boot path -- "I like loading the game quickly": a scenario named on the
        # command line skips the Character menu/dialog entirely. character_name, if also
        # given, only renames the default player template (see
        # DM_CharacterCreation.py's apply_character_creation, whose skill/race override step
        # is skipped whenever "allocation" is absent); leave it off to keep that template's
        # own name and skills exactly as characters.toml authored them.
        character = {"name": args.character_name} if args.character_name else None
        start_game(args.scenario, character, setting=args.setting)

    event_bus.publish("log_info", "Application started successfully.")

    gui_core.start()

if __name__ == "__main__":
    main()
