import copy
import os
import tomllib

from DM_Types import DMCoreProtocol

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
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "Rules", setting, "scenarios", f"{scenario_name}.toml")


def list_available_scenarios(setting="Fantasy"):
    """!
    @brief Every real gameplay scenario under Rules/<setting>/scenarios/, for a UI to offer as
        a choice before any DMCore exists -- same "pure, DMCore-independent, re-scan the TOML
        directly" precedent Character_Creation.py's load_character_creation_data sets, since
        GUICore needs this list before it can know whether a DMCore will ever exist.
        character_test.toml is deliberately excluded -- it's a minimal scenario built solely
        for TestCharacterCreationRename, not a real one to offer a player.
    @param setting Which Rules/ subdirectory to scan -- see scenario_file_path.
    @return A list of (scenario_key, display_name, description) tuples, sorted by key. A
        scenario file that's missing or fails to parse is silently skipped, the same
        per-file leniency load_rules applies to every other TOML file.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    scenarios_dir = os.path.join(base_dir, "Rules", setting, "scenarios")

    results = []
    if not os.path.isdir(scenarios_dir):
        return results
    for filename in sorted(os.listdir(scenarios_dir)):
        if not filename.endswith(".toml"):
            continue
        key = filename[: -len(".toml")]
        if key == "character_test":
            continue
        try:
            with open(os.path.join(scenarios_dir, filename), "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        scenario_table = data.get("scenario", {})
        results.append((key, scenario_table.get("name", key), scenario_table.get("description", "")))
    return results


class RulesMixin(DMCoreProtocol):
    """!
    @brief TOML rules/entity loading and scenario instancing (DMCore mixin -- only ever
        composed into DMCore, never instantiated on its own; relies on
        self.skills/self.entities/self.rules/self.scenario/self.scenario_entities/
        self.event_bus/self.player_name, set up by DMCore.__init__).
        _describe_scenario_characters calls self.describe_character (SocialMixin). Inherits
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

    def _current_scene_name(self):
        """!
        @brief The name to narrate the current scene with -- the room's own name if this is a
            multi-room dungeon (ex: "Entrance Hall"), else the flat scenario's own name (ex:
            "The Rusty Tankard"). Used for both the initial scenario_loaded payload and every
            later room_entered payload, so a multi-room dungeon's intro and every subsequent
            room transition are narrated the same way.
        @return The scene name string.
        """
        room = self._current_room()
        return room.get("name", "") if room else self.scenario.get("name", "")

    def _current_scene_description(self):
        """!
        @brief The description to narrate the current scene with -- see _current_scene_name.
        @return The scene description string.
        """
        room = self._current_room()
        return room.get("description", "") if room else self.scenario.get("description", "")

    def load_rules(self, rules_dir):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        full_dir = os.path.join(base_dir, rules_dir)

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

        self._validate_equipped_slots()

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

    def _validate_equipped_slots(self):
        """!
        @brief Cross-checks every loaded entity's own [entity.equipped] slot keys against
            get_equip_slots for its supertype/subtype, logging an error for any slot name
            not on that list (ex: a "tail" slot on a humanoid). Called once load_rules has
            finished reading every *.toml file, since an entity template (characters.toml)
            and rules.toml's own [[equip_slot]] table can load in either order within the
            same directory scan. Doesn't block loading -- same "malformed data degrades
            quietly" convention as load_rules' own per-file try/except -- just surfaces the
            mismatch instead of DM_Combat.py silently reading a slot key nothing declared.
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
        @brief Reads a named scenario file from Rules/Fantasy/scenarios/ into self.scenario.
            Scenarios live in their own subdirectory rather than the flat Rules/Fantasy/
            scan in load_rules (which only keeps whichever [scenario] table it reads last),
            so multiple named scenarios can coexist and one is selected explicitly by name.

            A scenario file is either a plain single room (arena/tavern/field/dungeon --
            entities listed directly under [scenario]) or a multi-room dungeon: one or more
            [[room]] tables, each with its own "entities"/"bands"/"enclosed" plus a list of
            [[room.exit]] sub-tables ({band, direction, destination, arrival_band}), and
            [scenario].start_room naming which room to begin in. A room's own "entities"
            list never includes the player -- only room-local creatures/traps/chests -- the
            player (and anything else meant to persist across the whole dungeon) is instead
            listed once, at the top level, under [scenario].entities, positioned at their
            starting band in the starting room (see load_scenario/_populate_room). Each exit
            is only usable from the specific band it names, which is what lets a room have
            more than one exit at all (ex: one exit at band 2 leading right to a side room,
            another at band 3 continuing forward) -- a real branch, not just a corridor's
            forward/back pair. self.rooms stays an empty dict for the plain-scenario shape --
            load_scenario/enter_room both branch on "is self.rooms populated" rather than a
            separate flag, so a plain scenario file never has to declare anything extra just
            to opt out of the room-graph machinery.
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
        self.rooms = {room.get("key"): room for room in data.get("room", [])}
        self.current_room_key = self.scenario.get("start_room")
        # room_key -> the list of instance names created for it the first time it was
        # entered (see enter_room) -- what makes a revisited room stay exactly as the player
        # left it (a cleared trap stays cleared, a dead creature stays dead, a looted chest
        # stays empty) instead of respawning fresh from template on every visit.
        self.visited_rooms = {}

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
            - "template" -- looked up in self.entity_templates instead (see
              Rules/Fantasy/templates.toml), a stub with no hand-authored [entity.skills]/
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
        @return The list of instance names created, in entity_entries order.
        """
        party_pool = party_pool if party_pool is not None else []
        instance_names = []
        occurrence_counts = {}

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

            occurrence_counts[template_name] = occurrence_counts.get(template_name, 0) + 1
            occurrence = occurrence_counts[template_name]
            instance_name = template_name if occurrence == 1 else f"{template_name}_{occurrence}"

            instance = copy.deepcopy(template)
            instance["entity_id"] = instance_name
            # Objective, 1-indexed band position (see DM_Movement.py) -- every entity gets
            # one, the player included, so gaps are computed the same way for everyone.
            # Defaults to band 1 for any entry that doesn't specify one.
            instance["band"] = entry.get("band", 1)
            # "conditions" is the template's starting state (ex: a chest's [entity.conditions.locked]);
            # "active_conditions" is the per-instance runtime dict apply_condition/dismiss_condition
            # mutate, so it must start as its own copy rather than sharing the template's dict.
            instance["active_conditions"] = dict(instance.get("conditions", {}))
            self.entities[instance_name] = instance
            if is_generated_template:
                self._apply_npc_generation(instance_name, party_pool, instance_names, skip_llm_generation)
            self._auto_roll_notice(instance_name)
            instance_names.append(instance_name)

        return instance_names

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
        if not notice or "hidden" not in entity.get("active_conditions", {}):
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
        self.scenario_entities = list(self.persistent_entities) + room_entities

    def load_scenario(self, skip_llm_generation=False):
        """!
        @brief Instances the scenario's own top-level "entities" list -- for a plain
            single-room scenario (arena/tavern/field/dungeon) that's everyone in the scene;
            for a multi-room dungeon it's just whatever persists across the whole
            playthrough (today, only the player, positioned at their own starting band --
            see load_scenario_definition), with the *starting room's* own entities merged in
            via _populate_room. Always a fresh instancing (covers __init__, load_game, and
            ad-hoc test scenarios that reassign self.scenario/self.rooms directly and call
            this again) -- see "Scenario instancing" in CLAUDE.md.
        @param skip_llm_generation Forwarded to _instance_entities/_populate_room -- true only
            from DM_Persistence.py's load_game (re-instancing a save shouldn't pay for a real
            LLM round trip just to immediately overwrite the result with saved values).
        """
        # party_pool = [] for this top-level call: nothing persistent exists yet (this is
        # what's *building* self.persistent_entities), so a generate=true template at the
        # scenario's own top level resolving target_cr = "party" can only see whichever
        # player/is_party entities have already been instanced earlier in this same list --
        # see _apply_npc_generation's own docstring for why self.scenario_entities itself
        # isn't usable here.
        self.scenario_entities = self._instance_entities(
            self.scenario.get("entities", []), party_pool=[], skip_llm_generation=skip_llm_generation,
        )
        self.persistent_entities = list(self.scenario_entities)
        if self.rooms:
            self._populate_room(self.current_room_key, skip_llm_generation=skip_llm_generation)

        # Snaps thane/anne/etc. into formation around wherever the player actually starts --
        # a party member's own TOML-authored "band" is a starting guess, not authoritative,
        # since _apply_party_formation always wins on the very next player move anyway (see
        # DM_Movement.py); doing it here too means a scenario/room that starts the player
        # somewhere other than band 1 doesn't leave the party visibly out of formation before
        # anyone's taken a single action.
        self._apply_party_formation()

        # Keeps current_target in sync with scenario_entities on every load -- covers
        # __init__, load_game, and ad-hoc test scenarios that reassign self.scenario directly
        # and call load_scenario() again (see CLAUDE.md's "Scenario instancing").
        self.current_target = self._choose_combat_target()

        self.event_bus.publish("log_info", f"Scenario loaded: {self.scenario_entities}")

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

        self.current_target = self._choose_combat_target()
        self.event_bus.publish("log_info", f"Entered room '{room_key}': {self.scenario_entities}")
        return True
