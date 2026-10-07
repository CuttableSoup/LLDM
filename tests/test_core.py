import os
import unittest
from dm.DM_Core import DMCore
from tests.event_contract import EventContractError, ValidatingEventBus, consumed_keys
from llm import Narration_Prompts
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


class TestEventBus(unittest.TestCase):
    """!
    @brief Event_Bus.py's publish/subscribe dispatch, including the one subtlety worth its
        own regression test: publish() must dispatch over a snapshot of the subscriber list,
        not the live one, so a handler that itself calls subscribe() for the same event_type
        it's currently handling (ex: LLDM.py's cold-start "load_requested" handler
        constructing a fresh DMCore, whose own __init__ subscribes _on_load_requested) doesn't
        also have that brand-new callback invoked within this same publish -- it should only
        ever fire starting from the *next* publish call.
    """

    def test_publish_calls_every_subscriber_with_the_message(self):
        bus = ValidatingEventBus()
        received = []
        bus.subscribe("ping", received.append)
        bus.subscribe("ping", received.append)
        bus.publish("ping", "hello")
        self.assertEqual(received, ["hello", "hello"])


    def test_a_handler_subscribing_mid_dispatch_is_not_invoked_until_the_next_publish(self):
        bus = ValidatingEventBus()
        calls = []

        def late_subscriber(message):
            calls.append(("late", message))

        def subscribes_another_handler(message):
            calls.append(("first", message))
            bus.subscribe("event", late_subscriber)

        bus.subscribe("event", subscribes_another_handler)
        bus.publish("event", 1)
        self.assertEqual(calls, [("first", 1)])  # late_subscriber must not fire yet

        bus.publish("event", 2)
        self.assertEqual(calls, [("first", 1), ("first", 2), ("late", 2)])


class TestEventContract(unittest.TestCase):
    """!
    @brief events/schemas.py's declared payloads, from both sides: the validating bus rejects a
        producer's key the schema doesn't know, and every key a consumer reads has to be one the
        schema declares -- a key on only one side is a silent None.
    """

    def test_a_payload_with_an_undeclared_key_fails_at_the_line_that_published_it(self):
        bus = ValidatingEventBus()
        with self.assertRaises(EventContractError) as caught:
            bus.publish("item_interaction_resolved", {"intent": "take", "found": True, "ammount": 5})
        self.assertIn("unknown key 'ammount'", str(caught.exception))

    def test_a_missing_required_key_and_a_wrongly_typed_value_are_caught(self):
        bus = ValidatingEventBus()
        with self.assertRaises(EventContractError):
            bus.publish("item_interaction_resolved", {"found": True})
        with self.assertRaises(EventContractError):
            bus.publish("dialogue_resolved", {"found": "yes"})
        with self.assertRaises(EventContractError):
            bus.publish("llm_response_ready", {"not": "a string"})

    def test_a_turn_is_checked_clause_by_clause(self):
        bus = ValidatingEventBus()
        bus.publish("turn_detected", {"clauses": [{"kind": "action", "skill": "blades", "score": 0.9}], "input": "x"})
        with self.assertRaises(EventContractError):
            bus.publish("turn_detected", {"clauses": [{"kind": "action", "skil": "blades"}], "input": "x"})
        with self.assertRaises(EventContractError):
            bus.publish("turn_detected", {"clauses": [{"kind": "mystery"}], "input": "x"})

    def test_strict_off_lets_a_deliberately_malformed_payload_through(self):
        bus = ValidatingEventBus()
        bus.strict = False
        bus.publish("llm_response_ready", {"anything": 1})

    def test_unschemad_events_are_never_checked(self):
        ValidatingEventBus().publish("log_info", {"whatever": object()})

    def _declared(self, schema):
        import typing
        return set(typing.get_type_hints(schema))

    def test_every_key_the_narrators_read_from_an_item_result_is_declared(self):
        import glob
        import importlib
        from events.schemas import ItemInteractionResolved

        declared = self._declared(ItemInteractionResolved)
        consumers = [Narration_Prompts.item_interaction]
        for path in glob.glob(os.path.join("intents", "*.py")):
            module = importlib.import_module("intents." + os.path.basename(path)[:-3])
            consumers += [fn for name, fn in vars(module).items() if name.startswith("narrate") and callable(fn)]
        for function in consumers:
            with self.subTest(consumer=function.__module__ + "." + function.__name__):
                self.assertEqual(consumed_keys(function, "data") - declared, set())

    def test_every_key_the_dialogue_narrator_reads_is_declared(self):
        from events.schemas import DialogueResolved
        self.assertEqual(consumed_keys(Narration_Prompts.npc_dialogue, "data") - self._declared(DialogueResolved), set())

    def test_every_key_dmcore_reads_from_a_turn_and_its_clauses_is_declared(self):
        from events.schemas import ActionClause, ItemClause, TurnDetected
        self.assertEqual(consumed_keys(DMCore._on_turn_detected, "data") - self._declared(TurnDetected), set())
        clause_keys = self._declared(ActionClause) | self._declared(ItemClause)
        self.assertEqual(consumed_keys(DMCore._on_turn_detected, "clause") - clause_keys, set())


if __name__ == "__main__":
    unittest.main()
