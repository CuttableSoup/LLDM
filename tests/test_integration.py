"""!
@file test_integration.py
@brief Integration tests that hit a real, running Ollama -- as opposed to test_unit.py,
    which is entirely fast and network-independent. Every TestCase/test function here is
    gated on the same _ollama_reachable() check, so each *skips* (not fails) when nothing's
    listening on 127.0.0.1:11434, rather than dragging the rest of the suite's pass/fail status
    down with them. Nothing is expected to already be running in the background, though --
    importing this module calls Ollama_Launcher.ensure_ollama_running() once, synchronously,
    before any skip gate evaluates (see the module-level call below), the same bootstrap
    LLDM.py's own main() runs, just blocking instead of backgrounded so the gates see a live
    server by the time they check. A from-scratch machine pays the one-time install/pull cost
    documented on ensure_ollama_running itself (which can be many minutes); every run after that
    only pays the cost of spawning the process fresh, since the binary and pulled model persist
    on disk. Run with `python -m pytest -q test_integration.py` (or just let
    `python -m pytest -q` pick both files up); expect ~20-40s per test against a real,
    already-loaded model (TestSaveAndResumeConversation costs roughly double that, since it
    boots two full sessions -- see its docstring), so the whole file runs a few minutes.
"""

import atexit
import os
import random
import shutil
import time
import unittest
import urllib.request

import pytest

from dm.DM_ActionOutcome import DamageEffect, DefenderDetailsEffect, RevealEffect
from dm.DM_Core import DMCore
from Event_Bus import EventBus
from llm.LLM_Core import LLMCore
from llm.Ollama_Launcher import ensure_ollama_running
from nlp.NLP_Core import NLPCore
from gui.Textual_Core import TextualCore
from textual.widgets import RichLog


def _ollama_reachable():
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/v1/models", timeout=2)
        return True
    except Exception:
        return False


# Boots a local Ollama (installing one first if this machine has never had one) before any
# @unittest.skipUnless(_ollama_reachable(), ...)/@pytest.mark.skipif below evaluates -- those
# evaluate at import time, so this has to run synchronously here rather than on a background
# thread the way LLDM.py's own bootstrap does. Only ever terminates the process this call itself
# started (mirrors LLDM.py main()'s own _stop_ollama_if_started) -- a pre-existing Ollama
# instance, or one left running from an earlier test session, is never touched.
_ollama_process = ensure_ollama_running(log=lambda message: print(f"[Ollama bootstrap] {message}"))
if _ollama_process is not None:
    atexit.register(lambda: _ollama_process.poll() is None and _ollama_process.terminate())


def lines_of(app, widget_id):
    return [str(line) for line in app.query_one(f"#{widget_id}", RichLog).lines]


class _LivePipelineTestCase(unittest.TestCase):
    """!
    @brief Shared setup/polling helpers for TestCase-based integration tests that drive the
        real NLPCore/LLMCore/DMCore pipeline via literal user_input_submitted publishes
        against a live Ollama. Not a test itself -- pytest/unittest only collect classes
        named "Test*" by default, and this one deliberately isn't. Subclasses set
        scenario_name and call self._boot() from their own setUp.
    """
    scenario_name = "tavern"
    # A finished character-creation result ({"race", "allocation", "name"}), or None (the
    # default every existing subclass relies on) to boot with the player template's own
    # untouched skills/name -- see DMCore.__init__'s own "character" param.
    character = None

    def _boot(self):
        self.event_bus = EventBus()
        self.responses = []
        self.event_bus.subscribe("llm_response_ready", self.responses.append)

        self.nlp_core = NLPCore(self.event_bus)
        self.llm_core = LLMCore(self.event_bus)
        self.dm_core = DMCore(
            self.event_bus, scenario_name=self.scenario_name, character=self.character,
        )

        self._wait_for_responses(1)  # the scene intro, fired during DMCore.__init__

    def _wait_for_responses(self, count, timeout=30):
        deadline = time.time() + timeout
        while len(self.responses) < count and time.time() < deadline:
            time.sleep(0.2)
        self.assertGreaterEqual(
            len(self.responses), count,
            f"Timed out waiting for the {count}th LLM response (Ollama may be slow/unloaded).",
        )

    def _say(self, player_input):
        self.event_bus.publish("user_input_submitted", player_input)
        self._wait_for_responses(len(self.responses) + 1)
        return self.responses[-1]


@unittest.skipUnless(_ollama_reachable(), "Ollama not reachable at http://127.0.0.1:11434")
class TestInnkeeperConversation(_LivePipelineTestCase):
    """!
    @brief End-to-end conversation test against a real, running Ollama -- the only way to
           actually verify the LLM uses fed context (scenario setting, character roster,
           per-turn defender_details) rather than just checking the prompt shape. Skipped
           entirely (not failed) when Ollama isn't reachable, so the rest of the suite
           stays fast and network-independent.
    """
    scenario_name = "tavern"

    def setUp(self):
        self._boot()

    def test_full_conversation_with_innkeeper(self):
        # NLPCore's confidence_threshold still rejects a lot of naturally-phrased social
        # action once it names a topic (see the dilution gotcha in NLP_Core.py's module
        # notes) -- _generate_match_candidates' alternate phrasings and _match_by_keyword's
        # literal-keyword fallback (added later, see TestNlpConfidenceThreshold) claw some of
        # that back, but neither of these two follow-ups happens to contain a literal
        # skills.toml keyword or strip down to a near-bare one, so they still land below
        # threshold in practice (~0.48 and ~0.37 respectively -- closer than the ~0.41/~0.32
        # this comment used to cite, but not over the line). So this conversation still
        # deliberately mixes both real paths a player will actually hit: one turn that clears
        # the threshold (genuine action_resolved + defender_details) and natural follow-ups
        # that don't (routed to action_not_understood's clarification response instead). Both
        # should stay coherent and grounded, since the persistent system-message roster
        # covers either path.
        action_events = []
        round_events = []
        self.event_bus.subscribe("action_resolved", action_events.append)
        self.event_bus.subscribe("round_resolved", round_events.append)

        turns = [
            "I try to charm her",  # verified: scores ~0.60 on "charisma", clears the threshold
            "Have you heard anything about trouble on the road?",  # verified: ~0.48, still below it
            "I'm sorry -- what happened to your husband?",  # verified: ~0.37, still below it
        ]
        transcript = []
        for player_input in turns:
            response = self._say(player_input)
            transcript.append((player_input, response))
            self.assertTrue(response.strip())
            self.assertNotIn("Could not connect to the local LLM", response)

        print("\n=== Innkeeper conversation transcript ===")
        for player_input, response in transcript:
            print(f"> {player_input}\n{response}\n")

        # A friendly NPC's dialogue should never batch into combat-round narration, and the
        # one turn that did resolve as a real action should be about the innkeeper specifically.
        self.assertEqual(round_events, [])
        self.assertEqual(len(action_events), 1)
        action = action_events[0]["actions"][0]
        self.assertEqual(action.defender, "innkeeper")
        defender_details = next(e.text for e in action.effects if isinstance(e, DefenderDetailsEffect))
        self.assertIn("innkeeper", defender_details)

        # Deliberately NOT asserting on exact narrative content past this point (ex: that the
        # husband question literally says "bandit"/"husband") -- a real run showed the LLM can
        # convey her grief ("a deep, painful sadness... vague sigh") without ever using those
        # words, so a keyword check on live LLM output is just flaky, not a real regression
        # signal. The printed transcript above is how this actually gets verified.


@unittest.skipUnless(_ollama_reachable(), "Ollama not reachable at http://127.0.0.1:11434")
class TestRagGroundedNarration(_LivePipelineTestCase):
    """!
    @brief Verifies the real, end-to-end RAG pipeline (Settings/Fantasy/*.pdf -> RagIndex's
        extraction/chunking/embedding -> LLMCore.perform_rag's per-request retrieval) actually
        fires against a live LLM request -- test_unit.py's TestRagIndex/TestLlmPerformRag cover
        each piece in isolation with fakes, but this is the only way to confirm the whole chain
        is wired together correctly, the same reasoning TestInnkeeperConversation gives for why
        a live Ollama conversation test earns its keep alongside the offline suite.

        Skipped (not failed) if the index isn't built/cached yet -- the first build takes
        minutes against the real sourcebook (see CLAUDE.md's "RAG / sourcebook grounding"),
        and this suite must never become flaky just because a fresh machine hasn't warmed the
        cache. Run test_unit.py or boot the app once beforehand to warm it.
    """
    scenario_name = "tavern"

    def setUp(self):
        self._boot()
        if not self._wait_for_rag_ready(timeout=15):
            self.skipTest("RAG index not built/cached yet -- run the app once to warm the cache.")

    def _wait_for_rag_ready(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline and not self.llm_core.rag_index.ready:
            time.sleep(0.5)
        return self.llm_core.rag_index.ready

    def test_lore_relevant_input_triggers_a_real_rag_retrieval(self):
        # perform_rag runs synchronously inside _queue_narration, before the network call --
        # so a "RAG retrieved" log_info fires whether or not the LLM call itself succeeds,
        # making this a clean signal that retrieval actually happened (not a proxy for the
        # LLM's eventual wording, which this deliberately never asserts on -- see
        # TestInnkeeperConversation's own note on why that's the wrong thing to check).
        log_messages = []
        self.event_bus.subscribe("log_info", log_messages.append)

        response = self._say("What do you know about the nation of Brevoy?")

        self.assertTrue(response.strip())
        self.assertTrue(
            any("RAG retrieved" in message for message in log_messages),
            "Expected a lore-relevant question to retrieve at least one sourcebook chunk.",
        )


@unittest.skipUnless(_ollama_reachable(), "Ollama not reachable at http://127.0.0.1:11434")
class TestArenaCombatConversation(_LivePipelineTestCase):
    """!
    @brief Dialogue's combat counterpart: real NLPCore/DMCore/LLMCore driving several rounds
        of the "arena" scenario (gladstone, two wolves, and the ally thane) via literal attack
        input, checking that real round narration actually flows through a live LLM turn after
        turn -- the mechanics themselves (behavior resolution, damage, targeting) are already
        exhaustively unit-tested with no LLM involved at all, by TestCombatLoop/
        TestEntityBehavior in test_unit.py. roll_dice is genuinely random (see DM_Combat.py),
        not seeded here, so every assertion below checks the event/narration *structure* holds
        up round after round rather than who lands a hit or who wins.
    """
    scenario_name = "arena"

    def setUp(self):
        self._boot()

    def test_several_combat_rounds_narrate_through_the_real_pipeline(self):
        round_events = []
        action_events = []
        self.event_bus.subscribe("round_resolved", round_events.append)
        self.event_bus.subscribe("action_resolved", action_events.append)

        turns = ["I attack the wolf", "I attack the wolf", "I attack the wolf"]
        transcript = []
        for player_input in turns:
            response = self._say(player_input)
            transcript.append((player_input, response))
            self.assertTrue(response.strip())
            self.assertNotIn("Could not connect to the local LLM", response)

        print("\n=== Arena combat transcript ===")
        for player_input, response in transcript:
            print(f"> {player_input}\n{response}\n")

        # A hostile target must always batch into round narration, never the single-action path.
        self.assertEqual(action_events, [])
        self.assertEqual(len(round_events), len(turns))
        self.assertEqual([r["round"] for r in round_events], [1, 2, 3])

        # thane (the ally) is guaranteed a valid opponent (current_target) in round 1 -- both
        # wolves and thane all start the scene alive, so unlike whether either wolf actually
        # lands a bite back, this doesn't depend on any roll's outcome.
        first_round_actors = [turn["actor"] for turn in round_events[0].get("turns", [])]
        self.assertIn("thane", first_round_actors)


@unittest.skipUnless(_ollama_reachable(), "Ollama not reachable at http://127.0.0.1:11434")
class TestMultiActionCombatConversation(_LivePipelineTestCase):
    """!
    @brief The West End Games multi-action rule's own live-pipeline proof (see DM_Core.py's
        "Multiple actions" docstring) -- real NLPCore clause-splitting a genuinely
        conjunction-phrased turn ("...and...") into more than one action, and real DMCore
        still batching all of it into exactly one round. The penalty math itself
        (dice_penalty reducing the pool, never the defender's own roll) and the
        engaged_combat_target regression are already exhaustively unit-tested with no LLM
        involved at all (TestMultipleActions, test_unit.py); this is only checking that a
        real, unseeded roll_dice and a real LLM narration call don't trip over the batch
        shape end to end.
    """
    scenario_name = "arena"

    def setUp(self):
        self._boot()

    def test_two_actions_in_one_turn_narrate_as_one_round(self):
        round_events = []
        action_events = []
        self.event_bus.subscribe("round_resolved", round_events.append)
        self.event_bus.subscribe("action_resolved", action_events.append)

        # "with my weapon", not a bare "attack the wolf" -- the target's own name is also a
        # spell's own name ("summon spectral wolf"), and a bare "attack the wolf" loses to it
        # on raw semantic similarity (both share the literal word "wolf") despite blades' own
        # "attack the creature"/"attack the creature with my weapon" keywords existing
        # specifically to fight this lexical-magnetism risk (see skills.toml's own comment) --
        # naming the weapon explicitly is what actually closes the gap for a target whose name
        # collides with an ability's own.
        turn_input = "I attack the wolf with my weapon and attack it again"
        response = self._say(turn_input)

        self.assertTrue(response.strip())
        self.assertNotIn("Could not connect to the local LLM", response)
        print(f"\n=== Multi-action turn ===\n> {turn_input}\n{response}\n")

        # Two clauses, both hostile-target attacks -- one round, not two, and the player's own
        # "actions" list actually has both entries (see DM_Core.py's engaged_combat_target note
        # for why this used to be at risk of silently dropping to zero or one).
        self.assertEqual(action_events, [])
        self.assertEqual(len(round_events), 1)
        self.assertEqual(round_events[0]["round"], 1)
        actions = round_events[0]["actions"]
        self.assertEqual(len(actions), 2)
        self.assertEqual([a.skill for a in actions], ["blades", "blades"])

    def test_a_multi_action_turn_does_not_leak_state_into_the_next_ordinary_turn(self):
        round_events = []
        self.event_bus.subscribe("round_resolved", round_events.append)

        first = self._say("I attack the wolf and attack it again")
        second = self._say("I attack the wolf")

        for response in (first, second):
            self.assertTrue(response.strip())
            self.assertNotIn("Could not connect to the local LLM", response)

        # The batch machinery is entirely local to one _on_turn_detected call (dice_penalty
        # is recomputed fresh from len(clauses) every time) -- this is the live-pipeline check
        # that nothing about resolving a 2-action round leaves any stray state (an inflated
        # penalty, an extra queued action, ...) behind for the very next, perfectly ordinary
        # single-action turn.
        self.assertEqual(len(round_events), 2)
        self.assertEqual([r["round"] for r in round_events], [1, 2])
        self.assertEqual(len(round_events[0]["actions"]), 2)
        self.assertEqual(len(round_events[1]["actions"]), 1)


@unittest.skipUnless(_ollama_reachable(), "Ollama not reachable at http://127.0.0.1:11434")
class TestCreatedCharacterConversation(_LivePipelineTestCase):
    """!
    @brief End-to-end proof that a freshly created, custom-named, custom-race character --
        not characters.toml's own hand-authored "gladstone" -- narrates and fights correctly
        through the real pipeline. Uses Rules/Fantasy/scenarios/character_test.toml, a
        scenario that never names any specific character by its literal template name (see
        DM_Rules.py's PLAYER_PLACEHOLDER), so this is exercising the same generic-player
        resolution real scenarios (arena/tavern/field/dungeon/crypt) now rely on too, without
        coupling this test's own pass/fail to a rebalance of any of them.
    """
    scenario_name = "character_test"
    character = {
        "race": "elf",
        "allocation": {"arcane": 5, "stealth": 5, "observation": 5},
        "name": "Aria",
    }

    def setUp(self):
        self._boot()

    def test_scene_intro_and_combat_narrate_the_custom_character_not_gladstone(self):
        self.assertEqual(self.dm_core.player_name, "Aria")
        intro = self.responses[-1]
        self.assertTrue(intro.strip())
        self.assertNotIn("Could not connect to the local LLM", intro)

        round_events = []
        self.event_bus.subscribe("round_resolved", round_events.append)
        response = self._say("I attack the wolf")

        self.assertTrue(response.strip())
        self.assertNotIn("Could not connect to the local LLM", response)
        self.assertEqual(len(round_events), 1)
        self.assertEqual(round_events[0]["actions"][0].entity, "Aria")


@unittest.skipUnless(_ollama_reachable(), "Ollama not reachable at http://127.0.0.1:11434")
class TestChestSagaConversation(_LivePipelineTestCase):
    """!
    @brief Item-interaction's full-pipeline counterpart: real NLPCore/DMCore/LLMCore driving
        the dungeon chest's entire lifecycle (locked -> picked -> opened -> examined -> taken)
        via literal input, checking real LLM narration lands at each step and actually matches
        the underlying state change -- distinct from TestLockedChest/TestItemInteraction/
        TestOpenClose in test_unit.py, which call DMCore's methods directly with no NLP or LLM
        in the loop at all.
    """
    scenario_name = "dungeon"

    def setUp(self):
        self._boot()

    def tearDown(self):
        # random.seed() is a Python-process-wide global, not scoped to this test -- reseeding
        # from OS entropy here (rather than leaving the deterministic seed from the test below
        # in place) is what stops this test from silently making every *other* test's dice
        # rolls deterministic too, for the rest of this pytest run. Found the hard way: a
        # single shared pytest process runs test_integration.py before test_unit.py
        # (alphabetical collection order), and leaving seed(3) in place made an unrelated
        # test_unit.py combat roll deterministic enough to kill a wolf that test never expected
        # to die, silently changing self.dm_core.current_target out from under it.
        random.seed()

    def test_examine_pick_open_check_examine_take_through_the_real_pipeline(self):
        # Two real rolls happen in this saga, consumed in order: the lockpick (gladstone's
        # finesse, 3 dice, vs the chest's flat entity.test difficulty of 12), then the arcane
        # curse-detection check against the dagger itself (2 dice vs its own difficulty of 8).
        # Left unseeded, either roll has a real chance to land on its failure branch (the
        # chest's is permanent -- "jammed" -- so an unseeded run would only reach "take" on
        # ~1 in 4 attempts) -- seeding makes both real dice-rolling code paths deterministic
        # without mocking either: seed 13 rolls [3, 3, 6] = 12 (a pass) then [6, 2] = 8 (also a
        # pass, exactly on the line). Re-pick this seed if characters.toml's finesse/arcane
        # dice or either entity.test's difficulty ever change. tearDown restores real entropy
        # afterward so this doesn't leak into any other test in the same run.
        random.seed(13)

        resolved_events = []
        action_events = []
        round_events = []
        self.event_bus.subscribe("item_interaction_resolved", resolved_events.append)
        self.event_bus.subscribe("action_resolved", action_events.append)
        self.event_bus.subscribe("round_resolved", round_events.append)
        player_name = self.dm_core.player_name

        transcript = []

        def say(player_input):
            response = self._say(player_input)
            transcript.append((player_input, response))
            self.assertTrue(response.strip())
            self.assertNotIn("Could not connect to the local LLM", response)
            return response

        say("examine the chest")
        self.assertEqual(resolved_events[-1]["reason"], "locked")

        say("pick the lock")
        pick_result = action_events[-1]["actions"][0]
        self.assertEqual(pick_result.skill, "finesse")
        self.assertTrue(
            pick_result.success, "Seeded roll should have passed -- see the seed comment above."
        )
        self.assertFalse(self.dm_core.is_locked("chest"))

        say("open the chest")
        self.assertEqual(resolved_events[-1]["intent"], "open")
        self.assertTrue(resolved_events[-1]["found"])
        self.assertFalse(self.dm_core.is_closed("chest"))
        # This is the actual hallucination fix from the previous session: real contents, not
        # invented treasure -- and the dagger's own "cursed" tag must not appear here, only
        # its flavor description (see NLPCore.map_to_target/DMCore._resolve_item_test_target
        # below for the only real way to learn that fact).
        self.assertEqual(len(resolved_events[-1]["contents"]), 1)
        self.assertIn("runes", resolved_events[-1]["contents"][0])

        # NLPCore.map_to_target now matches this against items carrying their own
        # [entity.test], not just creatures (see the NLP_Core.py module notes) -- "the dagger"
        # confidently resolves to "cursed dagger" while it's still sitting in the open chest,
        # not yet taken, exercising DMCore._resolve_item_test_target's container-reachability
        # path rather than the (already unit-tested) player-inventory one. Phrasing matters
        # here more than usual: "dagger" is itself a literal "blades" keyword (skills.toml),
        # which dominates skill matching hard enough that "I check the dagger for curses" and
        # several other natural phrasings actually resolved to "blades", not "arcane" -- this
        # one was verified live to clear confidence_threshold on "arcane" instead.
        say("I channel arcane mana into the dagger")
        self.assertEqual(round_events, [])  # inspecting an item is never combat
        check_result = action_events[-1]["actions"][0]
        self.assertEqual(check_result.skill, "arcane")
        self.assertEqual(check_result.defender, "cursed dagger")
        self.assertTrue(check_result.success, "Seeded roll should have passed -- see the seed comment above.")
        reveal_effects = [e for e in check_result.effects if isinstance(e, RevealEffect)]
        self.assertEqual(reveal_effects[0].tags, ["cursed"])
        self.assertTrue(self.dm_core.is_identified("cursed dagger"))

        say("examine the cursed dagger")
        self.assertEqual(resolved_events[-1]["item_name"], "cursed dagger")
        self.assertTrue(resolved_events[-1]["found"])
        self.assertEqual(resolved_events[-1]["revealed"], ["cursed"])
        self.assertNotIn("cursed dagger", self.dm_core.entities[player_name]["inventory"])

        say("take the cursed dagger")
        self.assertIn("cursed dagger", self.dm_core.entities[player_name]["inventory"])
        self.assertNotIn("cursed dagger", self.dm_core.entities["chest"]["inventory"])

        print("\n=== Chest saga transcript ===")
        for player_input, response in transcript:
            print(f"> {player_input}\n{response}\n")


@unittest.skipUnless(_ollama_reachable(), "Ollama not reachable at http://127.0.0.1:11434")
class TestChestTradeConversation(_LivePipelineTestCase):
    """!
    @brief Economy's full-pipeline counterpart: real NLPCore/DMCore/LLMCore driving a "buy"
        attempt against the dungeon chest (reused as an ad-hoc shop -- same convention
        TestGiveAndTrade's unit tests already use, since it's the only entity in the ruleset
        with both a price and something to sell) via literal input, checking both the
        affordability gate and a successful purchase narrate coherently and actually move
        currency/inventory. Deliberately bypasses lockpicking in setUp (dismiss_condition
        called directly, not a real "pick the lock" attempt) since that state machine is
        TestChestSagaConversation's job -- trading itself rolls no dice at all (transfer_
        currency/transfer_item are both deterministic), so this test needs no seeded randomness.
    """
    scenario_name = "dungeon"

    def setUp(self):
        self._boot()
        self.dm_core.dismiss_condition("chest", "locked")
        self.dm_core.dismiss_condition("chest", "closed")

    def test_afford_gate_then_successful_purchase_through_the_real_pipeline(self):
        resolved_events = []
        self.event_bus.subscribe("item_interaction_resolved", resolved_events.append)
        player_name = self.dm_core.player_name
        price = self.dm_core.entities["cursed dagger"]["value"]
        starting_chest_currency = self.dm_core.entities["chest"]["currency"]

        self.dm_core.entities[player_name]["currency"] = 0
        denied_response = self._say("buy the cursed dagger")
        self.assertEqual(resolved_events[-1]["reason"], "cant_afford")
        self.assertEqual(resolved_events[-1]["price"], price)
        self.assertIn("cursed dagger", self.dm_core.entities["chest"]["inventory"])
        self.assertEqual(self.dm_core.entities[player_name]["currency"], 0)

        self.dm_core.entities[player_name]["currency"] = 100
        bought_response = self._say("buy the cursed dagger")
        self.assertTrue(resolved_events[-1]["found"])
        self.assertEqual(resolved_events[-1]["price"], price)
        self.assertIn("cursed dagger", self.dm_core.entities[player_name]["inventory"])
        self.assertNotIn("cursed dagger", self.dm_core.entities["chest"]["inventory"])
        self.assertEqual(self.dm_core.entities[player_name]["currency"], 100 - price)
        self.assertEqual(self.dm_core.entities["chest"]["currency"], starting_chest_currency + price)

        for response in (denied_response, bought_response):
            self.assertTrue(response.strip())
            self.assertNotIn("Could not connect to the local LLM", response)

        print("\n=== Chest trade transcript ===")
        print(f"> buy the cursed dagger (broke)\n{denied_response}\n")
        print(f"> buy the cursed dagger (funded)\n{bought_response}\n")


@unittest.skipUnless(_ollama_reachable(), "Ollama not reachable at http://127.0.0.1:11434")
class TestCryptDungeonConversation(_LivePipelineTestCase):
    """!
    @brief The multi-room dungeon's full-pipeline counterpart: real NLPCore/DMCore/LLMCore
        driving an actual room-to-room crawl through "crypt" (Rules/Fantasy/scenarios/
        crypt.toml) -- a trap's disarm-or-be-damaged check, a real combat kill, a room
        transition narrated through a live LLM call (DMCore._resolve_room_transition_intent /
        LLMCore.generate_item_interaction_response's "move" branch -- nothing in
        test_unit.py's TestMultiRoomDungeon touches the LLM side of this at all, only the
        mechanics), the band-gated *branch* into the hidden alcove and back out again, and a
        second, room-local chest -- distinct from TestChestSagaConversation, which never
        leaves its one room at all.
    """
    scenario_name = "crypt"

    def setUp(self):
        self._boot()

    def tearDown(self):
        # See TestChestSagaConversation's own tearDown for why this matters: leaving a
        # deterministic seed in place would silently affect other tests' unrelated rolls for
        # the rest of this pytest process.
        random.seed()

    def test_disarm_trap_kill_spider_branch_and_loot_through_the_real_pipeline(self):
        # Seed 44 gives a clean run start to finish, verified directly against the mechanics
        # (no LLM involved in producing these numbers, only in narrating them): the entrance's
        # dart trap disarm (finesse, 3 dice, difficulty 9) rolls 14 -- a pass. The spider fight
        # is a three-front affair -- thane and anne (allies present in every room, not just
        # combat, see CLAUDE.md's "Combat") both join gladstone's own attacks each round -- so
        # the same two "I attack the spider" actions as before still account for the whole
        # fight, carried by anne's own "splash flow" (arcane 12 vs difficulty 2 for 8 damage,
        # then 16 vs difficulty 6 for 12) and gladstone's own second-round blades hit (22 vs
        # difficulty 19 for 9 damage) while thane whiffs both his own rolls; the hidden
        # alcove's coffer lock (finesse, difficulty 8) rolls 11; the iron chest's own lock
        # (finesse, difficulty 10) rolls 10 -- both passes. Re-pick this seed if any of these
        # dice/difficulties/entities/character skills ever change.
        random.seed(44)

        action_events = []
        item_events = []
        round_events = []
        self.event_bus.subscribe("action_resolved", action_events.append)
        self.event_bus.subscribe("item_interaction_resolved", item_events.append)
        self.event_bus.subscribe("round_resolved", round_events.append)
        player_name = self.dm_core.player_name

        transcript = []

        def say(player_input):
            response = self._say(player_input)
            transcript.append((player_input, response))
            self.assertTrue(response.strip())
            self.assertNotIn("Could not connect to the local LLM", response)
            return response

        say("I disarm the trap")
        disarm_result = action_events[-1]["actions"][0]
        self.assertTrue(disarm_result.success)
        # a passed disarm never damages the player
        self.assertFalse(any(isinstance(e, DamageEffect) for e in disarm_result.effects))

        say("I advance")
        say("continue deeper")
        self.assertEqual(item_events[-1]["room_name"], "The Hall of Webs")
        self.assertEqual(self.dm_core.current_room_key, "hall_of_webs")

        say("I attack the spider")
        say("I attack the spider")
        self.assertEqual(self.dm_core.get_current_hp("giant spider"), 0)
        # A hostile creature always batches into round narration, never the single-action path.
        self.assertEqual(len(round_events), 2)

        say("continue deeper")
        self.assertEqual(self.dm_core.current_room_key, "guard_chamber")

        # The actual point of this test: guard_chamber has a real branch (see DM_Rules.py's
        # room-graph notes) -- "right" only resolves once the player has actually moved to
        # band 2, matching this room's own [[room.exit]] declaration, not just the word said.
        say("I advance")
        say("go right")
        self.assertEqual(item_events[-1]["room_name"], "The Hidden Alcove")
        self.assertEqual(self.dm_core.current_room_key, "hidden_alcove")

        say("I pick the lock")
        self.assertTrue(action_events[-1]["actions"][0].success)
        say("open the coffer")
        say("take the health potion")
        self.assertIn("health potion", self.dm_core.entities[player_name]["inventory"])

        # Back out of the branch, then push on to the room's own main-path chest.
        say("go back the way we came")
        self.assertEqual(self.dm_core.current_room_key, "guard_chamber")
        say("I advance")
        say("I pick the lock")
        self.assertTrue(action_events[-1]["actions"][0].success)
        say("open the chest")
        say("take the health potion")

        # Started with 3 health potions, +1 from the coffer, +1 from the chest.
        self.assertEqual(self.dm_core.entities[player_name]["inventory"].count("health potion"), 5)
        # Every check passed and the trap was disarmed cleanly -- no damage the whole way through.
        self.assertEqual(self.dm_core.get_current_hp(player_name), 36)

        print("\n=== Crypt dungeon transcript ===")
        for player_input, response in transcript:
            print(f"> {player_input}\n{response}\n")


@unittest.skipUnless(_ollama_reachable(), "Ollama not reachable at http://127.0.0.1:11434")
class TestSaveAndResumeConversation(unittest.TestCase):
    """!
    @brief Simulates an actual app restart, not just a save_game/load_game round-trip: session
        A drives a real conversation and saves via a literal typed "save as <slot>" command
        (exercising NLPCore's own prefix-detection intercept -- see _detect_save_load_intent --
        not DMCore.save_game called directly, which is all TestSaveLoad/TestLLMSaveLoad in
        test_unit.py ever do), then session B -- an entirely separate EventBus/NLPCore/LLMCore/
        DMCore, standing in for a fresh process -- resumes it via a literal "load <slot>"
        command and keeps talking. Costs roughly double a normal test here, since the slow
        sentence-transformers load happens once per session -- the honest price of actually
        simulating two processes instead of reusing state under the hood.
    """

    def setUp(self):
        self.slot = "test-integration-save-resume"
        self.slot_dir = None

    def tearDown(self):
        if self.slot_dir:
            shutil.rmtree(self.slot_dir, ignore_errors=True)

    def _wait_for(self, responses, count, timeout=30):
        deadline = time.time() + timeout
        while len(responses) < count and time.time() < deadline:
            time.sleep(0.2)
        self.assertGreaterEqual(
            len(responses), count,
            f"Timed out waiting for the {count}th LLM response (Ollama may be slow/unloaded).",
        )

    def test_session_resumes_with_prior_context_via_real_save_load_commands(self):
        # --- Session A: have a real turn, then save via a literal typed command ---
        bus_a = EventBus()
        responses_a = []
        save_events = []
        bus_a.subscribe("llm_response_ready", responses_a.append)
        bus_a.subscribe("game_saved", save_events.append)
        NLPCore(bus_a)
        llm_a = LLMCore(bus_a)
        dm_a = DMCore(bus_a, scenario_name="tavern")
        self._wait_for(responses_a, 1)  # the scene intro

        self.slot_dir = dm_a._save_slot_dir(self.slot)

        bus_a.publish("user_input_submitted", "I try to charm her")
        self._wait_for(responses_a, 2)

        bus_a.publish("user_input_submitted", f"save as {self.slot}")
        self.assertEqual(save_events, [{"slot": self.slot}])
        self.assertTrue(os.path.exists(os.path.join(self.slot_dir, "dm_state.json")))
        self.assertTrue(os.path.exists(os.path.join(self.slot_dir, "llm_state.json")))

        pre_save_context = list(llm_a.context_window)

        # --- Session B: entirely fresh instances, standing in for a real app restart ---
        bus_b = EventBus()
        responses_b = []
        load_events = []
        bus_b.subscribe("llm_response_ready", responses_b.append)
        bus_b.subscribe("game_loaded", load_events.append)
        NLPCore(bus_b)
        llm_b = LLMCore(bus_b)
        DMCore(bus_b, scenario_name="tavern")
        self._wait_for(responses_b, 1)  # session B's own fresh intro, unrelated to the save

        responses_before_load = len(responses_b)
        bus_b.publish("user_input_submitted", f"load {self.slot}")

        self.assertEqual(len(load_events), 1)
        self.assertEqual(load_events[0]["slot"], self.slot)
        # Loading is silent -- no new LLM call queued -- so resuming shouldn't add a response.
        time.sleep(1)
        self.assertEqual(len(responses_b), responses_before_load)
        self.assertEqual(llm_b.context_window, pre_save_context)

        # The conversation should continue coherently from the restored context, not repeat or
        # reset the opening scene.
        bus_b.publish("user_input_submitted", "have you heard anything about trouble on the road")
        self._wait_for(responses_b, responses_before_load + 1)
        resumed_response = responses_b[-1]
        self.assertTrue(resumed_response.strip())
        self.assertNotIn("Could not connect to the local LLM", resumed_response)

        print("\n=== Session A (pre-save) ===")
        for msg in pre_save_context:
            print(f"[{msg['role']}] {msg['content'][:200]}")
        print("\n=== Session B (post-load, continuing) ===")
        print(f"> have you heard anything about trouble on the road\n{resumed_response}")


@unittest.skipUnless(_ollama_reachable(), "Ollama not reachable at http://127.0.0.1:11434")
class TestAdHocRemovalLive(unittest.TestCase):
    """!
    @brief A real, live tool-calling round trip against decide_entity_removal (AdHoc_Generation.py)
        -- proves the currently-loaded model actually honors the hostile_entities guardrail
        (DM_Improvisation.py's _attempt_entity_removal) rather than just checking the prompt
        shape. Without hostile_entities, live probing against this same model found it complies
        with "get rid of that wolf, this fight is too hard" unconditionally, every phrasing,
        every trial -- a free, dice-free win button against anything currently trying to kill
        the player (see AdHoc_Generation.py's own module note on decide_entity_removal). No
        NLPCore/LLMCore needed -- calls _attempt_entity_removal directly, the same real
        candidate-set-building + live LLM call ADaM's own help channel uses, bypassing only
        NLP_Core.py's cheap local REMOVAL_KEYWORDS pre-check (never the real arbiter).
    """

    def setUp(self):
        self.dm_core = DMCore(EventBus(), scenario_name="arena")

    def test_declines_to_remove_a_live_hostile_just_because_the_fight_is_hard(self):
        outcome = self.dm_core._attempt_entity_removal(
            "ADaM, get rid of that wolf, this fight is too hard"
        )
        print(f"\n=== hostile removal attempt ===\n{outcome}")
        self.assertFalse(outcome.get("removed"), f"model removed a live hostile: {outcome}")
        self.assertIn("wolf", self.dm_core.scenario_entities)

    def test_declines_to_banish_a_live_hostile_the_player_wants_to_skip(self):
        outcome = self.dm_core._attempt_entity_removal(
            "ADaM, banish the wolf, I don't want to fight it"
        )
        print(f"\n=== hostile banish attempt ===\n{outcome}")
        self.assertFalse(outcome.get("removed"), f"model removed a live hostile: {outcome}")
        self.assertIn("wolf", self.dm_core.scenario_entities)

    def test_still_allows_removing_something_genuinely_harmless(self):
        # Proves the guardrail is targeted at live threats, not a blanket removal refusal --
        # thane is present but positive-disposition (never hostile, see arena.toml's own
        # thane), so a plainly non-combat removal request about him should still be honored
        # normally.
        outcome = self.dm_core._attempt_entity_removal(
            "ADaM, thane just left to get supplies, please remove him from the scene"
        )
        print(f"\n=== harmless removal attempt ===\n{outcome}")
        self.assertTrue(outcome.get("removed"), f"model wrongly declined a harmless removal: {outcome}")
        self.assertEqual(outcome.get("name"), "thane")
        self.assertNotIn("thane", self.dm_core.scenario_entities)


@unittest.skipUnless(_ollama_reachable(), "Ollama not reachable at http://127.0.0.1:11434")
class TestNpcGenerationLive(unittest.TestCase):
    """!
    @brief A real, live tool-calling round trip against Ollama -- test_unit.py's own
        TestNpcGeneration/TestNpcGenerationDMCoreIntegration cover the pure math and the
        DMCore-side wiring with an injected fake, but only a real call actually proves the
        currently-loaded model reliably returns a valid tool_calls response shaped the way
        NPC_Generation.py expects (enum-constrained keywords, a real backstory) rather than
        silently falling back every time. No NLPCore/LLMCore needed -- generation happens
        synchronously during DMCore's own construction, nothing here narrates.
    """

    def test_generated_stranger_generates_a_real_npc_fit_to_the_players_own_cr(self):
        dm_core = DMCore(EventBus(), scenario_name="npc_generation_test")
        entity = dm_core.entities["generated_stranger"]

        self.assertTrue(entity["generated"])
        self.assertTrue(entity["name"])
        self.assertNotEqual(entity["name"], "Unnamed Stranger")  # the offline-fallback name
        self.assertTrue(entity["description"])
        self.assertTrue(entity["skills"])
        self.assertGreater(entity["max_hp"], 0)

        npc_cr = dm_core.get_challenge_rating("generated_stranger")
        player_cr = dm_core.get_challenge_rating(dm_core.player_name)
        # variance=0.15 (the module default) plus this template's own hp_share/keyword-count
        # slop -- a generous band, since this is checking "the whole pipeline produced a
        # sane, roughly-matched NPC," not pinning down the exact fitting math (already
        # covered exactly by test_unit.py's own round-trip test).
        self.assertLess(
            abs(npc_cr - player_cr), player_cr * 0.5,
            f"generated CR {npc_cr} too far from player CR {player_cr}",
        )

        print(f"\n=== Generated NPC ===\n{entity['name']} (CR {npc_cr}, player CR {player_cr})")
        print(f"{entity['description']}")
        print(f"skills: {entity['skills']}")

        roster_line = dm_core.describe_character("generated_stranger")
        self.assertTrue(roster_line.startswith(entity["name"]))


@pytest.mark.skipif(not _ollama_reachable(), reason="Ollama not reachable at 127.0.0.1:11434")
@pytest.mark.asyncio
async def test_innkeeper_dialogue_through_textual():
    """!
    @brief Full-stack integration test for the tavern dialogue: the real NLPCore, LLMCore
        (hitting a real, running Ollama -- skipped, not failed, if nothing's listening),
        DMCore, and TextualCore all wired together, driven by actual keystrokes into
        "#input_box" rather than synthetic event_bus.publish calls. This is the Textual
        counterpart to TestInnkeeperConversation above, which drives the identical conversation
        via direct publishes and asserts on internal event payloads -- this one instead
        verifies the player-facing surface: what a person actually watching the History pane
        would see, end to end, through the real UI layer.
    """
    event_bus = EventBus()
    responses = []
    action_events = []
    round_events = []
    event_bus.subscribe("llm_response_ready", responses.append)
    event_bus.subscribe("action_resolved", action_events.append)
    event_bus.subscribe("round_resolved", round_events.append)

    # Same construction order LLDM.py boots in: NLPCore, LLMCore, the UI, DMCore last (it
    # fires scenario_loaded during __init__, so everything that narrates from it must already
    # be subscribed).
    nlp_core = NLPCore(event_bus)
    llm_core = LLMCore(event_bus)
    app = TextualCore(event_bus)
    dm_core = DMCore(event_bus, scenario_name="tavern")

    async def wait_for_response_count(pilot, count, timeout=30):
        deadline = time.time() + timeout
        while len(responses) < count and time.time() < deadline:
            await pilot.pause(0.2)
        assert len(responses) >= count, (
            f"Timed out waiting for the {count}th LLM response (Ollama may be slow/unloaded)."
        )
        # One more pump so a response that just arrived is actually written into the widget --
        # TextualCore's call_safely posts through call_from_thread, which needs the app's own
        # loop to run before the write lands.
        await pilot.pause()

    async with app.run_test() as pilot:
        # The scene intro is queued during DMCore.__init__, before the app ever mounted --
        # TextualCore buffers it and on_mount flushes it once mounting finishes (see
        # test_unit.py's test_events_published_before_mount_are_buffered_then_flushed), but the
        # real Ollama call behind it can still take a few seconds, so this waits on the
        # event itself rather than assuming it's already landed.
        await wait_for_response_count(pilot, 1)

        # Same three turns as TestInnkeeperConversation, minus punctuation Pilot's key-press
        # API can't send directly (apostrophes/"?"/"--") -- verified separately that stripping
        # it doesn't change which turns clear confidence_threshold (only "charm her" does).
        turns = [
            "i try to charm her",
            "have you heard anything about trouble on the road",
            "im sorry what happened to your husband",
        ]
        for player_input in turns:
            target = len(responses) + 1
            await pilot.click("#input_box")
            keys = ["space" if c == " " else c for c in player_input]
            await pilot.press(*keys)
            await pilot.press("enter")
            await wait_for_response_count(pilot, target)

        history = lines_of(app, "history")
        print("\n=== Innkeeper conversation transcript (via Textual) ===")
        for line in history:
            print(line)

        for player_input in turns:
            assert any(f"> {player_input}" in line for line in history)
        assert not any("Could not connect to the local LLM" in line for line in history)

        # A friendly NPC's dialogue should never batch into combat-round narration, and the
        # one turn that did resolve as a real action (see the probe above) should be about the
        # innkeeper specifically -- same regression guard TestInnkeeperConversation makes,
        # just observed through the UI's own event subscriptions instead of a direct call.
        assert round_events == []
        assert len(action_events) == 1
        assert action_events[0]["actions"][0].defender == "innkeeper"
        assert len(responses) == len(turns) + 1


if __name__ == "__main__":
    unittest.main()
