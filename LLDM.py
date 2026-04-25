import sys
from Event_Bus import EventBus
from Logger import Logger
from LLM_Core import LLMCore
from DM_Core import DMCore
from GUI_Core import GUICore
from NLP_Core import NLPCore

def main():
    # 1. Initialize Event Bus and Logger
    event_bus = EventBus()
    logger = Logger(event_bus)

    # 2. Initialize cores that subscribe to events
    # NLPCore needs to hear 'rules_loaded' from DMCore
    nlp_core = NLPCore(event_bus)
    # LLMCore needs to hear 'user_input_submitted' from GUICore
    llm_core = LLMCore(event_bus)
    # GUICore needs to hear 'llm_response_ready' and 'rules_loaded'
    gui_core = GUICore(event_bus)

    # 3. Initialize DMCore last as it publishes 'rules_loaded' in its __init__
    dm_core = DMCore(event_bus)

    event_bus.publish("log_info", "Application started successfully.")
    
    gui_core.start()

if __name__ == "__main__":
    main()