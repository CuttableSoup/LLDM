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
        # The player's persisted combat target -- distinct from _get_target_name()'s "first
        # non-player entity" (which stays purely for non-combat interaction resolution, ex:
        # the dungeon's chest or the tavern's innkeeper). Set for real by load_scenario()
        # (via _choose_combat_target()) once entities/scenario are actually loaded below.
        self.current_target = None
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
        @brief Event handler that resolves a detected player action, opposed by the player's
            current combat target if one exists, and applies damage if the action hit with
            an attack ability. Combat (a target present that is hostile toward the player)
            narrates once per round via "round_resolved"; everything else (no target, or a
            non-hostile target like a tavern NPC) narrates immediately via "action_resolved".
        @param data The action_detected payload from NLPCore ({skill, score, input, target?}).
            "skill" is usually a plain skill name, but may also be a named technique/spell
            the player owns (ex: "cleave") -- resolve_named_ability/select_ability_skill are
            what convert that into the skill it's actually rolled with, while keeping the
            named ability itself to use directly for damage further down. "target", if
            present, is NLPCore's best-guess entity name match (see map_to_target) -- honored
            as an item-test target (see _resolve_item_test_target) if it names a reachable,
            testable item; otherwise as a combat redirect if it names a live, hostile,
            in-scene entity; otherwise the persisted self.current_target is left alone.
        """
        skill_name = data.get("skill")
        if not skill_name:
            return

        named_ability = self.resolve_named_ability(self.player_name, skill_name)
        if named_ability:
            skill_name = self.select_ability_skill(self.player_name, named_ability) or skill_name

        explicit_target = data.get("target")

        # A named item (ex: "the cursed dagger") with its own [entity.test], reached one level
        # deeper than the scene itself (already in the player's inventory, or sitting in an
        # unlocked/open container) -- resolved as its own flat check, never as a combat
        # redirect, since inspecting an item isn't an attack. Checked first, ahead of combat
        # targeting, since the two are mutually exclusive outcomes for the same explicit_target.
        item_test_target = self._resolve_item_test_target(explicit_target, skill_name)
        if item_test_target:
            result = self._resolve_item_test(item_test_target, skill_name)
            result["input"] = data.get("input")
            self.event_bus.publish("action_resolved", result)
            return

        if (
            explicit_target
            and explicit_target in self.scenario_entities
            and self.is_hostile(explicit_target, self.player_name)
            and self.get_current_hp(explicit_target) > 0
        ):
            self.current_target = explicit_target

        target_name = self.current_target
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
            # Every other living entity in the scene gets to act this round, via its own
            # [[entity.behavior]] -- not just target_name -- so combat is mutual (a wolf
            # biting back) and allies pull their weight too (ex: thane striking whatever the
            # player is currently fighting). A hostile entity attacks the player; a
            # non-hostile one (an ally) attacks self.current_target instead. An entity with no
            # matching behavior (no behavior data, or none of its requirements currently hold,
            # ex: it's already at 0 HP) simply doesn't act. Every actor's outcome is still
            # resolved independently against the state at the start of the round (see the
            # current_target note below) -- initiative only orders how the round is presented,
            # it doesn't make an earlier actor's roll affect a later one's.
            result["initiative"] = self.roll_initiative(self.player_name)
            turns = []
            for entity_name in self.scenario_entities:
                if entity_name == self.player_name:
                    continue
                opponent = self.player_name if self.is_hostile(entity_name, self.player_name) else self.current_target
                if not opponent:
                    continue
                turn_result = self.resolve_behavior_action(entity_name, opponent)
                if turn_result:
                    turn_result["actor"] = entity_name
                    turn_result["initiative"] = self.roll_initiative(entity_name)
                    turns.append(turn_result)
            if turns:
                turns.sort(key=lambda turn: turn["initiative"], reverse=True)
                result["turns"] = turns
            # current_target only advances once, at the end of the round, if it died -- not
            # interrupted mid-round by an earlier actor's kill (ex: an ally finishing it off
            # before the round is even done resolving).
            if self.current_target and self.get_current_hp(self.current_target) <= 0:
                self.current_target = self._choose_combat_target()
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
            # A plain look never surfaces a hidden property (ex: the cursed dagger's curse) --
            # only once is_identified is true (a passed [entity.test], ex: an arcane check)
            # does examining it start including what that check actually revealed.
            revealed = list(self.entities.get(item_name, {}).get("tags", [])) if self.is_identified(item_name) else []
            resolved(True, description=description, container=target_name, revealed=revealed)
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
            # Real contents, not a guess: each item's own describe_character() output (its
            # flavor description only -- the same purely-descriptive, no-mechanical-data
            # field selection describe_character already uses for entities) -- never its
            # "tags"/damage_value/etc., so a cursed item's actual curse tag is never handed
            # to the LLM here. Without this, "open the chest" had nothing to narrate from and
            # invented plausible-sounding treasure instead of the real contents.
            contents = [
                description for description in (
                    self.describe_character(item_name)
                    for item_name in self.entities.get(target_name, {}).get("inventory", [])
                )
                if description
            ]
            resolved(True, container=target_name, contents=contents)
            return

        if self.is_closed(target_name):
            resolved(False, reason="already_closed", container=target_name)
            return
        self.apply_condition(target_name, "closed", duration="permanent", dismiss="")
        resolved(True, container=target_name)

    def _resolve_item_test_target(self, target_name, skill_name):
        """!
        @brief Resolves target_name to an item entity whose own [entity.test] accepts
            skill_name, if that item is actually reachable right now -- either already in the
            player's own inventory, or sitting in the current scene target's inventory (and
            that container isn't locked or closed). This is what lets a skill check be aimed
            at something one level *deeper* than the scene itself (ex: "I check the dagger for
            curses" with arcane, once it's inside the chest or already picked up) -- a
            scene-level target's own [entity.test] (ex: the chest's own lock) was already
            reachable via self.current_target before this existed; this only closes the gap
            for an item *contained* by the scene, not the scene target itself.
        @param target_name NLPCore's best-guess entity name match (see map_to_target), or None.
        @param skill_name The skill the player is attempting to use.
        @return target_name itself if it resolves to a reachable, testable item for this
                skill, else None.
        """
        if not target_name:
            return None
        test = self.entities.get(target_name, {}).get("test")
        if not test or not self.is_test_available(target_name, test, skill_name):
            return None

        if target_name in self.entities.get(self.player_name, {}).get("inventory", []):
            return target_name

        container_name = self._get_target_name()
        if (
            container_name
            and not self.is_locked(container_name)
            and not self.is_closed(container_name)
            and target_name in self.entities.get(container_name, {}).get("inventory", [])
        ):
            return target_name

        return None

    def _resolve_item_test(self, item_name, skill_name):
        """!
        @brief Resolves a flat [entity.test] check against a reachable item (see
            _resolve_item_test_target) -- the item-level counterpart to _on_action_detected's
            own scene-target test branch, kept as a separate path since an item is never a
            combat target (no round, no defender_details, no damage).
        @param item_name The item entity's name (already confirmed reachable/testable by
            _resolve_item_test_target).
        @param skill_name The skill the player is attempting to use.
        @return A resolve_action-shaped result dict, plus "revealed" (the item's own "tags"
                list) if the check passed and its outcome had a truthy "reveal" key.
        """
        test = self.entities[item_name]["test"]
        result = self.resolve_action(self.player_name, skill_name, test.get("difficulty", 0))
        result["defender"] = item_name
        result["opposing_skill"] = None
        outcome = test.get("pass") if result["success"] else test.get("fail")
        self.apply_test_outcome(item_name, outcome)
        if self.is_identified(item_name):
            result["revealed"] = list(self.entities[item_name].get("tags", []))
        return result

    def _get_target_name(self):
        """!
        @brief Picks the current opposed target from the instantiated scenario entities.
        @return The name of the first non-player entity instance in the scenario, or None if there isn't one.
        """
        for instance_name in self.scenario_entities:
            if instance_name != self.player_name:
                return instance_name
        return None

    def _choose_combat_target(self):
        """!
        @brief Picks self.current_target: the first living, hostile-toward-the-player entity
            in scenario_entities order. If none qualifies (ex: every wolf is dead, or
            nothing in the scene was ever hostile -- the dungeon's chest, the tavern's
            innkeeper), falls back to the first *living* non-player entity instead, so a
            defeated enemy is never left as a stale target once combat is over -- unlike
            _get_target_name(), which returns the first non-player entity unconditionally
            (dead or not) and stays reserved for non-combat interaction resolution, where
            that's always correct since a chest/NPC is never "defeated". Used both to set
            the initial current_target (via load_scenario()) and to advance it once the
            previous target dies (see _on_action_detected's end-of-round check).
        @return The chosen entity name, or None if no non-player entity in the scenario is alive.
        """
        for instance_name in self.scenario_entities:
            if instance_name == self.player_name:
                continue
            if self.is_hostile(instance_name, self.player_name) and self.get_current_hp(instance_name) > 0:
                return instance_name
        for instance_name in self.scenario_entities:
            if instance_name == self.player_name:
                continue
            if self.get_current_hp(instance_name) > 0:
                return instance_name
        return None
