import argparse
import os
import sys
from Character_Creation import load_character_creation_data
from Character_Creation_GUI import run_character_creation_dialog
from Event_Bus import EventBus
from Logger import Logger
from LLM_Core import LLMCore
from DM_Core import DMCore, scenario_file_path
from GUI_Core import GUICore
from NLP_Core import NLPCore

def main():
    parser = argparse.ArgumentParser(description="LLDM - an autonomous dungeon master.")
    parser.add_argument(
        "scenario",
        nargs="?",
        default="arena",
        help="Scenario to load, matching a file in Rules/Fantasy/scenarios/ (default: arena).",
    )
    parser.add_argument(
        "--skip-character-creation",
        action="store_true",
        help="Boot straight into the scenario with the player template's own default skills, "
             "instead of showing the race/point-buy character creation dialog first.",
    )
    args = parser.parse_args()

    # Fail fast on a bad scenario name before spending ~15-20s loading NLPCore's
    # sentence-transformers model, rather than silently continuing with no scenario
    # data (which used to let the LLM hallucinate a scene from nothing).
    if not os.path.exists(scenario_file_path(args.scenario)):
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

    # 2.5. Race/point-buy character creation -- blocks (via wait_window, same pattern
    # GUICore.request_load already uses) on GUICore's own root, before DMCore exists at all,
    # so its result can feed straight into DMCore's own "character" param below. Loaded
    # independently of DMCore (see Character_Creation.py's module docstring) since no DMCore
    # exists yet for it to read races/skills off of. Cancelling the dialog (or --skip-
    # character-creation) leaves character None, which DMCore treats as "use the player
    # template's own default skills, unchanged" -- today, characters.toml's gladstone.
    character = None
    if not args.skip_character_creation:
        skills, races, character_creation = load_character_creation_data()
        character = run_character_creation_dialog(gui_core.root, skills, races, character_creation)

    # 3. Initialize DMCore last as it publishes 'rules_loaded' in its __init__
    # and needs to hear 'action_detected' from NLPCore
    dm_core = DMCore(event_bus, scenario_name=args.scenario, character=character)

    event_bus.publish("log_info", "Application started successfully.")
    
    gui_core.start()

if __name__ == "__main__":
    main()