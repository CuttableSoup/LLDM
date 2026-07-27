import argparse
import json
import os
import sys

from Event_Bus import EventBus
from Logger import Logger
from LLM_Core import LLMCore
from DM_Core import DMCore, scenario_file_path
from GUI_Core import GUICore
from NLP_Core import NLPCore

DEFAULT_SCENARIO = "arena"


def _peek_saved_scenario_key(slot_name, fallback):
    """!
    @brief Reads just a save slot's own "scenario_key" straight out of its dm_state.json --
        without needing a live DMCore to ask (see DM_Persistence.py's save_game/load_game) --
        so main()'s own cold-start "Load..." handler knows which scenario to construct a
        brand new DMCore against *before* DMCore.load_game() itself has anything to run
        against. Mirrors DM_Persistence.py's own _save_slot_dir sanitizing (os.path.basename,
        stripped) so this reads the exact same directory a real load_game would.
    @param slot_name The save slot name, as picked from GUICore's own load-slot picker.
    @param fallback Returned if the slot doesn't exist or its dm_state.json can't be read/
        parsed -- DMCore.load_game itself is what surfaces a real "no such slot" error to the
        player (via "game_load_failed"); this only ever has to pick *some* scenario to
        construct DMCore with in the first place.
    @return The slot's own scenario_key, or fallback.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    safe_name = os.path.basename(slot_name.strip()) or "unnamed"
    path = os.path.join(base_dir, "Saves", safe_name, "dm_state.json")
    try:
        with open(path, "r") as f:
            return json.load(f).get("scenario_key", fallback)
    except (OSError, ValueError):
        return fallback


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
    args = parser.parse_args()

    # Fail fast on a bad scenario name before spending ~15-20s loading NLPCore's
    # sentence-transformers model, rather than silently continuing with no scenario
    # data (which used to let the LLM hallucinate a scene from nothing).
    if args.scenario is not None and not os.path.exists(scenario_file_path(args.scenario)):
        print(f"Error: scenario '{args.scenario}' not found in Rules/Fantasy/scenarios/.", file=sys.stderr)
        sys.exit(1)

    # 1. Initialize Event Bus and Logger
    event_bus = EventBus()
    logger = Logger(event_bus)

    # 2. Initialize cores that subscribe to events
    # NLPCore needs to hear 'rules_loaded' from DMCore
    nlp_core = NLPCore(event_bus)
    # LLMCore needs to hear 'action_resolved' from DMCore
    llm_core = LLMCore(event_bus)
    # GUICore needs to hear 'llm_response_ready' and 'rules_loaded'
    gui_core = GUICore(event_bus)

    # 2.5. No scenario/character is loaded automatically -- DMCore (and the scenario it
    # publishes "scenario_loaded" for during its own __init__) is only ever constructed in
    # response to an actual choice: the "scenario" CLI argument below, GUICore's Character ->
    # Create... dialog ("character_created"), or GUICore's Character -> Load... picker
    # ("load_requested"). DMCore doesn't exist yet for either of the latter two to have
    # anything to publish to, so this closure -- not DMCore itself -- is what's subscribed to
    # react to them the very first time; DMCore's own _on_load_requested (subscribed inside
    # its own __init__, once one exists) transparently takes back over for every subsequent
    # load from then on, since this handler no-ops as soon as dm_core is no longer None.
    dm_core = None

    def start_game(scenario_name, character):
        nonlocal dm_core
        if dm_core is not None:
            return
        # 3. Constructed last, as it publishes 'rules_loaded' in its __init__ and needs to
        # hear 'action_detected' from NLPCore -- both already subscribed above regardless of
        # when this actually fires.
        dm_core = DMCore(event_bus, scenario_name=scenario_name, character=character)

    def on_character_created(data):
        if dm_core is not None:
            event_bus.publish(
                "log_warning",
                "character_created with a game already active; ignored (restart to create "
                "a different character).",
            )
            return
        start_game(args.scenario or DEFAULT_SCENARIO, data.get("character"))

    def on_load_requested(data):
        if dm_core is not None:
            return  # an active game already exists; DMCore's own handler owns this load now
        slot = data.get("slot")
        if not slot:
            event_bus.publish("log_warning", "load_requested with no slot name; ignored.")
            return
        scenario_name = _peek_saved_scenario_key(slot, args.scenario or DEFAULT_SCENARIO)
        start_game(scenario_name, None)
        dm_core.load_game(slot)

    event_bus.subscribe("character_created", on_character_created)
    event_bus.subscribe("load_requested", on_load_requested)

    if args.scenario is not None:
        # Quick-boot path -- "I like loading the game quickly": a scenario named on the
        # command line skips the Character menu/dialog entirely. character_name, if also
        # given, only renames the default player template (see
        # DM_CharacterCreation.py's apply_character_creation, whose skill/race override step
        # is skipped whenever "allocation" is absent); leave it off to keep that template's
        # own name and skills exactly as characters.toml authored them.
        character = {"name": args.character_name} if args.character_name else None
        start_game(args.scenario, character)

    event_bus.publish("log_info", "Application started successfully.")

    gui_core.start()

if __name__ == "__main__":
    main()
