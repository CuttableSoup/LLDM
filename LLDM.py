import sys
from Event_Bus import EventBus
from Logger import Logger
from LLM_Core import LLMCore
from DM_Core import DMCore
from GUI_Core import GUICore
from NLP_Core import NLPCore

def main():
    event_bus = EventBus()
    logger = Logger(event_bus)

    llm_core = LLMCore(event_bus)
    dm_core = DMCore(event_bus)
    gui_core = GUICore(event_bus)
    nlp_core = NLPCore(event_bus)

    event_bus.publish("log_info", "Application started successfully.")

if __name__ == "__main__":
    main()