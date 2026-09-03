import copy
import os
import tomllib

from dm.DM_Types import DMCoreProtocol
from resolution.Program_Interpreter import run_program
from paths import PROJECT_ROOT

# Reserved sentinel a scenario/room "entities" entry can use in place of a literal character
# name to mean "whichever entity is currently the player" (self.player_name) -- resolved in
# _instance_entities, below. This is what lets every scenario file stay agnostic of which
# template is_player=true actually names, and of apply_character_creation's own optional
# rename (DM_CharacterCreation.py) -- a scenario never has to be updated just because a
# playthrough's character has a different name than the one its own template started with.
PLAYER_PLACEHOLDER = "player"


def scenario_file_path(scenario_name, setting="Fantasy"):
    """!
    @brief Resolves a scenario name to its file path under Rules/<setting>/scenarios/.
    @param scenario_name The scenario's filename without extension (ex: "arena", "tavern").
    @param setting Which Rules/ subdirectory to resolve against (ex: "Fantasy", "Zombie") --
        each setting is a self-contained TOML data pack (its own skills/rules/entities/
        scenarios), never mixed with another setting's, so a scenario name only has to be
        unique within its own setting. Defaults to "Fantasy" so every existing call site
        that predates settings plural keeps resolving exactly where it always has.
    @return The absolute filepath, whether or not it actually exists.
    """
    return os.path.join(PROJECT_ROOT, "Rules", setting, "scenarios", f"{scenario_name}.toml")


def list_available_scenarios(setting="Fantasy"):
    """!
    @brief Every real gameplay scenario under Rules/<setting>/scenarios/, for a UI to offer as
        a choice before any DMCore exists -- same "pure, DMCore-independent, re-scan the TOML
        directly" precedent Character_Creation.py's load_character_creation_data sets, since
        GUICore needs this list before it can know whether a DMCore will ever exist.
        character_test.toml/scenario_entity_test.toml are deliberately excluded -- minimal
        scenarios built solely for TestCharacterCreationRename/TestScenarioLocalEntities, not
        real ones to offer a player.
    @param setting Which Rules/ subdirectory to scan -- see scenario_file_path.
    @return A list of (scenario_key, display_name, description) tuples, sorted by key. A
        scenario file that's missing or fails to parse is silently skipped, the same
        per-file leniency load_rules applies to every other TOML file.
    """
    scenarios_dir = os.path.join(PROJECT_ROOT, "Rules", setting, "scenarios")

    results = []
    if not os.path.isdir(scenarios_dir):
        return results
    for filename in sorted(os.listdir(scenarios_dir)):
        if not filename.endswith(".toml"):
            continue
        key = filename[: -len(".toml")]
        if key in ("character_test", "scenario_entity_test"):
            continue
        try:
            with open(os.path.join(scenarios_dir, filename), "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        scenario_table = data.get("scenario", {})
        results.append((key, scenario_table.get("name", key), scenario_table.get("description", "")))
    return results


def list_available_settings():
    """!
    @brief Every self-contained data pack under Rules/ (ex: "Fantasy", "Zombie") -- for a UI
        to offer as a choice before any DMCore exists, the same "pure, DMCore-independent,
        re-scan the directory directly" precedent list_available_scenarios/
        load_character_creation_data already set, since GUICore needs this list before it can
        know whether a DMCore -- let alone which setting it'll load -- will ever exist.
    @return A sorted list of setting names (each a Rules/<name>/ subdirectory), or [] if
        Rules/ itself is missing.
    """
    rules_dir = os.path.join(PROJECT_ROOT, "Rules")
    if not os.path.isdir(rules_dir):
        return []
    return sorted(
        name for name in os.listdir(rules_dir)
        if os.path.isdir(os.path.join(rules_dir, name))
    )


class RulesMixin(DMCoreProtocol):
    """!
    @brief TOML rules/entity loading and scenario instancing (DMCore mixin -- only ever
        composed into DMCore, never instantiated on its own; relies on
        self.skills/self.entities/self.rules/self.scenario/self.scenario_entities/
        self.event_bus/self.player_name, set up by DMCore.__init__).
        _describe_scenario_characters calls self.describe_character (SocialMixin);
        _enter_location/enter_room call self._sync_mount_bands (MovementMixin). Inherits
        DMCoreProtocol purely so type checkers can resolve these shared attributes/
        cross-mixin methods -- see DM_Types.py.
    """

    def _describe_scenario_characters(self):
        """!
        @brief Builds the "characters" roster (describe_character per scenario instance,
            skipping entities with no descriptive data) shared by scenario_loaded's
            initial payload and game_loaded's post-load payload. Also skips anything still
            "hidden" (is_hidden -- ex: items.toml's dart trap before its own [entity.notice]
            auto-roll succeeds, see _auto_roll_notice) so the LLM's narration prompt never
            gets spoiled with a hazard the player hasn't actually noticed yet.
        @return A list of non-empty character description strings.
        """
        return [
            description for description in (
                self.describe_character(entity_name, toward_name=self.player_name)
                for entity_name in self.scenario_entities
                if not self.is_hidden(entity_name)
            ) if description
        ]

    def _current_room(self):
        """!
        @brief The current room's own table, for a multi-room dungeon (see load_scenario_
            definition) -- None for a plain single-room scenario (arena/tavern/field/dungeon),
            which has no [[room]] tables at all.
        @return self.rooms[self.current_room_key], or None.
        """
        if not self.rooms:
            return None
        return self.rooms.get(self.current_room_key)

    def _current_location(self):
        """!
        @brief The current location's own table (see load_scenario_definition's own
            [[location]] notes), or {} if no location is active yet (before the first
            load_scenario() call completes).
        @return self.locations[self.current_location_key], or {}.
        """
        return self.locations.get(self.current_location_key, {})

    def _current_scene_name(self):
        """!
        @brief The name to narrate the current scene with -- the room's own name if the current
            location has one active (ex: "Entrance Hall"), else the current location's own name
            (ex: "The Market Square") -- a location's own name is what's shown when there's no
            room to narrate more specifically (a freeform location, or a room-based one before
            any room is entered). Used for both the initial scenario_loaded payload and every
            later room/location-entered payload, so a scene's intro and every subsequent
            transition are narrated the same way.
        @return The scene name string.
        """
        room = self._current_room()
        return room.get("name", "") if room else self._current_location().get("name", "")

    def _current_scene_description(self):
        """!
        @brief The description to narrate the current scene with -- see _current_scene_name.
        @return The scene description string.
        """
        room = self._current_room()
        return room.get("description", "") if room else self._current_location().get("description", "")

    def load_rules(self, rules_dir):
        full_dir = os.path.join(PROJECT_ROOT, rules_dir)

        if not os.path.exists(full_dir):
            self.event_bus.publish("log_error", f"Rules directory not found: {full_dir}")
            return

        for filename in os.listdir(full_dir):
            if filename.endswith(".toml"):
                filepath = os.path.join(full_dir, filename)
                try:
                    with open(filepath, "rb") as f:
                        data = tomllib.load(f)
                    if "skill" in data:
                        for skill in data["skill"]:
                            self.skills[skill.get("name")] = skill
                    if "entity" in data:
                        for entity in data["entity"]:
                            self.entities[entity.get("name")] = entity
                    if "entity_template" in data:
                        for entity_template in data["entity_template"]:
                            self.entity_templates[entity_template.get("name")] = entity_template
                    for key, value in data.items():
                        if key not in ("skill", "entity", "entity_template"):
                            self.rules[key] = value
                except Exception as e:
                    self.event_bus.publish("log_error", f"Error loading {filename}: {e}")

        # A flat set of every ability name appearing in any [[skill]]'s own "abilities" field --
        # a skill-listed ability is usable by any entity, no ownership check at all. Built once
        # here, alongside self.skills itself, so resolve_named_ability's own skill-list fallback
        # (DM_Combat.py) stays a cheap membership check rather than a per-turn scan over every
        # loaded skill.
        self.universal_abilities = {
            ability_name for skill in self.skills.values() for ability_name in skill.get("abilities", [])
        }

    def get_equip_slots(self, entity_name):
        """!
        @brief Resolves the valid [entity.equipped] slot names for entity_name, from
            rules.toml's own [[equip_slot]] table: a "subtype"-specific entry for this
            entity's own supertype beats a supertype-only entry (no "subtype" key at all),
            same override precedence as get_attitude's name/supertype/default lookup.
        @param entity_name The name of the entity (template or live instance) to look up.
        @return The list of valid slot names, or [] if no [[equip_slot]] entry matches this
                entity's own supertype/subtype at all.
        """
        entity = self.entities.get(entity_name, {})
        supertype = entity.get("supertype")
        subtype = entity.get("subtype")

        supertype_only_slots = None
        for rule in self.rules.get("equip_slot", []):
            if rule.get("supertype") != supertype:
                continue
            if "subtype" in rule:
                if rule.get("subtype") == subtype:
                    return list(rule.get("slots", []))
            elif supertype_only_slots is None:
                supertype_only_slots = list(rule.get("slots", []))

        return supertype_only_slots if supertype_only_slots is not None else []

    def get_current_bulk(self, entity_name, _visited=None):
        """!
        @brief Sums the "bulk" field of every item entity_name is currently carrying (its own
            "inventory" list -- an equipped item is always also listed there, see
            entity_schema.toml's own [entity.equipped] comment, so it's never double-counted),
            plus, now that "bulk" also means something on a creature (see entity_schema.toml's
            own "bulk"/"mount" comments), the load contributed by every currently-present
            entity whose own "mount" currently names entity_name -- each rider's own flat
            "bulk" (body weight), plus their own get_current_bulk (their carried gear) too if
            rules.toml's [bulk] table opts in via "count_rider_gear" (default true). A rider
            mounted on a rider mounted on entity_name is handled the same recursive way (their
            own get_current_bulk already folds in whoever's mounted on *them*); _visited
            guards against a malformed cyclic "mount" chain (never authored in shipped data).
        @param entity_name The name of the entity to total.
        @param _visited Internal recursion guard; never pass explicitly.
        @return The summed bulk, 0 if entity_name carries nothing or is unknown.
        """
        visited = _visited or set()
        if entity_name in visited:
            return 0
        visited = visited | {entity_name}

        entity = self.entities.get(entity_name, {})
        own_cargo = sum(self.entities.get(item_name, {}).get("bulk", 0) for item_name in entity.get("inventory", []))

        count_rider_gear = self.rules.get("bulk", {}).get("count_rider_gear", True)
        rider_load = 0
        for rider_name in self.scenario_entities:
            if rider_name == entity_name or entity_name not in self._resolve_mount_targets(rider_name):
                continue
            rider = self.entities.get(rider_name, {})
            rider_load += rider.get("bulk", 0)
            if count_rider_gear:
                rider_load += self.get_current_bulk(rider_name, visited)

        return own_cargo + rider_load

    def _resolve_mount_targets(self, entity_name):
        """!
        @brief entity_name's own currently-present, still-living "mount" entries (see
            entity_schema.toml's own "mount") -- a bare string or a list, normalized to a
            list here. An absent "mount" field, or one naming something no longer in the
            scene or reduced to 0 HP, is silently dropped rather than raising -- losing a
            mount, by any means, just unwinds the relationship with no error (see
            entity_schema.toml's own "mount" comment).
        @param entity_name The entity whose own "mount" field to resolve.
        @return A list of zero or more real, present, living entity names.
        """
        raw = self.entities.get(entity_name, {}).get("mount")
        if not raw:
            return []
        names = [raw] if isinstance(raw, str) else list(raw)
        return [name for name in names if name in self.scenario_entities and self.get_current_hp(name) > 0]

    def get_carrying_capacity(self, entity_name, _visited=None):
        """!
        @brief entity_name's own real-time load-bearing capacity -- get_max_bulk directly if
            it has no live "mount" of its own (a leaf provider, ex: a horse, a car), else the
            *sum* of every currently-present mount's own get_carrying_capacity (ex: a cart's
            own capacity is whatever its currently-hitched team can bear, never a number
            authored on the cart itself) -- see entity_schema.toml's own "mount" comment for
            why capacity aggregates by sum where _resolve_travel_speed (DM_Travel.py)
            aggregates by minimum. A provider that itself resolves to None (uncapped)
            contributes 0 rather than making the whole sum unknown. _visited guards against a
            malformed cyclic "mount" chain (never authored in shipped data).
        @param entity_name The entity to check.
        @param _visited Internal recursion guard; never pass explicitly.
        @return The summed capacity, or get_max_bulk's own None if entity_name has no live
                mount and no capacity of its own either.
        """
        visited = _visited or set()
        if entity_name in visited:
            return 0
        visited = visited | {entity_name}

        mounts = self._resolve_mount_targets(entity_name)
        if not mounts:
            return self.get_max_bulk(entity_name)
        return sum(self.get_carrying_capacity(name, visited) or 0 for name in mounts)

    def _would_exceed_mount_capacity(self, mount_name, rider_name):
        """!
        @brief Checks whether rider_name mounting (or loading cargo onto) mount_name would
            push mount_name's own current load past its own get_carrying_capacity -- the same
            "would this exceed capacity" shape _bulk_would_be_exceeded already checks for the
            player's own personal inventory, just against a mount's own team-aware capacity
            instead of a flat get_max_bulk. rider_name's own contribution is computed exactly
            the way get_current_bulk already folds in a live rider (their own "bulk" plus,
            if opted in, their own carried gear) -- calling this ahead of actually setting
            "mount" previews the same number get_current_bulk(mount_name) would report the
            instant afterward.
        @param mount_name The entity being mounted/loaded.
        @param rider_name The entity that would newly be mounted on it.
        @return True if this would exceed capacity; always False if get_carrying_capacity
                returns None (uncapped).
        """
        capacity = self.get_carrying_capacity(mount_name)
        if capacity is None:
            return False
        rider = self.entities.get(rider_name, {})
        added = rider.get("bulk", 0)
        if self.rules.get("bulk", {}).get("count_rider_gear", True):
            added += self.get_current_bulk(rider_name)
        return self.get_current_bulk(mount_name) + added > capacity

    def _is_mount_overloaded(self, mount_name):
        """!
        @brief Whether mount_name is *currently* carrying more than it can bear
            (get_current_bulk > get_carrying_capacity) -- unlike _would_exceed_mount_capacity
            (a one-time preview checked only at the moment of mounting/hitching), this is
            re-checked every time movement is attempted, so gear picked up mid-ride, a second
            rider mounting after the first, or a puller dying out of a team can all ground an
            already-underway trip, not just block a fresh one. DM_Movement.py's
            advance_or_retreat and DM_Travel.py's _resolve_grid_travel_intent both refuse to
            move at all while this is true.
        @param mount_name The entity to check.
        @return True if overloaded; always False for an uncapped mount
                (get_carrying_capacity returns None).
        """
        capacity = self.get_carrying_capacity(mount_name)
        if capacity is None:
            return False
        return self.get_current_bulk(mount_name) > capacity

    def _mount_chain(self, entity_name, _visited=None):
        """!
        @brief Every entity reachable by walking entity_name's own "mount" field forward --
            whatever it currently defers to, and whatever *that* in turn defers to. Unlike
            _resolve_mount_targets, not filtered by current scene presence or liveness: this
            is used to decide who should be *carried along* into a new location/room
            (_carry_mounts_into_scene), not to resolve a live stat off someone already known
            to be there -- a mount that's between scenes (ex: about to be re-added by the very
            call this feeds into) still needs to be found by name here.
        @param entity_name The entity whose own mount chain to walk.
        @param _visited Internal recursion guard; never pass explicitly.
        @return A set of entity names (never includes entity_name itself).
        """
        visited = _visited if _visited is not None else {entity_name}
        raw = self.entities.get(entity_name, {}).get("mount")
        names = [] if not raw else ([raw] if isinstance(raw, str) else list(raw))
        chain = set()
        for name in names:
            if name in visited:
                continue
            visited.add(name)
            chain.add(name)
            chain |= self._mount_chain(name, visited)
        return chain

    def _carry_mounts_into_scene(self):
        """!
        @brief Ensures whatever the player currently rides/is hitched to (walked
            transitively via _mount_chain) is actually present in self.scenario_entities --
            called everywhere that list gets rebuilt from scratch on a location/room change
            (_populate_room, _enter_location's freeform branch), since neither of those
            otherwise has any notion of "this ad hoc entity was tagging along" the way an
            authored is_party member (named directly in that location/room's own "entities"
            list) does. Without this, a mounted horse would just vanish the moment the player
            actually arrived anywhere new -- present for the one grid-travel leg whose
            block/speed math already ran, gone for the next. A mount never needs
            re-instancing here -- it already has a live, mutable copy in self.entities from
            whenever it was first mounted/hitched -- this only ever appends a reference.
            Deliberately scoped to the player alone, not every present is_party member, since
            "mount"/"dismount" are themselves player-only intents today (DM_Movement.py) --
            nothing else can actually have a "mount" field set through ordinary play yet.
        """
        for name in self._mount_chain(self.player_name):
            if name not in self.scenario_entities:
                self.scenario_entities.append(name)

    def get_max_bulk(self, entity_name):
        """!
        @brief entity_name's own carrying capacity -- its own authored "max_bulk" field if it
            has one (ex: Rules/Zombie's "riley"/"car", a flat number with no formula behind it),
            else resolves rules.toml's own [bulk] table -- min_bulk plus this entity's own
            "skill" dice times mod_multiplier (ex: Fantasy's min_bulk = 3, mod_multiplier = 2,
            so a 2D strength character carries 3 + 2*2 = 7 bulk before DM_Inventory.py's own
            _bulk_would_be_exceeded starts refusing "take"/"trade" with reason
            "bulk_exceeded"). An authored field always wins over the formula when both are
            available -- an explicit number is a deliberate override, not something a generic
            rule should second-guess.
        @param entity_name The name of the entity to look up.
        @return The max bulk this entity can carry, or None if it authors no "max_bulk" field
                of its own and the current setting authors no [bulk] table either (ex: most of
                Rules/Zombie/) -- callers treat None as "uncapped", never as zero.
        """
        entity = self.entities.get(entity_name, {})
        if "max_bulk" in entity:
            return entity["max_bulk"]
        formula = self.rules.get("bulk")
        if not formula:
            return None
        skill_stats = entity.get("skills", {}).get(formula.get("skill"), {})
        return formula.get("min_bulk", 0) + skill_stats.get("dice", 0) * formula.get("mod_multiplier", 1)

    def _validate_equipped_slots(self):
        """!
        @brief Cross-checks every loaded entity's own [entity.equipped] slot keys against
            get_equip_slots for its supertype/subtype, logging an error for any slot name
            not on that list (ex: a "tail" slot on a humanoid). Called from
            DM_Validation.py's validate_loaded_data, after load_scenario_definition -- not
            from load_rules itself, since a scenario-local entity (declared in a scenario file,
            not one of the shared Rules/<setting>/*.toml catalogs) isn't loaded until after
            load_rules finishes, and needs this same check too. Doesn't block loading -- same
            "malformed data degrades quietly" convention as load_rules' own per-file try/except
            -- just surfaces the mismatch instead of DM_Combat.py silently reading a slot key
            nothing declared.
        """
        for name, entity in self.entities.items():
            equipped = entity.get("equipped")
            if not equipped:
                continue
            valid_slots = self.get_equip_slots(name)
            for slot in equipped:
                if slot not in valid_slots:
                    self.event_bus.publish(
                        "log_error",
                        f"Entity '{name}' equips slot '{slot}', not valid for "
                        f"supertype/subtype {entity.get('supertype')}/{entity.get('subtype')} "
                        f"(valid slots: {valid_slots or 'none'})."
                    )

    def _resolve_player_name(self):
        """!
        @brief Finds the one entity template marked `is_player = true` (ex: characters.toml's
            gladstone) and returns its name, to stand in as the active player character.
        @raises ValueError if no loaded entity template has `is_player = true` -- fatal on
                purpose, same reasoning as load_scenario_definition's missing-scenario-file
                check: silently falling back to some default here would let the rest of
                DMCore run against a player_name that matches no real entity, failing later
                in confusing, indirect ways instead of failing clearly at boot.
        @return The name of the player entity template.
        """
        for name, entity in self.entities.items():
            if entity.get("is_player"):
                return name
        raise ValueError("No entity template has is_player = true; cannot determine the player character.")

    def load_scenario_definition(self, scenario_name):
        """!
        @brief Reads a named scenario file from Rules/Fantasy/scenarios/ into self.scenario/
            self.locations. Scenarios live in their own subdirectory rather than the flat
            Rules/Fantasy/ scan in load_rules (which only keeps whichever [scenario] table it
            reads last), so multiple named scenarios can coexist and one is selected explicitly
            by name.

            A scenario is one or more [[location]] tables (a place: a town square, a building,
            a dungeon), self.scenario's own "start_location" naming which one to begin in --
            see CLAUDE.md's "Scenarios and rooms" for the full [[location]] shape. A location
            may declare "entities" directly (freeform, no bands) and/or its own [[location.room]]
            list (identical shape to a standalone [[room]]: bands/enclosed/entities plus
            [[location.room.exit]] sub-tables to sibling rooms *within that same location*) --
            each location's own "room" TOML array is folded into a {room_key: room_table} dict
            here, exactly like this method already did for self.rooms at the scenario's own top
            level before locations existed, just one level deeper and scoped per-location (a
            room's own "key" only has to be unique within its owning location, not
            scenario-wide). A location's own "exit"/"encounter" lists (see DM_Movement.py's
            _resolve_travel_intent / DM_Encounters.py) are kept as-is, read directly from the
            location table.

            A scenario file may also declare its own [[entity]]/[[entity_template]] tables,
            sibling to [scenario]/[[location]] -- the same two top-level keys load_rules reads
            from every flat Rules/<setting>/*.toml file, just scoped to this one scenario
            instead. This is what lets a scenario-specific entity (a boss, a one-off prop) or a
            scenario-specific NPC-generation stub (see "NPC generation") live in the same file
            as the scenario that references it, rather than having to be authored into a shared
            file like creatures.toml/items.toml just to be nameable at all -- every location's
            own "entities"/room "entities" list resolves a "name"/"template" against
            self.entities/self.entity_templates exactly the same way regardless of which file
            actually defined it. Loaded after load_rules (see DMCore.__init__/
            DM_Persistence.py's load_game, both of which call load_rules immediately before
            this), so a scenario-local entity/template can reuse a shared name on purpose to
            override it for this one scenario, and so both are re-populated fresh on every
            load, same as everything load_rules itself loads.
        @param scenario_name The scenario's filename without extension (ex: "arena", "tavern").
        @raises FileNotFoundError if no matching scenario file exists. Unlike load_rules'
                blanket per-file try/except, a missing/malformed scenario is fatal on purpose:
                silently continuing with an empty self.scenario used to let LLMCore narrate an
                opening scene with no name/description, which the LLM would happily hallucinate
                (ex: a "featureless gray void") with no indication anything had gone wrong.
        """
        filepath = scenario_file_path(scenario_name, self.setting)

        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Scenario '{scenario_name}' not found (expected {filepath}).")

        with open(filepath, "rb") as f:
            data = tomllib.load(f)
        self.scenario = data.get("scenario", {})
        self.locations = {}
        for location in data.get("location", []):
            location = dict(location)
            location["rooms"] = {room.get("key"): room for room in location.get("room", [])}
            self.locations[location.get("key")] = location
        for entity in data.get("entity", []):
            self.entities[entity.get("name")] = entity
        for entity_template in data.get("entity_template", []):
            self.entity_templates[entity_template.get("name")] = entity_template
        # Reset per-playthrough location/room state -- populated for real by _enter_location
        # (called from load_scenario(), below) once entities/templates are all loaded.
        self.current_location_key = None
        self.location_runtime = {}
        self.rooms = {}
        self.current_room_key = None
        self.visited_rooms = {}
        self.persistent_entities = []
        # Both persist for the rest of this DMCore's lifetime (until the next
        # load_scenario_definition -- __init__ or load_game) -- see _instance_entities' own
        # docstring for why this has to survive across separate calls now, not just within one.
        self.entity_occurrence_counts = {}
        # name -> a pristine deep copy of self.entities[name] as it stood the moment it was
        # first ever instanced, captured before _place_new_entity's own self.entities[name] =
        # instance overwrite -- see _instance_entities' own docstring.
        self._pristine_entity_templates = {}
        # The exact chronological sequence of "a location/room scope was instanced for the
        # first time" events this playthrough -- ("location", location_key) or ("room",
        # location_key, room_key) tuples, appended by _enter_location/_populate_room's own
        # cache-miss branches (never by _instance_entities itself, which also runs for
        # encounter-conjured entities that aren't a location/room scope at all). Saved and
        # replayed verbatim by DM_Persistence.py's load_game, so a reload's own from-scratch
        # re-instancing reproduces the same "wolf"/"wolf_2" assignments the original live
        # playthrough made even when two *different* locations were visited in an interleaved
        # order -- see _instance_entities' own docstring for the disambiguation this feeds.
        self.entity_instancing_order = []

    def _instance_entities(self, entity_entries, party_pool=None, skip_llm_generation=False):
        """!
        @brief Instantiates a list of entity entries as independent copies of their
            templates, so duplicate creatures (ex: two wolves) get separate HP/conditions
            instead of sharing the same template dict. Used for the scenario's own top-level
            "entities" list (the player, and any other entity meant to persist across the
            whole playthrough -- see load_scenario) and for a single room's own "entities"
            list (see _populate_room) -- a room's list never includes the player at all, so
            this never needs to special-case it.

            An entry names its template one of two mutually exclusive ways:
            - "name" -- looked up in self.entities, a real, directly usable entity/creature
              template. PLAYER_PLACEHOLDER ("player") resolves to self.player_name instead of
              being looked up literally -- no template is ever actually named "player"; every
              shipped scenario/room references the player this way rather than a specific
              character's own template name (ex: "gladstone"), so a scenario keeps working
              unchanged regardless of which template is_player=true or whatever a freshly
              created character was renamed to (see DM_CharacterCreation.py).
            - "template" -- looked up in self.entity_templates instead (see, ex:
              Rules/Fantasy/scenarios/tavern_random.toml's own [[entity_template]] tables), a
              stub with no hand-authored [entity.skills]/
              max_hp/name of its own. NpcGenerationMixin._apply_npc_generation fills those in
              immediately after the instance is stored, mutating it in place (it has to
              already be in self.entities by then, since the CR-fitting math calls back into
              get_challenge_rating/_best_damage_dice_pips, both keyed off self.entities[name]).
              Keeping these in a separate dict, resolved only via "template" (never "name"),
              is what makes a generation stub impossible to reference by accident -- a typo'd
              `name = "generated_innkeeper"` fails the same "unknown entity" way a typo'd real
              name would, rather than silently working.
        @param entity_entries A list of {name, band} or {template, band} tables.
        @param party_pool Forwarded to _apply_npc_generation for a "template" entry's own
            target_cr = "party" resolution -- entities already known to be part of the party
            *before* this call started (self.persistent_entities for a room-level call, []
            for the top-level scenario call; see _apply_npc_generation's own docstring for why
            self.scenario_entities itself can't be used here). Ignored by every "name" entry.
        @param skip_llm_generation Forwarded to _apply_npc_generation -- true while
            re-instancing during a save-game load, where whatever a fresh generation call
            would produce is about to be overwritten by the saved values anyway (see
            DM_Persistence.py's load_game).
        @return The list of instance names created, in entity_entries order -- excluding any
                entry whose resolved instance_name is in self.removed_entities (see
                DM_Improvisation.py's remove_entity_from_scene), which is skipped entirely
                rather than being re-instanced.

            Disambiguation (self.entity_occurrence_counts) is scoped to this whole DMCore's
            lifetime, not to one call -- deliberately not a fresh per-call counter, and
            deliberately not _unique_entity_key's own "check self.entities" logic either (see
            that method's own docstring for the *different* case it solves). self.entities is
            one flat namespace for the entire game, so two *different* calls (ex: two different
            rooms in the same multi-room dungeon that happen to declare the same creature name)
            have to be disambiguated against each other too, not just within their own
            entity_entries list -- a call-scoped counter would have both independently produce
            "wolf" and silently collide, the second call's own _place_new_entity overwriting the
            first's live HP/conditions. A shared, ever-incrementing counter fixes that: whichever
            call happens first claims the bare name, every later call with the same template_name
            gets the next suffix, regardless of which scope it came from. self._pristine_entity_
            templates (a "name"-branch-only sibling cache, same lifetime) fixes the other half of
            the same problem: without it, a second occurrence's own deep copy would come from
            whatever the first occurrence's own _place_new_entity already overwrote self.entities
            [template_name] with -- a live, possibly-wounded instance -- rather than the original
            authored data.

            This still stays correct for DM_Persistence.py's load_game, which re-derives every
            visited location/room from scratch rather than trusting live instance names: every
            call site that could otherwise repeat the *same* scope's own instancing (a location
            already cached, a room already in self.visited_rooms) is gated by its caller's own
            cache check (_enter_location's "persistent_names" cache, _populate_room's
            visited_rooms cache) before it ever reaches here -- so within one load_game() call,
            each location/room scope's own entities are only ever instanced once. self.entity_
            instancing_order (appended by those same two cache-miss branches, never by this
            method) records the exact chronological sequence those scopes were first instanced
            in, live -- saved and replayed verbatim by load_game, so the disambiguation above
            reproduces the original playthrough's own "wolf"/"wolf_2" assignments even when the
            player interleaved visits across two *different* locations (left one location
            mid-dungeon, visited a second, then returned to the first for new rooms), not just
            within a single location's own rooms. A save written before this ordering existed
            has no self.entity_instancing_order of its own; load_game falls back to its own
            previous nested location-then-rooms replay for one of those, which stays exactly as
            correct as it always was for the non-interleaved case.
        """
        party_pool = party_pool if party_pool is not None else []
        instance_names = []
        occurrence_counts = self.entity_occurrence_counts

        for entry in entity_entries:
            is_generated_template = "template" in entry
            if is_generated_template:
                template_name = entry.get("template")
                template = self.entity_templates.get(template_name)
                if template is None:
                    self.event_bus.publish(
                        "log_error", f"Scenario references unknown entity template: {template_name}"
                    )
                    continue
            else:
                template_name = entry.get("name")
                if template_name == PLAYER_PLACEHOLDER:
                    template_name = self.player_name
                template = self.entities.get(template_name)
                if template is None:
                    self.event_bus.publish("log_error", f"Scenario references unknown entity: {template_name}")
                    continue
                # self.entities holds templates and live instances under the same key -- the
                # very first entry naming template_name still finds the pristine, hand-authored
                # data here (nothing has instanced it yet), but a *second* entry (a different
                # room/location scope reusing the same creature name -- the whole reason
                # self.entity_occurrence_counts now spans more than one call, above) would
                # otherwise find whatever the first entry's own _place_new_entity already
                # overwrote that key with instead: a live, possibly-wounded instance, not the
                # original template. Snapshotting it here, once, the first time this
                # template_name is ever seen, is what keeps every later occurrence a clean copy
                # of the authored original regardless of what's happened to the first instance
                # since.
                if template_name not in self._pristine_entity_templates:
                    self._pristine_entity_templates[template_name] = copy.deepcopy(template)
                template = self._pristine_entity_templates[template_name]

            occurrence_counts[template_name] = occurrence_counts.get(template_name, 0) + 1
            occurrence = occurrence_counts[template_name]
            instance_name = template_name if occurrence == 1 else f"{template_name}_{occurrence}"

            # Forcibly removed via ImprovisationMixin.remove_entity_from_scene
            # (DM_Improvisation.py) at some point this playthrough -- never respawn it just
            # because a scenario/room's own static "entities" list still names it, whether
            # this is a fresh room visit or a save reload. occurrence_counts above is still
            # incremented first, so a later same-named entry in this same list keeps
            # disambiguating correctly regardless.
            if instance_name in self.removed_entities:
                self.event_bus.publish("log_info", f"Skipping removed entity: {instance_name}")
                continue

            instance = copy.deepcopy(template)
            # Defaults to band 1 for any entry that doesn't specify one.
            self._place_new_entity(instance_name, instance, entry.get("band", 1))
            if is_generated_template:
                self._apply_npc_generation(instance_name, party_pool, instance_names, skip_llm_generation)
            self._auto_roll_notice(instance_name)
            instance_names.append(instance_name)

        return instance_names

    def _place_new_entity(self, name, entity, band):
        """!
        @brief The shared primitive behind every path that turns a raw entity dict into a live
            self.entities participant -- scenario/room loading (_instance_entities, above) and
            every ad hoc placement in DM_Improvisation.py (plain item, container/trap, conjured
            creature) all go through this rather than each hand-writing the same three fields.
            Deliberately thin: scenario_entities insertion, current_target claiming, and
            item_catalog_updated publishing differ per caller (front-insert vs end-append vs
            never; claimed vs not; published vs not) and stay the caller's own job.
        @param name The entity's own self.entities key (already disambiguated by the caller --
            _instance_entities' own occurrence_counts for a load batch, _unique_entity_key
            (below) for a single ad hoc placement -- two genuinely different scopes this
            primitive doesn't need to know about).
        @param entity The raw entity dict (a deep-copied template, or one an LLM just invented).
            Mutated in place and also returned for convenience.
        @param band Objective, 1-indexed band position (see DM_Movement.py) -- every entity
            gets one, the player included, so gaps are computed the same way for everyone.
        @return entity, mutated in place and now stored at self.entities[name].
        """
        entity["entity_id"] = name
        entity["band"] = band
        # "conditions" is a template's own starting state (ex: a chest's
        # [entity.conditions.locked]); "active_conditions" is the per-instance runtime dict
        # apply_condition/dismiss_condition mutate. setdefault -- not an unconditional overwrite
        # -- is what lets one rule serve every caller: a real template's "conditions" gets
        # copied in (instance never has "active_conditions" of its own yet); an ad hoc
        # container/trap's own already-authored active_conditions (locked/closed, armed -- see
        # AdHoc_Generation.py) is left untouched rather than wiped; an ad hoc creature with
        # neither key falls back to {}.
        entity.setdefault("active_conditions", dict(entity.get("conditions", {})))
        self.entities[name] = entity
        return entity

    def _unique_entity_key(self, base_name):
        """!
        @brief Picks a self.entities key guaranteed not to collide with anything currently live
            (or the player) for DM_Improvisation.py's own ad hoc item/creature placement -- a
            single name straight from the LLM, never enum-constrained, unlike
            decide_entity_removal/decide_entity_edit's own name field. Checked against the live
            self.entities universe directly (unlike _instance_entities' own
            self.entity_occurrence_counts, which tracks claimed names itself rather than
            re-deriving them from self.entities -- see that method's own docstring for why: a
            location/room scope that load_game re-instances has to stay idempotent across that
            one call, and self.entities can hold a stale, not-yet-overwritten previous instance
            at the moment a scope is re-derived). A single ad hoc placement has no such
            repeat-call concern -- it only ever runs once, live -- so checking self.entities
            directly is both safe and necessary here: without it, self.entities[name] = entity
            would silently clobber whatever already held that
            key, up to and including the player entity itself if the LLM happened to invent a
            matching name. entity["name"] (the display text narration reads) is left untouched
            by the caller -- DM_Social.py's describe_character already falls back to
            entity.get("name", entity_name) for exactly this dict-key-vs-display-name split, the
            same split a generated NPC's own occurrence-suffixed instance name already relies on.
        @param base_name The LLM-invented name to disambiguate.
        @return base_name unchanged if free, else base_name with a "_2", "_3", ... suffix.
        """
        if base_name != self.player_name and base_name not in self.entities:
            return base_name
        suffix = 2
        while f"{base_name}_{suffix}" in self.entities or f"{base_name}_{suffix}" == self.player_name:
            suffix += 1
        return f"{base_name}_{suffix}"

    def _auto_roll_notice(self, instance_name):
        """!
        @brief Silently rolls an entity's own [entity.notice] check the moment it's instanced,
            if it starts with the "hidden" condition active (ex: items.toml's dart trap) --
            unlike [entity.test] (player-initiated, matched against spoken skill/input),
            nothing about this needs player input at all: it's the DM checking, on the
            player's behalf, whether they'd have spotted it just by being in the room. A
            passed check dismisses "hidden" immediately, so the entity joins the roster
            _describe_scenario_characters builds like anything else; a failed one leaves it
            hidden -- and since this only ever runs once, at instancing (the same "state
            carries forward exactly as it was left" rule every other per-room condition
            already follows -- see _populate_room/visited_rooms), a hidden hazard that was
            missed on first entry stays missed for the rest of that playthrough rather than
            re-rolling for free on every revisit.
        @param instance_name The freshly-instanced entity to check (a no-op for anything with
            no [entity.notice] table, or that doesn't start "hidden" -- ex: the player, an
            ally, or any ordinary creature/object).
        """
        entity = self.entities.get(instance_name, {})
        notice = entity.get("notice")
        if not notice or not self.has_condition(instance_name, "hidden"):
            return
        result = self.resolve_action(self.player_name, notice.get("skill", ""), notice.get("difficulty", 0))
        if result["success"]:
            self.dismiss_condition(instance_name, "hidden")

    def _populate_room(self, room_key, skip_llm_generation=False):
        """!
        @brief Instances (or, for a room visited before, restores) room_key's own "entities"
            list and merges it with whatever's already persistent in self.persistent_entities
            (the player, plus anything else declared at [scenario].entities -- ex: crypt.toml's
            "thane") -- shared by load_scenario (the dungeon's starting room) and enter_room
            (every later transition), so both go through the exact same visited-rooms
            bookkeeping. A room's own entity list never includes the player -- only room-local
            things (creatures/traps/chests) -- which is what lets a revisit restore *exactly*
            the same instances (a cleared trap stays cleared, a dead creature stays dead, a
            looted chest stays empty) without needing to reconcile a player entry that would
            otherwise appear once per room in the TOML for no reason beyond bookkeeping.
        @param room_key The room to populate, matching a key in self.rooms.
        @param skip_llm_generation Forwarded to _instance_entities -- see its own docstring.
        """
        room = self.rooms.get(room_key, {})
        if room_key in self.visited_rooms:
            room_entities = list(self.visited_rooms[room_key])
        else:
            # self.persistent_entities is already finalized by the time any room is
            # populated (load_scenario sets it right before its own _populate_room call
            # below; every later call is via enter_room, long after) -- safe to pass as the
            # live party pool for a room-local generate=true template's own target_cr =
            # "party" resolution, unlike self.scenario_entities itself (see
            # _apply_npc_generation's docstring).
            room_entities = self._instance_entities(
                room.get("entities", []), party_pool=self.persistent_entities,
                skip_llm_generation=skip_llm_generation,
            )
            self.visited_rooms[room_key] = list(room_entities)
            self.entity_instancing_order.append(("room", self.current_location_key, room_key))
        self.scenario_entities = list(self.persistent_entities) + room_entities
        self._carry_mounts_into_scene()

    def load_scenario(self, skip_llm_generation=False):
        """!
        @brief Enters the scenario's own start_location -- see _enter_location for what that
            actually does. Always a fresh instancing (covers __init__, load_game, and ad-hoc
            test scenarios that reassign self.scenario/self.locations directly and call this
            again) -- see "Scenario instancing" in CLAUDE.md.
        @param skip_llm_generation Forwarded to _enter_location -- true only from
            DM_Persistence.py's load_game (re-instancing a save shouldn't pay for a real
            LLM round trip just to immediately overwrite the result with saved values).
        """
        # Seeds any location a scenario wants already known ahead of a first visit (ex:
        # plains.toml's own known_locations -- ordinarily "having visited it," but "some other
        # in-fiction means" per docs/downtime.md's "Travel" covers a map the player starts
        # with). A no-op union with whatever's already known -- harmless to re-run on every
        # load_scenario call (__init__, load_game, an ad hoc test scenario).
        self.known_locations.update(self.scenario.get("known_locations", []))
        self._enter_location(self.scenario.get("start_location"), skip_llm_generation=skip_llm_generation)
        self.event_bus.publish("log_info", f"Scenario loaded: {self.scenario_entities}")

    def _instance_location_persistent_names(self, location, skip_llm_generation=False):
        """!
        @brief Instances a location's own "entities" list (whoever persists across the whole
            location, ex: crypt's thane/anne) and guarantees the player is among them --
            shared by _enter_location (below) and DM_Persistence.py's load_game, which both
            need this exact same "build this location's own persistent_names, once" logic
            (load_game pre-populates every visited location's own cache up front, before
            _enter_location ever runs, so a saved mid-playthrough location switch finds it
            already there instead of re-instancing).
        @param location A [[location]] table (self.locations[some_key]).
        @param skip_llm_generation Forwarded to _instance_entities.
        @return The list of persistent instance names, player_name always included.
        """
        # party_pool = [] here: nothing else about this location is known yet (this is what's
        # *building* this location's own persistent_names) -- same reasoning load_scenario's
        # old top-level call already followed, just scoped per-location.
        persistent_names = self._instance_entities(
            location.get("entities", []), party_pool=[], skip_llm_generation=skip_llm_generation,
        )
        # The player doesn't need to be (and, in a multi-location scenario, should NOT be)
        # named in a location's own "entities" -- unlike thane/anne/etc., re-instancing the
        # player via _instance_entities on every new location's first visit would silently
        # wipe active_conditions (any status effect gained mid-playthrough), since that
        # unconditionally overwrites from the template's static "conditions" field. The player
        # is guaranteed present here without ever touching self.entities[player_name] itself --
        # its band is set explicitly by _enter_location regardless of this list.
        if self.player_name not in persistent_names:
            persistent_names.insert(0, self.player_name)
        return persistent_names

    def _enter_location(self, location_key, arrival_room=None, arrival_band=1, skip_llm_generation=False):
        """!
        @brief Moves the player into a different [[location]] -- the location-graph
            counterpart to enter_room's room-graph move, called both for the scenario's own
            starting location (load_scenario, above) and for every later player-issued travel
            (DM_Movement.py's _resolve_travel_intent). self.rooms/self.current_room_key/
            self.visited_rooms keep their exact existing meaning (see enter_room/_populate_room/
            _find_room_exit, all otherwise UNCHANGED) -- this method just re-points them at
            whichever location is now active, via self.location_runtime's own per-location
            cache (location_key -> {"persistent_names", "visited_rooms"}), the same "instance
            once, restore thereafter" treatment visited_rooms itself already gives a single
            room.

            A location's own "entities" list (if any) is instanced exactly once per location,
            the first time it's ever entered, and cached as this location's own
            "persistent_names" -- exactly the role self.persistent_entities/[scenario].entities
            already play today, just scoped per-location instead of scenario-wide (see
            location_schema.toml's own note on why "entities" isn't mutually exclusive with
            having rooms: a party member persisting across every room of *this* location, ex:
            crypt's thane/anne, still needs this same list). For a location with no rooms at
            all, that's the entire scene (freeform, no band positioning); for one that does have
            [[location.room]], self.current_room_key (arrival_room, or the location's own
            start_room) is populated via the existing _populate_room, merging this location's
            own persistent_names with that specific room's own local entities exactly as today.
        @param location_key Another [[location]]'s own "key".
        @param arrival_room Which of the destination location's own [[location.room]] entries to
            land in -- defaults to that location's own "start_room" if absent. Ignored for a
            freeform (room-less) location.
        @param arrival_band Where the player ends up within arrival_room -- ignored for a
            freeform location, where the player is always pinned to band 1 (no real positioning).
        @param skip_llm_generation Forwarded to _instance_entities/_populate_room -- true only
            from DM_Persistence.py's load_game.
        """
        self.current_location_key = location_key
        # Grid-based travel's own gate (see DM_Travel.py/docs/downtime.md's "Travel") -- every
        # location entered, gridded or not, becomes reachable-by-name from now on. A set, so
        # revisiting somewhere already known is a no-op.
        self.known_locations.add(location_key)
        location = self.locations.get(location_key, {})
        cache = self.location_runtime.setdefault(location_key, {})

        if "persistent_names" not in cache:
            cache["persistent_names"] = self._instance_location_persistent_names(
                location, skip_llm_generation=skip_llm_generation,
            )
            self.entity_instancing_order.append(("location", location_key))
        # A direct reference, not a copy -- remove_entity_from_scene mutates
        # self.persistent_entities in place (ex: removing a party member), which has to reach
        # this location's own cache directly so a later re-entry doesn't resurrect it. Mirrors
        # self.visited_rooms below, which is already a direct reference for the same reason.
        self.persistent_entities = cache["persistent_names"]

        if location.get("rooms"):
            self.rooms = location["rooms"]
            self.visited_rooms = cache.setdefault("visited_rooms", {})
            self.current_room_key = arrival_room or location.get("start_room")
            self._populate_room(self.current_room_key, skip_llm_generation=skip_llm_generation)
            self.entities[self.player_name]["band"] = arrival_band
        else:
            self.rooms = {}
            self.current_room_key = None
            self.visited_rooms = {}
            self.scenario_entities = list(self.persistent_entities)
            self._carry_mounts_into_scene()
            # No bands to speak of in a freeform location -- pinning everyone (the player
            # included) to band 1 is what keeps is_in_range/get_distance_between correct with
            # zero special-casing (see DM_Movement.py's _clamp_band).
            self.entities[self.player_name]["band"] = 1

        # Snaps thane/anne/etc. into formation around wherever the player actually starts --
        # a party member's own TOML-authored "band" is a starting guess, not authoritative,
        # since _apply_party_formation always wins on the very next player move anyway (see
        # DM_Movement.py); doing it here too means a location/room that starts the player
        # somewhere other than band 1 doesn't leave the party visibly out of formation before
        # anyone's taken a single action.
        self._apply_party_formation()
        # A carried-along mount needs its own band snapped to the player's too -- their own
        # TOML-authored "entities" list obviously never mentions it (see
        # _carry_mounts_into_scene above), so nothing else here would ever place it correctly.
        self._sync_mount_bands(self.player_name)

        # Keeps current_target in sync with scenario_entities on every location entry -- covers
        # __init__, load_game, and every later travel/room move alike.
        self.current_target = self._choose_combat_target()

        self._resolve_location_encounter(self._current_room() or location)
        self._run_on_enter_programs()

    def _run_on_enter_programs(self):
        """!
        @brief Runs every present scene entity's own [entity.on_enter] program -- an entity
            opts in on its own template; nothing fires for one that doesn't declare it. No
            "actor" role for
            this trigger (same as on_round_upkeep) -- there's no single entity this is "done by",
            it's a passive reaction to the scene itself being entered. Called once per
            _enter_location, after this location/room's own [[location.encounter]] roll.
        """
        for entity_name in list(self.scenario_entities):
            entity = self.entities.get(entity_name, {})
            program = entity.get("on_enter")
            if program:
                run_program(program, {"actor": None, "target": entity_name}, self.entities, self.rules, self.event_bus)

    def enter_room(self, room_key, arrival_band=1, skip_llm_generation=False):
        """!
        @brief Moves the player to a different room in the same multi-room dungeon --
            DMCore._on_item_interaction_detected's "move" handling is the only live-gameplay
            caller (gated there on the current room actually declaring a matching exit -- see
            DMCore._find_room_exit -- and being clear of living hostiles first), always with
            skip_llm_generation left at its default (a real move should really generate);
            DM_Persistence.py's load_game is the only caller that ever passes True, to restore
            a saved current room without paying for a throwaway LLM call. Never touches
            the player's own live instance beyond repositioning their band (see
            _populate_room) -- HP, inventory, currency, and conditions carry over across
            rooms exactly as combat/looting left them. A room visited before is restored
            from visited_rooms rather than re-instanced (see _populate_room); a room visited
            for the first time is instanced fresh, same as any scenario load. The party
            (thane/anne/etc.) follows the player through the doorway too --
            _apply_party_formation snaps them back into formation around the player's own
            arrival_band, rather than leaving them stranded at whatever band they happened to
            occupy in the room just left.
        @param room_key The target room's own "key", as named in the exit that's moving here.
        @param arrival_band Where the player ends up in the new room -- the exit's own
            "arrival_band" (defaulting to 1, "just past the doorway", if the exit doesn't
            specify one).
        @param skip_llm_generation Forwarded to _populate_room -- see load_scenario's own
            docstring.
        @return True if room_key names a real room and the move happened, False otherwise
                (ex: a stale/typo'd exit reference -- callers are expected to have already
                validated room_key came from the current room's own declared exits).
        """
        if room_key not in self.rooms:
            return False

        self.current_room_key = room_key
        self.entities[self.player_name]["band"] = arrival_band
        self._populate_room(room_key, skip_llm_generation=skip_llm_generation)
        self._apply_party_formation()
        self._sync_mount_bands(self.player_name)

        self.current_target = self._choose_combat_target()
        self.event_bus.publish("log_info", f"Entered room '{room_key}': {self.scenario_entities}")
        return True
