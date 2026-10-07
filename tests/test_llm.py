import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import zipfile
from types import SimpleNamespace
from typing import get_args
from unittest.mock import MagicMock, patch
import numpy as np
from sentence_transformers import SentenceTransformer
from dm.DM_ActionOutcome import (
    ActionOutcome,
    DefenderDetailsEffect,
    LootEffect,
    MovementOutcome,
    RolledOutcome,
    SummonEffect,
    TransferOutcome,
)
from tests.event_contract import ValidatingEventBus
from llm.LLM_Core import CHARS_PER_TOKEN, CONTEXT_TOKEN_BUDGET, RESPONSE_TOKEN_RESERVE, LLMCore
from llm import Narration_Prompts
from llm.Narration_Prompts import _OUTCOME_FORMATTERS, Narration, NarratorState, Notice, Skip
import llm.LLM_Backend as LLM_Backend
import llm.Ollama_Launcher as Ollama_Launcher
from llm.Ollama_Launcher import ensure_ollama_running
from llm.LLM_Rag import RagIndex
from tests.support import (
    LLMTestCase,
    script_llm,
)
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


class TestClarificationResponse(LLMTestCase):
    def test_unmatched_input_queues_a_clarification_prompt_not_a_dice_roll(self):
        # _queue appends to context_window synchronously before spawning the
        # background network fetch, so this is checkable without waiting on (or mocking) LM
        # Studio -- the point here is the prompt shape, not the LLM's actual reply.
        self.event_bus.publish("action_not_understood", {"input": "hey there innkeeper", "score": 0.32})

        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("hey there innkeeper", prompt)
        # No roll data (that's describe_outcome's shape, used by the other narration paths).
        self.assertNotIn("Skill used:", prompt)
        self.assertNotIn("difficulty", prompt)

    def test_prompt_explicitly_forbids_inventing_a_new_character_item_or_location(self):
        # This is the narration trigger with the least real state behind it -- see
        # docs/narration-llm.md's "Denial-path grounding" -- so the prompt has to say so itself
        # rather than trusting the standing system message alone (ex: a bare "who is here"-
        # shaped question that missed SCENE_QUERY_KEYWORDS previously fell through to here and
        # got three fully invented patrons back).
        self.event_bus.publish("action_not_understood", {"input": "who all is here", "score": 0.1})

        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("without inventing", prompt)

    def testdescribe_outcome_includes_loot_so_the_llm_isnt_left_guessing(self):
        # Without this, the LLM has no idea what was actually gained and will happily invent
        # contents that don't match the real game state (observed: it narrated a "silver key
        # and leather-bound journal" for a chest that actually just held currency).
        result = RolledOutcome(
            entity="gladstone", skill="finesse", roll=18, difficulty=12,
            success=True, defender="chest", effects=[LootEffect(currency=20, items=[])],
            input="I pick the lock",
        )
        description = Narration_Prompts.describe_outcome(result)
        self.assertIn("20 coins", description)
        self.assertNotIn("currency", description)

    def testdescribe_outcome_uses_the_loot_effects_own_coin_text(self):
        result = RolledOutcome(
            entity="gladstone", skill="finesse", roll=18, difficulty=12, success=True, defender="chest",
            effects=[LootEffect(currency=1.2, items=[], currency_text="1 gold piece and 2 silver pieces")],
            input="I pick the lock",
        )
        self.assertIn("1 gold piece and 2 silver pieces", Narration_Prompts.describe_outcome(result))

    def testdescribe_outcome_mentions_a_successful_summon(self):
        # Without this, a summoning spell's own roll outcome narrates exactly like an ordinary
        # no-damage opposed check -- nothing tells the LLM a creature actually appeared.
        result = RolledOutcome(
            entity="gladstone", skill="arcane", roll=18, difficulty=12,
            success=True, effects=[SummonEffect(name="spectral wolf")],
            input="I summon a wolf",
        )
        description = Narration_Prompts.describe_outcome(result)
        self.assertIn("summons spectral wolf", description)

    def test_outcome_formatters_cover_every_actionoutcome_variant(self):
        # A future ActionOutcome variant with no matching _OUTCOME_FORMATTERS entry would only
        # surface as a live KeyError mid-narration -- this catches it as a fast, obvious unit
        # test instead, the same "one new variant per commit" pattern this table exists to keep
        # up with. MovementOutcome/TransferOutcome are the two deliberate exceptions -- neither
        # carries "input" at all, so neither ever reaches _OUTCOME_FORMATTERS (see
        # describe_outcome's own two early-return isinstance checks, ahead of the dict dispatch).
        for variant in get_args(ActionOutcome):
            if variant in (MovementOutcome, TransferOutcome):
                continue
            self.assertIn(variant, _OUTCOME_FORMATTERS)


class TestFreeformDialogueNarration(LLMTestCase):
    """!
    @brief LLMCore's own side of DM_Dialogue.py's channel: generate_npc_dialogue, and the
        presence-tagging/filtering machinery every _queue/_queue call now
        threads through (see _filter_present_history). Exercised directly against
        "dialogue_resolved" payloads -- no DMCore involved -- the same "prompt shape, not the
        LLM's actual reply" scope TestClarificationResponse already keeps to.
    """

    def test_found_dialogue_queues_a_first_person_prompt_via_the_dialogue_path(self):
        self.event_bus.publish("dialogue_resolved", {
            "target": "innkeeper", "input": "have you heard anything from the road",
            "found": True, "persona": "innkeeper - A weary tavern keeper.",
            "attitude": "Attitude toward gladstone: is warm and well-disposed toward them.",
            "present_entities": ["gladstone", "innkeeper"],
        })

        entry = self.llm_core.context_window[-1]
        self.assertIn("have you heard anything from the road", entry["content"])
        self.assertEqual(entry["present"], ["gladstone", "innkeeper"])

    def _speech_prompt(self, **payload):
        self.event_bus.publish("dialogue_resolved", {
            "target": "market_person_3", "target_label": "the Fishmonger", "found": True,
            "persona": "A fishmonger.", "attitude": "neutral",
            "present_entities": ["gladstone", "market_person_3"], **payload,
        })
        return self.llm_core.context_window[-1]["content"]

    def test_a_greeting_asks_the_npc_to_open_rather_than_quoting_the_command(self):
        prompt = self._speech_prompt(input="talk to the fishmonger", speech_form="greet", utterance=None)
        self.assertIn("You approach the Fishmonger", prompt)
        self.assertIn("speaks first", prompt)
        self.assertNotIn("talk to the fishmonger", prompt)

    def test_a_reported_request_reaches_the_model_as_what_was_asked(self):
        prompt = self._speech_prompt(
            input="ask about the kelp beds", speech_form="reported", utterance="You ask about the kelp beds.",
        )
        self.assertEqual(prompt, "Speaking to the Fishmonger: You ask about the kelp beds.")

    def test_verbatim_speech_is_quoted_as_said(self):
        prompt = self._speech_prompt(
            input="do you ever get tired?", speech_form="verbatim", utterance="Do you ever get tired?",
        )
        self.assertEqual(prompt, 'You say to the Fishmonger: "Do you ever get tired?"')

    def test_dialogue_prompts_never_call_the_pc_the_player(self):
        # "the player" in the prompt is what the model copied into replies ("doesn't look
        # directly at the player").
        for payload in (
            {"input": "hi", "speech_form": "greet", "utterance": None},
            {"input": "hi", "speech_form": "verbatim", "utterance": "hi"},
            {"input": "hi", "language_barrier": True, "target_language": "dwarvish", "nonsense_phrase": None},
        ):
            with self.subTest(payload=payload):
                self.assertNotIn("the player", self._speech_prompt(**payload))
        system = Narration_Prompts.dialogue_system_message("the Fishmonger", "A fishmonger.", "neutral", "")
        self.assertIn('never call them "the player"', system)
        self.assertNotIn("only the player", system)

    def test_dialogue_system_message_asks_for_speech_in_the_npcs_own_voice(self):
        system = Narration_Prompts.dialogue_system_message(
            "the Fishmonger", "A fishmonger. | Voice: gruff and clipped", "wary", "",
        )
        self.assertIn("Who the Fishmonger is: A fishmonger. | Voice: gruff and clipped", system)
        self.assertIn("How the Fishmonger feels about you: wary", system)
        self.assertIn("At most one short action beat", system)
        self.assertIn("Length follows mood", system)

    def test_not_found_dialogue_falls_back_to_ordinary_gm_narration(self):
        self.event_bus.publish("dialogue_resolved", {
            "target": None, "input": "hello?", "found": False, "reason": "no_one_here",
            "present_entities": ["gladstone"],
        })

        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("no one here to talk to", prompt)

    def test_not_found_dialogue_forbids_inventing_an_explanation_for_the_absence(self):
        # Regression: this exact branch is what turned "ask the merchant" (no such entity ever
        # authored) into a fully invented scene of the merchant slipping into a doorway with a
        # "shadowy figure" -- the real fact (not present) has to be stated plainly, nothing more.
        #
        # Still fully reachable, and more load-bearing than before rather than less: DMCore now
        # usually materializes an addressed-but-absent person instead of denying (see
        # TestDialoguePromotion), but every one of that gate's vetoes lands here -- the model
        # declining, a live hostile in the scene, the ad hoc budget spent, Ollama unreachable.
        # This is the floor beneath promotion, not something promotion replaced.
        self.event_bus.publish("dialogue_resolved", {
            "target": "merchant", "input": "what are you selling", "found": False,
            "reason": "not_present", "present_entities": ["gladstone"],
        })

        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("isn't here to respond", prompt)
        self.assertIn("don't invent", prompt)

    def test_language_barrier_dialogue_queues_gibberish_prompt_not_the_players_words(self):
        # DM_Dialogue.py's _detect_language_barrier still resolves "found": True (the target is
        # present and willing to react) but flags language_barrier instead of a normal reply --
        # the queued prompt must steer the model away from actually answering what was asked.
        self.event_bus.publish("dialogue_resolved", {
            "target": "innkeeper", "input": "where is the nearest blacksmith",
            "found": True, "language_barrier": True, "target_language": "dwarvish",
            "nonsense_phrase": "Grunthak dol bregnir uzdum",
            "persona": "innkeeper - A weary tavern keeper.",
            "attitude": "Attitude toward gladstone: is warm and well-disposed toward them.",
            "present_entities": ["gladstone", "innkeeper"],
        })

        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("does not understand this at all", prompt)
        self.assertIn("dwarvish", prompt)
        self.assertIn("Grunthak dol bregnir uzdum", prompt)
        # The player's own words are still relayed as context (so the model reacts to *something*
        # being said), but the prompt must not read as an ordinary answerable dialogue turn.
        self.assertIn("invented gibberish", prompt)

    def test_language_barrier_prompt_omits_example_when_no_race_claims_the_language(self):
        prompt = Narration_Prompts.build_language_barrier_prompt(
            "hello", "stranger", "goblin tongue", None,
        )
        self.assertIn("goblin tongue", prompt)
        self.assertNotIn("For phonetic flavor", prompt)

    def _fake_response(self, content):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": content}}]}
        ).encode("utf-8")
        return response

    def test_dialogue_sends_the_players_actual_question_when_the_label_differs_from_the_key(self):
        # Regression: _queue used to filter history by whichever string phrases the
        # prompt (data["target_label"], ex: "the Fishmonger") instead of the raw entity key
        # present_entities/_filter_present_history actually tag entries with -- a display
        # label never literally matches a raw key, so fetch_from_llm's own
        # self._filter_present_history(target) call (inside the closure, not reachable by
        # calling _filter_present_history directly from outside) came back empty on every
        # dialogue turn: only the system message reached the model, no player question at
        # all. Every other test in this class happens to omit "target_label", so speaker ==
        # target by coincidence and masks the bug -- this one deliberately gives a different
        # label, the way narration-driven population always does in practice, and actually
        # runs the queued closure (mock_thread.call_args.kwargs["target"]()) rather than just
        # re-checking context_window, which was never broken.
        with patch("threading.Thread") as mock_thread, \
             patch("urllib.request.urlopen", return_value=self._fake_response("Aye, I know a bit.")):
            self.event_bus.publish("dialogue_resolved", {
                "target": "market_person_3", "target_label": "the Fishmonger",
                "input": "tell me what you know", "found": True,
                "persona": "A fishmonger.", "attitude": "neutral",
                "present_entities": ["gladstone", "market_person_3"],
            })
            mock_thread.call_args.kwargs["target"]()

        debug_events = []
        self.event_bus.subscribe("llm_debug_updated", debug_events.append)
        with patch("threading.Thread") as mock_thread, \
             patch("urllib.request.urlopen", return_value=self._fake_response("The kelp beds? Out past the pier.")):
            self.event_bus.publish("dialogue_resolved", {
                "target": "market_person_3", "target_label": "the Fishmonger",
                "input": "ask about the kelp beds", "found": True,
                "persona": "A fishmonger.", "attitude": "neutral",
                "present_entities": ["gladstone", "market_person_3"],
            })
            mock_thread.call_args.kwargs["target"]()

        self.assertIn('You say to the Fishmonger: "ask about the kelp beds"', debug_events[0]["query"])
        # The prior turn's own exchange is still there too -- presence-filtered history, not
        # just the triggering turn alone.
        self.assertIn('You say to the Fishmonger: "tell me what you know"', debug_events[0]["query"])
        self.assertIn("Aye, I know a bit.", debug_events[0]["query"])

    def test_filter_present_history_excludes_entries_the_entity_never_witnessed(self):
        self.llm_core.context_window = [
            {"role": "user", "content": "entrance room narration", "present": ["gladstone", "dart trap"]},
            {"role": "assistant", "content": "...", "present": ["gladstone", "dart trap"]},
            {"role": "user", "content": "hall of webs narration", "present": ["gladstone", "giant spider"]},
        ]

        spider_history = self.llm_core._filter_present_history("giant spider")
        trap_history = self.llm_core._filter_present_history("dart trap")

        self.assertEqual([e["content"] for e in spider_history], ["hall of webs narration"])
        self.assertEqual(
            [e["content"] for e in trap_history], ["entrance room narration", "..."],
        )

    def test_untagged_entries_are_excluded_from_every_filtered_view(self):
        # A clarification/load-failed prompt (no DMCore scenario_entities to tag it with --
        # see _queue's own present_entities docstring) must never leak into a
        # specific NPC's own witnessed history just because it's untagged.
        self.llm_core.context_window = [{"role": "user", "content": "no one understood that"}]

        self.assertEqual(self.llm_core._filter_present_history("innkeeper"), [])

    def test_api_messages_strips_the_present_bookkeeping_tag(self):
        entries = [{"role": "user", "content": "hi", "present": ["gladstone"]}]
        self.assertEqual(self.llm_core._api_messages(entries), [{"role": "user", "content": "hi"}])


class TestMultiActionNarration(LLMTestCase):
    """!
    @brief describe_player_actions -- the West End Games multi-action penalty's own narration
        side (see DM_Core.py's own _on_action_detected docstring). A single-action turn
        describes exactly like before this mechanic existed; a multi-action turn also names
        the shared penalty so the model's narration reads as one character splitting their
        attention, not several independent attacks.
    """

    def test_single_action_has_no_penalty_line(self):
        result = {"actions": [RolledOutcome(entity="gladstone", skill="blades", roll=15, difficulty=10, success=True)]}
        description = Narration_Prompts.describe_player_actions(result)
        self.assertNotIn("splitting their attention", description)
        self.assertIn("Skill used: blades", description)

    def test_two_actions_name_the_shared_penalty_and_describe_both(self):
        result = {"actions": [
            RolledOutcome(entity="gladstone", skill="blades", roll=12, difficulty=10, success=True),
            RolledOutcome(entity="gladstone", skill="finesse", roll=9, difficulty=12, success=False),
        ]}
        description = Narration_Prompts.describe_player_actions(result)
        self.assertIn("2 actions this turn", description)
        self.assertIn("-1D", description)
        self.assertIn("Skill used: blades", description)
        self.assertIn("Skill used: finesse", description)

    def test_a_creature_turn_in_a_combat_round_is_labelled_as_its_own(self):
        # Found by playtest: a behavior-driven turn has no "input", so its bare "Skill used:
        # charisma" line followed the player's own and was narrated as the player's.
        prompts = []
        self.llm_core._queue = lambda request: prompts.append(request.prompt)
        self.llm_core.generate_round_response({
            "round": 2,
            "actions": [RolledOutcome(entity="gladstone", skill="brawling", roll=9, difficulty=0, success=True)],
            "turns": [{"actor": "Silas", "initiative": 3, "outcome": RolledOutcome(
                entity="Silas", skill="charisma", roll=0, difficulty=7, success=False,
            )}],
        })
        self.assertIn("Silas's own turn (not the player's): Skill used: charisma", prompts[0])

    def test_two_empty_replies_get_one_last_try_without_history(self):
        # Found by playtest: two instant empty replies to a 14 KB request left a turn with no
        # narration at all; an identical retry just repeats it.
        sent = []
        replies = ["", "", "The vendor glares."]
        self.llm_core._request_completion = lambda data: (sent.append(data["messages"]), replies.pop(0))[1]
        published = []
        self.event_bus.subscribe("llm_response_ready", published.append)
        messages = [{"role": "system", "content": "sys"}, {"role": "assistant", "content": "old"},
                    {"role": "user", "content": "now"}]
        self.llm_core._fetch_and_publish(messages, present_entities=None)
        self.assertEqual(sent[-1], [messages[0], messages[-1]])
        self.assertEqual(published, ["The vendor glares."])

    def test_the_user_is_rewritten_as_you_before_it_reaches_the_history(self):
        # Found by playtest: one "Finn stares at the user" was copied from the history into
        # nearly every reply after it.
        self.llm_core._request_completion = lambda data: "Finn stares at the user. The user's coin is short."
        published = []
        self.event_bus.subscribe("llm_response_ready", published.append)
        self.llm_core._fetch_and_publish([{"role": "user", "content": "now"}], present_entities=None)
        self.assertEqual(published, ["Finn stares at you. Your coin is short."])
        self.assertEqual(self.llm_core.context_window[-1]["content"], published[0])

    def test_three_actions_name_minus_2d(self):
        result = {"actions": [
            RolledOutcome(entity="gladstone", skill="blades", roll=9, difficulty=10, success=False),
            RolledOutcome(entity="gladstone", skill="finesse", roll=9, difficulty=12, success=False),
            RolledOutcome(entity="gladstone", skill="charisma", roll=9, difficulty=10, success=False),
        ]}
        description = Narration_Prompts.describe_player_actions(result)
        self.assertIn("3 actions this turn", description)
        self.assertIn("-2D", description)


class TestGenerateItemInteractionResponseDispatchesFreeStandingIntents(LLMTestCase):
    """!
    @brief Proves LLM_Core.py's generate_item_interaction_response actually wires into
        intents/registry.py's own HANDLERS for a free-standing intent, rather than falling
        through to the item-named ladder below it -- the plumbing TestFreeStandingIntentHandlers
        above deliberately bypasses by calling narrate() directly.
    """

    def test_rest_narration_reaches_the_context_window(self):
        self.llm_core.generate_item_interaction_response({
            "intent": "rest", "found": True, "blocks_spent": 1,
            "healed": {"gladstone": {"healed": 5, "remaining_hp": 30}},
            "time": {"is_day": True, "day": 0}, "input": "i rest",
        })
        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("gladstone recovers 5 HP", prompt)

    def test_move_grounds_ongoing_scenario_description_on_the_llmcore_itself(self):
        self.llm_core.generate_item_interaction_response({
            "intent": "move", "found": True, "direction": "forward",
            "room_name": "the antechamber", "room_description": "Dust hangs in the still air.",
            "characters": [], "input": "go forward",
        })
        self.assertEqual(self.llm_core.scenario_description, "Dust hangs in the still air.")

    def test_a_failed_move_leaves_the_narrators_scene_untouched(self):
        self.llm_core.scenario_description = "A large arena."
        self.llm_core.generate_item_interaction_response({
            "intent": "move", "found": False, "reason": "no_exit", "direction": "forward",
            "room_description": "Somewhere else.", "input": "go forward",
        })
        self.assertEqual(self.llm_core.scenario_description, "A large arena.")

    def test_travel_grounds_ongoing_scenario_description_on_the_llmcore_itself(self):
        self.llm_core.generate_item_interaction_response({
            "intent": "travel", "found": True, "location_name": "border stones",
            "location_description": "A ring of weathered stones.", "characters": ["warden"],
            "time": {"is_day": True, "day": 0}, "input": "travel to the stones",
        })
        self.assertEqual(self.llm_core.scenario_description, "A ring of weathered stones.")
        self.assertEqual(self.llm_core.scenario_characters, ["warden"])


class TestDeniedItemInteractionNarration(LLMTestCase):
    """!
    @brief The "found": false branch of generate_item_interaction_response -- states its own
        real reason (DMCore's own "locked"/"not_present"/"cant_afford"/...) but must not
        embellish past it (see docs/narration-llm.md's "Denial-path grounding").
    """

    def test_prompt_states_the_real_reason_and_forbids_inventing_more(self):
        self.llm_core.generate_item_interaction_response({
            "intent": "take", "item_name": "gold", "found": False, "reason": "locked",
            "container": "chest", "input": "take the gold",
        })
        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("chest is locked shut", prompt)
        self.assertIn("don't invent", prompt)


class TestFailedAttempts(LLMTestCase):
    """!
    @brief An attempt the engine couldn't resolve is told to the player out of character (a
        "player_notice") rather than narrated -- nothing enters the context window, so the
        narrator can't fill the gap with things that didn't happen.
    """

    def setUp(self):
        super().setUp()
        self.notices = []
        self.event_bus.subscribe("player_notice", self.notices.append)

    def test_an_unresolved_action_is_a_notice_not_narration(self):
        self.event_bus.publish("action_not_understood", {"input": "give me that net", "score": 0.3, "reason": "unresolved_action"})
        self.assertEqual(self.llm_core.context_window, [])
        self.assertIn("Not sure what that does", self.notices[0]["message"])

    def test_musing_is_still_acknowledged_in_character(self):
        self.event_bus.publish("action_not_understood", {"input": "what a day", "score": 0.0, "reason": "musing"})
        self.assertEqual(self.notices, [])
        self.assertIn("what a day", self.llm_core.context_window[-1]["content"])

    def test_something_not_here_is_a_notice_naming_it(self):
        self.llm_core.generate_item_interaction_response({
            "intent": "take", "item_name": "sword", "found": False, "reason": "not_present", "input": "take the sword",
        })
        self.assertEqual(self.llm_core.context_window, [])
        self.assertEqual(self.notices[0]["message"], "There's no \"sword\" here to take.")

    def test_something_not_here_is_quoted_in_the_players_words(self):
        # Found by playtest: "belt knife" matched the catalog's "belt pouch", and the notice named the pouch.
        self.llm_core.generate_item_interaction_response({
            "intent": "take", "item_name": "belt pouch", "phrase": "belt knife", "found": False,
            "reason": "not_present", "input": "take the belt knife",
        })
        self.assertEqual(self.notices[0]["message"], "There's no \"belt knife\" here to take.")

    def test_improvisation_reasons_name_the_phrase(self):
        self.event_bus.publish("action_not_understood", {
            "input": "buy a lantern", "score": 0.0, "reason": "no_seller", "phrase": "a lantern",
        })
        self.assertEqual(self.notices[0]["message"], "There's no one here to buy \"a lantern\" from.")

    def test_cant_afford_names_the_price_in_coins(self):
        self.llm_core.generate_item_interaction_response({
            "intent": "trade", "item_name": "lantern", "found": False, "reason": "cant_afford",
            "price": 0.7, "price_text": "7 silver pieces", "input": "buy the lantern",
        })
        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("can't afford the 7 silver pieces it costs", prompt)
        self.assertNotIn("currency", prompt)


class TestCurrencyNarration(LLMTestCase):
    """!
    @brief Money in narration prompts reads as the setting's coins (DMCore's price_text/
        amount_text), never the internal "currency" field name.
    """

    def test_a_trade_names_the_price_in_coins(self):
        self.llm_core.generate_item_interaction_response({
            "intent": "trade", "item_name": "lantern", "found": True, "container": "shopkeeper",
            "price": 0.8, "price_text": "8 silver pieces", "input": "buy the lantern",
        })
        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("pays 8 silver pieces to shopkeeper", prompt)
        self.assertNotIn("currency", prompt)

    def test_giving_and_taking_coins_name_the_amount_in_coins(self):
        for intent, expected in (("give", "gives 3 gold pieces to"), ("take", "takes 3 gold pieces and")):
            with self.subTest(intent=intent):
                self.llm_core.generate_item_interaction_response({
                    "intent": intent, "item_name": "currency", "found": True, "container": "innkeeper",
                    "amount": 3, "amount_text": "3 gold pieces", "input": f"{intent} coins",
                })
                self.assertIn(expected, self.llm_core.context_window[-1]["content"])

    def test_without_coin_text_it_falls_back_to_plain_coins(self):
        self.llm_core.generate_item_interaction_response({
            "intent": "trade", "item_name": "lantern", "found": True, "container": "shopkeeper",
            "price": 5, "input": "buy the lantern",
        })
        self.assertIn("pays 5 coins to shopkeeper", self.llm_core.context_window[-1]["content"])


class TestLLMBackend(unittest.TestCase):
    """!
    @brief LLM_Backend.py -- choosing local Ollama vs OpenRouter (load_backend) and how a request
        to each is shaped, through the two request paths that read it (LLM_Client's
        call_chat_completion, LLMCore._request_completion) with urlopen patched.
    """

    def setUp(self):
        self._saved_backend = LLM_Backend.get_backend()
        self.addCleanup(LLM_Backend.set_backend, self._saved_backend)
        self.sent = []

    def _config(self, text):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, "llm_config.toml")
        with open(path, "w", encoding="utf-8") as config_file:
            config_file.write(text)
        return path

    def _capture(self, request, timeout=None):
        self.sent.append((request, json.loads(request.data)))
        response = MagicMock()
        response.read.return_value = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")
        response.__enter__.return_value = response
        return response

    def test_local_is_the_default_and_a_flag_beats_the_file(self):
        missing = os.path.join(tempfile.gettempdir(), "no_such_llm_config.toml")
        backend = LLM_Backend.load_backend(config_path=missing, environ={})
        self.assertEqual((backend.name, backend.model, backend.launches_ollama), ("local", "gemma4", True))

        path = self._config('backend = "openrouter"\n[openrouter]\napi_key = "from-file"\n')
        backend = LLM_Backend.load_backend(config_path=path, environ={})
        self.assertEqual((backend.name, backend.api_key, backend.launches_ollama), ("openrouter", "from-file", False))
        self.assertEqual((backend.model, *backend.fallback_models), LLM_Backend.OPENROUTER_FREE_MODELS)
        # The environment variable wins over the file's key.
        backend = LLM_Backend.load_backend(config_path=path, environ={"OPENROUTER_API_KEY": "from-env"})
        self.assertEqual(backend.api_key, "from-env")
        self.assertEqual(LLM_Backend.load_backend("local", config_path=path, environ={}).name, "local")

    def test_openrouter_keeps_at_most_three_models_and_an_unknown_name_is_refused(self):
        path = self._config('backend = "openrouter"\n[openrouter]\nmodels = ["a", "b", "c", "d"]\n')
        backend = LLM_Backend.load_backend(config_path=path, environ={})
        self.assertEqual((backend.model, backend.fallback_models), ("a", ("b", "c")))
        with self.assertRaises(ValueError):
            LLM_Backend.load_backend("cloud", config_path=path, environ={})

    def test_a_request_carries_its_own_backends_key_fallbacks_and_reasoning_field(self):
        from llm.LLM_Client import DEFAULT_MODEL, call_chat_completion
        LLM_Backend.set_backend(LLM_Backend.openrouter_backend({"api_key": "k"}, environ={}))
        messages = [{"role": "user", "content": "hi"}]
        with patch("urllib.request.urlopen", side_effect=self._capture):
            call_chat_completion(None, messages, reasoning_effort="none")
            call_chat_completion(None, messages, model="just/this")
            call_chat_completion("http://elsewhere/v1/chat/completions", messages)
        (online, body), (_pinned, pinned_body), (elsewhere, elsewhere_body) = self.sent
        self.assertEqual(online.full_url, LLM_Backend.OPENROUTER_URL)
        self.assertEqual(online.get_header("Authorization"), "Bearer k")
        self.assertEqual(body["models"], list(LLM_Backend.OPENROUTER_FREE_MODELS))
        self.assertEqual(body["reasoning"], {"enabled": False})
        self.assertNotIn("reasoning_effort", body)
        # Asking for one model drops the fallbacks.
        self.assertEqual((pinned_body["model"], "models" in pinned_body), ("just/this", False))
        # The key never goes to any other URL.
        self.assertIsNone(elsewhere.get_header("Authorization"))
        self.assertEqual(elsewhere_body["model"], DEFAULT_MODEL)

        LLM_Backend.set_backend(LLM_Backend.local_backend())
        with patch("urllib.request.urlopen", side_effect=self._capture):
            call_chat_completion(None, messages, reasoning_effort="none")
        local, local_body = self.sent[-1]
        self.assertEqual((local.full_url, local_body["reasoning_effort"]), (LLM_Backend.OLLAMA_URL, "none"))
        self.assertNotIn("models", local_body)
        self.assertIsNone(local.get_header("Authorization"))

    def test_google_sends_its_key_and_minimal_thinking_on_every_request(self):
        from llm.LLM_Client import call_chat_completion
        path = self._config('backend = "google"\n')
        backend = LLM_Backend.load_backend(config_path=path, environ={"GEMINI_API_KEY": "g"})
        self.assertEqual((backend.name, backend.model, backend.fallback_models, backend.launches_ollama),
                         ("google", "gemma-4-26b-a4b-it", (), False))
        LLM_Backend.set_backend(backend)
        with patch("urllib.request.urlopen", side_effect=self._capture):
            call_chat_completion(None, [{"role": "user", "content": "hi"}], reasoning_effort="none")
            LLMCore._request_completion(SimpleNamespace(), {"messages": [{"role": "user", "content": "x"}]})
        for request, body in self.sent:
            self.assertEqual((request.full_url, request.get_header("Authorization")), (LLM_Backend.GOOGLE_URL, "Bearer g"))
            # Google rejects "none" for Gemma, and unset it thinks for seconds -- narration included.
            self.assertEqual(body["reasoning_effort"], "minimal")
            self.assertNotIn("models", body)

    def test_a_failed_google_request_says_why(self):
        import io
        import urllib.error
        backend = LLM_Backend.google_backend({}, environ={})

        def error(code, body="{}"):
            return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body.encode("utf-8")))
        self.assertIn("quota", backend.failure_message(error(429)))
        self.assertIn("overloaded", backend.failure_message(error(503)))
        self.assertIn("GEMINI_API_KEY", backend.failure_message(error(400, '{"error":{"message":"API key not valid."}}')))
        self.assertEqual(backend.failure_message(ConnectionError()), "Could not reach Google AI Studio.")
        self.assertIn("no API key", LLM_Backend.describe(backend))

    def test_narration_requests_go_to_the_current_backend(self):
        LLM_Backend.set_backend(LLM_Backend.openrouter_backend({"api_key": "k"}, environ={}))
        with patch("urllib.request.urlopen", side_effect=self._capture):
            reply = LLMCore._request_completion(SimpleNamespace(), {"messages": [{"role": "user", "content": "x"}]})
        request, body = self.sent[0]
        self.assertEqual((reply, request.get_header("Authorization")), ("ok", "Bearer k"))
        self.assertEqual(body["model"], LLM_Backend.OPENROUTER_FREE_MODELS[0])
        # Reasoning is off for narration too online -- a free model's thinking came back as the story.
        self.assertEqual(body["reasoning"], {"enabled": False})

    def test_a_failed_online_request_says_why(self):
        import io
        import urllib.error
        backend = LLM_Backend.openrouter_backend({}, environ={})

        def error(code, body):
            return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body.encode("utf-8")))
        self.assertIn("daily", backend.failure_message(error(429, '{"error":{"message":"Rate limit exceeded: free-models-per-day"}}')))
        self.assertIn("busy", backend.failure_message(error(429, '{"error":{"message":"rate-limited upstream"}}')))
        self.assertIn("API key", backend.failure_message(error(401, "{}")))
        self.assertEqual(backend.failure_message(ConnectionError()), "Could not reach OpenRouter.")
        self.assertEqual(LLM_Backend.local_backend().failure_message(ConnectionError()), "Could not connect to the local LLM.")

    def test_sourcebook_excerpts_and_the_adjudication_wait_follow_the_backend(self):
        index = SimpleNamespace(query=lambda query: [({"source": "Book", "page": 3, "text": "Lore."}, 0.9)])
        core = SimpleNamespace(rag_index=index, event_bus=ValidatingEventBus())
        self.assertIn("Lore.", LLMCore.perform_rag(core, "q"))
        LLM_Backend.set_backend(LLM_Backend.openrouter_backend({"sourcebook_grounding": False}, environ={}))
        self.assertEqual(LLMCore.perform_rag(core, "q"), "")

        from resolution.AdHoc_Generation import adjudicate_player_input
        waits = []
        script_llm(self, lambda *a, timeout=None, **k: waits.append(timeout))
        adjudicate_player_input("x")
        self.assertEqual(waits, [LLM_Backend.OPENROUTER_ADJUDICATION_TIMEOUT])

    def test_ad_hoc_generation_waits_longer_locally(self):
        # Found by playtest: local item generation took 4-8s against an 8s budget.
        from resolution.AdHoc_Generation import generate_ad_hoc_item
        self.assertEqual(LLM_Backend.local_backend().generation_timeout, LLM_Backend.LOCAL_GENERATION_TIMEOUT)
        self.assertEqual(LLM_Backend.local_backend({"generation_timeout": 20}).generation_timeout, 20)
        for backend, expected in (
            (LLM_Backend.local_backend(), LLM_Backend.LOCAL_GENERATION_TIMEOUT),
            (LLM_Backend.google_backend({}, environ={}), LLM_Backend.ONLINE_GENERATION_TIMEOUT),
            (LLM_Backend.openrouter_backend({}, environ={}), LLM_Backend.ONLINE_GENERATION_TIMEOUT),
        ):
            LLM_Backend.set_backend(backend)
            waits = []
            script_llm(self, lambda *a, timeout=None, **k: waits.append(timeout))
            generate_ad_hoc_item("a rope", "take", "A dock.")
            self.assertEqual(waits, [expected], backend.name)


class TestOllamaLauncher(unittest.TestCase):
    """!
    @brief Ollama_Launcher.py's ensure_ollama_running -- exercised entirely through its own
        is_reachable/which/popen/download/fetch_text injection seams (the same dependency-
        scripted-transport pattern (tests/support.py's script_llm) used elsewhere), so this needs no
        real network probe, PATH lookup, subprocess spawn, or multi-gigabyte download. Every
        test that could otherwise reach _install_vendored_ollama passes its own isolated
        vendor_dir (a TemporaryDirectory), never the real vendor/ this module ships with.
    """

    def _write_fake_zip(self, zip_path):
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("ollama.exe", b"fake-binary-contents")

    def test_stop_ollama_takes_the_model_runner_child_with_it(self):
        # The whole point of stop_ollama over a bare terminate(): on Windows, ollama.exe's
        # llama-server.exe child survives its parent and keeps the model resident in VRAM, one
        # stranded runner per run, until the card is full and everything silently falls back to
        # CPU. taskkill /T is what reaps the tree.
        process = MagicMock()
        process.poll.return_value = None
        process.pid = 4321
        calls = []

        with patch.object(Ollama_Launcher.os, "name", "nt"):
            stopped = Ollama_Launcher.stop_ollama(process, run=lambda *a, **k: calls.append(a[0]))

        self.assertTrue(stopped)
        self.assertEqual(calls, [["taskkill", "/T", "/F", "/PID", "4321"]])
        process.wait.assert_called_once()
        process.terminate.assert_not_called()

    def test_stop_ollama_falls_back_to_terminate_when_taskkill_fails(self):
        process = MagicMock()
        process.poll.return_value = None
        process.pid = 99

        def exploding_run(*args, **kwargs):
            raise OSError("taskkill missing")

        with patch.object(Ollama_Launcher.os, "name", "nt"):
            Ollama_Launcher.stop_ollama(process, run=exploding_run)

        process.terminate.assert_called_once()

    def test_stop_ollama_leaves_a_server_it_never_started_alone(self):
        # ensure_ollama_running returns None when a server was already reachable -- that one
        # belongs to whoever started it, and the "never touch a pre-existing instance" rule is
        # the same one LLDM.py's own atexit hook has always kept.
        run = MagicMock()

        self.assertFalse(Ollama_Launcher.stop_ollama(None, run=run))
        self.assertEqual(run.call_count, 0)

    def test_stop_ollama_is_a_noop_for_an_already_exited_process(self):
        process = MagicMock()
        process.poll.return_value = 0
        run = MagicMock()

        self.assertFalse(Ollama_Launcher.stop_ollama(process, run=run))
        self.assertEqual(run.call_count, 0)
        process.terminate.assert_not_called()

    def test_already_running_is_a_noop(self):
        fake_which = MagicMock()
        fake_popen = MagicMock()
        fake_pull = MagicMock()
        result = ensure_ollama_running(
            is_reachable=lambda host: True, which=fake_which, popen=fake_popen,
            list_models=lambda host: ["gemma4:latest"], pull_model=fake_pull,
        )
        self.assertIsNone(result)
        fake_which.assert_not_called()
        fake_popen.assert_not_called()
        fake_pull.assert_not_called()  # already pulled -- nothing to do

    def test_spawns_ollama_serve_when_system_executable_found(self):
        fake_process = MagicMock(pid=1234)
        fake_popen = MagicMock(return_value=fake_process)
        result = ensure_ollama_running(
            is_reachable=lambda host: False, which=lambda name: "C:\\real\\ollama.exe", popen=fake_popen,
            ready_timeout=0,  # never becomes reachable in this test -- skip the model-pull wait
        )
        self.assertIs(result, fake_process)
        args, kwargs = fake_popen.call_args
        self.assertEqual(args[0], ["C:\\real\\ollama.exe", "serve"])
        # A second slot so input adjudication never queues behind narration, and the model kept loaded.
        self.assertEqual((kwargs["env"]["OLLAMA_NUM_PARALLEL"], kwargs["env"]["OLLAMA_KEEP_ALIVE"]), ("2", "-1"))

    def test_a_server_setting_the_user_already_chose_wins(self):
        environment = Ollama_Launcher._server_environment({"OLLAMA_NUM_PARALLEL": "4", "PATH": "x"})
        self.assertEqual(environment, {"OLLAMA_NUM_PARALLEL": "4", "OLLAMA_KEEP_ALIVE": "-1", "PATH": "x"})

    def test_failed_launch_returns_none(self):
        def exploding_popen(*args, **kwargs):
            raise OSError("no permission")

        result = ensure_ollama_running(
            is_reachable=lambda host: False, which=lambda name: "C:\\real\\ollama.exe", popen=exploding_popen,
        )
        self.assertIsNone(result)

    def test_missing_everywhere_and_failed_install_returns_none(self):
        with tempfile.TemporaryDirectory() as vendor_dir:
            def failing_download(url, dest_path, log):
                raise ConnectionError("offline")

            fake_popen = MagicMock()
            result = ensure_ollama_running(
                is_reachable=lambda host: False, which=lambda name: None, popen=fake_popen,
                download=failing_download, fetch_text=lambda url: "", vendor_dir=vendor_dir,
            )
            self.assertIsNone(result)
            fake_popen.assert_not_called()

    def test_installs_a_vendored_copy_when_nothing_found(self):
        with tempfile.TemporaryDirectory() as vendor_dir:
            def fake_download(url, dest_path, log):
                self._write_fake_zip(dest_path)

            fake_process = MagicMock(pid=99)
            fake_popen = MagicMock(return_value=fake_process)

            result = ensure_ollama_running(
                is_reachable=lambda host: False, which=lambda name: None, popen=fake_popen,
                download=fake_download, fetch_text=lambda url: "", vendor_dir=vendor_dir,
                ready_timeout=0,  # never becomes reachable in this test -- skip the model-pull wait
            )

            self.assertIs(result, fake_process)
            args, kwargs = fake_popen.call_args
            self.assertTrue(args[0][0].endswith("ollama.exe"))
            self.assertTrue(os.path.exists(args[0][0]))
            # The downloaded zip archive itself is cleaned up after extraction, not left behind.
            self.assertEqual(os.listdir(vendor_dir), ["ollama.exe"])

    def test_reuses_a_previously_vendored_copy_without_downloading(self):
        with tempfile.TemporaryDirectory() as vendor_dir:
            existing = os.path.join(vendor_dir, "ollama.exe")
            with open(existing, "wb") as f:
                f.write(b"already installed")

            fake_download = MagicMock()
            fake_process = MagicMock(pid=7)
            fake_popen = MagicMock(return_value=fake_process)

            result = ensure_ollama_running(
                is_reachable=lambda host: False, which=lambda name: None, popen=fake_popen,
                download=fake_download, vendor_dir=vendor_dir,
                ready_timeout=0,  # never becomes reachable in this test -- skip the model-pull wait
            )

            self.assertIs(result, fake_process)
            fake_download.assert_not_called()
            args, kwargs = fake_popen.call_args
            self.assertEqual(args[0][0], existing)

    def test_checksum_mismatch_discards_the_download(self):
        with tempfile.TemporaryDirectory() as vendor_dir:
            def fake_download(url, dest_path, log):
                self._write_fake_zip(dest_path)

            fake_popen = MagicMock()
            result = ensure_ollama_running(
                is_reachable=lambda host: False, which=lambda name: None, popen=fake_popen,
                download=fake_download,
                fetch_text=lambda url: "0" * 64 + "  ./ollama-windows-amd64.zip\n",
                vendor_dir=vendor_dir,
            )

            self.assertIsNone(result)
            fake_popen.assert_not_called()
            self.assertEqual(os.listdir(vendor_dir), [])

    def test_pulls_a_missing_model_once_the_server_is_reachable(self):
        fake_pull = MagicMock()
        result = ensure_ollama_running(
            is_reachable=lambda host: True, list_models=lambda host: [], pull_model=fake_pull,
        )
        self.assertIsNone(result)  # already running -- no process for this call to own
        fake_pull.assert_called_once()
        args, kwargs = fake_pull.call_args
        self.assertEqual(args[1], "gemma4")  # (host, model, log)

    def test_skips_pulling_a_model_already_present_under_its_implicit_latest_tag(self):
        # /api/tags always reports a tag ("gemma4:latest"), even though the bare "gemma4" (the
        # default model requested here) never explicitly names one -- _model_already_pulled has
        # to bridge that, not just do an exact string match.
        fake_pull = MagicMock()
        ensure_ollama_running(
            is_reachable=lambda host: True, list_models=lambda host: ["gemma4:latest"], pull_model=fake_pull,
        )
        fake_pull.assert_not_called()

    def test_gives_up_on_model_check_if_server_never_becomes_reachable(self):
        fake_list_models = MagicMock()
        fake_pull = MagicMock()
        logged = []
        result = ensure_ollama_running(
            is_reachable=lambda host: False, which=lambda name: "C:\\real\\ollama.exe",
            popen=MagicMock(return_value=MagicMock(pid=1)), list_models=fake_list_models,
            pull_model=fake_pull, ready_timeout=0, log=logged.append,
        )
        self.assertIsNotNone(result)  # the server process itself still spawned successfully
        fake_list_models.assert_not_called()
        fake_pull.assert_not_called()
        self.assertTrue(any("never became reachable" in message for message in logged))

    def test_default_pull_model_tolerates_a_total_with_no_completed_yet(self):
        # Regression: Ollama's own "pulling <digest>" status line can carry "total" before
        # "completed" has appeared at all -- _default_pull_model used to compute
        # `completed * 100 // total` unconditionally once total was truthy, crashing with
        # "unsupported operand type(s) for *: 'NoneType' and 'int'" on a real pull.
        class _FakeStreamResponse:
            def __init__(self, lines):
                self._lines = lines

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                return iter(self._lines)

        lines = [
            json.dumps({"status": "pulling manifest"}).encode("utf-8"),
            json.dumps({"status": "pulling abc123", "total": 100}).encode("utf-8"),
            json.dumps({"status": "pulling abc123", "total": 100, "completed": 50}).encode("utf-8"),
            json.dumps({"status": "success"}).encode("utf-8"),
        ]
        logged = []
        with patch("urllib.request.urlopen", return_value=_FakeStreamResponse(lines)):
            Ollama_Launcher._default_pull_model("http://127.0.0.1:11434", "gemma4", logged.append)

        self.assertIn("success", logged)


class TestLLMSaveLoad(LLMTestCase):
    def setUp(self):
        super().setUp()
        self.slot_dirs = []

    def tearDown(self):
        for slot_dir in self.slot_dirs:
            shutil.rmtree(slot_dir, ignore_errors=True)

    def _track(self, slot_name):
        self.slot_dirs.append(self.llm_core._save_slot_dir(slot_name))
        return slot_name

    def test_save_writes_context_window_and_scenario_bookkeeping(self):
        slot = self._track("test_llm_save")
        self.llm_core.context_window = [{"role": "user", "content": "I attack the wolf"}]
        self.llm_core.scenario_name = "The Arena"
        self.llm_core.scenario_description = "A large arena."
        self.llm_core.scenario_characters = ["gladstone - A man"]

        self.llm_core.save_game(slot)

        with open(os.path.join(self.llm_core._save_slot_dir(slot), "llm_state.json")) as f:
            data = json.load(f)
        self.assertEqual(data["context_window"], self.llm_core.context_window)
        self.assertEqual(data["scenario_name"], "The Arena")


class TestLlmDebugEvent(LLMTestCase):
    """!
    @brief fetch_from_llm's own network path (LLM_Core.py's _queue) never runs for
        real in this offline suite -- threading.Thread is patched so its target is captured
        and invoked directly/synchronously instead of on a real background thread, with
        urllib.request.urlopen mocked in place of a real Ollama connection."""

    def _run_fetch(self, prompt, urlopen_result=None, urlopen_side_effect=None):
        with patch("threading.Thread") as mock_thread, \
             patch("urllib.request.urlopen", return_value=urlopen_result, side_effect=urlopen_side_effect):
            self.llm_core._queue(Narration(prompt))
            mock_thread.call_args.kwargs["target"]()

    def test_successful_request_publishes_the_full_query_and_raw_response(self):
        debug_events = []
        self.event_bus.subscribe("llm_debug_updated", debug_events.append)
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": "The wolf snarls."}}]}
        ).encode("utf-8")

        self._run_fetch("The wolf attacks.", urlopen_result=fake_response)

        self.assertEqual(len(debug_events), 1)
        self.assertIn("[system]", debug_events[0]["query"])
        self.assertIn("The wolf attacks.", debug_events[0]["query"])
        self.assertEqual(debug_events[0]["response"], "The wolf snarls.")


class TestContextBudgetAndEmptyResponses(LLMTestCase):
    """!
    @brief The two halves of the starved-context bug (see LLM_Core.py's CONTEXT_TOKEN_BUDGET
        note): context_window's own 100-*message* cap has no idea how big a message is, so a
        long session grew prompts until the model had almost no room left to reply in -- every
        narration came back truncated, and about half the time it returned nothing at all,
        which was then published as the turn's narration and stored as an assistant turn.
        Observed live against Ollama at prompt_tokens=4001/completion_tokens=95, and caught by
        two integration tests that died on a blank response several turns in.
    """

    def _fake_response(self, content):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": content}}]}
        ).encode("utf-8")
        return response

    def test_fit_history_drops_oldest_until_there_is_room_to_reply(self):
        # Far more history than could ever fit, newest last.
        history = [{"role": "user", "content": f"{i} " + "x" * 2000} for i in range(50)]
        kept = self.llm_core._fit_history("system message", history)

        budget_chars = (CONTEXT_TOKEN_BUDGET - RESPONSE_TOKEN_RESERVE) * CHARS_PER_TOKEN
        self.assertLess(sum(len(e["content"]) for e in kept), budget_chars)
        self.assertLess(len(kept), len(history))
        # Newest kept, oldest dropped -- recent turns are what ground the current moment.
        self.assertEqual(kept[-1], history[-1])

    def test_fit_history_always_sends_at_least_the_triggering_turn(self):
        # One entry over budget on its own still goes: sending the prompt that prompted this
        # turn and letting the model truncate beats sending a system message with no action.
        history = [{"role": "user", "content": "x" * 999999}]
        self.assertEqual(len(self.llm_core._fit_history("system message", history)), 1)

    def test_an_empty_response_is_retried_once_and_never_published_raw(self):
        responses = []
        self.event_bus.subscribe("llm_response_ready", responses.append)
        with patch("threading.Thread") as mock_thread,              patch("urllib.request.urlopen", side_effect=[
                 self._fake_response(""), self._fake_response("The wolf snarls."),
             ]):
            self.llm_core._queue(Narration("The wolf attacks."))
            mock_thread.call_args.kwargs["target"]()

        self.assertEqual(responses, ["The wolf snarls."])

    def test_a_persistently_empty_response_never_pollutes_the_context_window(self):
        # An empty assistant turn isn't something the scene witnessed -- storing it would
        # spend budget on nothing and teach the model that empty replies belong here.
        responses = []
        self.event_bus.subscribe("llm_response_ready", responses.append)
        with patch("threading.Thread") as mock_thread,              patch("urllib.request.urlopen", side_effect=[
                 self._fake_response(""), self._fake_response("   "),
             ]):
            self.llm_core._queue(Narration("The wolf attacks."))
            mock_thread.call_args.kwargs["target"]()

        self.assertTrue(responses[-1].strip())
        self.assertNotIn(
            "assistant", [entry["role"] for entry in self.llm_core.context_window],
        )


class TestAdamNarration(LLMTestCase):
    """!
    @brief LLMCore's own side of DM_Help.py's channel: generate_adam_response/
        adam_system_message/_queue. The load-bearing property under test
        is the isolation guarantee -- unlike every other narration trigger, an ADaM exchange
        must never touch context_window at all (see LLM_Core.py's own module notes for why).
    """

    def _help_payload(self, **overrides):
        payload = {
            "input": "what are my skills",
            "present_entities": ["gladstone"],
            "skills": ["blades: 5D+0", "finesse: 3D+0"],
            "abilities": ["fireball: A ball of fire."],
            "equipped": {"rhand": "longsword"},
            "inventory": ["longsword", "health potion"],
            "scene_name": "The Arena",
            "scene_description": "A large arena.",
            "present": ["gladstone - A man"],
            "exits": [{"direction": "forward", "destination_name": "The Hall of Webs"}],
        }
        payload.update(overrides)
        return payload

    def test_publishing_help_resolved_never_touches_context_window(self):
        # No thread/network mocking needed -- _queue never appends to
        # context_window at all, synchronously, before the background thread even starts (the
        # same style TestFreeformDialogueNarration already uses to assert dialogue's own
        # pre-fetch context_window append, just proving the opposite here).
        self.assertEqual(self.llm_core.context_window, [])

        self.event_bus.publish("help_resolved", self._help_payload())

        self.assertEqual(self.llm_core.context_window, [])

    def test_system_message_includes_general_guidance_and_the_live_payload(self):
        message = Narration_Prompts.adam_system_message(self._help_payload(), None)

        self.assertIn("ADaM", message)
        self.assertIn("out-of-character", message)
        # General command guidance -- the actual onboarding gap this persona closes.
        self.assertIn("equip/wear", message)
        self.assertIn("save/load", message)
        # Found by playtest: "There are no facts provided regarding...", six times in one run.
        self.assertIn("Never mention \"the facts\"", message)
        # The live, dynamic payload.
        self.assertIn("blades: 5D+0", message)
        self.assertIn("fireball: A ball of fire.", message)
        self.assertIn("longsword", message)
        self.assertIn("The Arena", message)
        self.assertIn("The Hall of Webs", message)

    def test_system_message_mentions_a_creature_conjured_this_turn(self):
        payload = self._help_payload(created_creature={"created_creature": True, "name": "cave rat"})
        message = Narration_Prompts.adam_system_message(payload, None)
        self.assertIn("cave rat", message)
        self.assertIn("conjured", message)

    def test_system_message_mentions_an_edit_made_this_turn(self):
        payload = self._help_payload(edited={"edited": True, "name": "wolf", "reason": "player asked"})
        message = Narration_Prompts.adam_system_message(payload, None)
        self.assertIn("wolf", message)
        self.assertIn("edited", message)

    def test_fetch_still_publishes_llm_response_ready_without_storing_in_context(self):
        # The real fetch path (threading.Thread + urllib.request.urlopen mocked, same style as
        # TestLlmDebugEvent) confirms _fetch_and_publish's new store_in_context=False actually
        # skips the append on the success path too, not just before the thread starts.
        response_events = []
        self.event_bus.subscribe("llm_response_ready", response_events.append)
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": "You know blades and finesse."}}]}
        ).encode("utf-8")

        with patch("threading.Thread") as mock_thread, \
             patch("urllib.request.urlopen", return_value=fake_response):
            self.event_bus.publish("help_resolved", self._help_payload())
            mock_thread.call_args.kwargs["target"]()

        self.assertEqual(response_events, ["You know blades and finesse."])
        self.assertEqual(self.llm_core.context_window, [])


class TestSceneQueryNarration(LLMTestCase):
    """!
    @brief LLMCore's own side of DM_Help.py's scene-query channel: generate_scene_query_response/
        scene_query_system_message/_queue. The two load-bearing properties
        under test are the mirror image of TestAdamNarration's: this one speaks as the ordinary
        Game Master (never ADaM's own persona) and *does* join context_window (unlike ADaM's own
        deliberately-excluded exchanges).
    """

    def _scene_payload(self, **overrides):
        payload = {
            "input": "what do i see",
            "present_entities": ["gladstone"],
            "scene_name": "The Arena",
            "scene_description": "A large arena.",
            "present": ["gladstone - A man"],
            "ground_items": ["dagger: A slim, balanced dagger built for speed rather than force."],
            "exits": [{"direction": "forward", "destination_name": "The Hall of Webs"}],
        }
        payload.update(overrides)
        return payload

    def test_system_message_speaks_as_the_gm_not_adam_and_grounds_strictly(self):
        message = Narration_Prompts.scene_query_system_message(self._scene_payload(), None)

        self.assertIn("Game Master", message)
        self.assertNotIn("ADaM", message)
        self.assertNotIn("out-of-character", message)
        # The same strict anti-hallucination discipline ADaM's own system message uses.
        self.assertIn("never invent", message)
        # The live, dynamic payload.
        self.assertIn("The Arena", message)
        self.assertIn("gladstone - A man", message)
        self.assertIn("dagger: A slim", message)
        self.assertIn("The Hall of Webs", message)

    def test_publishing_scene_query_resolved_appends_to_context_window(self):
        # The opposite assertion from TestAdamNarration's own isolation test -- this channel is
        # meant to be built on by later turns, unlike ADaM's own excluded exchanges.
        self.assertEqual(self.llm_core.context_window, [])

        with patch("threading.Thread"):
            self.event_bus.publish("scene_query_resolved", self._scene_payload())

        self.assertEqual(len(self.llm_core.context_window), 1)
        self.assertEqual(self.llm_core.context_window[0]["present"], ["gladstone"])

    def test_fetch_publishes_llm_response_ready_and_stores_the_reply_in_context(self):
        response_events = []
        self.event_bus.subscribe("llm_response_ready", response_events.append)
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": "You see a dagger glinting on the floor."}}]}
        ).encode("utf-8")

        with patch("threading.Thread") as mock_thread, \
             patch("urllib.request.urlopen", return_value=fake_response):
            self.event_bus.publish("scene_query_resolved", self._scene_payload())
            mock_thread.call_args.kwargs["target"]()

        self.assertEqual(response_events, ["You see a dagger glinting on the floor."])
        self.assertEqual(len(self.llm_core.context_window), 2)
        self.assertEqual(self.llm_core.context_window[1]["role"], "assistant")


class FakeRagIndex:
    """!
    @brief Duck-typed stand-in for LLM_Rag.RagIndex's query() method, so LLMCore-level tests
        (perform_rag formatting, system_message wiring) don't need a real PDF/model --
        that mechanism is covered on its own by TestRagIndex below.
    """

    def __init__(self, matches):
        self.matches = matches

    def query(self, text, top_k=None, confidence_threshold=None):
        return self.matches


class TestLlmPerformRag(LLMTestCase):


    def test_queue_never_persists_rag_context_into_context_window(self):
        # Retrieved fresh into the per-request system message each time (see
        # system_message), not stored in context_window -- otherwise every future turn
        # would replay every past turn's lore excerpts too, ballooning the rolling window.
        self.llm_core.rag_index = FakeRagIndex([
            ({"source": "Inner Sea World Guide", "page": 23, "text": "Brevoy is a nation of two rival houses."}, 0.57),
        ])
        self.llm_core._queue(Narration("The player asks about Brevoy."))
        stored_prompt = self.llm_core.context_window[-1]["content"]
        self.assertNotIn("Brevoy is a nation of two rival houses", stored_prompt)
        self.assertEqual(stored_prompt, "The player asks about Brevoy.")


class TestRagIndex(unittest.TestCase):
    """!
    @brief Tests LLM_Rag.RagIndex's own mechanics directly -- chunking, caching, and
        nearest-neighbor query ranking -- independent of any real PDF (see
        test_chunking_and_caching_use_no_model_or_pdf) or a real SentenceTransformer model
        where one's actually needed (see setUpClass), never the real, gitignored Settings/
        sourcebook itself: that file may not exist on every machine this suite runs on, and
        even when it does, processing it fully takes minutes (see CLAUDE.md).
    """

    @classmethod
    def setUpClass(cls):
        # Paying SentenceTransformer's ~15-20s load once for the whole class, the same
        # setUpClass pattern TestNlpConfidenceThreshold/TestGameBoot already use.
        cls.index = RagIndex.__new__(RagIndex)
        cls.index.event_bus = ValidatingEventBus()
        cls.index.model = SentenceTransformer("all-MiniLM-L6-v2")

    def setUp(self):
        # Fresh per test -- these are cheap, in-memory attributes, not the shared model.
        self.index = self.__class__.index
        self.index.top_k = 3
        self.index.confidence_threshold = 0.3
        self.index.chunks = []
        self.index.chunk_embeddings = None
        self.index.ready = False

    def test_chunk_page_text_splits_long_text_into_word_bounded_chunks(self):
        sentence = "The dragon flies over the mountain peak. "
        long_text = sentence * 40  # ~320 words, well past MAX_CHUNK_WORDS (180)
        chunks = self.index._chunk_page_text(long_text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.split()), 180)


    def test_cache_key_changes_when_a_source_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "book.pdf")
            with open(path, "wb") as f:
                f.write(b"original content")
            key_before = self.index._cache_key([path])

            with open(path, "wb") as f:
                f.write(b"edited content, different size")
            key_after = self.index._cache_key([path])

            self.assertNotEqual(key_before, key_after)


    def test_query_ranks_the_closest_chunk_first_and_respects_the_threshold(self):
        chunks = [
            {"source": "book", "page": 1, "text": "Brevoy is a cold northern nation of two rival houses."},
            {"source": "book", "page": 2, "text": "The chef seasons the soup with fresh basil and garlic."},
            {"source": "book", "page": 3, "text": "House Orlovsky and House Surtova both claim Brevoy's throne."},
        ]
        embeddings = self.index.model.encode([c["text"] for c in chunks], convert_to_numpy=True)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        self.index.chunks = chunks
        self.index.chunk_embeddings = embeddings / norms
        self.index.ready = True

        results = self.index.query("Tell me about the rival houses of Brevoy", top_k=2)

        self.assertGreater(len(results), 0)
        self.assertLessEqual(len(results), 2)
        top_chunk, top_score = results[0]
        self.assertIn(top_chunk["page"], (1, 3))
        for _chunk, score in results:
            self.assertGreaterEqual(score, self.index.confidence_threshold)


class TestSceneRosterNarration(LLMTestCase):
    """!
    @brief The LLMCore half of scene_roster_updated -- the regression for narration prompts
        still naming a scene the player left.
    """

    def test_the_roster_event_repoints_the_narration_characters_line(self):
        self.event_bus.publish("scenario_loaded", {
            "name": "Market", "description": "A square.", "characters": ["a fruit seller"],
        })
        self.event_bus.publish("scene_roster_updated", {
            "characters": ["a gruff innkeeper"], "entities": [], "present_entities": [],
        })

        system_message = Narration_Prompts.system_message(self.llm_core.narrator, "")

        self.assertIn("a gruff innkeeper", system_message)
        self.assertNotIn("fruit seller", system_message)

    def test_an_attack_with_no_opponent_tells_the_narrator_not_to_invent_one(self):
        outcome = RolledOutcome(entity="gladstone", skill="brawling", roll=7, difficulty=0, success=True, no_opponent=True)
        self.assertIn("There is no opponent", Narration_Prompts.describe_outcome(outcome))
        outcome.no_opponent = False
        self.assertNotIn("no opponent", Narration_Prompts.describe_outcome(outcome))

    def test_an_incidental_target_is_never_named_to_the_narrator(self):
        outcome = RolledOutcome(
            entity="gladstone", skill="polearms", roll=6, difficulty=0, success=True,
            defender="Belor Hemlock", incidental_target=True,
        )
        outcome.effects.append(DefenderDetailsEffect(text="Belor Hemlock - Sandpoint's sheriff."))
        text = Narration_Prompts.describe_outcome(outcome)
        self.assertNotIn("Belor", text)
        self.assertIn("It isn't an attack on anyone", text)
        outcome.incidental_target = False
        self.assertIn("against Belor Hemlock (no defense)", Narration_Prompts.describe_outcome(outcome))

    def test_narration_is_told_to_keep_the_dice_hidden(self):
        # Found by playtest: "The successful roll means your strike connects cleanly".
        self.event_bus.publish("action_resolved", {
            "actions": [RolledOutcome(entity="gladstone", skill="blades", roll=9, difficulty=5, success=True)],
            "input": "swing at the post", "player_gear": ["longsword"],
        })
        self.assertIn("Never mention dice, rolls, difficulty or checks", self.llm_core.context_window[-1]["content"])

    def test_a_trivial_check_is_narrated_without_a_roll(self):
        outcome = RolledOutcome(entity="gladstone", skill="observation", roll=0, difficulty=0, success=True, trivial=True)
        text = Narration_Prompts.describe_outcome(outcome)
        self.assertIn("no roll needed", text)
        self.assertNotIn("rolled", text)

    def test_action_narration_is_told_to_narrate_the_attempt_and_stay_inside_the_players_gear(self):
        # Found by playtest: a polearms mismatch got the player a polearm they never owned.
        outcome = RolledOutcome(entity="gladstone", skill="polearms", roll=7, difficulty=0, success=True,
                                input="grab the finest jar of spices")
        text = Narration_Prompts.describe_player_actions({"actions": [outcome], "player_gear": ["longsword", "chain mail"]})
        self.assertIn("Narrate what the player actually tried", text)
        self.assertIn("The player's gear is exactly: longsword, chain mail", text)

    def test_narration_pins_the_player_to_the_engines_scene_except_on_a_real_move(self):
        # Found by playtest: the narrator walked the player into a tavern, then into underground
        # ruins, over clarification/skill replies while the engine never left the market.
        self.event_bus.publish("scenario_loaded", {"name": "Market", "description": "A square.", "characters": []})
        self.event_bus.publish("scene_roster_updated", {
            "characters": [], "entities": [], "present_entities": [], "scene_name": "The Fish Market",
        })
        self.event_bus.publish("location_exits_updated", {"destinations": [{"key": "inn", "name": "The Rusty Dragon"}]})

        pinned = Narration_Prompts.system_message(self.llm_core.narrator, "", label="clarification")
        self.assertIn("The player is at The Fish Market and stays there", pinned)
        self.assertIn("Ways out from here: The Rusty Dragon.", pinned)
        self.assertNotIn("stays there", Narration_Prompts.system_message(self.llm_core.narrator, "", label="item_interaction:travel"))


class TestNarrationOrder(LLMTestCase):
    """!@brief Narrations publish in the order they were queued, however fast each reply comes
        back (LLMCore._take_publish_ticket / _publish_in_order)."""

    def test_a_reply_that_comes_back_first_waits_for_the_one_queued_before_it(self):
        first, second = self.llm_core._take_publish_ticket(), self.llm_core._take_publish_ticket()
        published = []

        def publish(ticket, text):
            with self.llm_core._publish_in_order(ticket):
                published.append(text)

        later = threading.Thread(target=publish, args=(second, "the guard steps in"))
        later.start()
        later.join(timeout=0.3)
        self.assertEqual(published, [])  # still waiting on the first
        publish(first, "the attack lands")
        later.join(timeout=5)
        self.assertEqual(published, ["the attack lands", "the guard steps in"])

    def test_a_direct_call_with_no_ticket_never_waits(self):
        self.llm_core._take_publish_ticket()  # an earlier narration that never publishes
        with self.llm_core._publish_in_order(None):
            pass


class TestArrestNarration(LLMTestCase):
    """!@brief LLMCore voices arrests from the payload's facts only; fleeing is a notice."""

    def setUp(self):
        super().setUp()
        self.notices = []
        self.event_bus.subscribe("player_notice", self.notices.append)

    def test_the_demand_carries_the_record_facts(self):
        self.event_bus.publish("arrest_confronted", {
            "kind": "arrest", "enforcer": "Belor Hemlock", "polity": "Varisia", "addressed_as": None,
            "amount_text": "5 gold pieces", "charges": ["theft (Ven Vinder)"], "witnessed": True,
        })
        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("Belor Hemlock steps in to arrest you for theft (Ven Vinder). They saw it happen", prompt)
        self.assertIn("They demand 5 gold pieces", prompt)

    def test_resisting_turns_the_guard_on_you_without_deciding_the_fight(self):
        self.event_bus.publish("arrest_resolved", {"outcome": "resisted", "how": "refused", "enforcer": "Vachedi"})
        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("You refuse to submit. Vachedi turns on you", prompt)
        self.assertIn("Don't narrate them grabbing, hitting or restraining you", prompt)

    def test_the_options_come_right_after_the_demand(self):
        replies = []
        self.event_bus.subscribe("llm_response_ready", lambda text: replies.append(("narration", text)))
        self.event_bus.subscribe("player_notice", lambda data: replies.append(("notice", data["message"])))
        with patch.object(self.llm_core, "_request_completion", return_value="Belor bars the door."):
            self.llm_core.generate_arrest_response({
                "kind": "arrest", "enforcer": "Belor Hemlock", "amount_text": "5 gold pieces",
                "charges": ["theft"], "notice": "Belor Hemlock wants 5 gold pieces. Reply with one of: pay.",
            })
            for _ in range(50):
                if len(replies) == 2:
                    break
                time.sleep(0.05)
        self.assertEqual(replies, [
            ("narration", "Belor bars the door."),
            ("notice", "Belor Hemlock wants 5 gold pieces. Reply with one of: pay."),
        ])
        self.assertIn("don't narrate them touching, grabbing or restraining you", self.llm_core.context_window[0]["content"])

    def test_fleeing_is_told_out_of_character(self):
        self.event_bus.publish("arrest_resolved", {"outcome": "fled", "enforcer": "Belor Hemlock", "polity": "Varisia"})
        self.assertEqual(self.llm_core.context_window, [])
        self.assertIn("Resisting arrest is now on your record in Varisia.", self.notices[0]["message"])


class TestCrimeNarration(LLMTestCase):
    """!@brief The narrator is told exactly who saw a crime, once, on the next narration."""

    def test_the_next_narration_names_the_witnesses_and_only_them(self):
        self.event_bus.publish("crime_witnessed", {"crime": "theft", "offender": "gladstone", "witnesses": ["shopkeeper"]})
        self.llm_core.generate_item_interaction_response({
            "intent": "take", "item_name": "dagger", "found": True, "container": "shopkeeper", "input": "steal the dagger",
        })
        self.assertIn("Seen by: shopkeeper. Nobody else present noticed", self.llm_core.context_window[-1]["content"])

        self.llm_core.generate_item_interaction_response({
            "intent": "take", "item_name": "rope", "found": True, "input": "take the rope",
        })
        self.assertNotIn("Seen by", self.llm_core.context_window[-1]["content"])


class TestNarrationPromptsWithoutLLMCore(unittest.TestCase):
    """!
    @brief llm/Narration_Prompts.py driven with a bare NarratorState and event payloads -- what the
        narrator is told, with no LLMCore, thread, network or EventBus.
    """

    def setUp(self):
        self.state = NarratorState()

    def test_an_item_pickup_is_a_labelled_narration_tagged_with_who_was_present(self):
        result = Narration_Prompts.item_interaction(self.state, {
            "intent": "take", "item_name": "rope", "found": True, "input": "take the rope", "present_entities": ["a"],
        })

        self.assertIsInstance(result, Narration)
        self.assertEqual(result.kind, "narration")
        self.assertEqual(result.label, "item_interaction:take")
        self.assertEqual(result.present_entities, ["a"])
        self.assertEqual(result.rag_query, "take the rope")
        self.assertIn('The player takes "rope"', result.prompt)

    def test_a_denied_pickup_that_nothing_in_the_world_refused_is_a_notice_not_prose(self):
        result = Narration_Prompts.item_interaction(self.state, {
            "intent": "take", "item_name": "rope", "found": False, "reason": "not_present", "phrase": "belt knife",
        })

        self.assertIsInstance(result, Notice)
        self.assertIn('no "belt knife" here', result.message)
        self.assertEqual(result.log[0], "Generating item interaction response (take).")

    def test_a_denial_the_world_gave_is_narrated_with_only_its_real_reason(self):
        result = Narration_Prompts.item_interaction(self.state, {
            "intent": "take", "item_name": "rope", "found": False, "reason": "locked", "container": "chest",
        })
        self.assertIn("chest is locked shut", result.prompt)
        self.assertIn("don't invent", result.prompt)

    def test_a_quiet_clause_narrates_nothing(self):
        result = Narration_Prompts.item_interaction(self.state, {"intent": "advance", "quiet": True})
        self.assertIsInstance(result, Skip)
        self.assertIn("quiet", result.log)

    def test_dialogue_carries_everything_its_own_system_message_needs(self):
        result = Narration_Prompts.npc_dialogue(self.state, {
            "target": "innkeeper_2", "target_label": "the Innkeeper", "found": True, "utterance": "hello",
            "speech_form": "address", "persona": "Gruff.", "attitude": "wary", "input": "hello",
        })

        self.assertEqual((result.kind, result.target_key, result.speaker), ("dialogue", "innkeeper_2", "the Innkeeper"))
        self.assertEqual((result.persona, result.attitude), ("Gruff.", "wary"))
        self.assertEqual(result.label, "dialogue:innkeeper_2")

    def test_adam_and_scene_queries_say_which_system_message_frames_them(self):
        adam = Narration_Prompts.adam(self.state, {"input": "help"})
        query = Narration_Prompts.scene_query(self.state, {"input": "what do I see", "present_entities": ["x"]})

        self.assertEqual((adam.kind, adam.present_entities), ("adam", None))
        self.assertEqual((query.kind, query.present_entities), ("scene_query", ["x"]))

    def test_the_scenario_intro_records_the_scene_it_narrates(self):
        result = Narration_Prompts.scene_intro(self.state, {
            "name": "The Arena", "description": "Sand.", "characters": ["gladstone - a man"],
        })

        self.assertEqual((self.state.scenario_name, self.state.scene_name), ("The Arena", "The Arena"))
        self.assertIn("Characters present: gladstone - a man", result.prompt)
        self.assertEqual(result.label, "scenario_intro")

    def test_a_throwaway_scenario_intro_still_updates_the_state_but_narrates_nothing(self):
        result = Narration_Prompts.scene_intro(self.state, {"name": "X", "description": "d", "skip_intro": True})
        self.assertIsInstance(result, Skip)
        self.assertEqual(self.state.scenario_name, "X")

    def test_a_fled_arrest_is_told_out_of_character(self):
        result = Narration_Prompts.arrest(self.state, {"outcome": "fled", "enforcer": "the guard", "polity": "Crown"})
        self.assertIsInstance(result, Notice)
        self.assertIn("fled from the guard", result.message)

    def test_the_system_message_grounds_the_gm_in_the_scene_and_the_retrieved_lore(self):
        self.state.scenario_name = "The Arena"
        self.state.scenario_description = "Sand."
        self.state.scenario_characters = ["gladstone - a man"]
        self.state.scene_name = "The Arena"

        message = Narration_Prompts.system_message(self.state, "A lore excerpt.")

        self.assertIn('Setting: "The Arena" - Sand.', message)
        self.assertIn("Characters: gladstone - a man", message)
        self.assertIn("A lore excerpt.", message)
        self.assertIn("stays there", message)


if __name__ == "__main__":
    unittest.main()
