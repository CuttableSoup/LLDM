import unittest

from tests.support import FakeMatcher, ScriptedSession
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


class TestScriptedSession(unittest.TestCase):
    """!
    @brief tests/support.py's ScriptedSession -- type a line, assert what the game did -- over the
        debug scenario's arena (a hostile wolf, a second wolf, the ally thane), with no model load
        and no network. These are the shape new end-to-end tests take; the real-model corpus tests
        (TestPlayerInputCorpus) still own classification quality.
    """

    def _session(self, **matches):
        return ScriptedSession(self, FakeMatcher(**matches))

    def test_an_attack_on_the_wolf_runs_the_whole_pipeline_into_a_combat_round(self):
        session = self._session(
            actions={"attack the wolf": ("blades", 0.9)}, targets={"attack the wolf": ("wolf", 0.9)},
        )

        events = session.say("I attack the wolf")

        names = session.names(events)
        self.assertLess(names.index("turn_detected"), names.index("round_resolved"))
        self.assertEqual(session.core.current_target, "wolf")
        [round_result] = session.payloads(events, "round_resolved")
        self.assertEqual(round_result["input"], "attack the wolf")

    def test_a_line_nobody_can_make_sense_of_is_not_understood_and_changes_nothing(self):
        session = self._session()
        before = session.core.current_target

        events = session.say("mrrgghfff")

        self.assertIn("action_not_understood", session.names(events))
        self.assertEqual(session.core.current_target, before)

    def test_an_unreachable_model_never_blocks_the_rules(self):
        # The default transport raises: every structured decision answers "unavailable".
        session = self._session(actions={"attack the wolf": ("blades", 0.9)}, targets={"attack the wolf": ("wolf", 0.9)})
        events = session.say("I attack the wolf")
        self.assertIn("round_resolved", session.names(events))

    def test_each_say_returns_only_what_that_line_published(self):
        session = self._session(actions={"attack the wolf": ("blades", 0.9)}, targets={"attack the wolf": ("wolf", 0.9)})
        session.say("I attack the wolf")

        events = session.say("mrrgghfff")

        self.assertNotIn("round_resolved", session.names(events))

    def test_talking_to_someone_present_is_dialogue_not_a_skill_roll(self):
        session = self._session()

        events = session.say("talk to thane")

        self.assertEqual([resolved["target"] for resolved in session.payloads(events, "dialogue_resolved")], ["thane"])
        self.assertNotIn("turn_detected", session.names(events))

    def test_a_weak_attack_on_whoever_you_are_talking_to_asks_first_and_yes_goes_ahead(self):
        session = self._session(actions={"hit you": ("blades", 0.6)})
        session.say("talk to thane")
        session.core.current_target = None

        asked = session.say("I hit you")

        self.assertIn("confirmation_requested", session.names(asked))
        self.assertFalse({"action_resolved", "round_resolved"} & set(session.names(asked)))  # nothing resolved yet
        self.assertFalse(session.core.is_hostile("thane", session.core.player_name))

        answered = session.say("yes")

        self.assertIn("confirmation_answered", session.names(answered))
        self.assertTrue(session.core.is_hostile("thane", session.core.player_name))

    def test_a_weak_attack_answered_no_leaves_everyone_alone(self):
        session = self._session(actions={"hit you": ("blades", 0.6)})
        session.say("talk to thane")
        session.core.current_target = None
        session.say("I hit you")

        session.say("no")

        self.assertFalse(session.core.is_hostile("thane", session.core.player_name))


if __name__ == "__main__":
    unittest.main()
