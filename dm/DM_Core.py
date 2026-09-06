import copy
import os
import re

from dm.DM_ActionOutcome import (
    ActionPreventedOutcome, CureEffect, DamageEffect, DefenderDetailsEffect, DispelEffect, LanguageBarrierOutcome,
    LootEffect, MissingSpellMaterialsOutcome, OutOfRangeOutcome, RevealEffect, RolledOutcome, SummonEffect,
    TeleportEffect, rolled_outcome_from_roll,
)
from dm.DM_CharacterCreation import CharacterCreationMixin
from dm.DM_Combat import CombatMixin
from dm.DM_Crafting import CraftingMixin
from dm.DM_Dialogue import DialogueMixin
from dm.DM_Encounters import EncounterMixin
from dm.DM_Help import HelpMixin
from dm.DM_Improvisation import ImprovisationMixin
from dm.DM_Inventory import InventoryMixin
from dm.DM_Movement import MovementMixin
from dm.DM_NpcGeneration import NpcGenerationMixin
from dm.DM_Persistence import PersistenceMixin
from dm.DM_Rules import RulesMixin, scenario_file_path
from dm.DM_Social import SocialMixin
from dm.DM_Status import StatusMixin
from dm.DM_Summoning import SummoningMixin
from dm.DM_Time import TimeMixin
from dm.DM_Travel import TravelMixin
from dm.DM_Validation import ValidationMixin
from intents.registry import HANDLERS as FREE_STANDING_INTENT_HANDLERS
from resolution.Combat_Resolution import matches_supertype_or_subtype
from resolution.Program_Interpreter import run_program

# Multi-instance combat targeting (see DMCore._resolve_named_instance_ambiguity): NLPCore's own
# map_to_target (NLP_Core.py) picks one specific live instance name by raw text similarity to
# that instance's own registered name/description phrases -- given two identically-templated
# creatures ("wolf"/"wolf_2"), it has no way to prefer one over the other just because the
# player said "the second wolf"/"the other wolf"/"the wounded wolf", since none of those
# qualifier words are part of any registered phrase. These are deliberately small, literal
# keyword sets (matching this codebase's own TRAVEL_KEYWORDS/DIALOGUE_KEYWORDS convention in
# Intent_Classification.py) rather than a general sentiment/adjective system.
TARGET_ORDINAL_KEYWORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
TARGET_OTHER_KEYWORDS = ("other", "another")
TARGET_WOUNDED_KEYWORDS = ("wounded", "hurt", "injured")
TARGET_HEALTHY_KEYWORDS = ("healthy", "unhurt", "uninjured", "unharmed")
# The same 0.40 hp_per_remain cutoff rules.toml's own "wounded" status tier -- and debug.toml's
# wolf retreat behavior -- already use elsewhere in this codebase (see CLAUDE.md's "Combat"),
# reused here rather than inventing a second threshold. A candidate has to actually cross this
# line before "wounded"/"healthy" is honored -- calling a room full of undamaged creatures
# "wounded" shouldn't silently redirect to whichever one merely has the least HP among equals.
TARGET_WOUNDED_HP_CUTOFF = 0.40

class DMCore(InventoryMixin, SocialMixin, StatusMixin, CombatMixin, MovementMixin, RulesMixin, PersistenceMixin, CharacterCreationMixin, NpcGenerationMixin, DialogueMixin, HelpMixin, ImprovisationMixin, EncounterMixin, SummoningMixin, CraftingMixin, ValidationMixin, TimeMixin, TravelMixin):
    """!
    @brief Main class handling the core mechanics of the RPG system. The implementation is
        composed from domain mixins in sibling files -- DM_Rules.py (rules/scenario
        loading), DM_Combat.py (dice/damage/ability resolution), DM_Status.py (the
        status/condition system and entity tests), DM_Inventory.py (currency/item
        transfer, plus the equip/drop/use/container item-interaction intents), DM_Social.py
        (attitudes and character description), DM_Movement.py (distance tracking,
        advance/retreat, range-based difficulty, plus the room-transition/location-travel/
        formation item-interaction intents), DM_Persistence.py (save/load), DM_CharacterCreation.py
        (baking a finished character-creation result -- race + point-buy skill allocation,
        see Character_Creation.py's own module docstring -- onto the player entity),
        DM_NpcGeneration.py (turning a generate=true template into a real stat block at
        instancing time, see NPC_Generation.py's own module docstring), DM_Dialogue.py
        (resolving who's being directly addressed in free-form conversation), DM_Help.py
        (the reserved "ADaM" out-of-character help channel -- see its own module docstring),
        DM_Improvisation.py (ad hoc entity creation/removal via LLM function calling, see
        its own module docstring and AdHoc_Generation.py), DM_Encounters.py (resolving a
        location/room's own [[location.encounter]] weighted-choice table on entry -- see its
        own module docstring), DM_Summoning.py (a spell/ability's own "summon" field --
        conjuring a real, hand-authored entity as a temporary ally, and expiring it after its
        own duration in combat rounds -- see its own module docstring), and DM_Validation.py
        (load-time referential-integrity checks over everything load_rules/
        load_scenario_definition just loaded -- see its own module docstring) -- so that every
        dm_core.<method>(...) call site throughout the codebase and
        test_all.py keeps working unchanged regardless of which file actually defines a given
        method (Python's MRO flattens every mixin method onto this one class). DM_Core.py
        itself is reduced to __init__ (boot wiring) plus the three real event handlers,
        _on_turn_detected, _on_item_interaction_detected, and _on_dialogue_detected, and their
        direct helpers -- the pieces that orchestrate calls across every mixin and don't
        belong to any single domain. _on_turn_detected also calls _on_item_interaction_detected
        directly, once per item-kind clause in a mixed turn -- see its own "Multiple actions"
        docstring.
    """

    def __init__(self, event_bus, scenario_name="debug", character=None, setting="Fantasy", start_location=None):
        """!
        @brief Initializes the DM core and loads system references.
        @param event_bus The central event bus instance.
        @param scenario_name Which scenario to load, matching a file in
            Rules/<setting>/scenarios/ (ex: "debug" loads scenarios/debug.toml).
        @param start_location Overrides the loaded scenario's own [scenario].start_location --
            None (the default) leaves it untouched. Lets a caller land in one specific area of
            a multi-area scenario file (ex: "debug"'s own "arena_grounds") without needing a
            dedicated scenario file per area -- the same override
            DMTestCase._load_ad_hoc_scenario (tests/test_unit.py) already applies by hand today,
            just as a constructor param. Applied right after load_scenario_definition, before
            load_scenario() actually instances anything.
        @param character An optional finished character-creation result
            ({"race": race_name, "allocation": {skill_name: dice_int}}), applied to the
            player entity via apply_character_creation (DM_CharacterCreation.py) right
            after self.player_name is resolved and the scenario's own TOML data is loaded
            (load_scenario_definition), but before any of it is actually instanced
            (load_scenario) -- see that method's own docstring for why. None (the
            default) leaves the player template's own hand-authored skills untouched --
            every existing caller that doesn't pass this keeps working exactly as before.
        @param setting Which Rules/ subdirectory to load everything from (skills/entities/
            rules/scenarios) -- each setting is a self-contained, independently-authored TOML
            data pack (see CLAUDE.md's own top-level architecture note); nothing about the
            engine itself is fantasy-specific, "Fantasy" is just the one setting shipped as
            the default (every existing caller that doesn't pass this keeps loading from
            Rules/Fantasy exactly as before). "Zombie" is a second, bare-bones setting proving
            that out.
        """
        self.event_bus = event_bus
        self.setting = setting
        self.skills = {}
        self.entities = {}
        # Stub templates for NPC generation (see NPC_Generation.py/DM_NpcGeneration.py,
        # CLAUDE.md's "NPC generation") -- kept in their own namespace, loaded from
        # [[entity_template]] tables (declared inline in a scenario file, ex:
        # Rules/Fantasy/scenarios/debug.toml -- or any flat Rules/<setting>/*.toml
        # file via load_rules, for a genuinely shared one), not [[entity]] ones, so they can
        # never collide with (or be mistakenly referenced as) a real, directly usable
        # entity/creature template in self.entities. A scenario/room entry opts into one via
        # its own "template" field, never "name" -- see DM_Rules.py's _instance_entities.
        self.entity_templates = {}
        self.scenario = {}
        self.scenario_entities = []
        # Populated for real by load_scenario_definition -- location_key -> location table (a
        # place: a town square, a building, a dungeon -- see CLAUDE.md's "Scenarios and rooms").
        # self.current_location_key/self.location_runtime track which one is active and each
        # location's own per-visit instancing cache; self.rooms/self.current_room_key/
        # self.visited_rooms/self.persistent_entities keep their exact pre-existing meaning, but
        # now describe the *current location's* own state, re-pointed by DM_Rules.py's
        # _enter_location every time the active location changes rather than fixed once at
        # scenario-load time (see that method's own docstring).
        self.locations = {}
        self.current_location_key = None
        self.location_runtime = {}
        # Grid-based travel's own knowledge gate (see DM_Travel.py/docs/downtime.md's "Travel")
        # -- every location key ever entered, plus whatever a scenario's own [scenario].
        # known_locations seeds ahead of time. Round-tripped through save_game/load_game the
        # same way removed_entities is.
        self.known_locations = set()
        self.persistent_entities = []
        self.rooms = {}
        self.current_room_key = None
        self.visited_rooms = {}
        # Instance names forcibly removed from the scene via ImprovisationMixin's
        # remove_entity_from_scene (DM_Improvisation.py) -- consulted by DM_Rules.py's
        # _instance_entities so a removed hand-authored entity never respawns on a later room
        # revisit or a reload. Must be set before load_scenario()/load_scenario_definition()
        # below, both of which call _instance_entities.
        self.removed_entities = set()
        self.rules = {}
        self.round_number = 0
        # The block clock (see docs/downtime.md / DM_Time.py) -- a single monotonic counter of
        # every 8-hour (by default) "block" elapsed since the scenario started, a fully separate
        # axis from round_number above (tactical/per-turn vs. strategic/per-downtime-action).
        # Round-tripped through save_game/load_game the same way round_number already is.
        self.current_block = 0
        # Night watch's own "fixed rotation, not always the same member" cursor
        # (docs/extended-goals.md's "Night watch and surprise") -- an index into whichever
        # is_party members are present at the time, advanced once per night block a watch is
        # actually rolled (see DM_Travel.py's _roll_night_watch). Round-tripped through
        # save_game/load_game the same way current_block already is.
        self.watch_rotation_index = 0
        # A paused downtime action (grid travel or rest) waiting on a hostile encounter to
        # clear before it resumes -- see docs/downtime.md's "Pausing for a fight". None
        # whenever nothing is interrupted (the overwhelmingly common case). Shape while set:
        # {"kind": "travel"|"rest", "blocks_done", ...kind-specific fields} -- "rest" also
        # carries "blocks_total" (fixed up front, nothing moves during a rest); "travel"
        # instead carries "distance"/"distance_covered" (DM_Travel.py's _advance_pending_travel),
        # since terrain/roads can make its own block count vary along the route.
        # -- plain JSON-safe data only, no live object references, so it round-trips through
        # save_game/load_game exactly like current_block.
        self.pending_downtime = None
        # The player's persisted combat target -- distinct from _get_target_name()'s "first
        # non-player entity" (which stays purely for non-combat interaction resolution, ex:
        # the dungeon's chest or the tavern's innkeeper). Set for real by load_scenario()
        # (via _choose_combat_target()) once entities/scenario are actually loaded below.
        self.current_target = None
        # The filename passed to load_scenario_definition -- distinct from self.scenario's
        # own "name" field (a display string, ex: "The Arena") -- kept so save_game/load_game
        # know which scenarios/*.toml file a saved slot belongs to.
        self.scenario_key = scenario_name
        self.load_rules(os.path.join("Rules", self.setting))
        # No party/character selection exists yet, so the one entity template marked
        # is_player = true (characters.toml's gladstone) stands in as the active player
        # character. Resolved once here, from templates, rather than per-scenario-load, so
        # ad-hoc test scenarios that omit gladstone entirely still keep the same player_name
        # they booted with.
        self.player_name = self._resolve_player_name()
        # load_scenario_definition before apply_character_creation (reversed from this
        # project's earlier ordering) so a character-creation rename's own collision check
        # sees scenario-local entities too, not just the shared Rules/<setting>/*.toml
        # catalog -- see DM_CharacterCreation.py's own docstring for why the rename still
        # has to land before load_scenario() itself (the actual instancing step) either way.
        self.load_scenario_definition(scenario_name)
        if start_location is not None:
            self.scenario["start_location"] = start_location
        # A fresh start only -- load_game sets current_block from the save file directly and
        # never calls __init__ again, so this never runs on a reload (see DM_Time.py's own
        # docstring for why re-deriving this here would otherwise clobber a restored save).
        self._seed_starting_date()
        self.apply_character_creation(character)
        self.validate_loaded_data()
        self.load_scenario()
        self.event_bus.publish("log_info", "DMCore initialized.")
        self.event_bus.publish("rules_loaded", {
            "skills": self.skills,
            "entities": self.entities,
            "equip_slots": self.rules.get("equip_slot", []),
            "scenario_entities": self.scenario_entities,
        })
        self.event_bus.publish("scenario_loaded", {
            # For a multi-room dungeon this narrates the *starting room* specifically (ex:
            # "Entrance Hall"), not just the dungeon's own overall blurb -- see
            # _current_scene_name/_current_scene_description in DM_Rules.py.
            "name": self._current_scene_name(),
            "description": self._current_scene_description(),
            "characters": self._describe_scenario_characters(),
            # Room-level presence snapshot (see DM_Dialogue.py's own module docstring and
            # LLMCore._filter_present_history) -- who was actually here to witness this
            # narration, tagged onto every DM-published narration-triggering event the same
            # way (see _on_turn_detected/_on_item_interaction_detected/_on_dialogue_detected
            # below), so a later per-entity dialogue query can tell what a given NPC has
            # actually seen apart from the DM's own always-omniscient narration.
            "present_entities": list(self.scenario_entities),
        })
        self.event_bus.subscribe("turn_detected", self._on_turn_detected)
        self.event_bus.subscribe("item_interaction_detected", self._on_item_interaction_detected)
        self.event_bus.subscribe("dialogue_detected", self._on_dialogue_detected)
        self.event_bus.subscribe("help_detected", self._on_help_detected)
        self.event_bus.subscribe("improvisation_requested", self._on_improvisation_requested)
        self.event_bus.subscribe("save_requested", self._on_save_requested)
        self.event_bus.subscribe("load_requested", self._on_load_requested)

    def _on_turn_detected(self, data):
        """!
        @brief Event handler that resolves everything the player attempted this turn --
            item interactions and skill/ability actions alike, arbitrarily mixed in one input
            -- opposed by the player's current combat target where one applies, and applies
            damage for each action that hit with an attack ability. Combat (the player's own
            current_target ends the turn hostile toward the player) narrates once per round
            via "round_resolved"; everything else narrates immediately via "action_resolved" --
            exactly once either way, no matter how many actions the player attempted, since
            _resolve_combat_round is only ever called once, after every one of the player's own
            skill/ability actions has already resolved (see "Multiple actions" below).

            **Multiple actions.** NLPCore splits a single input into one or more independent
            clauses (see NLP_Core.py's ACTION_CLAUSE_PATTERN/_split_action_clauses) and
            classifies each as either an item interaction or a skill/ability action, publishing
            both kinds together in one "clauses" list -- always plural, even for the
            overwhelmingly common single-clause case, so this handler, DM_Combat.py's
            resolve_action/resolve_opposed_action, and every "action_resolved"/"round_resolved"
            consumer (LLM_Core.py) are all built around one consistent shape. This is the West
            End Games D6 "multiple actions" rule: a character may attempt as many actions as
            they want in one turn, but every action beyond the first (movement and speech
            excepted -- neither ever reaches this handler at all, see DM_Movement.py/
            DM_Dialogue.py) costs every one of that turn's actions a cumulative -1D. Drawing a
            weapon, picking something up, giving/trading/opening/using an item are all "an
            action" the same way swinging a sword is -- item interactions cost a turn slot in
            `clauses` exactly like a skill/ability entry does, they just never receive
            dice_penalty themselves, since they never rolled anything to begin with (same
            treatment a future reload entry would get). dice_penalty below is computed from the
            *combined* clause count -- 0 for a single clause of either kind (unchanged from
            before this mechanic existed), N-1 for N. Diceless item-*test* outcomes (ex:
            picking a lock) still go through the skill/ability loop below and do receive
            dice_penalty, since they do roll dice -- distinct from an item *interaction*
            (give/take/equip/...), which is resolved via the existing, entirely unchanged
            _on_item_interaction_detected (its own resolution logic never rolled anything, so
            it needs no dice_penalty awareness at all -- it's simply called once per item-kind
            clause here instead of being the sole top-level handler for a whole input). A craft
            attempt (DM_Crafting.py) is a third case: item-*named* like an interaction (its
            item_name is the recipe/result item map_to_item resolved), but dice-rolling and
            dice_penalty-subject like an item test -- handled by its own explicit branch in the
            loop below rather than either existing path.
        @param data The turn_detected payload from NLPCore ({clauses: [{kind: "item", intent,
            item_name} | {kind: "action", skill, score, target?, modifier?}, ...], input}).
            Item-kind clauses resolve immediately, in clause order, via
            _on_item_interaction_detected (narrating right away); action-kind clauses
            accumulate into one batch, resolved and narrated together exactly the way a lone
            action always has. Each action entry's "skill" is usually a plain skill name, but
            may also be a named technique/spell the player owns (ex: "cleave") --
            resolve_named_ability/select_ability_skill are what convert that into the skill
            it's actually rolled with, while keeping the named ability itself to use directly
            for damage further down. Each action entry's own "target", if present, is NLPCore's
            best-guess entity name match for that one clause (see map_to_target) -- honored as
            an item-test target (see _resolve_item_test_target) if it names a reachable,
            testable item; otherwise as a combat redirect if it names a live, hostile, in-scene
            entity; otherwise the persisted self.current_target is left alone. Each action
            entry's own optional "modifier" names a trained combat-trick/metamagic [[entity]]
            (ex: "power attack", "empowered") NLPCore matched literally within the same clause,
            stripped out before the ordinary skill/ability match ran on the remainder (see
            NLP_Core.py's own literal-name pre-pass) -- _resolve_action_modifier resolves it the
            same owned-ability way resolve_named_ability already does for skill_name.
        """
        clauses = data.get("clauses")
        if not clauses:
            return
        # Once per real player turn, regardless of whether it turns out to be item-only,
        # action-only, or mixed -- the current location/room's own "ambient" [[location.
        # encounter]] entries (DM_Encounters.py), if any, get their repeating per-turn roll
        # before anything else this turn resolves, the same "encounter resolves, then the
        # player's own action proceeds against whatever state now exists" timing an "on_enter"
        # roll already has relative to the very first action taken in a freshly-entered room.
        self._resolve_ambient_encounter()
        input_text = data.get("input")
        # Every clause this turn -- item interaction or skill/ability action alike -- shares
        # the same cumulative -1D economy (see this method's own "Multiple actions" note). 0
        # for a single clause, unchanged from every existing call site's behavior before this
        # mechanic existed.
        dice_penalty = max(0, len(clauses) - 1)

        player_actions = []
        # Whether any action-kind clause this turn actually went through target-based
        # resolution, as opposed to every one of them being an item test (which never engages
        # self.current_target at all, and must never trigger a round just because that
        # persistent, cross-turn field happens to already be hostile from some earlier turn --
        # ex: identifying a potion while a dead-but-still-nominally-current wolf sits in
        # self.current_target). Item-*interaction* clauses (resolved separately, below) never
        # set this either -- they never touch self.current_target at all.
        engaged_combat_target = False
        for entry in clauses:
            if entry.get("kind") == "item":
                if entry.get("intent") == "craft":
                    # A craft attempt is item-*named* (map_to_item resolved the recipe/result
                    # item, same as any other item-kind clause) but genuinely rolls dice
                    # against a real difficulty (DM_Crafting.py) -- unlike every other item
                    # interaction, which never rolls anything. It shares the turn's own
                    # dice_penalty pool and narrates through the batched action_resolved/
                    # round_resolved path below, same as an item test's own roll already does,
                    # rather than through the diceless item_interaction_resolved path. It
                    # deliberately never touches self.current_target, so it doesn't set
                    # engaged_combat_target below -- same as an item test's own roll.
                    craft_result = self._try_craft_action(entry.get("item_name"), dice_penalty)
                    player_actions.append(craft_result)
                    continue
                # Resolved via the exact same, unchanged item-interaction pipeline a bare
                # item_interaction_detected event already uses -- narrates immediately, in
                # clause order, rather than joining player_actions below, which only ever
                # covers the skill/ability portion of the turn.
                self._on_item_interaction_detected({
                    "intent": entry.get("intent"), "item_name": entry.get("item_name"), "input": input_text,
                })
                continue

            skill_name = entry.get("skill")
            if not skill_name:
                continue
            skill_name, named_ability = self._resolve_action_skill(skill_name)
            modifier = self._resolve_action_modifier(entry.get("modifier"))

            explicit_target = entry.get("target")
            item_result = self._try_item_test_action(explicit_target, skill_name, input_text, dice_penalty)
            if item_result is not None:
                player_actions.append(item_result)
                continue

            self._apply_target_redirect(explicit_target, input_text)
            target_name = self.current_target
            engaged_combat_target = True

            result, ability, via_test = self._resolve_roll(skill_name, named_ability, target_name, dice_penalty, modifier)
            self._finish_rolled_outcome(result, skill_name, named_ability, ability, target_name, via_test, input_text)
            player_actions.append(result)

        if not player_actions:
            return

        result = {
            "actions": player_actions,
            "input": input_text,
            # Room-level presence snapshot -- see scenario_loaded's own publish for why every
            # DM-published narration-triggering event carries one.
            "present_entities": list(self.scenario_entities),
        }

        # Checked once, after every one of the player's own skill/ability actions this turn
        # has already resolved (and any explicit per-action target redirect has already
        # happened) -- not per action, which is what actually keeps a multi-action turn to
        # exactly one round (see this method's own "Multiple actions" note).
        if (
            engaged_combat_target
            and self.current_target is not None
            and self.is_hostile(self.current_target, self.player_name)
        ):
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
            action; "party_status_changed" carries the same "entities"/"equip_slots"/
            "scenario_entities" shape but only GUICore listens for it.
        """
        self.event_bus.publish("party_status_changed", {
            "entities": self.entities,
            "equip_slots": self.rules.get("equip_slot", []),
            "scenario_entities": self.scenario_entities,
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

    def _resolve_action_modifier(self, modifier_name):
        """!
        @brief Resolves a clause's own "modifier" field (ex: "power attack", "empowered") down
            to the [[entity]] it names -- reusing resolve_named_ability's own owned/universal
            check unchanged, the same one a named technique/spell already goes through. A
            combat-trick/metamagic modifier is deliberately never added to skills.toml's own
            universal abilities list, so this only ever succeeds for an entity that actually
            trained it (its own "abilities" list) -- resolve_named_ability's "you don't have to
            be trained" fallback simply never fires for one of these.
        @param modifier_name The candidate modifier name from the clause (NLPCore's own literal
            name match against every live supertype == "modifier" entity), or None.
        @return The resolved modifier [[entity]] table, or None if absent/untrained.
        """
        if not modifier_name:
            return None
        return self.resolve_named_ability(self.player_name, modifier_name)

    def _apply_ability_modifier(self, ability, modifier):
        """!
        @brief Builds a per-cast copy of ability with modifier's own damage benefit folded in
            -- never mutates the shared spells.toml/creatures.toml/weapon entity itself, the
            same "ephemeral copy" precedent multi-action dice_penalty already keeps (it's
            recomputed per turn, never stored on anything). modifier's own cost half
            (skill_divisor) isn't applied here at all -- it's returned to the caller
            (_resolve_roll) to forward into resolve_action/resolve_opposed_action directly,
            since that's a roll-time parameter, not an ability field.
        @param ability The resolved weapon/spell/technique table, already confirmed to match
            modifier's own "applies_to" filter.
        @param modifier The resolved combat-trick/metamagic [[entity]] (ex: "power attack",
            "empowered") -- "damage_bonus" ({dice, pips, bonus}, added) and "damage_multiplier"
            (a plain number, applied after) are both optional; an ability with no damage_value
            at all (ex: a pure debuff spell) has nothing for either to act on, so it's simply
            returned unchanged -- the same "wrong shape just wastes it" precedent cure/dispel
            already set for a mismatched target.
        @return A deep copy of ability, its own "damage_value" adjusted if present.
        """
        modified = copy.deepcopy(ability)
        damage_value = modified.get("damage_value")
        if not damage_value:
            return modified

        damage_bonus = modifier.get("damage_bonus")
        if damage_bonus:
            damage_value["dice"] = damage_value.get("dice", 0) + damage_bonus.get("dice", 0)
            damage_value["pips"] = damage_value.get("pips", 0) + damage_bonus.get("pips", 0)
            if isinstance(damage_value.get("bonus", 0), (int, float)):
                damage_value["bonus"] = damage_value.get("bonus", 0) + damage_bonus.get("bonus", 0)

        damage_multiplier = modifier.get("damage_multiplier")
        if damage_multiplier:
            damage_value["dice"] = damage_value.get("dice", 0) * damage_multiplier
            damage_value["pips"] = damage_value.get("pips", 0) * damage_multiplier
            if isinstance(damage_value.get("bonus", 0), (int, float)):
                damage_value["bonus"] = damage_value.get("bonus", 0) * damage_multiplier

        return modified

    def _try_item_test_action(self, explicit_target, skill_name, input_text, dice_penalty=0):
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
        @param dice_penalty Forwarded to _resolve_item_test -- an item test still rolls dice
            (see resolve_action's own docstring), so it shares this turn's multi-action
            penalty exactly like an opposed roll does (see _on_turn_detected's own
            "Multiple actions" note).
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
        result = self._resolve_item_test(item_test_target, skill_name, dice_penalty)
        result.input = input_text
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

    def _apply_target_redirect(self, explicit_target, input_text=""):
        """!
        @brief Honors an explicit, NLP-matched target as a combat redirect -- only if it
            names a live, hostile, in-scene entity. Naming a confidently-matched but
            non-hostile entity (ex: an ally) is silently ignored rather than making it the
            target; leaves self.current_target untouched if explicit_target doesn't qualify.
            Resolves multi-instance ambiguity (see _resolve_named_instance_ambiguity) first,
            so a disambiguating word in input_text can redirect explicit_target to a same-
            family sibling instance before the hostile/alive checks below ever run.
        @param explicit_target NLPCore's best-guess target name (map_to_target), or None.
        @param input_text The player's raw turn input, forwarded to
            _resolve_named_instance_ambiguity.
        """
        explicit_target = self._resolve_named_instance_ambiguity(explicit_target, input_text)
        if (
            explicit_target
            and explicit_target in self.scenario_entities
            and self.is_hostile(explicit_target, self.player_name)
            and self.get_current_hp(explicit_target) > 0
        ):
            self.current_target = explicit_target

    def _instance_family(self, entity_name):
        """!
        @brief Strips DM_Rules.py's own _unique_entity_key "_<N>" disambiguating suffix, if
            present, to recover the shared base name multiple live instances of the same
            template were instanced under (ex: "wolf_2" -> "wolf"). A name with no numeric
            suffix -- including one that was never actually duplicated -- returns unchanged.
        @param entity_name An entity's own self.entities key.
        @return The base name this instance's family is keyed by.
        """
        match = re.match(r"^(.*)_(\d+)$", entity_name)
        return match.group(1) if match else entity_name

    def _live_instances_sharing_family(self, entity_name):
        """!
        @brief Every living, in-scene entity sharing entity_name's own instance family (see
            _instance_family), in stable creation order -- the bare base name first (if
            still alive), then "_2", "_3", ... by DM_Rules.py's own _unique_entity_key
            numbering. A name with no live duplicates returns a single-element list.
        @param entity_name Any live entity name -- the base name or a "_N" instance alike.
        @return The ordered list of same-family living entity names.
        """
        family = self._instance_family(entity_name)

        def _suffix(name):
            match = re.match(r"^.*_(\d+)$", name)
            return int(match.group(1)) if match else 1

        candidates = [
            name for name in self.scenario_entities
            if self._instance_family(name) == family and self.get_current_hp(name) > 0
        ]
        candidates.sort(key=_suffix)
        return candidates

    def _resolve_named_instance_ambiguity(self, explicit_target, input_text):
        """!
        @brief Re-checks input_text for a disambiguating word whenever explicit_target has
            one or more living same-family siblings still in the scene (ex: a second
            "wolf") -- map_to_target's own embedding match has no way to prefer a sibling
            just because the player said "the second wolf"/"the other wolf"/"the wounded
            wolf" instead of the plain species name, since none of those qualifier words
            are part of any registered target phrase (see NLP_Core.py's own on_rules_loaded).
            A single live instance (the overwhelmingly common case) short-circuits
            immediately with no further work. Checked in order -- ordinal, then other/
            another, then wounded, then healthy -- and falls back to explicit_target
            unchanged if input_text carries none of them, or if a wounded/healthy claim
            doesn't actually match any candidate's real HP (see TARGET_WOUNDED_HP_CUTOFF).
        @param explicit_target NLPCore's best-guess target name, or None.
        @param input_text The player's raw turn input.
        @return explicit_target, or a same-family sibling instance name input_text actually
            pointed at.
        """
        if not explicit_target:
            return explicit_target
        candidates = self._live_instances_sharing_family(explicit_target)
        if len(candidates) <= 1:
            return explicit_target

        text = input_text.lower()

        for word, position in TARGET_ORDINAL_KEYWORDS.items():
            if position <= len(candidates) and re.search(rf"\b{word}\b", text):
                return candidates[position - 1]

        if any(re.search(rf"\b{word}\b", text) for word in TARGET_OTHER_KEYWORDS):
            others = [name for name in candidates if name != self.current_target]
            if others:
                return others[0]

        if any(re.search(rf"\b{word}\b", text) for word in TARGET_WOUNDED_KEYWORDS):
            wounded = [
                name for name in candidates
                if (self.get_comparable_value(name, "hp_per_remain") or 1.0) < TARGET_WOUNDED_HP_CUTOFF
            ]
            if wounded:
                return min(wounded, key=lambda name: self.get_comparable_value(name, "hp_per_remain"))

        if any(re.search(rf"\b{word}\b", text) for word in TARGET_HEALTHY_KEYWORDS):
            healthy = [
                name for name in candidates
                if (self.get_comparable_value(name, "hp_per_remain") or 0.0) >= TARGET_WOUNDED_HP_CUTOFF
            ]
            if healthy:
                return max(healthy, key=lambda name: self.get_comparable_value(name, "hp_per_remain"))

        return explicit_target

    def _resolve_roll(self, skill_name, named_ability, target_name, dice_penalty=0, modifier=None):
        """!
        @brief Rolls the actual check for this action: a flat difficulty check against the
            target's own [entity.test] if one applies (ex: a chest's lock), a range-gated flat
            check against the ability's own authored "difficulty" if it has one and no
            [entity.test] matched (ex: spells.toml's "suggestion"/"fireball"), a range-gated
            opposed check against a live combat target otherwise, or an untargeted
            resolve_action if there's no target at all.
        @param skill_name The skill being used, already resolved from any named ability.
        @param named_ability The resolved ability entity (technique/spell), or None.
        @param target_name self.current_target, or None.
        @param dice_penalty Forwarded to resolve_action/resolve_opposed_action for the
            player's own roll only -- see _on_turn_detected's own "Multiple actions" note.
            0 for an ordinary single action, unchanged from this method's behavior before that
            mechanic existed.
        @param modifier A trained combat-trick/metamagic [[entity]] (ex: "power attack",
            "empowered"), already resolved by _resolve_action_modifier, or None. Only ever
            consulted in the range-gated target branch below, once the actual attack ability is
            known -- see _apply_ability_modifier for how its own "skill_divisor"/"damage_bonus"/
            "damage_multiplier" fields are folded in.
        @return (result, ability, via_test) -- ability is the attack ability resolved for the
            roll (a per-cast copy with modifier's own damage bonus/multiplier already applied,
            if one matched), needed again by _apply_damage_if_hit; via_test is True if this was
            a flat [entity.test] check, which must never also roll bonus weapon damage.
        """
        # Checked ahead of even materials -- an entity unable to act at all this turn (ex: a
        # "pinned" condition, prevents_action = true) can't cast/attack/anything, the most
        # fundamental "can't do it, don't roll" gate of all.
        if self.is_action_prevented(self.player_name):
            return ActionPreventedOutcome(self.player_name, skill_name), None, False

        # A spell/technique/innate ability's own "materials" (same {item, quantity} shape a
        # craft recipe's own "materials" uses -- DM_Crafting.py's _has_materials, reused
        # directly) gates every roll path below uniformly -- checked ahead of the entity-test/
        # opposed/untargeted split, same "prerequisite before any roll" precedent
        # "out_of_range" already follows for range. Consuming the materials (success or
        # failure alike, once a roll actually happens) is _on_turn_detected's own job, right
        # after this method returns -- this method only ever gates, never mutates inventory.
        if named_ability and named_ability.get("materials") and not self._has_materials(
            self.player_name, named_ability["materials"],
        ):
            return MissingSpellMaterialsOutcome(self.player_name, skill_name), named_ability, False

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
            roll = self.resolve_action(
                self.player_name, skill_name, test.get("difficulty", 0), dice_penalty=dice_penalty,
            )
            outcome = test.get("pass") if roll["success"] else test.get("fail")
            test_effects = self.apply_test_outcome(target_name, outcome) or {}
            self._run_test_outcome_program(test, roll["success"], target_name)
            effects = []
            loot = test_effects.get("loot")
            if loot and (loot["currency"] or loot["items"]):
                effects.append(LootEffect(currency=loot["currency"], items=loot["items"]))
            damage = test_effects.get("damage")
            if damage:
                # A trap's failed disarm/dodge attempt (ex: dart trap, scythe trap) -- same
                # DamageEffect a normal weapon hit already attaches (see
                # _apply_damage_if_hit), just sourced from the target's own
                # [entity.test.fail] instead of an attack roll.
                effects.append(DamageEffect(
                    defender=damage["defender"], net_damage=damage["net_damage"],
                    remaining_hp=damage["remaining_hp"],
                ))
            result = rolled_outcome_from_roll(roll, effects=effects)
            result.defender = target_name
            result.opposing_skill = None
        elif target_name:
            # Looked up before rolling (not just for damage afterward, as before) so distance
            # can gate the attack roll itself -- is_in_range (DM_Movement.py) is a pure
            # reachability check, not a difficulty modifier (see its own module note for why
            # that per-tier accuracy idea was dropped). A skill with no matching ability
            # (ex: "charisma") always comes back in range (nothing physical to be out of
            # reach of), so this never gates a non-combat opposed check. has_medium_access
            # (DM_Movement.py) is the same "can't do it, don't roll" shape for a second,
            # independent reason: a melee-only attacker can't touch a flying/submerged/
            # burrowed defender regardless of band distance (the Pathfinder Fly/Swim/Burrow
            # shape) -- reported as the same OutOfRangeOutcome, an honest simplification
            # rather than a distinct outcome type for what's still fundamentally "can't reach
            # them right now."
            ability = named_ability or self.find_attack_ability(self.player_name, skill_name)
            skill_divisor = 1
            if modifier and ability and matches_supertype_or_subtype(ability, modifier.get("applies_to", {})):
                # The modifier costs the attacker's own skill (skill_divisor, folded into
                # whichever of resolve_action/resolve_opposed_action fires below -- the same
                # attacker-side pool math both already share) and pays off in a copied
                # damage_value -- never the shared spells.toml/creatures.toml entity itself.
                ability = self._apply_ability_modifier(ability, modifier)
                skill_divisor = modifier.get("skill_divisor", 1)
            if not self.is_in_range(self.player_name, target_name, ability) or not self.has_medium_access(self.player_name, target_name, ability):
                result = OutOfRangeOutcome(self.player_name, skill_name, target_name)
            elif (
                self._ability_requires_language(skill_name, ability)
                and not self._shares_language_with(target_name)
            ):
                # Same "can't do it, don't roll" shape as is_in_range above -- a
                # language_dependent ability/skill (ex: persuade) auto-fails outright with no
                # roll attempted at all when the player's own current language isn't shared
                # with target_name, rather than a penalized roll or a "resolves but reads as
                # garbled" compromise.
                result = LanguageBarrierOutcome(self.player_name, skill_name, target_name)
            elif ability and ability.get("difficulty"):
                # An ability that authors its own "difficulty" (ex: spells.toml's "suggestion",
                # "fireball") resolves as a flat check against that fixed number instead of the
                # ordinary opposed-vs-defender's-skill roll below -- the number the caster needs
                # to roll on the ability's own skill to pull the working off at all, independent
                # of who it's aimed at. No weapon ability ever authors "difficulty" (find_
                # attack_ability's own candidates never do), so this branch is spell/technique-
                # only and every existing weapon attack keeps its unchanged opposed resolution.
                # A target that actually wants to resist authors its own [entity.test] instead
                # (skill = [...], difficulty = ...) -- checked above this branch, via_test,
                # which already takes priority whenever it matches.
                roll = self.resolve_action(
                    self.player_name, skill_name, ability["difficulty"], dice_penalty=dice_penalty, skill_divisor=skill_divisor,
                )
                result = rolled_outcome_from_roll(roll)
                result.defender = target_name
            else:
                roll = self.resolve_opposed_action(
                    self.player_name, skill_name, target_name, dice_penalty=dice_penalty, ability=ability, skill_divisor=skill_divisor,
                )
                result = rolled_outcome_from_roll(roll)
        else:
            roll = self.resolve_action(self.player_name, skill_name, dice_penalty=dice_penalty)
            result = rolled_outcome_from_roll(roll)
        return result, ability, via_test

    def _finish_rolled_outcome(self, result, skill_name, named_ability, ability, target_name, via_test, input_text=None):
        """!
        @brief The single post-roll step for the player's own action: everything that might
            apply once a roll has actually happened, in one call instead of the four separately
            isinstance-gated ones this replaces. Guards the RolledOutcome type exactly once --
            each of the four steps below then only checks its own distinct predicate (success,
            target, via_test, ability, named_ability materials, hidden), since none of those
            ever share the same gate (ex: material consumption and defender details fire on a
            failed roll too, deliberately -- see their own docstrings). Order matters: materials
            are spent before damage/summon effects are appended, mirroring a botched craft
            attempt's own "consume regardless of outcome" precedent. Scoped to the player's own
            _on_turn_detected pipeline only -- resolve_behavior_action (DM_Combat.py, a
            creature's own turn) has no attitude to nudge and no spell materials/summons of its
            own to resolve, so it keeps its own narrower, separate damage-only handling rather
            than routing through this.
        @param result The roll result from _resolve_roll, mutated in place.
        @param skill_name The skill being used, already resolved from any named ability.
        @param named_ability The resolved ability entity (technique/spell), or None.
        @param ability The attack ability from _resolve_roll, if already resolved there.
        @param target_name self.current_target, or None.
        @param via_test True if the roll was a flat [entity.test] check.
        @param input_text The player's raw turn text, forwarded only to _run_ability_outcome_program
            (see its own docstring for why) -- optional, defaulting to None so every existing
            direct caller (ex: test_unit.py's own ability-outcome-program tests) keeps working
            unchanged.
        """
        if not isinstance(result, RolledOutcome):
            return
        self._consume_spell_materials_if_rolled(result, named_ability)
        self._apply_damage_if_hit(result, skill_name, named_ability, ability, target_name, via_test)
        self._apply_summon_if_hit(result, named_ability)
        self._apply_dispel_if_hit(result, named_ability, target_name)
        self._apply_cure_if_hit(result, named_ability, target_name)
        self._apply_teleport_if_hit(result, named_ability)
        self._run_ability_outcome_program(result, skill_name, named_ability, ability, target_name, via_test, input_text)
        self._attach_defender_details(result, target_name)

    def _run_ability_outcome_program(self, result, skill_name, named_ability, ability, target_name, via_test, input_text=None):
        """!
        @brief Runs the resolved ability's own on_pass/on_fail program once a real
            ability-based roll has resolved -- closes the "22
            dead skills" gap: a skill whose only mechanical effect is a condition/attitude nudge
            (ex: intimidate, trip/disarm/sunder) now actually does something on a pass/fail,
            without a new Python branch per skill. Never fires for a flat [entity.test] check
            (that has its own, separate on_pass/on_fail attachment point -- see
            _run_test_outcome_program) or for a roll with no resolvable ability at all (ex: a
            bare skill check with nothing named/equipped matching it).
        @param result The roll result from _resolve_roll, already confirmed to be a RolledOutcome.
        @param skill_name The skill being used, already resolved from any named ability.
        @param named_ability The resolved ability entity (technique/spell), or None.
        @param ability The attack ability _resolve_roll already resolved, if any -- only the
            test/no-target branches leave it None, in which case it's re-derived here exactly
            like _apply_damage_if_hit's own fallback does.
        @param target_name self.current_target, or None.
        @param via_test True if the roll was a flat [entity.test] check.
        @param input_text The player's raw turn text, threaded into the program's own ctx as
            "input" -- read by ops that want the actual free-text content of this turn rather
            than a fixed, scripted value (ex: spells.toml's "suggestion", whose own on_pass omits
            a literal "text" specifically so Program_Interpreter.py's "inject_directive" op falls
            back to this). Optional, defaulting to None -- a program that never references
            ctx["input"] is entirely unaffected either way.
        """
        if via_test:
            return
        if ability is None:
            ability = named_ability or self.find_attack_ability(self.player_name, skill_name)
        if not ability:
            return
        program = ability.get("on_pass" if result.success else "on_fail")
        if not program:
            return
        # resolve_targets (DM_Combat.py) is [target_name] alone (or [None], untargeted) for
        # every ability with no authored "targets" table -- one run, unchanged from before this
        # existed -- or the wider AoE/multi-target/discriminated pool its own {number, aoe,
        # side} table describes, run once per resolved target so ex: a discriminating area
        # effect's own on_pass (a condition applied via Program_Interpreter's apply_condition
        # op) actually lands on every ally/enemy it caught, not just target_name.
        for program_target in self.resolve_targets(self.player_name, target_name, ability):
            run_program(
                program, {"actor": self.player_name, "target": program_target, "input": input_text},
                self.entities, self.rules, self.event_bus,
            )

    def _run_test_outcome_program(self, test, success, entity_name):
        """!
        @brief Runs an [entity.test]'s own on_pass/on_fail program -- sibling to the
            test's existing flat "pass"/"fail" outcome tables, for a consequence that needs a
            conditional the flat table can't express. actor is the checking entity (always the
            player today -- entity tests are player-initiated); target is the entity the test
            itself lives on (a scene target, or an item one level deeper -- see
            _resolve_item_test_target).
        @param test The entity's own [entity.test] table.
        @param success Whether the roll passed.
        @param entity_name The entity carrying the test.
        """
        program = test.get("on_pass" if success else "on_fail")
        if program:
            run_program(program, {"actor": self.player_name, "target": entity_name}, self.entities, self.rules, self.event_bus)

    def _apply_damage_if_hit(self, result, skill_name, named_ability, ability, target_name, via_test):
        """!
        @brief Rolls and attaches bonus weapon/ability damage if the roll succeeded against a
            target and wasn't a flat [entity.test] check -- a test-path success (ex: a
            lockpick) must never also roll bonus weapon damage even if skill_name happens to
            match an equipped weapon/ability (ex: a future finesse-based dagger matching the
            chest's finesse-skill lock test). Also gated on the resolved ability actually
            carrying a "damage_value" -- a named ability with none (ex: a summoning spell, see
            _apply_summon_if_hit) is a real, matched ability but not an attack, and must not
            get a spurious "damage": {"net_damage": 0, ...} entry just because it happened to
            resolve against a target/current_target that was present at the time.
        @param result The roll result from _resolve_roll, mutated in place with "damage" if hit.
        @param skill_name The skill being used, already resolved from any named ability.
        @param named_ability The resolved ability entity (technique/spell), or None.
        @param ability The attack ability from _resolve_roll, if already resolved there --
            only the test/no-target branches leave it None, in which case it's re-derived here.
        @param target_name self.current_target, or None.
        @param via_test True if the roll was a flat [entity.test] check.
        """
        if result.success and not via_test:
            if ability is None:
                ability = named_ability or self.find_attack_ability(self.player_name, skill_name)
            if ability and "damage_value" in ability:
                # resolve_targets (DM_Combat.py) is [target_name] alone for every ability with
                # no authored "targets" table -- unchanged single-target behavior, [None] if
                # there's also no target_name at all (skipped below, nothing to hit) -- or the
                # wider AoE/multi-target/discriminated/self-only pool its own {number, aoe,
                # side} table describes (ex: techniques.toml's cleave, a fireball-style blast, a
                # self-only ward that needs no named target at all). Each real hit gets its own
                # damage roll/DamageEffect/attitude nudge, same as a lone target already did.
                for defender_name in self.resolve_targets(self.player_name, target_name, ability):
                    if not defender_name:
                        continue
                    damage = self.calculate_damage(self.player_name, defender_name, ability)
                    result.effects.append(DamageEffect(
                        defender=damage["defender"], net_damage=damage["net_damage"],
                        remaining_hp=damage["remaining_hp"],
                    ))
                    # "combat_hit" attitude drift (DM_Social.py's nudge_attitude_from_event) -- how
                    # hard the hit landed relative to the defender's own max_hp, not a flat
                    # per-swing amount, so a graze barely registers and a near-kill genuinely
                    # scares them (the "threat" axis) even while disposition stays pinned at
                    # is_hostile's own floor. _nudge_combat_hit_attitude is the shared call-site
                    # shape resolve_behavior_action (DM_Combat.py, an entity's own combat-turn
                    # attack) also uses -- only who's attacking differs, never the shape.
                    self._nudge_combat_hit_attitude(defender_name, self.player_name, damage.get("net_damage", 0))
                # "on_action" statuses (ex: Frightful Presence) -- see DM_Status.py's
                # evaluate_proximity_statuses. Fired once per player turn that actually landed
                # at least one hit with a damage-dealing ability, not once per resolved target
                # -- the actor's own qualifying requirements don't change per-target, so
                # re-evaluating them once per resolve_targets() entry would be redundant work
                # for the same result.
                self.evaluate_proximity_statuses(self.player_name, "on_action")

    def _nudge_combat_hit_attitude(self, target_name, attacker_name, net_damage):
        """!
        @brief The shared "someone just landed a hit" attitude-drift shape -- a "combat_hit"
            nudge on the victim's own attitude toward whoever hit it, plus the "bonds made on
            the battlefield" ripple to bystanders (_nudge_shared_enemy_bonds). Shared by
            _apply_damage_if_hit (the player's own attack) and resolve_behavior_action
            (DM_Combat.py, any other entity's own combat-turn attack) -- the same call-site
            shape either way, just parameterized on who actually swung. Stays one-directional,
            same as before this was shared: only the victim's attitude toward the attacker
            moves, never the reverse (an attacker's own feelings are already fully authored via
            [[entity.behavior]]/[entity.attitudes]).
        @param target_name The entity that was just hit.
        @param attacker_name The entity that landed the hit.
        @param net_damage The hit's own net_damage, scaled here against target_name's max_hp.
        """
        max_hp = self.entities.get(target_name, {}).get("max_hp") or 1
        magnitude = min(1.0, net_damage / max_hp)
        self.nudge_attitude_from_event(target_name, attacker_name, "combat_hit", magnitude)
        self._nudge_shared_enemy_bonds(target_name, attacker_name, magnitude)

    def _nudge_shared_enemy_bonds(self, target_name, attacker_name, magnitude):
        """!
        @brief "Bonds made on the battlefield" -- every other living scene entity that already
            considers target_name a real enemy (is_hostile(observer, target_name)) gets a
            small "shared_enemy" attitude nudge toward attacker_name, scaled by the same
            magnitude the hit itself just earned (see _nudge_combat_hit_attitude) -- a decisive
            blow against a common enemy warms an onlooker up more than a graze. Deliberately not
            restricted to allies/party members -- even a bystander merely wary of attacker_name
            can start softening if attacker_name keeps fighting something that bystander already
            hates. attacker_name need not be the player -- any entity's resolved attack routes
            through here (see resolve_behavior_action, DM_Combat.py), so an ally striking down a
            shared foe earns the same bystander warmth a player blow would.
            Safe to call for every observer in scenario_entities regardless of whether it has
            real attitude data of its own: is_hostile(observer, target_name) returns True
            unconditionally for a tableless creature (ex: another wolf -- see is_hostile's own
            docstring), but nudge_attitude_from_event's own "attitudes" in entity gate silently
            no-ops for exactly that case, so a mindless hostile creature never actually
            accumulates a bond it has no data to hold.
        @param target_name The entity that was just hit -- excluded from its own "observers".
        @param attacker_name The entity that landed the hit -- also excluded from "observers".
        @param magnitude The same 0..1 magnitude _nudge_combat_hit_attitude already computed for
            this hit's own "combat_hit" nudge.
        """
        for observer_name in self.scenario_entities:
            if observer_name in (attacker_name, target_name):
                continue
            if self.is_hostile(observer_name, target_name):
                self.nudge_attitude_from_event(observer_name, attacker_name, "shared_enemy", magnitude)

    def _apply_summon_if_hit(self, result, named_ability):
        """!
        @brief Conjures a temporary ally (_summon_creature, DM_Summoning.py) if this turn's
            named ability is a summoning spell/technique (its own "summon" field -- a
            {"name"|"template", "duration"} table) and the roll succeeded -- mirrors
            _apply_damage_if_hit's own "only on a successful roll" gate, just for a different
            kind of on-hit effect. Checked regardless of target_name/via_test: a summon isn't
            "against" anyone the way damage is, so it fires the same way whether this was a
            flat auto-success resolve_action (no current_target at all) or a contested opposed
            roll against a hostile current_target (the caster's own casting resisted by the
            target's willpower/arcane, the same as any other opposed skill use).
        @param result The roll result from _resolve_roll, mutated in place with "summoned"
            (the new instance's own name) if a creature was actually conjured.
        @param named_ability The resolved ability entity (technique/spell), or None.
        """
        if not result.success or not named_ability:
            return
        summon_spec = named_ability.get("summon")
        if not summon_spec:
            return
        summoned_name = self._summon_creature(summon_spec)
        if summoned_name:
            result.effects.append(SummonEffect(name=summoned_name))

    def _apply_dispel_if_hit(self, result, named_ability, target_name):
        """!
        @brief Banishes target_name outright (remove_entity_from_scene) if this turn's named
            ability is a dispel-shaped cast (its own "dispel" field -- a {"supertypes",
            "subtypes"} filter, the same shape/matching rule damage_bonus_vs already uses --
            matches_supertype_or_subtype, Combat_Resolution.py) and the roll succeeded, but only
            if target_name's own supertype/subtype actually matches; a mismatched target simply
            isn't dispellable by this cast (ex: pointing "dispel magic" -- {supertypes = ["spell"],
            subtypes = ["spell"]} -- at an ordinary creature does nothing) -- the same "used on
            the wrong thing just wastes the action" shape Pathfinder's real Dispel Magic already
            has, rather than a hard pre-roll gate the way missing spell materials/out-of-range
            are. Mirrors _apply_summon_if_hit exactly, just removing an entity instead of
            conjuring one. Player-only, same scope every other cast-time effect (summon/teleport)
            already keeps.
        @param result The roll result from _resolve_roll, mutated in place with a DispelEffect
            if something was actually banished.
        @param named_ability The resolved ability entity (technique/spell), or None.
        @param target_name self.current_target, or None.
        """
        if not result.success or not named_ability or not target_name:
            return
        dispel_spec = named_ability.get("dispel")
        if not dispel_spec:
            return
        target = self.entities.get(target_name, {})
        if not matches_supertype_or_subtype(target, dispel_spec):
            return
        self.remove_entity_from_scene(target_name)
        result.effects.append(DispelEffect(name=target_name))

    def _apply_cure_if_hit(self, result, named_ability, target_name):
        """!
        @brief Dismisses every one of target_name's own active conditions matching this turn's
            named ability's own "cure" field (a {"supertypes", "subtypes"} filter against the
            [[condition]] catalog rather than the entity catalog -- matches_supertype_or_subtype
            has no entity-specific logic, so a [[condition]] entry authoring those same two
            optional fields works identically) if the roll succeeded. Mirrors
            _apply_dispel_if_hit exactly, just removing a condition instead of banishing an
            entity: {subtypes = ["disease"]} is the Pathfinder "Remove Disease" shape (cures
            whichever disease is active without the caster needing to name it),
            {supertypes = ["affliction"]} a broader panacea also catching poison/curse. A
            target with nothing matching simply has nothing cured -- the same "used on the
            wrong thing just wastes it" shape dispel already has, still reported (as an empty
            CureEffect) rather than silently skipped. Player-only, same scope every other
            cast-time effect (summon/dispel/teleport) already keeps.
        @param result The roll result from _resolve_roll, mutated in place with a CureEffect
            if this turn's named ability authors "cure" at all.
        @param named_ability The resolved ability entity (technique/spell), or None.
        @param target_name self.current_target, or None.
        """
        if not result.success or not named_ability or not target_name:
            return
        cure_spec = named_ability.get("cure")
        if not cure_spec:
            return
        cured = self.cure_conditions(target_name, cure_spec)
        result.effects.append(CureEffect(target=target_name, conditions=cured))

    def _apply_teleport_if_hit(self, result, named_ability):
        """!
        @brief Relocates the player outright if this turn's named ability authors a teleport-
            shaped field and the roll succeeded -- mirrors _apply_summon_if_hit's own "only on
            a successful roll" gate and its "not really 'against' anyone" scope (checked
            regardless of target_name/via_test, same as a summon).

            "teleport_to_band" (an int) jumps the player directly to that band within the
            CURRENT room -- move_entity (DM_Movement.py) is called with the signed delta needed
            to land there, reusing its own existing floor/ceiling clamping unchanged (Dimension
            Door). Deliberately not a new elevation/spatial mechanic or a "jump past
            intervening bands" movement op -- a teleport doesn't need to reuse the incremental
            advance/retreat walk at all, since band is just a field; setting it directly (via a
            computed delta, so the existing clamp keeps working unmodified) is the whole trick.

            "teleport_to_location" ({location, room, band}) instead jumps to a different,
            already-known location outright via _enter_location -- the exact same mechanism
            ordinary location-to-location travel already uses, just called directly rather than
            through _resolve_travel_intent, so it skips that path's own hostile-gate/grid-cost
            checks entirely (instant, no travel-time charged, and works even mid-combat --
            Teleport's whole appeal is escaping a losing fight). _enter_location's own "unknown
            location_key" tolerance (an empty/freeform location, not an error) is unchanged here
            -- same "malformed data degrades quietly" precedent every other loader follows.

            Player-only, same scope _apply_summon_if_hit/_consume_spell_materials_if_rolled
            already keep -- not wired into resolve_behavior_action, so an NPC's own behavior
            can't teleport itself (or the player) today.
        @param result The roll result from _resolve_roll, mutated in place with a
            TeleportEffect if a relocation actually happened.
        @param named_ability The resolved ability entity (technique/spell), or None.
        """
        if not result.success or not named_ability:
            return

        destination_band = named_ability.get("teleport_to_band")
        if destination_band is not None:
            new_band = self.move_entity(self.player_name, destination_band - self.get_band(self.player_name))
            if new_band is not None:
                result.effects.append(TeleportEffect(entity=self.player_name, band=new_band))

        destination = named_ability.get("teleport_to_location")
        if destination:
            self._enter_location(
                destination["location"], arrival_room=destination.get("room"), arrival_band=destination.get("band", 1),
            )
            result.effects.append(TeleportEffect(entity=self.player_name, location=destination["location"]))

    def _consume_spell_materials_if_rolled(self, result, named_ability):
        """!
        @brief Consumes a named ability's own "materials" (DM_Crafting.py's _consume_materials,
            reused directly -- same {item, quantity} shape and consumption primitive a craft
            recipe's own materials already uses) once a real roll actually happened for it --
            unconditionally, success or failure alike, same as a craft attempt's own materials
            (a fizzled cast still burns the reagent). Only ever called once _finish_rolled_outcome
            has already confirmed result is a RolledOutcome, so a no-roll short-circuit
            (missing_spell_materials -- _resolve_roll's own gate already refused the cast before
            getting here -- or out_of_range) never reaches this method at all.
        @param result The _resolve_roll result for this action.
        @param named_ability The resolved ability entity (technique/spell), or None.
        """
        if not named_ability or not named_ability.get("materials"):
            return
        self._consume_materials(self.player_name, named_ability["materials"])

    def _attach_defender_details(self, result, target_name):
        """!
        @brief Attaches defender flavor text to the result -- belt-and-suspenders against the
            persistent character roster (see scenario_loaded's "characters" payload) ever
            being stale. Skips a still-hidden target (is_hidden -- ex: an unnoticed dart trap
            that current_target has fallen back to) for the same reason
            _describe_scenario_characters does: an action resolving against it must not leak
            its flavor text into narration before the player would actually have noticed it.
        @param result The roll result, appended to with a DefenderDetailsEffect if
            describe_character returns anything. A no-roll outcome (out_of_range,
            missing_spell_materials, ...) has no "defender details" fragment in its own
            narration at all (see LLM_Core.py's _describe_outcome), so this only ever does
            anything for a RolledOutcome.
        @param target_name self.current_target, or None.
        """
        if target_name and not self.is_hidden(target_name):
            defender_details = self.describe_character(target_name, toward_name=self.player_name)
            if defender_details:
                result.effects.append(DefenderDetailsEffect(text=defender_details))

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
            presented, it doesn't make an earlier actor's roll affect a later one's. Also
            runs run_round_upkeep (StatusMixin) once, after every turn -- regeneration/
            fast-healing/bleed-style condition effects.
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
            turn_outcome = self.resolve_behavior_action(entity_name, opponent)
            if turn_outcome:
                # The envelope stays a plain dict (see DM_ActionOutcome.py's own module
                # docstring on scope) -- "actor"/"initiative" are this round's own bookkeeping
                # around a turn, not part of what actually happened, so they wrap the typed
                # outcome rather than living as fields on it.
                turns.append({
                    "actor": entity_name,
                    "initiative": self.roll_initiative(entity_name),
                    "outcome": turn_outcome,
                })
        if turns:
            turns.sort(key=lambda turn: turn["initiative"], reverse=True)
            result["turns"] = turns
        # Regeneration/fast-healing/bleed-style condition effects (see run_round_upkeep,
        # DM_Status.py) -- run after every actor's own turn (including the player's own
        # damage, already resolved before _resolve_combat_round was even called this turn) so
        # a condition's own upkeep_blocked_by_tags can already see this round's damage tags.
        self.run_round_upkeep()
        # current_target only advances once, at the end of the round, if it died -- not
        # interrupted mid-round by an earlier actor's kill (ex: an ally finishing it off
        # before the round is even done resolving, or upkeep damage finishing off a bleeding
        # target that survived the round's own attacks).
        if self.current_target and self.get_current_hp(self.current_target) <= 0:
            self.current_target = self._choose_combat_target()
        # A paused travel/rest (see docs/downtime.md's "Pausing for a fight") resumes the
        # instant the last hostile in the scene actually drops -- checked fresh every round
        # (not just when current_target itself dies) since an ally's own turn above, or this
        # same round's upkeep damage, can be what actually finishes off the last one.
        if self.pending_downtime and not self._any_hostile_present():
            self._resume_pending_downtime()

    def _any_hostile_present(self):
        """!
        @brief Whether any living entity hostile to the player is currently in the scene --
            the same is_hostile/get_current_hp>0 loop DM_Movement.py's own room-transition
            gates and DM_Travel.py's own grid-travel gate already each ran independently;
            factored out here since a paused-downtime resume check (_resolve_combat_round,
            above; _resolve_grid_travel_intent/rest's own defensive check) is now a fourth
            use of the exact same predicate.
        @return True if scenario_entities holds at least one hostile, living, non-player
                entity.
        """
        return any(
            self.is_hostile(entity_name, self.player_name) and self.get_current_hp(entity_name) > 0
            for entity_name in self.scenario_entities
            if entity_name != self.player_name
        )

    def _resume_pending_downtime(self):
        """!
        @brief Continues whatever grid travel/rest self.pending_downtime describes, from
            wherever its own block loop left off -- called once the hostile encounter that
            paused it has actually cleared (_resolve_combat_round, above, or a caller of
            DM_Movement.py's _resolve_travel_intent/DM_Time.py's rest opportunistically
            catching a pending downtime whose blocker was removed some other way, ex: ADaM
            despawning it). Unlike the
            *first* call into _advance_pending_travel/_advance_pending_rest (which still has
            the original turn's own "resolved" publisher closure on hand), this one runs on
            a turn that has nothing else to do with travel or rest at all, so there's no
            closure to call -- if the trip/rest turns out to finish here, this publishes
            "item_interaction_resolved" itself, with exactly the field names
            intents/travel.py's narrate_travel / intents/rest.py's narrate_rest already
            expect, so the player still gets an ordinary arrival/rest narration even though
            nothing they just typed was "travel" or "rest".
        """
        kind = self.pending_downtime["kind"]
        if kind == "travel":
            result = self._advance_pending_travel()
        else:
            result = self._advance_pending_rest()
        if result["interrupted"]:
            return
        payload = {k: v for k, v in result.items() if k != "interrupted"}
        self.event_bus.publish("item_interaction_resolved", {
            "intent": kind, "item_name": None, "input": "", "found": True,
            "present_entities": list(self.scenario_entities), **payload,
        })

    def _on_item_interaction_detected(self, data):
        """!
        @brief Event handler for a free-text item-interaction match (see NLPCore.map_to_item):
            "examine"/"take"/"give"/"trade"/"use"/"equip"/"unequip"/"drop" against a named
            item, "open"/"close" against the scene target itself, or one of the twelve
            free-standing intents (see CONTEXT.md's "Free-standing intent" -- "advance",
            "retreat", "formation_behind", "formation_abreast", "speak_language", "rest",
            "move", "travel", "mount", "dismount", "hitch", "unhitch"), each resolved and
            narrated by its own module under intents/
            (see intents/registry.py's own HANDLERS manifest) rather than a branch here.
            Deliberately bypasses the whole skill/dice system either way -- none of these
            warrant a roll (see DM_Movement.py's module docstring for why movement specifically
            is deterministic, not a check). Publishes "item_interaction_resolved" either way,
            with enough detail for narration to explain a miss (locked, closed, not present,
            not takeable, not usable, not equippable, cant_equip, not_equipped, no exit, wrong
            band, blocked by enemies, ...) rather than staying silent.

            "give"/"trade"/"examine"/"take" against a named item (see _resolve_transfer_intent)
            move it between the player and the current scene target -- "take"/"trade" toward the
            player, "give" away from them, "examine" never moves anything. "open"/"close" never
            move anything either. "use"
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
            container guarding it.
        @param data The item_interaction_detected payload from NLPCore
            ({intent, item_name, input, score}). "item_name" is None for "open"/"close" and
            every free-standing intent, none of which act on a named item; "move" also
            carries a "direction" (ex: "forward", "right"). "travel" carries no pre-parsed
            destination at all -- unlike "move", NLPCore has no catalog of location names to
            match against, so intents/travel.py resolves the destination itself from the raw
            input.
        """
        intent = data.get("intent")
        item_name = data.get("item_name")
        input_text = data.get("input")
        target_name = self._get_target_name()
        # "examine"/"take" against an item already sitting in the player's own inventory (ex:
        # DM_Improvisation.py placing an ad hoc item straight into inventory) resolve directly
        # against the player -- computed early, alongside the ground-item check below, so a
        # locked/closed *unrelated* scene target never blocks examining something the player
        # already possesses (the same reasoning the ground-item check already follows: reaching
        # something the player can already reach without going through target_name at all must
        # never be gated on target_name's own state). Excludes the case where target_name
        # *also* currently carries an item of this same shared-catalog name (ex: a second
        # "health potion" sitting in a chest after the player already picked one up elsewhere)
        # -- there, "take" still has something real left to actually move, so this must not
        # short-circuit into a self-transfer no-op that silently leaves the target's own copy
        # behind untaken.
        target_has_item = bool(target_name) and item_name in self.entities.get(target_name, {}).get("inventory", [])
        already_owned = (
            intent in ("examine", "take")
            and item_name in self.entities.get(self.player_name, {}).get("inventory", [])
            and not target_has_item
        )

        def resolved(found, **extra):
            if found:
                self._run_interact_program(intent, item_name, target_name)
            self.event_bus.publish("item_interaction_resolved", {
                "intent": intent, "item_name": item_name, "input": input_text, "found": found,
                # Room-level presence snapshot -- see scenario_loaded's own publish for why
                # every DM-published narration-triggering event carries one. Read fresh here
                # (not captured at the top of this handler) since a "move" intent's own
                # enter_room call already changed self.scenario_entities to the *new* room's
                # roster by the time this fires -- correct, since this narration is witnessed
                # by whoever's present now, not whoever was present when the input arrived.
                "present_entities": list(self.scenario_entities),
                **extra,
            })
            self._publish_party_status()

        handler = FREE_STANDING_INTENT_HANDLERS.get(intent)
        if handler:
            # A free-standing intent (see CONTEXT.md) -- unrelated to target_name/the locked
            # gate below, resolved and narrated entirely by its own module under intents/
            # (intents/registry.py's own HANDLERS manifest), not a branch here.
            resolve, _narrate = handler
            resolve(self, data, resolved)
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

        if not already_owned and target_name and self.is_locked(target_name):
            resolved(False, reason="locked", container=target_name)
            return

        if intent in ("open", "close"):
            self._resolve_open_close_intent(intent, target_name, resolved)
            return

        self._resolve_transfer_intent(intent, item_name, target_name, resolved)

    def _run_interact_program(self, intent, item_name, target_name):
        """!
        @brief Runs the interacted-with entity's own [entity.on_interact.<intent>] program (the
            cursed dagger's own on_interact.equip is the shipped worked example) -- the single
            shared funnel
            every intent already resolves through (resolved(), above), right before its one
            item_interaction_resolved publish, so this only ever fires once per intent
            regardless of which branch resolved it. Only on success (found -- resolved()'s own
            caller already gates this) -- a program that denies an interaction outright is out
            of scope for now. target is whichever entity the interaction actually named: the
            item itself for an item-named intent (equip/unequip/drop/examine/take/give/trade/
            use), or the scene target itself for "open"/"close" (which never resolve an
            item_name at all -- see NO_ITEM_LOOKUP_INTENTS). Deliberately never fires for
            move/travel/advance/retreat/formation -- none of those name an entity being
            interacted with in this sense.
        @param intent The resolved item-interaction intent.
        @param item_name The item entity named by this intent, or None.
        @param target_name The current scene target's name, or None.
        """
        interact_entity_name = item_name if item_name else (target_name if intent in ("open", "close") else None)
        if not interact_entity_name:
            return
        program = self.entities.get(interact_entity_name, {}).get("on_interact", {}).get(intent)
        if not program:
            return
        run_program(
            program, {"actor": self.player_name, "target": interact_entity_name},
            self.entities, self.rules, self.event_bus,
        )

    def _on_dialogue_detected(self, data):
        """!
        @brief Event handler for a free-text dialogue match (see NLPCore.DIALOGUE_KEYWORDS/
            _detect_dialogue_intent): the player directly addressing someone, as opposed to a
            skill-based social check (persuade/intimidate/deceive, still resolved the ordinary
            dice way through _on_turn_detected) or any of the item/scene intents
            _on_item_interaction_detected covers. Bypasses the skill/dice system entirely, the
            same as every intent above -- there's nothing to roll for simply talking. Thin by
            design (see this class's own docstring): the actual addressee resolution and
            gating live in DialogueMixin._resolve_dialogue (DM_Dialogue.py); this handler only
            threads the raw input through and tags the result with who's currently present,
            the same "present_entities" snapshot every other narration-triggering event
            carries (see scenario_loaded's own publish for why). Each of the three attitude
            axes gets its own (label, score) pair in the payload -- "sentiment"/"sentiment_score"
            (disposition), "threat_sentiment"/"threat_score", "familiarity_sentiment"/
            "familiarity_score" -- NLPCore's own local classification (see
            SentenceTransformerMatcher.classify_sentiment/classify_threat/classify_familiarity),
            bundled into one dict and threaded through to _resolve_dialogue so a found target's
            attitude toward the player can drift on all three at once (DM_Social.py's
            nudge_attitude) -- see CLAUDE.md's "Dialogue".
        @param data The dialogue_detected payload from NLPCore ({input, score, sentiment,
            sentiment_score, threat_sentiment, threat_score, familiarity_sentiment,
            familiarity_score}).
        """
        input_text = data.get("input")
        sentiments = {
            "disposition": (data.get("sentiment"), data.get("sentiment_score")),
            "threat": (data.get("threat_sentiment"), data.get("threat_score")),
            "familiarity": (data.get("familiarity_sentiment"), data.get("familiarity_score")),
        }
        result = self._resolve_dialogue(input_text, sentiments)
        result["input"] = input_text
        result["present_entities"] = list(self.scenario_entities)
        self.event_bus.publish("dialogue_resolved", result)

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

    def _resolve_item_test(self, item_name, skill_name, dice_penalty=0):
        """!
        @brief Resolves a flat [entity.test] check against a reachable item (see
            _resolve_item_test_target) -- the item-level counterpart to _on_turn_detected's
            own scene-target test branch, kept as a separate path since an item is never a
            combat target (no round, no defender_details, no damage).
        @param item_name The item entity's name (already confirmed reachable/testable by
            _resolve_item_test_target).
        @param skill_name The skill the player is attempting to use.
        @param dice_penalty Forwarded to resolve_action -- see _on_turn_detected's own
            "Multiple actions" note.
        @return A resolve_action-shaped result dict, plus "revealed" (the item's own "tags"
                list) if the check passed and its outcome had a truthy "reveal" key.
        """
        test = self.entities[item_name]["test"]
        roll = self.resolve_action(
            self.player_name, skill_name, test.get("difficulty", 0), dice_penalty=dice_penalty,
        )
        outcome = test.get("pass") if roll["success"] else test.get("fail")
        self.apply_test_outcome(item_name, outcome)
        self._run_test_outcome_program(test, roll["success"], item_name)
        effects = []
        if self.is_identified(item_name):
            effects.append(RevealEffect(tags=list(self.entities[item_name].get("tags", []))))
        result = rolled_outcome_from_roll(roll, effects=effects)
        result.defender = item_name
        result.opposing_skill = None
        return result

    def _is_party_member(self, entity_name):
        """!
        @brief Whether entity_name is on the player's own side -- the player themselves
            (is_player = true) or an ally like debug.toml's "thane" (is_party = true, same
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
            falls back to the first living, non-party entity instead (ex: debug.toml's own
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
            _on_turn_detected's end-of-round check).
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

    def _target_is_engaged(self):
        """!
        @brief Whether self.current_target is a fight actually in progress right now -- present,
            alive, and hostile toward the player. The one condition DM_Improvisation.py's own
            _claim_current_target_if_free (checked before letting a freshly-placed entity
            preempt current_target) and this class's own _choose_combat_target share in spirit
            (both care whether an active engagement should be left alone), even though the two
            callers otherwise ask genuinely different questions -- _choose_combat_target picks
            the best target across the whole scene from scratch, while
            _claim_current_target_if_free only ever asks "should I disturb what's already
            there," which is exactly this one check.
        @return True if current_target is a live, hostile-toward-the-player entity still
                present in the scene.
        """
        return bool(
            self.current_target
            and self.current_target in self.scenario_entities
            and self.get_current_hp(self.current_target) > 0
            and self.is_hostile(self.current_target, self.player_name)
        )
