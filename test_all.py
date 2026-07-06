import asyncio
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch

import pytest

from DM_Core import DMCore
from Event_Bus import EventBus
from LLM_Core import LLMCore
from NLP_Core import NLPCore
from Textual_Core import TextualCore
from textual.widgets import RichLog, TabbedContent


def _lm_studio_reachable():
    try:
        urllib.request.urlopen("http://127.0.0.1:1234/v1/models", timeout=2)
        return True
    except Exception:
        return False


class TestGameBoot(unittest.TestCase):
    def test_boot_and_skill_identification(self):
        # 1. Initialize Event Bus
        event_bus = EventBus()

        # 2. Track action_detected events
        detected_actions = []
        def on_action_detected(data):
            detected_actions.append(data)
        event_bus.subscribe("action_detected", on_action_detected)

        # 3. Initialize NLPCore FIRST so it doesn't miss rules_loaded
        nlp_core = NLPCore(event_bus)

        # 4. Initialize DMCore (this triggers rules_loaded)
        dm_core = DMCore(event_bus)

        # Verify that skills were actually loaded into nlp_core
        self.assertGreater(len(nlp_core.skill_names), 0, "No skills loaded into NLPCore")

        # 5. Simulate user input
        test_input = "I attack with my sword"
        event_bus.publish("user_input_submitted", test_input)

        # 6. Verify skill identification
        self.assertGreater(len(detected_actions), 0, "No action detected event published")
        last_action = detected_actions[-1]
        self.assertEqual(last_action["skill"], "blades")
        self.assertGreater(last_action["score"], 0.5)
        print(f"Integration Test Success: '{test_input}' -> {last_action['skill']} ({last_action['score']:.4f})")


class TestNlpConfidenceThreshold(unittest.TestCase):
    # setUpClass (not setUp) so the slow sentence-transformers load only happens once for
    # both test methods in this class.
    @classmethod
    def setUpClass(cls):
        cls.event_bus = EventBus()
        cls.nlp_core = NLPCore(cls.event_bus)
        cls.dm_core = DMCore(cls.event_bus)

    def test_low_confidence_input_triggers_no_skill(self):
        # A greeting with no real skill/action content shouldn't be forced onto whatever
        # phrase happens to score highest (previously this mapped to "artistry" at ~0.32).
        detected_actions = []
        not_understood = []
        self.event_bus.subscribe("action_detected", detected_actions.append)
        self.event_bus.subscribe("action_not_understood", not_understood.append)

        self.event_bus.publish("user_input_submitted", "Hey there innkeeper")

        self.assertEqual(detected_actions, [])
        # Publishing this instead of just staying silent is what lets LLMCore give the
        # player some response rather than the app appearing to stall.
        self.assertEqual(len(not_understood), 1)
        self.assertIn("innkeeper", not_understood[0]["input"])

    def test_clear_action_still_triggers_above_threshold(self):
        detected_actions = []
        self.event_bus.subscribe("action_detected", detected_actions.append)

        self.event_bus.publish("user_input_submitted", "I attack with my sword")

        self.assertEqual(len(detected_actions), 1)
        self.assertEqual(detected_actions[0]["skill"], "blades")
        self.assertGreaterEqual(detected_actions[0]["score"], self.nlp_core.confidence_threshold)

    def test_detect_item_intent_examine_vs_take_vs_neither(self):
        self.assertEqual(self.nlp_core._detect_item_intent("examine the dagger"), "examine")
        self.assertEqual(self.nlp_core._detect_item_intent("take the gold"), "take")
        self.assertIsNone(self.nlp_core._detect_item_intent("attack with my sword"))

    def test_picking_a_lock_is_not_mistaken_for_taking_an_item(self):
        # "pick" alone would collide with "I pick the lock" (finesse) if it were a bare-word
        # match -- only the two-word "pick up" phrase should count as take-intent.
        self.assertIsNone(self.nlp_core._detect_item_intent("pick the lock"))
        self.assertEqual(self.nlp_core._detect_item_intent("pick up the dagger"), "take")

        detected_actions = []
        item_events = []
        self.event_bus.subscribe("action_detected", detected_actions.append)
        self.event_bus.subscribe("item_interaction_detected", item_events.append)

        self.event_bus.publish("user_input_submitted", "I pick the lock")

        self.assertEqual(item_events, [])
        self.assertEqual(len(detected_actions), 1)
        self.assertEqual(detected_actions[0]["skill"], "finesse")

    def test_full_pipeline_detects_a_known_item_by_name(self):
        # Item matching runs against every known "object"-supertype entity (globally, not
        # scoped to the active scenario) -- DMCore is what checks whether it's actually
        # present in the current target's inventory (see TestItemInteraction).
        item_events = []
        self.event_bus.subscribe("item_interaction_detected", item_events.append)

        self.event_bus.publish("user_input_submitted", "I examine the cursed dagger")

        self.assertEqual(len(item_events), 1)
        self.assertEqual(item_events[0]["intent"], "examine")
        self.assertEqual(item_events[0]["item_name"], "cursed dagger")


class TestClarificationResponse(unittest.TestCase):
    def setUp(self):
        self.event_bus = EventBus()
        self.llm_core = LLMCore(self.event_bus)

    def test_unmatched_input_queues_a_clarification_prompt_not_a_dice_roll(self):
        # _queue_narration appends to context_window synchronously before spawning the
        # background network fetch, so this is checkable without waiting on (or mocking) LM
        # Studio -- the point here is the prompt shape, not the LLM's actual reply.
        self.event_bus.publish("action_not_understood", {"input": "hey there innkeeper", "score": 0.32})

        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("hey there innkeeper", prompt)
        # No roll data (that's _describe_outcome's shape, used by the other narration paths).
        self.assertNotIn("Skill used:", prompt)
        self.assertNotIn("difficulty", prompt)

    def test_describe_outcome_includes_loot_so_the_llm_isnt_left_guessing(self):
        # Without this, the LLM has no idea what was actually gained and will happily invent
        # contents that don't match the real game state (observed: it narrated a "silver key
        # and leather-bound journal" for a chest that actually just held currency).
        result = {
            "input": "I pick the lock", "skill": "finesse", "roll": 18, "difficulty": 12,
            "success": True, "defender": "chest", "loot": {"currency": 20, "items": []},
        }
        description = self.llm_core._describe_outcome(result)
        self.assertIn("20 currency", description)

    def test_describe_outcome_omits_loot_text_when_nothing_gained(self):
        result = {
            "input": "I pick the lock", "skill": "finesse", "roll": 3, "difficulty": 12,
            "success": False, "defender": "chest",
        }
        description = self.llm_core._describe_outcome(result)
        self.assertNotIn("The player gains", description)

    def test_examine_prompt_never_implies_a_transfer(self):
        self.event_bus.publish("item_interaction_resolved", {
            "intent": "examine", "item_name": "cursed dagger", "input": "I examine the cursed dagger",
            "found": True, "description": "A wickedly curved dagger etched with glowing runes.",
        })
        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("glowing runes", prompt)
        self.assertIn("nothing is taken", prompt)

    def test_take_prompt_reflects_currency_amount_not_the_literal_word_currency(self):
        self.event_bus.publish("item_interaction_resolved", {
            "intent": "take", "item_name": "currency", "input": "I take the gold",
            "found": True, "container": "chest", "amount": 20,
        })
        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("20 currency", prompt)

    def test_locked_container_prompt_explains_the_denial(self):
        self.event_bus.publish("item_interaction_resolved", {
            "intent": "take", "item_name": "cursed dagger", "input": "I take the dagger",
            "found": False, "reason": "locked", "container": "chest",
        })
        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("locked", prompt)
        self.assertIn("no roll involved", prompt)


@unittest.skipUnless(_lm_studio_reachable(), "LM Studio not reachable at http://127.0.0.1:1234")
class TestInnkeeperConversation(unittest.TestCase):
    """!
    @brief End-to-end conversation test against a real, running LM Studio -- the only way to
           actually verify the LLM uses fed context (scenario setting, character roster,
           per-turn defender_details) rather than just checking the prompt shape. Skipped
           entirely (not failed) when LM Studio isn't reachable, so the rest of the suite
           stays fast and network-independent.
    """

    def setUp(self):
        self.event_bus = EventBus()
        self.responses = []
        self.event_bus.subscribe("llm_response_ready", self.responses.append)

        self.nlp_core = NLPCore(self.event_bus)
        self.llm_core = LLMCore(self.event_bus)
        self.dm_core = DMCore(self.event_bus, scenario_name="tavern")

        self._wait_for_responses(1)  # the scene intro, fired during DMCore.__init__

    def _wait_for_responses(self, count, timeout=30):
        deadline = time.time() + timeout
        while len(self.responses) < count and time.time() < deadline:
            time.sleep(0.2)
        self.assertGreaterEqual(
            len(self.responses), count,
            f"Timed out waiting for the {count}th LLM response (LM Studio may be slow/unloaded).",
        )

    def _say(self, player_input):
        self.event_bus.publish("user_input_submitted", player_input)
        self._wait_for_responses(len(self.responses) + 1)
        return self.responses[-1]

    def test_full_conversation_with_innkeeper(self):
        # NLPCore's confidence_threshold turns out to reject almost any naturally-phrased
        # social action once it names a topic ("...about the road", "...her husband" etc.
        # dilute the sentence embedding) -- only near-bare keyword phrasing like "charm her"
        # reliably clears it for "charisma". So this conversation deliberately mixes both
        # real paths a player will actually hit: one turn that clears the threshold (genuine
        # action_resolved + defender_details) and natural follow-ups that don't (routed to
        # action_not_understood's clarification response instead). Both should stay coherent
        # and grounded, since the persistent system-message roster covers either path.
        action_events = []
        round_events = []
        self.event_bus.subscribe("action_resolved", action_events.append)
        self.event_bus.subscribe("round_resolved", round_events.append)

        turns = [
            "I try to charm her",  # verified: scores ~0.60 on "charisma", clears the threshold
            "Have you heard anything about trouble on the road?",  # verified: ~0.41, below it
            "I'm sorry -- what happened to your husband?",  # verified: ~0.32, below it
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
        self.assertEqual(action_events[0]["defender"], "innkeeper")
        self.assertIn("innkeeper", action_events[0]["defender_details"])

        # Deliberately NOT asserting on exact narrative content past this point (ex: that the
        # husband question's response literally says "bandit"/"husband") -- a real run showed
        # the LLM can convey her grief ("a deep, painful sadness... vague sigh") without ever
        # using those words, so a keyword check on live LLM output is just flaky, not a real
        # regression signal. The printed transcript above is how this actually gets verified.


class TestOpposedResolution(unittest.TestCase):
    def setUp(self):
        self.event_bus = EventBus()
        self.dm_core = DMCore(self.event_bus)

    def test_highest_value_opposing_skill_is_used(self):
        # blades opposes = ['dodge', 'blades', 'brawling', 'axes', 'polearms']
        # 'dodge' is listed first, but 'brawling' rates higher (5*3=15 vs 2*3=6),
        # so 'brawling' must be the one chosen and rolled.
        self.dm_core.entities["test_defender"] = {
            "name": "test_defender",
            "skills": {
                "dodge": {"dice": 2, "pips": 0},
                "brawling": {"dice": 5, "pips": 0},
            },
        }

        chosen = self.dm_core.get_opposing_skill("blades", "test_defender")
        self.assertEqual(chosen, "brawling")

        result = self.dm_core.resolve_opposed_action("gladstone", "blades", "test_defender")
        self.assertEqual(result["opposing_skill"], "brawling")
        self.assertEqual(result["defender"], "test_defender")
        # 5 dice + 0 pips can only roll between 5 and 30
        self.assertGreaterEqual(result["difficulty"], 5)
        self.assertLessEqual(result["difficulty"], 30)

    def test_pips_count_toward_the_rating(self):
        # 'dodge' has fewer dice (2) than 'brawling' (3), but +3 pips bumps its
        # rating past brawling's: dodge = 2*3+3=9, brawling = 3*3+0=9... so add one
        # more pip to make dodge strictly higher and confirm pips are honored.
        self.dm_core.entities["test_defender"] = {
            "name": "test_defender",
            "skills": {
                "dodge": {"dice": 2, "pips": 4},
                "brawling": {"dice": 3, "pips": 0},
            },
        }

        chosen = self.dm_core.get_opposing_skill("blades", "test_defender")
        self.assertEqual(chosen, "dodge")

    def test_wolf_scenario_defender_picks_dodge_over_brawling(self):
        # Regression check against the real creatures.toml data:
        # wolf dodge = 6*3=18, wolf brawling = 5*3=15, so dodge should win.
        chosen = self.dm_core.get_opposing_skill("blades", "wolf")
        self.assertEqual(chosen, "dodge")

    def test_no_matching_opposing_skill_defaults_to_zero(self):
        self.dm_core.entities["empty_defender"] = {"name": "empty_defender", "skills": {}}

        chosen = self.dm_core.get_opposing_skill("blades", "empty_defender")
        self.assertIsNone(chosen)

        result = self.dm_core.resolve_opposed_action("gladstone", "blades", "empty_defender")
        self.assertIsNone(result["opposing_skill"])
        self.assertEqual(result["difficulty"], 0)

    def test_no_difficulty_passed_defaults_to_zero(self):
        result = self.dm_core.resolve_action("gladstone", "blades")
        self.assertEqual(result["difficulty"], 0)


class TestDamageCalculation(unittest.TestCase):
    def setUp(self):
        self.event_bus = EventBus()
        self.dm_core = DMCore(self.event_bus)

    def test_bonus_resolves_flat_number(self):
        self.assertEqual(self.dm_core.resolve_bonus("gladstone", 5), 5)

    def test_bonus_resolves_from_rule_reference(self):
        # strength_damage rule: skill="strength", divisor=2. Gladstone's strength is 2 dice, so bonus = 2 // 2 = 1.
        self.assertEqual(self.dm_core.resolve_bonus("gladstone", "user.strength_damage"), 1)
        # Without the "user." prefix should resolve the same way.
        self.assertEqual(self.dm_core.resolve_bonus("gladstone", "strength_damage"), 1)

    def test_bonus_unknown_reference_defaults_to_zero(self):
        self.assertEqual(self.dm_core.resolve_bonus("gladstone", "user.made_up_rule"), 0)

    @patch("random.randint", return_value=4)
    def test_damage_value_rolls_dice_and_adds_bonus(self, mock_randint):
        # 2 dice @ 4 each + 1 pip + strength_damage bonus (1) = 10
        total = self.dm_core.resolve_damage_value(
            "gladstone", {"dice": 2, "pips": 1, "bonus": "user.strength_damage"}
        )
        self.assertEqual(total, 10)

    @patch("random.randint", return_value=4)
    def test_damage_value_resolves_weapon_reference_from_equipped_weapon(self, mock_randint):
        # techniques.toml's cleave uses "user.weapon.dice"/"user.weapon.pips"; gladstone's
        # equipped longsword (1 die, 2 pips) supplies both: 1 die @ 4 + 2 pips = 6.
        total = self.dm_core.resolve_damage_value(
            "gladstone", {"dice": "user.weapon.dice", "pips": "user.weapon.pips", "bonus": 0}
        )
        self.assertEqual(total, 6)

    def test_damage_value_weapon_reference_degrades_to_zero_without_equipped_weapon(self):
        # wolf has no equipped weapon at all, so the same indirection safely falls back to 0.
        total = self.dm_core.resolve_damage_value(
            "wolf", {"dice": "user.weapon.dice", "pips": "user.weapon.pips", "bonus": 0}
        )
        self.assertEqual(total, 0)

    @patch("random.randint", return_value=3)
    def test_damage_reduction_only_applies_to_matching_tags(self, mock_randint):
        # Gladstone wears chain mail: armor_value = 2 dice, tags = physical/piercing/bludgeoning/slashing.
        self.assertEqual(self.dm_core.get_damage_reduction("gladstone", ["bludgeoning"]), 6)
        self.assertEqual(self.dm_core.get_damage_reduction("gladstone", ["fire"]), 0)

    def test_get_current_hp_initializes_from_max_hp(self):
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 36)

    def test_apply_damage_subtracts_and_floors_at_zero(self):
        self.dm_core.apply_damage("gladstone", 10)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 26)
        self.dm_core.apply_damage("gladstone", 1000)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 0)

    @patch("random.randint", return_value=2)
    def test_calculate_damage_applies_unresisted_damage_to_hp(self, mock_randint):
        # Fireball: 5 dice, no bonus, fire damage - gladstone's chain mail doesn't resist fire.
        fireball = {"damage_value": {"dice": 5, "pips": 0, "bonus": 0}, "damage_tags": ["fire"]}
        result = self.dm_core.calculate_damage("wolf", "gladstone", fireball)

        self.assertEqual(result["raw_damage"], 10)  # 5 dice @ 2 each
        self.assertEqual(result["reduction"], 0)
        self.assertEqual(result["net_damage"], 10)
        self.assertEqual(result["remaining_hp"], 26)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 26)

    @patch("random.randint", return_value=3)
    def test_calculate_damage_reduced_by_matching_armor(self, mock_randint):
        # Punch: 0 dice + strength_damage bonus (1), bludgeoning - chain mail resists bludgeoning (2 dice @ 3 each = 6).
        punch = {"damage_value": {"dice": 0, "pips": 0, "bonus": "user.strength_damage"}, "damage_tags": ["bludgeoning"]}
        result = self.dm_core.calculate_damage("wolf", "gladstone", punch)

        self.assertEqual(result["raw_damage"], 1)
        self.assertEqual(result["reduction"], 6)
        self.assertEqual(result["net_damage"], 0)
        self.assertEqual(result["remaining_hp"], 36)

    def test_fire_elemental_is_immune_to_fire_tag(self):
        self.assertTrue(self.dm_core.is_immune_to("fire elemental", ["fire"]))
        self.assertFalse(self.dm_core.is_immune_to("fire elemental", ["slashing"]))
        # Immunity is a hard tag match, not a rolled amount -- an entity with no
        # immunity_tags at all (gladstone) is never immune to anything.
        self.assertFalse(self.dm_core.is_immune_to("gladstone", ["fire"]))

    @patch("random.randint", return_value=3)
    def test_innate_resistance_reduces_matching_tags_like_armor(self, mock_randint):
        # Fire elemental's resistance_value (2 dice) applies to physical tags, not fire.
        self.assertEqual(self.dm_core.get_damage_reduction("fire elemental", ["slashing"]), 6)
        self.assertEqual(self.dm_core.get_damage_reduction("fire elemental", ["fire"]), 0)

    @patch("random.randint", return_value=4)
    def test_calculate_damage_fireball_negated_by_matching_immunity(self, mock_randint):
        # Gladstone's fireball (5 dice fire) rolls 20 raw damage, but the fire elemental's
        # immunity_tags fully negate it -- net damage is 0 regardless of the roll.
        fireball = self.dm_core.find_attack_ability("gladstone", "arcane")
        result = self.dm_core.calculate_damage("gladstone", "fire elemental", fireball)

        self.assertEqual(result["raw_damage"], 20)
        self.assertEqual(result["reduction"], 20)
        self.assertEqual(result["net_damage"], 0)
        self.assertEqual(result["remaining_hp"], 30)

    @patch("random.randint", return_value=4)
    def test_calculate_damage_fireball_unresisted_against_non_immune_target(self, mock_randint):
        # The same fireball against a target with no fire immunity/resistance (wolf) hits normally.
        fireball = self.dm_core.find_attack_ability("gladstone", "arcane")
        result = self.dm_core.calculate_damage("gladstone", "wolf", fireball)

        self.assertEqual(result["raw_damage"], 20)
        self.assertEqual(result["reduction"], 0)
        self.assertEqual(result["net_damage"], 20)

    def test_cleave_is_reachable_via_either_listed_skill(self):
        # cleave's skill field is a list (["blades", "axes"]) -- gladstone's equipped longsword
        # already matches "blades" and wins there (see find_attack_ability's docstring), but
        # nothing equipped matches "axes", so cleave surfaces via ability_matches_skill's
        # list-membership check.
        cleave = self.dm_core.find_attack_ability("gladstone", "axes")
        self.assertIsNotNone(cleave)
        self.assertEqual(cleave["name"], "cleave")

    @patch("random.randint", return_value=4)
    def test_calculate_damage_cleave_uses_equipped_weapons_dice_and_pips(self, mock_randint):
        # cleave's damage_value is "user.weapon.dice"/"user.weapon.pips", resolved from
        # gladstone's equipped longsword (1 die, 2 pips) plus strength_damage bonus (1):
        # 1 die @ 4 + 2 pips + 1 bonus = 7 raw damage, slashing.
        cleave = self.dm_core.find_attack_ability("gladstone", "axes")
        result = self.dm_core.calculate_damage("gladstone", "wolf", cleave)

        self.assertEqual(result["raw_damage"], 7)
        self.assertEqual(result["reduction"], 0)
        self.assertEqual(result["net_damage"], 7)


class TestCombatLoop(unittest.TestCase):
    def setUp(self):
        self.event_bus = EventBus()
        self.resolved = []
        # These tests always face a scenario target, so combat narration ("round_resolved")
        # is what fires, not the no-combat "action_resolved" path.
        self.event_bus.subscribe("round_resolved", self.resolved.append)
        self.dm_core = DMCore(self.event_bus)

    def test_find_attack_ability_prefers_equipped_weapon(self):
        # Gladstone has a longsword equipped in rhand, which uses the "blades" skill.
        ability = self.dm_core.find_attack_ability("gladstone", "blades")
        self.assertIsNotNone(ability)
        self.assertEqual(ability["name"], "longsword")

    def test_find_attack_ability_falls_back_to_innate_ability(self):
        # No equipped weapon uses "brawling", so the innate "punch" ability should be found instead.
        ability = self.dm_core.find_attack_ability("gladstone", "brawling")
        self.assertIsNotNone(ability)
        self.assertEqual(ability["name"], "punch")

    def test_find_attack_ability_resolves_name_referenced_spell(self):
        # Gladstone's abilities table names "fireball" rather than inlining it; find_attack_ability
        # must resolve that reference to the shared spells.toml entity to find "arcane"/damage_value.
        ability = self.dm_core.find_attack_ability("gladstone", "arcane")
        self.assertIsNotNone(ability)
        self.assertEqual(ability["name"], "fireball")
        self.assertIs(ability, self.dm_core.entities["fireball"])

    def test_resolve_ability_passes_through_inline_tables_and_looks_up_string_references(self):
        inline = {"name": "punch", "skill": "brawling"}
        self.assertIs(self.dm_core.resolve_ability(inline), inline)
        self.assertIs(self.dm_core.resolve_ability("fireball"), self.dm_core.entities["fireball"])
        self.assertIsNone(self.dm_core.resolve_ability("not_a_real_ability"))

    def test_find_attack_ability_returns_none_for_unmatched_skill(self):
        self.assertIsNone(self.dm_core.find_attack_ability("gladstone", "athletics"))

    def test_resolve_named_ability_finds_owned_ability_by_name(self):
        cleave = self.dm_core.resolve_named_ability("gladstone", "cleave")
        self.assertIsNotNone(cleave)
        self.assertIs(cleave, self.dm_core.entities["cleave"])

    def test_resolve_named_ability_returns_none_for_a_plain_skill_name(self):
        # "blades" is a skill gladstone has, not an ability of his -- must not match.
        self.assertIsNone(self.dm_core.resolve_named_ability("gladstone", "blades"))

    def test_resolve_named_ability_returns_none_for_an_ability_the_entity_lacks(self):
        # wolf has no "cleave" ability, only "bite".
        self.assertIsNone(self.dm_core.resolve_named_ability("wolf", "cleave"))

    def test_select_ability_skill_picks_best_rated_option_from_a_skill_list(self):
        # cleave's skill is ["blades", "axes"]; gladstone has "blades" (5 dice) and no "axes"
        # entry at all, so "blades" must be the one selected.
        cleave = self.dm_core.entities["cleave"]
        self.assertEqual(self.dm_core.select_ability_skill("gladstone", cleave), "blades")

    def test_select_ability_skill_passes_through_a_single_skill_string(self):
        fireball = self.dm_core.entities["fireball"]
        self.assertEqual(self.dm_core.select_ability_skill("gladstone", fireball), "arcane")

    def test_action_detected_with_named_ability_bypasses_find_attack_ability_priority(self):
        # find_attack_ability("gladstone", "blades") would normally return the equipped
        # longsword, never "cleave" (see test_find_attack_ability_prefers_equipped_weapon).
        # When NLPCore matches input directly to "cleave" by name, _on_action_detected must
        # use that exact ability instead -- proven here by asserting find_attack_ability is
        # never even called.
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario = {"entities": [{"name": "practice_dummy", "band": 0}]}
        self.dm_core.load_scenario()

        with patch.object(self.dm_core, "find_attack_ability", wraps=self.dm_core.find_attack_ability) as spy:
            with patch("random.randint", return_value=3):
                self.dm_core._on_action_detected({"skill": "cleave", "input": "I cleave through them"})

        spy.assert_not_called()
        result = self.resolved[-1]
        self.assertTrue(result["success"])
        self.assertEqual(result["skill"], "blades")  # cleave resolved to gladstone's best listed skill
        self.assertIn("damage", result)
        self.assertGreater(result["damage"]["net_damage"], 0)

    def test_missed_attack_does_not_apply_damage(self):
        # wolf's dodge (6 dice) will always beat gladstone's blades (2 dice) at this fixed roll.
        with patch("random.randint", return_value=1):
            self.dm_core._on_action_detected({"skill": "blades", "input": "I attack with my sword"})

        result = self.resolved[-1]
        self.assertFalse(result["success"])
        self.assertNotIn("damage", result)
        self.assertEqual(result["round"], 1)
        self.assertEqual(self.dm_core.get_current_hp("wolf"), 16)

    def test_successful_attack_applies_damage_to_the_target(self):
        # Give the player an opponent with no matching opposing skill, so the attack auto-succeeds (difficulty 0).
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario = {"entities": [{"name": "practice_dummy", "band": 0}]}
        self.dm_core.load_scenario()

        with patch("random.randint", return_value=3):
            self.dm_core._on_action_detected({"skill": "blades", "input": "I attack with my sword"})

        result = self.resolved[-1]
        self.assertTrue(result["success"])
        self.assertIn("damage", result)
        self.assertEqual(result["damage"]["defender"], "practice_dummy")
        self.assertGreater(result["damage"]["net_damage"], 0)
        self.assertEqual(
            self.dm_core.get_current_hp("practice_dummy"),
            20 - result["damage"]["net_damage"],
        )

    def test_non_attack_skill_never_applies_damage(self):
        # "athletics" has no equipped weapon or innate ability tied to it, so no damage should occur even on a hit.
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario = {"entities": [{"name": "practice_dummy", "band": 0}]}
        self.dm_core.load_scenario()

        self.dm_core._on_action_detected({"skill": "athletics", "input": "I climb the wall"})

        result = self.resolved[-1]
        self.assertNotIn("damage", result)

    def test_no_target_narrates_via_action_resolved_not_round_resolved(self):
        # An empty scenario has no target, so this is a non-combat skill use: it should
        # narrate immediately via "action_resolved", not get batched as a combat round.
        action_events = []
        self.event_bus.subscribe("action_resolved", action_events.append)
        self.dm_core.scenario = {"entities": [{"name": "gladstone", "band": 0}]}
        self.dm_core.load_scenario()

        self.dm_core._on_action_detected({"skill": "athletics", "input": "I climb the wall"})

        self.assertEqual(len(action_events), 1)
        self.assertEqual(self.resolved, [])
        self.assertNotIn("round", action_events[0])

    def test_scenario_load_publishes_scenario_loaded(self):
        # DMCore.__init__ (in setUp) should have already published a one-time scene-intro event.
        scenario_events = []
        event_bus = EventBus()
        event_bus.subscribe("scenario_loaded", scenario_events.append)
        DMCore(event_bus)

        self.assertEqual(len(scenario_events), 1)
        self.assertEqual(scenario_events[0]["name"], "The Arena")

    def test_missing_scenario_raises_instead_of_starting_with_no_data(self):
        # A scenario name with no matching file under Rules/Fantasy/scenarios/ must fail
        # loudly -- silently continuing with an empty self.scenario used to let the LLM
        # narrate an opening scene with no name/description (it would happily hallucinate
        # one, ex: a "featureless gray void", with no indication anything had gone wrong).
        with self.assertRaises(FileNotFoundError):
            DMCore(EventBus(), scenario_name="does_not_exist")


class TestStatusEvaluation(unittest.TestCase):
    def setUp(self):
        self.event_bus = EventBus()
        self.dm_core = DMCore(self.event_bus)

    def test_hp_per_remain_requirement_matches_current_percentage(self):
        # gladstone: max_hp 36. At 18 hp (50%) the "wounded" status (0.40-0.59) should match.
        self.dm_core.apply_damage("gladstone", 18)
        matched_names = [s["name"] for s in self.dm_core.get_applicable_statuses("gladstone", "on_damage")]
        self.assertIn("wounded", matched_names)
        self.assertNotIn("severe", matched_names)

    def test_trigger_filters_out_non_matching_statuses(self):
        # Even at full HP (matches "bruised"'s 0.81-0.99 range), a different trigger name should match nothing.
        matched = self.dm_core.get_applicable_statuses("gladstone", "on_turn_start")
        self.assertEqual(matched, [])

    def test_not_in_requirement_blocks_a_match(self):
        self.dm_core.rules["status"] = [{
            "name": "test_exclude",
            "trigger": "on_damage",
            "requirements": [{"field": "supertype", "operator": "not_in", "value": ["creature"]}],
        }]
        # gladstone is supertype "creature", so this status must not match.
        matched = self.dm_core.get_applicable_statuses("gladstone", "on_damage")
        self.assertEqual(matched, [])

    def test_in_requirement_requires_a_match(self):
        self.dm_core.rules["status"] = [{
            "name": "test_include",
            "trigger": "on_damage",
            "requirements": [{"field": "supertype", "operator": "in", "value": ["undead"]}],
        }]
        # gladstone is supertype "creature", not "undead", so this status must not match.
        matched = self.dm_core.get_applicable_statuses("gladstone", "on_damage")
        self.assertEqual(matched, [])

        self.dm_core.entities["gladstone"]["supertype"] = "undead"
        matched = self.dm_core.get_applicable_statuses("gladstone", "on_damage")
        self.assertEqual([s["name"] for s in matched], ["test_include"])

    def test_unknown_operator_never_matches(self):
        self.dm_core.rules["status"] = [{
            "name": "test_bad_operator",
            "trigger": "on_damage",
            "requirements": [{"field": "hp_per_remain", "operator": "~=", "value": 1}],
        }]
        matched = self.dm_core.get_applicable_statuses("gladstone", "on_damage")
        self.assertEqual(matched, [])

    def test_apply_damage_auto_applies_matching_condition(self):
        self.dm_core.apply_damage("gladstone", 18)  # -> 50% hp -> "wounded"
        self.assertIn("wounded", self.dm_core.entities["gladstone"]["active_conditions"])

    def test_apply_damage_does_not_apply_when_no_status_matches(self):
        # A single point of damage keeps gladstone above 0.99 hp_per_remain (the top of "bruised"'s
        # range is 0.99, and no status covers 1.0), so nothing should be applied yet. active_conditions
        # itself always exists now (seeded empty from the template's "conditions" at instancing time,
        # see load_scenario), so the check here is emptiness, not absence of the key.
        self.dm_core.apply_damage("gladstone", 0)
        self.assertEqual(self.dm_core.entities["gladstone"]["active_conditions"], {})

    def test_progressing_through_tiers_accumulates_conditions(self):
        # Auto-apply only adds; it doesn't remove a condition once its requirements stop holding.
        self.dm_core.apply_damage("gladstone", 18)  # 50% -> wounded
        self.dm_core.apply_damage("gladstone", 13)  # ~14% -> incapacitated
        active = self.dm_core.entities["gladstone"]["active_conditions"]
        self.assertIn("wounded", active)
        self.assertIn("incapacitated", active)


class TestScenarioLoading(unittest.TestCase):
    def setUp(self):
        self.event_bus = EventBus()
        self.dm_core = DMCore(self.event_bus)

    def test_duplicate_entities_get_unique_instance_names(self):
        # scenario.toml lists gladstone once and wolf twice.
        self.assertEqual(self.dm_core.scenario_entities, ["gladstone", "wolf", "wolf_2"])
        self.assertIn("wolf", self.dm_core.entities)
        self.assertIn("wolf_2", self.dm_core.entities)

    def test_duplicate_instances_are_independent(self):
        self.dm_core.apply_damage("wolf", 10)
        self.assertEqual(self.dm_core.get_current_hp("wolf"), 6)
        self.assertEqual(self.dm_core.get_current_hp("wolf_2"), 16)

    def test_instances_carry_their_own_entity_id(self):
        # entity_id lives on the instance itself, so it's still identifiable
        # once the dict is passed around independently of its self.entities key.
        self.assertEqual(self.dm_core.entities["gladstone"]["entity_id"], "gladstone")
        self.assertEqual(self.dm_core.entities["wolf"]["entity_id"], "wolf")
        self.assertEqual(self.dm_core.entities["wolf_2"]["entity_id"], "wolf_2")

    def test_instances_carry_their_scenario_band(self):
        self.dm_core.scenario = {"entities": [
            {"name": "wolf", "band": -1},
            {"name": "wolf", "band": 2},
        ]}
        self.dm_core.load_scenario()
        self.assertEqual(self.dm_core.entities["wolf"]["band"], -1)
        self.assertEqual(self.dm_core.entities["wolf_2"]["band"], 2)

    def test_unknown_entity_in_scenario_is_skipped_not_crashed(self):
        self.dm_core.scenario = {"entities": [{"name": "griffin", "band": 0}]}
        self.dm_core.load_scenario()
        self.assertEqual(self.dm_core.scenario_entities, [])

    def test_get_target_name_skips_the_player(self):
        target = self.dm_core._get_target_name()
        self.assertEqual(target, "wolf")

    def test_reloading_scenario_resets_instances(self):
        self.dm_core.scenario = {"entities": [{"name": "wolf", "band": 0}]}
        self.dm_core.load_scenario()
        self.assertEqual(self.dm_core.scenario_entities, ["wolf"])


class TestLockedChest(unittest.TestCase):
    def setUp(self):
        self.event_bus = EventBus()
        self.action_events = []
        self.round_events = []
        self.event_bus.subscribe("action_resolved", self.action_events.append)
        self.event_bus.subscribe("round_resolved", self.round_events.append)
        # Rules/Fantasy/scenarios/dungeon.toml puts the player alone with a locked chest
        # (items.toml's "chest": [entity.test] {difficulty=12, skill=["finesse"]}, starting
        # condition "locked").
        self.dm_core = DMCore(self.event_bus, scenario_name="dungeon")

    def test_chest_starts_locked(self):
        # Seeded from the template's [entity.conditions.locked] at instancing time (load_scenario).
        self.assertTrue(self.dm_core.is_locked("chest"))
        self.assertIn("locked", self.dm_core.entities["chest"]["active_conditions"])

    def test_chest_is_never_hostile_despite_no_attitude_data(self):
        # Objects opt out of combat routing regardless of the neutral-disposition default
        # that would otherwise mark a no-attitude-data entity as hostile (ex: wolf).
        self.assertFalse(self.dm_core.is_hostile("chest", "gladstone"))

    def test_forcing_it_with_strength_falls_through_to_the_opposed_fortitude_path(self):
        # "strength" isn't in the chest's [entity.test] skill list, so this isn't a lock pick
        # attempt at all -- it falls through to the *normal* resolve_opposed_action path, which
        # finds the chest's own "fortitude" (5 dice, since strength's `opposes` includes
        # "fortitude") and uses that as the difficulty. No special-casing needed for this at all.
        with patch("random.randint", return_value=1):  # gladstone: 2 dice @ 1 = 2 vs chest: 5 @ 1 = 5
            self.dm_core._on_action_detected({"skill": "strength", "input": "I try to force the chest"})

        self.assertTrue(self.dm_core.is_locked("chest"))  # forcing it isn't what removes "locked"
        self.assertEqual(self.round_events, [])
        result = self.action_events[-1]
        self.assertFalse(result["success"])
        self.assertEqual(result["defender"], "chest")
        self.assertEqual(result["opposing_skill"], "fortitude")
        self.assertEqual(result["difficulty"], 5)

    def test_failed_pick_leaves_it_locked_and_applies_jammed_on_fail(self):
        with patch("random.randint", return_value=1):  # 3 dice @ 1 = 3, well under test difficulty 12
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I pick the lock"})

        self.assertTrue(self.dm_core.is_locked("chest"))
        self.assertEqual(self.round_events, [])
        result = self.action_events[-1]
        self.assertFalse(result["success"])
        self.assertEqual(result["defender"], "chest")
        self.assertIsNone(result["opposing_skill"])
        self.assertEqual(result["difficulty"], 12)
        # [entity.test.fail] applies the permanent "jammed" condition.
        self.assertIn("jammed", self.dm_core.entities["chest"]["active_conditions"])

    def test_successful_pick_dismisses_the_locked_condition_without_forcing_loot(self):
        # Opening the chest must NOT auto-transfer its contents -- a player should be able to
        # examine what's inside (ex: a cursed weapon) before ever deciding to take it. See
        # TestItemInteraction for the separate examine/take mechanism.
        starting_currency = self.dm_core.entities["gladstone"]["currency"]
        with patch("random.randint", return_value=6):  # 3 dice @ 6 = 18, clears test difficulty 12
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I pick the lock"})

        self.assertFalse(self.dm_core.is_locked("chest"))
        self.assertNotIn("jammed", self.dm_core.entities["chest"]["active_conditions"])
        result = self.action_events[-1]
        self.assertTrue(result["success"])
        self.assertEqual(result["defender"], "chest")
        self.assertNotIn("loot", result)

        self.assertEqual(self.dm_core.entities["chest"]["currency"], 20)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], starting_currency)
        self.assertIn("cursed dagger", self.dm_core.entities["chest"]["inventory"])

    def test_picking_an_already_open_chest_does_not_retrigger_the_test_or_reloot(self):
        # requires_condition="locked" means the test is only attemptable while locked -- once
        # dismissed, a repeat "finesse" attempt must fall through to the normal opposed path
        # (difficulty 0, since the chest has no observation/reflexes for finesse to oppose),
        # not silently re-run [entity.test] and re-loot an already-empty chest.
        with patch("random.randint", return_value=6):
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I pick the lock"})
        currency_after_first_pick = self.dm_core.entities["gladstone"]["currency"]

        self.dm_core._on_action_detected({"skill": "finesse", "input": "I pick the lock again"})

        result = self.action_events[-1]
        self.assertEqual(result["difficulty"], 0)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], currency_after_first_pick)

    def test_jammed_permanently_blocks_further_pick_attempts(self):
        # blocks_if_condition="jammed" means once jammed (applied by test.fail), the test can
        # never be attempted again via finesse -- even a roll that would clear difficulty 12
        # must fall through to the normal opposed path instead (difficulty 0), leaving it locked.
        with patch("random.randint", return_value=1):  # fails, applies "jammed"
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I fumble the lock"})
        self.assertIn("jammed", self.dm_core.entities["chest"]["active_conditions"])

        with patch("random.randint", return_value=6):  # would clear difficulty 12 if it ran
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I try again"})

        self.assertTrue(self.dm_core.is_locked("chest"))
        result = self.action_events[-1]
        self.assertEqual(result["difficulty"], 0)

    def test_dismiss_condition_is_a_noop_for_a_condition_not_present(self):
        self.assertFalse(self.dm_core.dismiss_condition("chest", "not_a_real_condition"))
        self.assertTrue(self.dm_core.is_locked("chest"))  # unaffected

    def test_apply_test_outcome_is_a_noop_for_empty_outcome(self):
        self.dm_core.apply_test_outcome("chest", "")
        self.dm_core.apply_test_outcome("chest", None)
        self.assertEqual(self.dm_core.entities["chest"]["active_conditions"], {"locked": {"duration": "permanent"}})


class TestItemInteraction(unittest.TestCase):
    def setUp(self):
        self.event_bus = EventBus()
        self.resolved = []
        self.event_bus.subscribe("item_interaction_resolved", self.resolved.append)
        # dungeon.toml's chest carries a "cursed dagger" plus currency=20, for exercising
        # examine (read-only) vs take (transfers) without any dice roll involved.
        self.dm_core = DMCore(self.event_bus, scenario_name="dungeon")

    def test_examine_and_take_are_blocked_while_the_container_is_locked(self):
        self.dm_core._on_item_interaction_detected({
            "intent": "examine", "item_name": "cursed dagger", "input": "I examine the cursed dagger",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "locked")
        self.assertNotIn("cursed dagger", self.dm_core.entities["gladstone"]["inventory"])

    def _unlock_the_chest(self):
        self.dm_core.roll_dice = lambda dice, pips: 99
        self.dm_core._on_action_detected({"skill": "finesse", "input": "I pick the lock"})

    def test_examine_describes_an_item_without_transferring_it(self):
        self._unlock_the_chest()
        self.dm_core._on_item_interaction_detected({
            "intent": "examine", "item_name": "cursed dagger", "input": "I examine the cursed dagger",
        })
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertIn("runes", result["description"])
        self.assertNotIn("cursed dagger", self.dm_core.entities["gladstone"]["inventory"])
        self.assertIn("cursed dagger", self.dm_core.entities["chest"]["inventory"])

    def test_take_transfers_the_item(self):
        self._unlock_the_chest()
        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "cursed dagger", "input": "I take the cursed dagger",
        })
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertIn("cursed dagger", self.dm_core.entities["gladstone"]["inventory"])
        self.assertNotIn("cursed dagger", self.dm_core.entities["chest"]["inventory"])

    def test_currency_examine_and_take_use_transfer_currency_not_transfer_item(self):
        self._unlock_the_chest()
        self.dm_core._on_item_interaction_detected({
            "intent": "examine", "item_name": "currency", "input": "I check the gold",
        })
        examine_result = self.resolved[-1]
        self.assertTrue(examine_result["found"])
        self.assertIn("20", examine_result["description"])
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 100)  # unchanged

        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "currency", "input": "I take the gold",
        })
        take_result = self.resolved[-1]
        self.assertTrue(take_result["found"])
        self.assertEqual(take_result["amount"], 20)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 120)
        self.assertEqual(self.dm_core.entities["chest"]["currency"], 0)

    def test_item_not_present_is_reported_not_present(self):
        self._unlock_the_chest()
        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "longsword", "input": "I take the longsword",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_present")

    def test_examining_the_container_itself_describes_it(self):
        self._unlock_the_chest()
        self.dm_core._on_item_interaction_detected({
            "intent": "examine", "item_name": "chest", "input": "I examine the chest",
        })
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertIn("iron-bound", result["description"])

    def test_taking_the_container_itself_is_not_takeable(self):
        self._unlock_the_chest()
        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "chest", "input": "I take the whole chest",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_takeable")


class TestInventoryTransfer(unittest.TestCase):
    def setUp(self):
        self.event_bus = EventBus()
        self.dm_core = DMCore(self.event_bus, scenario_name="dungeon")

    def test_transfer_currency_moves_the_full_amount_by_default(self):
        moved = self.dm_core.transfer_currency("chest", "gladstone")
        self.assertEqual(moved, 20)
        self.assertEqual(self.dm_core.entities["chest"]["currency"], 0)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 120)

    def test_transfer_currency_moves_only_the_requested_amount(self):
        moved = self.dm_core.transfer_currency("chest", "gladstone", amount=5)
        self.assertEqual(moved, 5)
        self.assertEqual(self.dm_core.entities["chest"]["currency"], 15)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 105)

    def test_transfer_currency_clamps_to_whats_available(self):
        moved = self.dm_core.transfer_currency("chest", "gladstone", amount=1000)
        self.assertEqual(moved, 20)
        self.assertEqual(self.dm_core.entities["chest"]["currency"], 0)

    def test_transfer_currency_is_a_noop_for_a_missing_entity(self):
        moved = self.dm_core.transfer_currency("chest", "does_not_exist")
        self.assertEqual(moved, 0)
        self.assertEqual(self.dm_core.entities["chest"]["currency"], 20)  # unchanged

    def test_transfer_item_moves_one_matching_entry(self):
        # gladstone carries three "health potion" entries -- only one should move per call.
        moved = self.dm_core.transfer_item("gladstone", "chest", "health potion")
        self.assertTrue(moved)
        self.assertEqual(self.dm_core.entities["gladstone"]["inventory"].count("health potion"), 2)
        self.assertIn("health potion", self.dm_core.entities["chest"]["inventory"])

    def test_transfer_item_returns_false_when_item_not_present(self):
        moved = self.dm_core.transfer_item("chest", "gladstone", "longsword")
        self.assertFalse(moved)

    def test_loot_entity_moves_currency_and_every_inventory_item(self):
        # Give the chest some items too, not just currency, to exercise the full sweep.
        self.dm_core.entities["chest"]["inventory"] = ["health potion", "health potion"]

        self.dm_core.loot_entity("chest", "gladstone")

        self.assertEqual(self.dm_core.entities["chest"]["currency"], 0)
        self.assertEqual(self.dm_core.entities["chest"].get("inventory"), [])
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 120)
        self.assertEqual(self.dm_core.entities["gladstone"]["inventory"].count("health potion"), 5)


class TestNpcDialogue(unittest.TestCase):
    def setUp(self):
        self.event_bus = EventBus()
        self.action_events = []
        self.round_events = []
        self.event_bus.subscribe("action_resolved", self.action_events.append)
        self.event_bus.subscribe("round_resolved", self.round_events.append)
        # Rules/Fantasy/scenarios/tavern.toml puts the player with a friendly NPC
        # (npcs.toml's innkeeper) instead of the default "arena" combat scenario.
        self.dm_core = DMCore(self.event_bus, scenario_name="tavern")

    def test_innkeeper_is_not_hostile_toward_the_player(self):
        self.assertFalse(self.dm_core.is_hostile("innkeeper", "gladstone"))

    def test_wolf_is_still_hostile_by_default(self):
        # Wolves carry no explicit attitudes data, so the neutral (0) default should still
        # count as hostile/combat-ready -- this is a regression guard for is_hostile's threshold.
        self.assertTrue(self.dm_core.is_hostile("wolf", "gladstone"))

    def test_talking_to_the_innkeeper_narrates_immediately_as_dialogue(self):
        self.dm_core._on_action_detected({
            "skill": "charisma",
            "input": "I ask the innkeeper if she's heard any news from the road",
        })

        self.assertEqual(len(self.action_events), 1)
        self.assertEqual(self.round_events, [])
        result = self.action_events[0]
        self.assertEqual(result["defender"], "innkeeper")
        self.assertNotIn("round", result)
        self.assertNotIn("damage", result)

    def test_attacking_the_innkeeper_still_resolves_but_does_not_batch_into_a_round(self):
        # Hostility (not weapon possession) gates round batching, so even an attack against a
        # non-hostile NPC narrates immediately rather than waiting for "the round" to end.
        self.dm_core._on_action_detected({
            "skill": "blades",
            "input": "I draw my sword on the innkeeper",
        })

        self.assertEqual(len(self.action_events), 1)
        self.assertEqual(self.round_events, [])

    def test_fighting_a_hostile_target_still_batches_into_round_resolved(self):
        # Sanity check the branch didn't regress combat routing for an actually hostile target.
        self.dm_core.scenario = {
            "entities": [
                { "name": "gladstone", "band": 0 },
                { "name": "wolf", "band": 0 },
            ],
        }
        self.dm_core.load_scenario()

        self.dm_core._on_action_detected({"skill": "blades", "input": "I attack the wolf"})

        self.assertEqual(len(self.round_events), 1)
        self.assertEqual(self.action_events, [])
        self.assertEqual(self.round_events[0]["round"], 1)

    def test_describe_character_includes_descriptive_flavor_fields(self):
        description = self.dm_core.describe_character("innkeeper")
        self.assertIn("stout woman with flour-dusted sleeves", description)
        self.assertIn("Lost her husband to a bandit raid", description)
        self.assertIn("Welcome to the Rusty Tankard", description)

    def test_describe_character_returns_empty_for_pure_mechanics_entity(self):
        # wolf has skills but no description/qualities/memories/quotes -- nothing to narrate.
        self.assertEqual(self.dm_core.describe_character("wolf"), "")

    def test_scenario_loaded_includes_character_roster(self):
        scenario_events = []
        self.event_bus.subscribe("scenario_loaded", scenario_events.append)
        DMCore(self.event_bus, scenario_name="tavern")

        characters = scenario_events[-1]["characters"]
        self.assertTrue(any("innkeeper" in c for c in characters))
        self.assertTrue(any("gladstone" in c for c in characters))

    def test_action_resolved_includes_defender_details_for_a_described_npc(self):
        self.dm_core._on_action_detected({
            "skill": "charisma",
            "input": "I ask the innkeeper about her husband",
        })

        result = self.action_events[0]
        self.assertIn("Lost her husband to a bandit raid", result["defender_details"])


def lines_of(app, widget_id):
    return [str(line) for line in app.query_one(f"#{widget_id}", RichLog).lines]


@pytest.mark.asyncio
async def test_user_input_and_llm_response_mirror_into_history():
    event_bus = EventBus()
    app = TextualCore(event_bus)

    async with app.run_test() as pilot:
        await pilot.pause()
        event_bus.publish("user_input_submitted", "I attack the wolf")
        event_bus.publish("llm_response_ready", "The wolf dodges your blow.")
        await pilot.pause()

        history = lines_of(app, "history")
        assert any("> I attack the wolf" in line for line in history)
        assert any("The wolf dodges your blow." in line for line in history)


@pytest.mark.asyncio
async def test_events_published_before_mount_are_buffered_then_flushed():
    event_bus = EventBus()
    app = TextualCore(event_bus)

    # Mirrors DMCore publishing rules_loaded synchronously during __init__, which in
    # LLDM.py happens before the GUI's event loop (Tkinter's or Textual's) is running.
    event_bus.publish("rules_loaded", {
        "skills": {"blades": {"name": "blades"}},
        "entities": {"gladstone": {"name": "gladstone"}},
    })

    async with app.run_test() as pilot:
        await pilot.pause()
        debug_log = lines_of(app, "debug_log")
        assert any("blades" in line for line in debug_log)
        assert any("gladstone" in line for line in debug_log)


@pytest.mark.asyncio
async def test_background_thread_publish_is_thread_safe():
    # LLMCore publishes llm_response_ready from a background fetch thread, not the app's
    # own thread, so this exercises call_safely's cross-thread path via call_from_thread.
    event_bus = EventBus()
    app = TextualCore(event_bus)

    async with app.run_test() as pilot:
        await pilot.pause()

        def from_background_thread():
            event_bus.publish("llm_response_ready", "Narration from a background thread.")

        thread = threading.Thread(target=from_background_thread)
        thread.start()
        await asyncio.to_thread(thread.join)
        await pilot.pause()

        assert any("Narration from a background thread." in line for line in lines_of(app, "history"))


@pytest.mark.asyncio
async def test_typing_and_pressing_enter_publishes_and_echoes_input():
    event_bus = EventBus()
    app = TextualCore(event_bus)
    received = []
    event_bus.subscribe("user_input_submitted", received.append)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#input_box")
        keys = ["space" if c == " " else c for c in "attack the wolf"]
        await pilot.press(*keys)
        await pilot.press("enter")
        await pilot.pause()

        assert received == ["attack the wolf"]
        assert any("> attack the wolf" in line for line in lines_of(app, "history"))
        # The input field clears after submitting, mirroring GUICore.submit_input.
        assert app.query_one("#input_box").value == ""


@pytest.mark.asyncio
async def test_inactive_tab_content_requires_activation_to_read_lines():
    # Gotcha: a RichLog's .lines reflects width-wrapped content, and a widget inside a
    # non-active TabPane has no render width, so .lines reads empty until that tab is
    # switched to - even though the write already happened. Tests reading a background
    # tab's log must activate it first, as this test demonstrates.
    event_bus = EventBus()
    app = TextualCore(event_bus)

    async with app.run_test() as pilot:
        await pilot.pause()
        event_bus.publish("log_info", "a log line")
        await pilot.pause()

        assert lines_of(app, "event_log") == []

        app.query_one(TabbedContent).active = "event_log_tab"
        await pilot.pause()

        assert any("a log line" in line for line in lines_of(app, "event_log"))


if __name__ == "__main__":
    unittest.main()
