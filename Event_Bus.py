"""!
@file Event_Bus.py
@brief Provides a simple publish/subscribe event bus architecture.
"""

class EventBus:
    """!
    @brief Manages subscriptions and event dispatching.
    """
    def __init__(self):
        """!
        @brief Initializes the event bus with an empty subscriber dictionary.
        """
        self.subscribers = {}

    def subscribe(self, event_type, callback):
        """!
        @brief Subscribes a callback function to a specific event type.
        @param event_type The string name of the event type.
        @param callback The function to call when the event is published.
        """
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        self.subscribers[event_type].append(callback)

    def publish(self, event_type, message):
        """!
        @brief Publishes an event to all subscribers.
        @param event_type The string name of the event type.
        @param message The data payload of the event.
        """
        if event_type in self.subscribers:
            for callback in self.subscribers[event_type]:
                callback(message)