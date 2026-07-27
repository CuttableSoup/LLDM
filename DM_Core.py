import os
import re

from DM_CharacterCreation import CharacterCreationMixin
from DM_Combat import CombatMixin
from DM_Inventory import InventoryMixin
from DM_Movement import MovementMixin
from DM_Persistence import PersistenceMixin
from DM_Rules import RulesMixin, scenario_file_path
from DM_Social import SocialMixin
from DM_Status import StatusMixin

class DMCore(InventoryMixin, SocialMixin, StatusMixin, CombatMixin, MovementMixin, RulesMixin, PersistenceMixin, CharacterCreationMixin):
    """!
    @brief Main class handling the core mechanics of the RPG system. The implementation is
        composed from domain mixins in sibling files -- DM_Rules.py (rules/scenario
        loading), DM_Combat.py (dice/damage/ability resolution), DM_Status.py (the
        status/condition system and entity tests), DM_Inventory.py (currency/item
        transfer), DM_Social.py (attitudes and character description), DM_Movement.py
        (distance tracking, advance/retreat, range-based difficulty), DM_Persistence.py
        (save/load), and DM_CharacterCreation.py (baking a finished character-creation
        result -- race + point-buy skill allocation, see Character_Creation.py's own
        module docstring -- onto the player entity) -- so that every dm_core.<method>(...)
        call site throughout the codebase and test_all.py keeps working unchanged
        regardless of which file actually defines a given method (Python's MRO flattens
        every mixin method onto this one class). DM_Core.py itself is reduced to __init__
        (boot wiring) plus the two real event handlers, _on_action_detected and
        _on_item_interaction_detected, and their direct helpers -- the pieces that
        orchestrate calls across every mixin and don't belong to any single domain.
    """

    def __init__(self, event_bus, scenario_name="arena", character=None):
        """!
        @brief Initializes the DM core and loads system references.
        @param event_bus The central event bus instance.
        @param scenario_name Which scenario to load, matching a file in
            Rules/Fantasy/scenarios/ (ex: "arena" loads scenarios/arena.toml).
        @param character An optional finished character-creation result
            ({"race": race_name, "allocation": {skill_name: dice_int}}), applied to the
            player entity via apply_character_creation (DM_CharacterCreation.py) right
            after self.player_name is resolved, before any scenario is loaded. None (the
            default) leaves the player template's own hand-authored skills untouched --
            every existing caller that doesn't pass this keeps working exactly as before.
        """
        self.event_bus = event_bus
        self.skills = {}
        self.entities = {}
        self.scenario = {}
        self.scenario_entities = []
        # The instance names from [scenario].entities (ex: the player, plus any ally like
        # crypt.toml's "thane" meant to persist across the whole dungeon) -- kept separate
        # from self.scenario_entities so _populate_room can rebuild the latter as
        # persistent_entities + the current room's own local entities on every room
        # transition, instead of collapsing back down to just the player (see DM_Rules.py).
        self.persistent_entities = []
        # Populated for real by load_scenario_definition -- empty/None here is what a plain
        # single-room scenario (arena/tavern/field/dungeon) keeps permanently, since it has
        # no [[room]] tables at all (see DM_Rules.py's "Scenario instancing"/room-graph notes).
        self.rooms = {}
        self.current_room_key = None
        self.visited_rooms = {}
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
        self.apply_character_creation(character)
        self.load_scenario_definition(scenario_name)
        self.load_scenario()
        self.event_bus.publish("log_info", "DMCore initialized.")
        self.event_bus.publish("rules_loaded", {
            "skills": self.skills,
            "entities": self.entities,
            "equip_slots": self.rules.get("equip_slot", []),
        })
        self.event_bus.publish("scenario_loaded", {
            # For a multi-room dungeon this narrates the *starting room* specifically (ex:
            # "Entrance Hall"), not just the dungeon's own overall blurb -- see
            # _current_scene_name/_current_scene_description in DM_Rules.py.
            "name": self._current_scene_name(),
            "description": self._current_scene_description(),
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
            The body below is a thin sequencer over the phase helpers just under it
            (_resolve_action_skill, _try_item_test_action, _apply_target_redirect,
            _resolve_roll, _apply_damage_if_hit, _attach_defender_details,
            _resolve_combat_round) -- each one is independently callable/testable the same
            way every other dm_core.<method>(...) call site already is.
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
        skill_name, named_ability = self._resolve_action_skill(skill_name)

        explicit_target = data.get("target")
        item_result = self._try_item_test_action(explicit_target, skill_name, data.get("input"))
        if item_result is not None:
            self.event_bus.publish("action_resolved", item_result)
            self._publish_party_status()
            return

        self._apply_target_redirect(explicit_target)
        target_name = self.current_target

        result, ability, via_test = self._resolve_roll(skill_name, named_ability, target_name)
        self._apply_damage_if_hit(result, skill_name, named_ability, ability, target_name, via_test)
        self._attach_defender_details(result, target_name)
        result["input"] = data.get("input")

        if target_name is not None and self.is_hostile(target_name, self.player_name):
            self._resolve_combat_round(result)
            self.event_bus.publish("round_resolved", result)
        else:
            self.event_bus.publish("action_resolved", result)
        self._publish_party_status()

    def _publish_party_status(self):
        """!
        @brief Re-publishes a fresh entities snapshot for GUICore's Party tab to redraw from,
            after anything that can change a party member's HP/equipment/inventory/
            conditions (a resolved action, an item interaction, a game load). Deliberately
            not "rules_loaded" -- NLPCore also rebuilds its sentence-transformer embeddings
            from that event, which would be far too expensive to redo after every single
            action; "party_status_changed" carries the same "entities"/"equip_slots" shape
            but only GUICore listens for it.
        """
        self.event_bus.publish("party_status_changed", {
            "entities": self.entities,
            "equip_slots": self.rules.get("equip_slot", []),
        })

    def _resolve_action_skill(self, skill_name):
        """!
        @brief Resolves a matched action name down to the skill it's actually rolled with.
            skill_name is usually already a plain skill name, but may instead name a
            technique/spell the player owns (ex: "cleave") -- resolve_named_ability/
            select_ability_skill convert that into the skill it rolls against, while keeping
            the named ability itself so the damage step further down uses it directly
            instead of re-deriving a generic weapon/innate ability.
        @param skill_name The skill or ability name from the action_detected payload.
        @return (skill_name, named_ability) -- named_ability is None if skill_name was
            already a plain skill.
        """
        named_ability = self.resolve_named_ability(self.player_name, skill_name)
        if named_ability:
            skill_name = self.select_ability_skill(self.player_name, named_ability) or skill_name
        return skill_name, named_ability

    def _try_item_test_action(self, explicit_target, skill_name, input_text):
        """!
        @brief Tries to resolve explicit_target as a named item one level deeper than the
            scene itself (ex: "the cursed dagger" already in inventory, or sitting in an
            unlocked/open container), reached via its own [entity.test] rather than as a
            combat redirect -- inspecting an item is never an attack. Checked ahead of combat
            targeting since the two are mutually exclusive outcomes for the same
            explicit_target. If skill_name doesn't qualify against the target's own test,
            tries _rescue_item_test_skill before giving up -- see its docstring for why the
            naively NLP-matched skill can be wrong here specifically (ex: "identify the
            dagger" resolving to "blades" purely because the item's own name collides with
            an unrelated skill's keyword).
        @param explicit_target NLPCore's best-guess target name (map_to_target), or None.
        @param skill_name The skill being used, already resolved from any named ability.
        @param input_text The player's raw input, attached to the result for narration.
        @return A publish-ready action_resolved result dict, or None if explicit_target isn't
            a reachable, testable item.
        """
        item_test_target = self._resolve_item_test_target(explicit_target, skill_name)
        if item_test_target is None:
            rescued_skill = self._rescue_item_test_skill(explicit_target, skill_name, input_text)
            if rescued_skill:
                item_test_target = self._resolve_item_test_target(explicit_target, rescued_skill)
                skill_name = rescued_skill
        if not item_test_target:
            return None
        result = self._resolve_item_test(item_test_target, skill_name)
        result["input"] = input_text
        return result

    def _rescue_item_test_skill(self, target_name, skill_name, input_text):
        """!
        @brief A literal-keyword-only second opinion for an item-test target whose naively
            matched skill_name doesn't qualify against its own [entity.test] -- ex: "identify
            the dagger" resolves skill_name to "blades" in NLPCore's general embedding match,
            purely because the item's own name collides with blades' "dagger" keyword
            (skills.toml). NLPCore's map_to_action already has its own keyword-fallback
            mechanism for the general case, but it only ever competes with the *whole* skill
            corpus -- here the field is already narrowed to one entity's own declared
            test["skill"] list, so a plain literal check is enough and never needs to touch
            the embedding matcher at all. That's what keeps this safe: it can't misfire the
            way a second semantic pass could (ex: it correctly stays silent for "attack with
            my dagger", which has no literal arcane-keyword support at all, unlike "identify
            the dagger" or "is this dagger cursed").
        @param target_name The entity possibly carrying a [entity.test].
        @param skill_name The skill NLPCore already resolved -- if this already qualifies,
            nothing needs rescuing.
        @param input_text The player's raw input.
        @return An alternate skill name from the entity's own test["skill"] list with literal
            keyword support, or None.
        """
        test = self.entities.get(target_name, {}).get("test") if target_name else None
        if not test or not input_text or self.is_test_available(target_name, test, skill_name):
            return None
        for candidate_skill in test.get("skill", []):
            if candidate_skill == skill_name:
                continue
            keywords = self.skills.get(candidate_skill, {}).get("keywords", [])
            if any(re.search(rf"\b{re.escape(keyword)}\b", input_text) for keyword in keywords):
                return candidate_skill
        return None

    def _apply_target_redirect(self, explicit_target):
        """!
        @brief Honors an explicit, NLP-matched target as a combat redirect -- only if it
            names a live, hostile, in-scene entity. Naming a confidently-matched but
            non-hostile entity (ex: an ally) is silently ignored rather than making it the
            target; leaves self.current_target untouched if explicit_target doesn't qualify.
        @param explicit_target NLPCore's best-guess target name (map_to_target), or None.
        """
        if (
            explicit_target
            and explicit_target in self.scenario_entities
            and self.is_hostile(explicit_target, self.player_name)
            and self.get_current_hp(explicit_target) > 0
        ):
            self.current_target = explicit_target

    def _resolve_roll(self, skill_name, named_ability, target_name):
        """!
        @brief Rolls the actual check for this action: a flat difficulty check against the
            target's own [entity.test] if one applies (ex: a chest's lock), a range-gated
            opposed check against a live combat target, or an untargeted resolve_action if
            there's no target at all.
        @param skill_name The skill being used, already resolved from any named ability.
        @param named_ability The resolved ability entity (technique/spell), or None.
        @param target_name self.current_target, or None.
        @return (result, ability, via_test) -- ability is the attack ability resolved for the
            roll, if any, needed again by _apply_damage_if_hit; via_test is True if this was a
            flat [entity.test] check, which must never also roll bonus weapon damage.
        """
        test = self.entities.get(target_name, {}).get("test") if target_name else None
        via_test = False
        ability = None
        if test and self.is_test_available(target_name, test, skill_name):
            via_test = True
            # An entity's own [entity.test] (ex: a chest's lock) is a flat difficulty check,
            # not an opposed roll -- it doesn't compete via the attacker's skill's `opposes`
            # list the way a creature's defense does, and isn't subject to range either (you
            # have to be adjacent to a chest to be interacting with its lock at all). Any
            # *other* skill against this same target (ex: forcing the chest with "strength")
            # still falls through to the normal opposed-skill path below, e.g. resolved
            # against its "fortitude" if it has one.
            result = self.resolve_action(self.player_name, skill_name, test.get("difficulty", 0))
            result["defender"] = target_name
            result["opposing_skill"] = None
            outcome = test.get("pass") if result["success"] else test.get("fail")
            effects = self.apply_test_outcome(target_name, outcome) or {}
            loot = effects.get("loot")
            if loot and (loot["currency"] or loot["items"]):
                result["loot"] = loot
            damage = effects.get("damage")
            if damage:
                # A trap's failed disarm/dodge attempt (ex: dart trap, scythe trap) -- same
                # "damage" key LLM_Core._describe_outcome already renders for a normal weapon
                # hit, just sourced from the target's own [entity.test.fail] instead of an
                # attack roll.
                result["damage"] = damage
        elif target_name:
            # Looked up before rolling (not just for damage afterward, as before) so distance
            # can gate the attack roll itself -- is_in_range (DM_Movement.py) is a pure
            # reachability check, not a difficulty modifier (see its own module note for why
            # that per-tier accuracy idea was dropped). A skill with no matching ability
            # (ex: "charisma") always comes back in range (nothing physical to be out of
            # reach of), so this never gates a non-combat opposed check.
            ability = named_ability or self.find_attack_ability(self.player_name, skill_name)
            if not self.is_in_range(self.player_name, target_name, ability):
                result = {
                    "entity": self.player_name, "skill": skill_name, "roll": None, "difficulty": None,
                    "success": False, "defender": target_name, "opposing_skill": None,
                    "reason": "out_of_range",
                }
            else:
                result = self.resolve_opposed_action(self.player_name, skill_name, target_name)
        else:
            result = self.resolve_action(self.player_name, skill_name)
        return result, ability, via_test

    def _apply_damage_if_hit(self, result, skill_name, named_ability, ability, target_name, via_test):
        """!
        @brief Rolls and attaches bonus weapon/ability damage if the roll succeeded against a
            target and wasn't a flat [entity.test] check -- a test-path success (ex: a
            lockpick) must never also roll bonus weapon damage even if skill_name happens to
            match an equipped weapon/ability (ex: a future finesse-based dagger matching the
            chest's finesse-skill lock test).
        @param result The roll result from _resolve_roll, mutated in place with "damage" if hit.
        @param skill_name The skill being used, already resolved from any named ability.
        @param named_ability The resolved ability entity (technique/spell), or None.
        @param ability The attack ability from _resolve_roll, if already resolved there --
            only the test/no-target branches leave it None, in which case it's re-derived here.
        @param target_name self.current_target, or None.
        @param via_test True if the roll was a flat [entity.test] check.
        """
        if result["success"] and target_name and not via_test:
            if ability is None:
                ability = named_ability or self.find_attack_ability(self.player_name, skill_name)
            if ability:
                result["damage"] = self.calculate_damage(self.player_name, target_name, ability)

    def _attach_defender_details(self, result, target_name):
        """!
        @brief Attaches defender flavor text to the result -- belt-and-suspenders against the
            persistent character roster (see scenario_loaded's "characters" payload) ever
            being stale. Skips a still-hidden target (is_hidden -- ex: an unnoticed dart trap
            that current_target has fallen back to) for the same reason
            _describe_scenario_characters does: an action resolving against it must not leak
            its flavor text into narration before the player would actually have noticed it.
        @param result The roll result, mutated in place with "defender_details" if
            describe_character returns anything.
        @param target_name self.current_target, or None.
        """
        if target_name and not self.is_hidden(target_name):
            defender_details = self.describe_character(target_name, toward_name=self.player_name)
            if defender_details:
                result["defender_details"] = defender_details

    def _resolve_combat_round(self, result):
        """!
        @brief Runs every other living scene entity's own turn this round -- not just
            self.current_target -- so combat is mutual (a wolf biting back) and allies pull
            their weight too (ex: thane striking whatever the player is currently fighting).
            A hostile entity attacks the player; a non-hostile one (an ally) attacks
            self.current_target instead. An entity with no matching behavior (no behavior
            data, or none of its requirements currently hold, ex: it's already at 0 HP) simply
            doesn't act. Every actor's outcome is still resolved independently against the
            state at the start of the round -- initiative only orders how the round is
            presented, it doesn't make an earlier actor's roll affect a later one's.
        @param result The player's own roll result, mutated in place with "round",
            "initiative", and "turns".
        """
        self.round_number += 1
        result["round"] = self.round_number
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

    def _on_item_interaction_detected(self, data):
        """!
        @brief Event handler for a free-text item-interaction match (see NLPCore.map_to_item):
            "examine"/"take"/"give"/"trade"/"use"/"equip"/"unequip"/"drop" against a named
            item, "open"/"close" against the scene target itself, "advance"/"retreat"
            against the whole scene, or "move" (with a "direction") to take a declared exit
            to a different room of the current multi-room dungeon. Deliberately bypasses the
            whole skill/dice system -- none of these warrant a roll (see DM_Movement.py's
            module docstring for why movement specifically is deterministic, not a check).
            Publishes "item_interaction_resolved" either way, with enough detail for
            narration to explain a miss (locked, closed, not present, not takeable, not
            usable, not equippable, cant_equip, not_equipped, no exit, wrong band, blocked by
            enemies, ...) rather than staying silent.

            "take"/"trade" move an item from the target to the player; "give" moves one from
            the player to the target -- same transfer_item/transfer_currency primitives,
            just with source/destination swapped. "trade" additionally charges the item's
            TOML `value` as a price (denied outright if the player can't afford it, rather
            than a partial payment). "examine" and "open"/"close" never move anything;
            "advance"/"retreat" move every living scenario entity's distance at once (see
            advance_or_retreat in DM_Movement.py), not just one target; "move" replaces the
            whole current room (see _resolve_room_transition_intent/_find_room_exit); "use"
            consumes or activates an item already in the player's own inventory (see
            _resolve_use_intent), never a target's -- NLPCore's own keyword set for it today
            is just "drink"/"quaff" (potions), but the intent itself is the generic "use" so
            a future item type (ex: a wand) only ever needs new keywords, not new handling
            here. "equip"/"unequip" (see _resolve_equip_intent/_resolve_unequip_intent) only
            ever touch the player's own [entity.equipped] slot mapping, same "already in your
            own inventory" restriction as "use"; "drop" (see _resolve_drop_intent) moves an
            item out of inventory onto the current room/scene's own ground (see
            _current_ground_items), where a later "take"/"examine" can reach it again --
            checked ahead of the target/locked gate below, since a dropped item has no
            container guarding it. "formation_behind"/"formation_abreast" (see
            _resolve_formation_intent, CLAUDE.md's "Party formation") direct any currently-
            present party member named in the input -- or every one present, if none is named
            -- to a new follow_offset, taking effect immediately.
        @param data The item_interaction_detected payload from NLPCore
            ({intent, item_name, input, score}). "item_name" is None for "open"/"close",
            "advance"/"retreat", "formation_behind"/"formation_abreast", and "move", none of
            which act on a named item; "move" also carries a "direction" (ex: "forward", "right").
        """
        intent = data.get("intent")
        item_name = data.get("item_name")
        input_text = data.get("input")
        target_name = self._get_target_name()

        def resolved(found, **extra):
            self.event_bus.publish("item_interaction_resolved", {
                "intent": intent, "item_name": item_name, "input": input_text, "found": found, **extra,
            })
            self._publish_party_status()

        if intent in ("advance", "retreat"):
            # Unlike everything else this handler resolves, this isn't about target_name/
            # the locked gate at all -- see advance_or_retreat's own docstring (DM_Movement.py)
            # for why it shifts every living scenario entity's distance at once rather than
            # just the current target.
            moved = self.advance_or_retreat(intent)
            resolved(True, moved=moved)
            return

        if intent in ("formation_behind", "formation_abreast"):
            # Also unrelated to target_name/the locked gate -- directing the party has
            # nothing to do with any scene target at all.
            self._resolve_formation_intent(intent, input_text, resolved)
            return

        if intent == "move":
            # Also unrelated to target_name/the locked gate -- leaving the room entirely has
            # nothing to do with reaching a container's contents, same reasoning advance/
            # retreat above already follows.
            self._resolve_room_transition_intent(data.get("direction"), resolved)
            return

        if intent == "use":
            # Also unrelated to target_name/the locked gate -- using something already in the
            # player's own inventory has nothing to do with any scene target at all.
            self._resolve_use_intent(item_name, resolved)
            return

        if intent == "equip":
            self._resolve_equip_intent(item_name, resolved)
            return

        if intent == "unequip":
            self._resolve_unequip_intent(item_name, resolved)
            return

        if intent == "drop":
            self._resolve_drop_intent(item_name, resolved)
            return

        if intent in ("examine", "take") and item_name in self._current_ground_items():
            self._resolve_ground_intent(intent, item_name, resolved)
            return

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

    def _find_room_exit(self, room, direction):
        """!
        @brief Finds the current room's own declared [[room.exit]] usable right now for the
            given direction -- "usable" meaning both the direction matches *and* the player
            is currently standing in the exact band that exit is declared at (DM_Rules.py's
            room-graph notes). This band gate is what actually enables a branch: a room can
            declare more than one exit (ex: one "right" at band 2, another "forward" at band
            3), and which one resolves depends on where the player has actually moved to
            (via ordinary advance/retreat) within the room, not just which word they said.
        @param room The current room's own table, or None (a plain, non-room scenario).
        @param direction "forward"/"back"/"left"/"right" (see NLP_Core.py's DIRECTION_PHRASES).
        @return (exit_table, None) if a usable match was found; (None, "no_exit") if the
                room has no exit at all in that direction; (None, "wrong_band") if it does,
                but not from the player's current band.
        """
        if not room:
            return None, "no_exit"
        matching_direction = [e for e in room.get("exit", []) if e.get("direction") == direction]
        if not matching_direction:
            return None, "no_exit"
        player_band = self.get_band(self.player_name)
        for exit_def in matching_direction:
            if exit_def.get("band") == player_band:
                return exit_def, None
        return None, "wrong_band"

    def _resolve_formation_intent(self, intent, input_text, resolved):
        """!
        @brief Handles "formation_behind"/"formation_abreast" -- a player-issued party
            positioning command (ex: "stay behind me, anne"), covering CLAUDE.md's "Party
            formation" own noted gap: follow_offset already lived on the entity instance for
            exactly this, nothing previously wrote to it in play. Which party member(s) get
            addressed is resolved by a plain, whole-word, case-insensitive search of the raw
            input for any currently-in-scene party member's own name (NLPCore never parses
            this out itself -- unlike map_to_item/map_to_target's embedding matches, a party
            member's own name either is or isn't literally said, so there's no ambiguity worth
            spending a semantic match on) -- if none is named, every party member currently
            present is addressed instead, so a bare "stay behind me" still does something
            sensible. The new follow_offset takes effect immediately (_apply_party_formation),
            not just on the party's next move.
        @param intent "formation_behind" (follow_offset -1, one band back) or
            "formation_abreast" (follow_offset 0, walks alongside).
        @param input_text The raw (lowercased, prefix-stripped) player input, searched for
            party member names.
        @param resolved The item_interaction_resolved publisher closure from the caller.
        """
        offset = -1 if intent == "formation_behind" else 0
        stance = "behind" if intent == "formation_behind" else "abreast"

        party_present = [
            name for name in self.scenario_entities
            if name != self.player_name and self.entities.get(name, {}).get("is_party")
        ]
        named = [
            name for name in party_present
            if re.search(rf"\b{re.escape(name.lower())}\b", input_text or "")
        ]
        addressed = named or party_present

        if not addressed:
            resolved(False, reason="no_party")
            return

        for name in addressed:
            self.entities[name]["follow_offset"] = offset
        self._apply_party_formation()
        resolved(True, members=addressed, stance=stance)

    def _resolve_room_transition_intent(self, direction, resolved):
        """!
        @brief Handles "move" -- taking a declared exit to a different room of the current
            multi-room dungeon (see DM_Rules.py's "Scenario instancing"/room-graph notes and
            _find_room_exit). Denied (reason "no_exit") if the current room has no exit at
            all in that direction -- including every plain, single-room scenario (arena/
            tavern/field/dungeon), which has no rooms/exits to speak of and so always fails
            this check, same as a "close" aimed at a creature fails "not_openable"; denied
            (reason "wrong_band") if that direction exists in this room but not from the
            player's current band; denied (reason "blocked_by_enemies") if any living
            hostile creature remains in the current room -- a dungeon-crawl convention: a
            room is cleared before moving past it, not slipped around while something is
            still actively fighting the player.
        @param direction "forward"/"back"/"left"/"right", from NLPCore's direction match.
        @param resolved The item_interaction_resolved publisher closure from the caller.
        """
        exit_def, reason = self._find_room_exit(self._current_room(), direction)
        if exit_def is None:
            resolved(False, reason=reason)
            return

        for entity_name in self.scenario_entities:
            if entity_name == self.player_name:
                continue
            if self.is_hostile(entity_name, self.player_name) and self.get_current_hp(entity_name) > 0:
                resolved(False, reason="blocked_by_enemies")
                return

        self.enter_room(exit_def["destination"], exit_def.get("arrival_band", 1))
        new_room = self._current_room()
        resolved(
            True,
            room_name=new_room.get("name", "") if new_room else "",
            room_description=new_room.get("description", "") if new_room else "",
            characters=self._describe_scenario_characters(),
        )

    def _resolve_use_intent(self, item_name, resolved):
        """!
        @brief Handles "use" (today's only keywords are "drink"/"quaff", both potion-flavored
            -- see NLP_Core.py's USE_KEYWORDS) -- activating/consuming an item already in the
            player's own inventory. Deliberately never reaches a target's inventory the way
            "take" can -- you can't use something you haven't picked up yet; take/examine
            already exist for reaching a container's contents first. Gated on a truthy
            "usable" field (reason "not_usable" otherwise, ex: trying to use a sword) rather
            than any particular subtype, since this is meant to cover more than potions --
            a future wand (subtype "wand", not "potion") opts in the same way, just by
            carrying `usable = true` plus whatever effect fields it defines.

            The only effect actually implemented yet is healing, read from the item's own
            "healing" {dice, pips} skill stat if present (ex: health potion) and rolled
            through apply_healing (DM_Status.py) -- the healing counterpart to
            calculate_damage's own roll_dice usage. An item with no "healing" stat still
            "uses" successfully (consumes a charge, may still identify/replace itself below),
            it just has no numeric effect yet -- there's nothing else to trigger until a
            second effect type is actually built.

            Using it also identifies it, whether or not it already was -- you now know
            exactly what it does, from experience, which is a strictly stronger kind of
            knowledge than a prior appraise/medicine check (see items.toml's health potion
            and its own [entity.test]).

            Consumption is charge-based (see _consume_charge): an item with no "charges"
            field at all is single-use, spent entirely on this one call (every potion
            today); one carrying a "charges" count only depletes by one per use and keeps
            working until it hits zero (a future wand's whole reason to have this field
            rather than being single-use like a potion). Either way, once charges reach
            zero, the item is removed from inventory and swapped for whatever its own
            "replace_with" names (ex: health potion -> glass vial, an empty husk) -- an item
            with no "replace_with" just vanishes, the same as before this field existed.
        @param item_name NLPCore's best-guess item match (map_to_item), or None.
        @param resolved The item_interaction_resolved publisher closure from the caller.
        """
        player = self.entities.get(self.player_name, {})
        if not item_name or item_name not in player.get("inventory", []):
            resolved(False, reason="not_present")
            return
        item = self.entities.get(item_name, {})
        if not item.get("usable"):
            resolved(False, reason="not_usable")
            return

        healing = item.get("skills", {}).get("healing")
        healed = 0
        remaining_hp = self.get_current_hp(self.player_name)
        if healing:
            healed = self.roll_dice(healing.get("dice", 0), healing.get("pips", 0))
            remaining_hp = self.apply_healing(self.player_name, healed)

        self.apply_condition(item_name, "identified", duration="permanent", dismiss="")

        charges_left = self._consume_charge(item_name)
        replaced_with = None
        if charges_left <= 0:
            player["inventory"].remove(item_name)
            replace_with = item.get("replace_with")
            if replace_with:
                if replace_with in self.entities:
                    player["inventory"].append(replace_with)
                    replaced_with = replace_with
                else:
                    self.event_bus.publish(
                        "log_error", f"{item_name}'s replace_with names unknown entity: {replace_with}"
                    )

        resolved(
            True, healed=healed, remaining_hp=remaining_hp,
            charges_left=max(charges_left, 0), replaced_with=replaced_with,
        )

    def _resolve_equip_intent(self, item_name, resolved):
        """!
        @brief Handles "equip" -- moving an item already in the player's own inventory into
            whichever [entity.equipped] slot it's actually valid for (see
            InventoryMixin.equip_item/DM_Rules.py's get_equip_slots). Deliberately never
            reaches a target's inventory (same "take it first" rule _resolve_use_intent
            already follows) -- gear has to be picked up before it can be worn.
        @param item_name NLPCore's best-guess item match (map_to_item), or None.
        @param resolved The item_interaction_resolved publisher closure from the caller.
        """
        player = self.entities.get(self.player_name, {})
        if not item_name or item_name not in player.get("inventory", []):
            resolved(False, reason="not_present")
            return
        if not self.entities.get(item_name, {}).get("equip_slot"):
            resolved(False, reason="not_equippable")
            return
        slot, previous = self.equip_item(self.player_name, item_name)
        if slot is None:
            # equip_item only returns None here when resolve_equip_slot found no candidate
            # slot valid for the player's own supertype/subtype -- the "not_equippable" case
            # above already ruled out "declares no equip_slot at all".
            resolved(False, reason="cant_equip")
            return
        resolved(True, slot=slot, replaced=previous)

    def _resolve_unequip_intent(self, item_name, resolved):
        """!
        @brief Handles "unequip" -- clearing whichever [entity.equipped] slot item_name
            currently occupies on the player (see InventoryMixin.unequip_item). The item
            stays in inventory either way (an equipped item is always also listed there --
            see entity_schema.toml's own [entity.equipped] comment), so this never calls
            transfer_item.
        @param item_name NLPCore's best-guess item match (map_to_item), or None.
        @param resolved The item_interaction_resolved publisher closure from the caller.
        """
        slot = self.unequip_item(self.player_name, item_name) if item_name else None
        if slot is None:
            resolved(False, reason="not_equipped")
            return
        resolved(True, slot=slot)

    def _current_ground_items(self):
        """!
        @brief The mutable list of item names dropped in the current room/scene -- a room's
            own "ground" key for a multi-room dungeon (persists across a revisit the same way
            a cleared trap does -- see DM_Rules.py's room-graph notes), or the flat
            scenario's own "ground" key otherwise. Created empty on first use; never
            authored in TOML. Known gap: unlike scenario_entities, nothing here is written to
            or restored from a save slot yet (see DM_Persistence.py's "Saving and loading"),
            so a drop made since the last save doesn't survive a save/load round trip.
        @return The mutable ground list itself (not a copy) -- callers append/remove in place.
        """
        room = self._current_room()
        scope = room if room is not None else self.scenario
        return scope.setdefault("ground", [])

    def _resolve_drop_intent(self, item_name, resolved):
        """!
        @brief Handles "drop" -- moving an item out of the player's own inventory (clearing
            its own [entity.equipped] slot first, if it happened to be equipped) and onto the
            current room/scene's own ground (see _current_ground_items), where a later
            "take"/"examine" can reach it again -- unlike _resolve_use_intent's "use it up"
            consumption, dropping an item never destroys it.
        @param item_name NLPCore's best-guess item match (map_to_item), or None.
        @param resolved The item_interaction_resolved publisher closure from the caller.
        """
        player = self.entities.get(self.player_name, {})
        if not item_name or item_name not in player.get("inventory", []):
            resolved(False, reason="not_present")
            return
        self.unequip_item(self.player_name, item_name)
        player["inventory"].remove(item_name)
        self._current_ground_items().append(item_name)
        resolved(True)

    def _resolve_ground_intent(self, intent, item_name, resolved):
        """!
        @brief Handles "examine"/"take" once item_name is already confirmed to be sitting on
            the current room/scene's own ground (see _current_ground_items) -- checked ahead
            of target_name/the locked gate in _on_item_interaction_detected, since a dropped
            item has no container guarding it at all, unlike everything else "take"/"examine"
            can reach. "examine" only describes; "take" moves it into the player's own
            inventory the same way transfer_item would, just off the ground list instead of
            another entity's own inventory.
        @param intent "examine" or "take".
        @param item_name The item entity confirmed present in _current_ground_items().
        @param resolved The item_interaction_resolved publisher closure from the caller.
        """
        if intent == "examine":
            description = self.entities.get(item_name, {}).get("description", "")
            revealed = list(self.entities.get(item_name, {}).get("tags", [])) if self.is_identified(item_name) else []
            resolved(True, description=description, revealed=revealed)
            return
        self._current_ground_items().remove(item_name)
        self.entities.setdefault(self.player_name, {}).setdefault("inventory", []).append(item_name)
        resolved(True)

    def _consume_charge(self, item_name):
        """!
        @brief Decrements an item's own "charges" by one and returns what's left --
            _resolve_use_intent's only source of truth for whether this use was the item's
            last. An item with no "charges" field at all is single-use: treated as if this
            one use was already its only charge, so it always returns 0 (every potion today,
            since none declare "charges"). An item that does declare one (ex: a future wand)
            survives repeated uses until the count actually reaches zero.
        @param item_name The entity being used.
        @return The remaining charge count (0 or negative means "used up").
        """
        item = self.entities.get(item_name, {})
        if "charges" not in item:
            return 0
        item["charges"] -= 1
        return item["charges"]

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

    def _is_party_member(self, entity_name):
        """!
        @brief Whether entity_name is on the player's own side -- the player themselves
            (is_player = true) or an ally like crypt.toml's "thane" (is_party = true, same
            flag GUICore's Party tab keys off of). Neither _get_target_name nor
            _choose_combat_target's fallback should ever resolve to a party member -- an ally
            standing in the same scenario_entities list as the room's actual trap/chest/
            creature is never a valid default interaction or combat target.
        @param entity_name The entity to check.
        @return True if entity_name is the player or a flagged ally.
        """
        if entity_name == self.player_name:
            return True
        entity = self.entities.get(entity_name, {})
        return bool(entity.get("is_player") or entity.get("is_party"))

    def _get_target_name(self):
        """!
        @brief Picks the current opposed target from the instantiated scenario entities.
        @return The name of the first non-party entity instance in the scenario, or None if there isn't one.
        """
        for instance_name in self.scenario_entities:
            if not self._is_party_member(instance_name):
                return instance_name
        return None

    def _choose_combat_target(self):
        """!
        @brief Picks self.current_target: the first living, hostile-toward-the-player entity
            in scenario_entities order. If none qualifies (ex: every wolf is dead, or nothing
            in the scene was ever hostile -- the dungeon's chest, the tavern's innkeeper),
            falls back to the first living, non-party entity instead (ex: crypt.toml's own
            "dart trap"), so an ally (ex: "thane") is never mistaken for the scene's own
            trap/chest/NPC just because it happens to sit earlier in scenario_entities than
            the real one. Only once *no* such entity exists either does it fall back further,
            to the first living entity at all -- including an ally, so current_target still
            has somewhere to land (rather than going stale on a corpse) in a scene with
            nothing left but the player's own party. Unlike _get_target_name(), which returns
            the first non-party entity unconditionally (dead or not) and stays reserved for
            non-combat interaction resolution, where that's always correct since a chest/NPC
            is never "defeated". Used both to set the initial current_target (via
            load_scenario()) and to advance it once the previous target dies (see
            _on_action_detected's end-of-round check).
        @return The chosen entity name, or None if nothing in the scenario is alive.
        """
        for instance_name in self.scenario_entities:
            if instance_name == self.player_name:
                continue
            if self.is_hostile(instance_name, self.player_name) and self.get_current_hp(instance_name) > 0:
                return instance_name
        for instance_name in self.scenario_entities:
            if self._is_party_member(instance_name):
                continue
            if self.get_current_hp(instance_name) > 0:
                return instance_name
        for instance_name in self.scenario_entities:
            if instance_name == self.player_name:
                continue
            if self.get_current_hp(instance_name) > 0:
                return instance_name
        return None
