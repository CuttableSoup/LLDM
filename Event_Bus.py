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
            # Dispatches over a snapshot, not the live list -- a handler that itself
            # subscribes a new callback for this same event_type mid-dispatch (ex: LLDM.py's
            # own cold-start "load_requested" handler constructing a fresh DMCore, whose
            # __init__ subscribes its own _on_load_requested) must not have that new callback
            # also invoked within this same publish call; it should only ever fire starting
            # from the *next* publish. Iterating the live list would otherwise pick up
            # append()s that happen partway through this very loop, double-processing the
            # event the moment it triggers its own new subscriber.
            for callback in list(self.subscribers[event_type]):
                callback(message)