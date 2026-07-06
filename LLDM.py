import argparse
import os
import sys
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

    # 3. Initialize DMCore last as it publishes 'rules_loaded' in its __init__
    # and needs to hear 'action_detected' from NLPCore
    dm_core = DMCore(event_bus, scenario_name=args.scenario)

    event_bus.publish("log_info", "Application started successfully.")
    
    gui_core.start()

if __name__ == "__main__":
    main()