import os

from DM_Combat import CombatMixin
from DM_Inventory import InventoryMixin
from DM_Persistence import PersistenceMixin
from DM_Rules import RulesMixin, scenario_file_path
from DM_Social import SocialMixin
from DM_Status import StatusMixin

class DMCore(InventoryMixin, SocialMixin, StatusMixin, CombatMixin, RulesMixin, PersistenceMixin):
    """!
    @brief Main class handling the core mechanics of the RPG system. The implementation is
           composed from domain mixins in sibling files -- DM_Rules.py (rules/scenario
           loading), DM_Combat.py (dice/damage/ability resolution), DM_Status.py (the
           status/condition system and entity tests), DM_Inventory.py (currency/item
           transfer), DM_Social.py (attitudes and character description), and
           DM_Persistence.py (save/load) -- so that every dm_core.<method>(...) call site
           throughout the codebase and test_all.py keeps working unchanged regardless of
           which file actually defines a given method (Python's MRO flattens every mixin
           method onto this one class). DM_Core.py itself is reduced to __init__ (boot
           wiring) plus the two real event handlers, _on_action_detected and
           _on_item_interaction_detected, and their direct helpers -- the pieces that
           orchestrate calls across every mixin and don't belong to any single domain.
    """

    def __init__(self, event_bus, scenario_name="arena"):
        """!
        @brief Initializes the DM core and loads system references.
        @param event_bus The central event bus instance.
        @param scenario_name Which scenario to load, matching a file in
               Rules/Fantasy/scenarios/ (ex: "arena" loads scenarios/arena.toml).
        """
        self.event_bus = event_bus
        self.skills = {}
        self.entities = {}
        self.scenario = {}
        self.scenario_entities = []
        self.rules = {}
        self.round_number = 0
        # The filename passed to load_scenario_definition -- distinct from self.scenario's
        # own "name" field (a display string, ex: "The Arena") -- kept so save_game/load_game
        # know which scenarios/*.toml file a saved slot belongs to.
        self.scenario_key = scenario_name
        self.load_rules(os.path.join("Rules", "Fantasy"))
        # No party/character selection exists yet, so the one entity template marked
        # is_player = true (characters.toml's gladstone) stands in as the active player
        # character. Resolved once here, from templates, rather than per-scenario-load, so
        # ad-hoc test scenarios that omit gladstone entirely still keep the same player_name
        # they booted with.
        self.player_name = self._resolve_player_name()
        self.load_scenario_definition(scenario_name)
        self.load_scenario()
        self.event_bus.publish("log_info", "DMCore initialized.")
        self.event_bus.publish("rules_loaded", {"skills": self.skills, "entities": self.entities})
        self.event_bus.publish("scenario_loaded", {
            "name": self.scenario.get("name"),
            "description": self.scenario.get("description"),
            "characters": self._describe_scenario_characters(),
        })
        self.event_bus.subscribe("action_detected", self._on_action_detected)
        self.event_bus.subscribe("item_interaction_detected", self._on_item_interaction_detected)
        self.event_bus.subscribe("save_requested", self._on_save_requested)
        self.event_bus.subscribe("load_requested", self._on_load_requested)

    def _on_action_detected(self, data):
        """!
        @brief Event handler that resolves a detected player action, opposed by a scenario target if one exists,
               and applies damage if the action hit with an attack ability. Combat (a target present that is
               hostile toward the player) narrates once per round via "round_resolved"; everything else
               (no target, or a non-hostile target like a tavern NPC) narrates immediately via "action_resolved".
        @param data The action_detected payload from NLPCore ({skill, score, input}). "skill" is
               usually a plain skill name, but may also be a named technique/spell the player
               owns (ex: "cleave") -- resolve_named_ability/select_ability_skill are what
               convert that into the skill it's actually rolled with, while keeping the named
               ability itself to use directly for damage further down.
        """
        skill_name = data.get("skill")
        if not skill_name:
            return

        named_ability = self.resolve_named_ability(self.player_name, skill_name)
        if named_ability:
            skill_name = self.select_ability_skill(self.player_name, named_ability) or skill_name

        target_name = self._get_target_name()
        test = self.entities.get(target_name, {}).get("test") if target_name else None
        via_test = False
        if test and self.is_test_available(target_name, test, skill_name):
            via_test = True
            # An entity's own [entity.test] (ex: a chest's lock) is a flat difficulty check,
            # not an opposed roll -- it doesn't compete via the attacker's skill's `opposes`
            # list the way a creature's defense does. Any *other* skill against this same
            # target (ex: forcing the chest with "strength") still falls through to the normal
            # opposed-skill path below, e.g. resolved against its "fortitude" if it has one.
            result = self.resolve_action(self.player_name, skill_name, test.get("difficulty", 0))
            result["defender"] = target_name
            result["opposing_skill"] = None
            outcome = test.get("pass") if result["success"] else test.get("fail")
            loot = self.apply_test_outcome(target_name, outcome)
            if loot and (loot["currency"] or loot["items"]):
                result["loot"] = loot
        elif target_name:
            result = self.resolve_opposed_action(self.player_name, skill_name, target_name)
        else:
            result = self.resolve_action(self.player_name, skill_name)

        if result["success"] and target_name and not via_test:
            # A test-path success (ex: a lockpick) is a flat difficulty check, not an attack --
            # it must never also roll bonus weapon damage even if skill_name happens to match
            # an equipped weapon/ability (ex: a future finesse-based dagger matching the chest's
            # finesse-skill lock test).
            ability = named_ability or self.find_attack_ability(self.player_name, skill_name)
            if ability:
                result["damage"] = self.calculate_damage(self.player_name, target_name, ability)

        if target_name:
            defender_details = self.describe_character(target_name, toward_name=self.player_name)
            if defender_details:
                result["defender_details"] = defender_details

        result["input"] = data.get("input")

        in_combat = target_name is not None and self.is_hostile(target_name, self.player_name)
        if in_combat:
            self.round_number += 1
            result["round"] = self.round_number
            # The target gets to act back the same round, via its own [[entity.behavior]]
            # (ex: a wolf biting back) -- this is what makes combat mutual instead of the
            # player only ever being the one rolling to hit. A target with no matching
            # behavior (no behavior data at all, or none of its requirements currently hold,
            # ex: it's already at 0 HP) simply doesn't act.
            enemy_result = self.resolve_behavior_action(target_name, self.player_name)
            if enemy_result:
                result["enemy_action"] = enemy_result
            self.event_bus.publish("round_resolved", result)
        else:
            self.event_bus.publish("action_resolved", result)

    def _on_item_interaction_detected(self, data):
        """!
        @brief Event handler for a free-text item-interaction match (see NLPCore.map_to_item):
               "examine"/"take"/"give"/"trade" against a named item, or "open"/"close" against
               the scene target itself. Deliberately bypasses the whole skill/dice system --
               none of these warrant a roll. Publishes "item_interaction_resolved" either way,
               with enough detail for narration to explain a miss (locked, closed, not present,
               not takeable, ...) rather than staying silent.

               "take"/"trade" move an item from the target to the player; "give" moves one from
               the player to the target -- same transfer_item/transfer_currency primitives,
               just with source/destination swapped. "trade" additionally charges the item's
               TOML `value` as a price (denied outright if the player can't afford it, rather
               than a partial payment). "examine" and "open"/"close" never move anything.
        @param data The item_interaction_detected payload from NLPCore
               ({intent, item_name, input, score}). "item_name" is None for "open"/"close",
               which act on the scene target directly rather than a named item.
        """
        intent = data.get("intent")
        item_name = data.get("item_name")
        input_text = data.get("input")
        target_name = self._get_target_name()

        def resolved(found, **extra):
            self.event_bus.publish("item_interaction_resolved", {
                "intent": intent, "item_name": item_name, "input": input_text, "found": found, **extra,
            })

        if target_name and self.is_locked(target_name):
            resolved(False, reason="locked", container=target_name)
            return

        if intent in ("open", "close"):
            self._resolve_open_close_intent(intent, target_name, resolved)
            return

        if item_name == target_name:
            # Interacting with the container/creature itself, not something inside it -- there's
            # nothing to "take"/"give"/"trade" about the target as a whole.
            if intent == "examine":
                description = self.describe_character(target_name, toward_name=self.player_name) or ""
                resolved(True, description=description)
            else:
                resolved(False, reason="not_takeable")
            return

        if target_name and self.is_closed(target_name):
            # A closed (but unlocked) container can still be examined/opened from the outside
            # (handled above/below) -- only reaching its *contents* is gated here.
            resolved(False, reason="closed", container=target_name)
            return

        if intent == "give":
            if not target_name:
                resolved(False, reason="no_recipient")
                return
            source_name, destination_name = self.player_name, target_name
        else:
            source_name, destination_name = target_name, self.player_name

        if item_name == "currency":
            if intent == "trade":
                # Trading for currency itself is meaningless -- nothing to buy or sell here.
                # Checked before availability, since this is wrong regardless of the amount.
                resolved(False, reason="not_takeable")
                return
            # Currency is a plain "currency" integer field, not an inventory item -- handled
            # separately from transfer_item/source_inventory below.
            available = self.entities.get(source_name, {}).get("currency", 0) if source_name else 0
            if available <= 0:
                resolved(False, reason="not_present")
                return
            if intent == "examine":
                resolved(True, description=f"{available} currency", container=target_name)
            else:
                moved = self.transfer_currency(source_name, destination_name)
                resolved(True, container=target_name, amount=moved)
            return

        source_inventory = self.entities.get(source_name, {}).get("inventory", []) if source_name else []
        if item_name not in source_inventory:
            resolved(False, reason="not_present")
            return

        if intent == "examine":
            description = self.entities.get(item_name, {}).get("description", "")
            resolved(True, description=description, container=target_name)
        elif intent == "trade":
            price = self.entities.get(item_name, {}).get("value", 0)
            buyer_currency = self.entities.get(self.player_name, {}).get("currency", 0)
            if buyer_currency < price:
                resolved(False, reason="cant_afford", price=price)
                return
            self.transfer_currency(self.player_name, target_name, price)
            self.transfer_item(source_name, destination_name, item_name)
            resolved(True, container=target_name, price=price)
        else:
            self.transfer_item(source_name, destination_name, item_name)
            resolved(True, container=target_name)

    def _resolve_open_close_intent(self, intent, target_name, resolved):
        """!
        @brief Handles "open"/"close" against the current scene target directly -- these act on
               the container itself, not a named item inside it, so (unlike the other item
               intents) they never go through map_to_item at all. Gated to subtype ==
               "container" (ex: items.toml's chest) so aiming "open"/"close" at a creature or a
               plain object with no openable nature fails safely instead of silently applying a
               nonsensical condition to it.
        @param intent "open" or "close".
        @param target_name The current scene target's name, or None if there isn't one.
        @param resolved The item_interaction_resolved publisher closure from the caller.
        """
        if not target_name or self.entities.get(target_name, {}).get("subtype") != "container":
            resolved(False, reason="not_openable")
            return

        if intent == "open":
            if not self.is_closed(target_name):
                resolved(False, reason="already_open", container=target_name)
                return
            self.dismiss_condition(target_name, "closed")
        else:
            if self.is_closed(target_name):
                resolved(False, reason="already_closed", container=target_name)
                return
            self.apply_condition(target_name, "closed", duration="permanent", dismiss="")

        resolved(True, container=target_name)

    def _get_target_name(self):
        """!
        @brief Picks the current opposed target from the instantiated scenario entities.
        @return The name of the first non-player entity instance in the scenario, or None if there isn't one.
        """
        for instance_name in self.scenario_entities:
            if instance_name != self.player_name:
                return instance_name
        return None
