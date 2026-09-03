from dm.DM_Combat import MOVEMENT_ACTIONS, TRANSFER_ACTIONS
from dm.DM_Rules import PLAYER_PLACEHOLDER
from dm.DM_Types import DMCoreProtocol

# Entity fields (see Rules/Fantasy/reference/template_schema.toml's own note) an
# [[entity_template]] must never author -- NPC_Generation.py fills these in at instancing time,
# fit to a target challenge rating, and never reads an authored value here at all. "name" is
# deliberately not in this list -- unlike skills/max_hp, a template's own "name" is required
# (load_rules indexes self.entity_templates by it, same as a real entity); generation only ever
# overwrites the *live instance's* own copy of it, never the template's.
TEMPLATE_FORBIDDEN_FIELDS = ("skills", "max_hp")


class ValidationMixin(DMCoreProtocol):
    """!
    @brief Load-time referential-integrity checks over everything load_rules/
        load_scenario_definition just loaded (DMCore mixin -- only ever composed into DMCore,
        never instantiated on its own; relies on self.entities/self.entity_templates/
        self.locations/self.skills/self.rules/self.event_bus, set up by DMCore.__init__).
        See docs/data-conventions.md's own "Load-time validation" and docs/extended-goals.md's
        "The TOML rule set itself needs standardizing" for the design this resolves.

        Every check here is non-blocking -- a problem is a single "log_error" publish, never a
        raised exception -- the same "malformed data degrades quietly, on purpose" convention
        load_rules' own per-file try/except and DM_Rules.py's pre-existing
        _validate_equipped_slots already follow. Scoped to *referential integrity* only: does a
        name/skill/room/location a field claims to point at actually resolve to something real.
        Not a field-shape/type schema check (a separate, larger effort), and deliberately skips
        [[location.encounter]]'s own weighted-choice keys -- a real entity/entity_template name,
        the reserved "nothing", or pure flavor text are indistinguishable without knowing the
        author's intent (see location_schema.toml's own comment on that three-way shape).

        Entirely setting-agnostic -- every check below reads only whatever's currently loaded
        into self.entities/self.entity_templates/self.locations/self.skills/self.rules for
        whichever setting booted, no Fantasy-specific assumption anywhere, so it applies
        unchanged to Rules/Zombie/ too. Inherits DMCoreProtocol purely so type checkers can
        resolve these shared attributes/cross-mixin methods -- see DM_Types.py.
    """

    def validate_loaded_data(self):
        """!
        @brief Runs every referential-integrity check, once per full (re)load -- called from
            DMCore.__init__ right after load_scenario_definition (every entity/template/
            location/skill/rule is loaded by then, nothing yet instanced) and from
            DM_Persistence.py's load_game at the same point, so a resumed save is re-checked
            against whatever Rules/<setting>/ looks like now, not whatever it looked like when
            the save was written.
        """
        self._validate_equipped_slots()
        self._validate_skill_references()
        self._validate_ability_references()
        self._validate_entity_references()
        self._validate_entity_template_shape()
        self._validate_location_references()

    # -----------------------------------------------------------------------------------------
    # Skill references

    def _check_skill_name(self, owner_label, skill_name):
        """!
        @brief Publishes one log_error if skill_name (a single name, never a list -- callers
            unwrap a list themselves) doesn't match anything in self.skills.
        @param owner_label Human-readable text identifying where this reference was found, for
            the log message (ex: "entity 'wolf' ability 'bite'").
        @param skill_name The candidate skill name; non-string values are ignored (a malformed
            field is somebody else's problem, not this check's).
        """
        if not isinstance(skill_name, str):
            return
        if skill_name not in self.skills:
            self.event_bus.publish("log_error", f"{owner_label} references unknown skill '{skill_name}'.")

    def _check_skill_field(self, owner_label, skill_field):
        """!
        @brief Checks a "skill" field that may be a single name or a list of candidate names
            (ex: techniques.toml's cleave -- ["blades", "axes"]) -- the same shape
            ability_matches_skill/select_ability_skill already read.
        """
        if isinstance(skill_field, list):
            for skill_name in skill_field:
                self._check_skill_name(owner_label, skill_name)
        elif skill_field is not None:
            self._check_skill_name(owner_label, skill_field)

    def _validate_skill_references(self):
        """!
        @brief Cross-checks every skill name referenced off an entity/entity_template against
            self.skills: the entity's own top-level "skill" (present directly on a weapon/spell/
            technique/universal-ability entity, ex: items.toml's longsword, maneuvers.toml's
            intimidate), every resolved ability's own "skill" (an entity's "abilities" list may
            mix bare-string catalog references and inline tables -- resolve_ability handles
            both), [entity.test]/[entity.craft]'s own "skill" list, and [entity.notice]'s own
            single "skill" name.
        """
        for namespace_label, namespace in (("entity", self.entities), ("entity_template", self.entity_templates)):
            for name, entity in namespace.items():
                label = f"{namespace_label} '{name}'"
                self._check_skill_field(label, entity.get("skill"))
                for ability in entity.get("abilities", []):
                    resolved = self.resolve_ability(ability)
                    if resolved is None:
                        continue
                    ability_name = resolved.get("name", ability if isinstance(ability, str) else "?")
                    self._check_skill_field(f"{label} ability '{ability_name}'", resolved.get("skill"))
                test = entity.get("test")
                if test:
                    self._check_skill_field(f"{label} [entity.test]", test.get("skill"))
                craft = entity.get("craft")
                if craft:
                    self._check_skill_field(f"{label} [entity.craft]", craft.get("skill"))
                notice = entity.get("notice")
                if notice:
                    self._check_skill_field(f"{label} [entity.notice]", notice.get("skill"))

    # -----------------------------------------------------------------------------------------
    # Behavior/ability references

    def _resolves_as_named_ability(self, entity, ability_name):
        """!
        @brief Mirrors resolve_named_ability's own resolution (DM_Combat.py) -- an entity's own
            "abilities" list, matched by resolved name, else self.universal_abilities -- but
            against an already-in-hand entity dict rather than a self.entities lookup by name,
            so this works for an entity_template too (never itself a self.entities key,
            template_schema.toml's own note is that every entity field, "abilities" included,
            still applies to it the same way).
        @param entity The entity/entity_template's own dict.
        @param ability_name The candidate action name.
        @return True if ability_name resolves, same semantics as resolve_named_ability.
        """
        for ability in entity.get("abilities", []):
            resolved = self.resolve_ability(ability)
            if resolved and resolved.get("name") == ability_name:
                return True
        return ability_name in self.universal_abilities

    def _validate_ability_references(self):
        """!
        @brief Cross-checks every [[entity.behavior]] entry's "action" -- skipping the reserved
            movement words (MOVEMENT_ACTIONS) and the reserved transfer words (TRANSFER_ACTIONS)
            -- against _resolves_as_named_ability, so
            this can never drift out of sync with what resolve_named_ability actually does the
            moment this entity gets a real turn. template_schema.toml notes every entity field
            (including behavior) applies to an entity_template the same way it does a real
            entity, so both namespaces are checked here, same as the skill-reference pass above.
        """
        for namespace_label, namespace in (("entity", self.entities), ("entity_template", self.entity_templates)):
            for name, entity in namespace.items():
                for behavior in entity.get("behavior", []):
                    action_name = behavior.get("action")
                    if not isinstance(action_name, str) or action_name in MOVEMENT_ACTIONS or action_name in TRANSFER_ACTIONS:
                        continue
                    if not self._resolves_as_named_ability(entity, action_name):
                        self.event_bus.publish(
                            "log_error",
                            f"{namespace_label} '{name}' behavior names unknown action '{action_name}'.",
                        )

    # -----------------------------------------------------------------------------------------
    # Entity-name / rule-name references

    def _check_entity_name(self, owner_label, field_label, entity_name):
        """!
        @brief Publishes one log_error if entity_name doesn't match anything in self.entities.
        """
        if not isinstance(entity_name, str):
            return
        if entity_name not in self.entities:
            self.event_bus.publish(
                "log_error", f"{owner_label} {field_label} references unknown entity '{entity_name}'."
            )

    def _check_materials(self, owner_label, materials):
        """!
        @brief Cross-checks a [{item, quantity}, ...] materials list (shared by an ability's
            own "materials" and [entity.craft]'s own "materials" -- same {item, quantity} shape,
            same _has_materials/_consume_materials primitives) against self.entities.
        """
        for material in materials or []:
            self._check_entity_name(owner_label, "material", material.get("item"))

    def _validate_entity_references(self):
        """!
        @brief Cross-checks every entity-name-shaped field against self.entities (or
            self.entity_templates, for a summon's own "template" option): "inventory" items,
            [entity.equipped] values, "replace_with", an ability's own "summon.name"/
            "summon.template" and "materials", and [entity.craft]'s own "materials". Also
            resolves damage_value.bonus's "user.<rule_name>" indirection against self.rules,
            mirroring Combat_Resolution.resolve_bonus's own lookup exactly.
        """
        for namespace_label, namespace in (("entity", self.entities), ("entity_template", self.entity_templates)):
            for name, entity in namespace.items():
                label = f"{namespace_label} '{name}'"

                for item_name in entity.get("inventory", []):
                    self._check_entity_name(label, "inventory item", item_name)
                for slot, item_name in entity.get("equipped", {}).items():
                    self._check_entity_name(label, f"equipped[{slot}]", item_name)
                self._check_entity_name(label, "replace_with", entity.get("replace_with"))

                self._check_damage_bonus(label, entity.get("damage_value"))
                craft = entity.get("craft")
                if craft:
                    self._check_materials(f"{label} [entity.craft]", craft.get("materials"))

                for ability in entity.get("abilities", []):
                    resolved = self.resolve_ability(ability)
                    if resolved is None:
                        continue
                    ability_name = resolved.get("name", ability if isinstance(ability, str) else "?")
                    ability_label = f"{label} ability '{ability_name}'"
                    self._check_damage_bonus(ability_label, resolved.get("damage_value"))
                    self._check_materials(ability_label, resolved.get("materials"))
                    summon = resolved.get("summon")
                    if summon:
                        if "name" in summon:
                            self._check_entity_name(ability_label, "summon.name", summon.get("name"))
                        elif "template" in summon and summon.get("template") not in self.entity_templates:
                            self.event_bus.publish(
                                "log_error",
                                f"{ability_label} summon.template references unknown entity_template "
                                f"'{summon.get('template')}'.",
                            )

    def _check_damage_bonus(self, owner_label, damage_value):
        """!
        @brief If damage_value's own "bonus" is a "user.<rule_name>" string reference, checks
            rule_name exists in self.rules -- mirrors Combat_Resolution.resolve_bonus's own
            `bonus.split(".")[-1]` lookup exactly, so this never drifts out of sync with what
            actually happens the moment damage is rolled.
        """
        if not damage_value:
            return
        bonus = damage_value.get("bonus")
        if not isinstance(bonus, str):
            return
        rule_name = bonus.split(".")[-1]
        if rule_name not in self.rules:
            self.event_bus.publish(
                "log_error", f"{owner_label} damage_value.bonus references unknown rule '{bonus}'."
            )

    # -----------------------------------------------------------------------------------------
    # entity_template shape

    def _validate_entity_template_shape(self):
        """!
        @brief Flags an entity_template that authors "skills"/"max_hp" (TEMPLATE_FORBIDDEN_FIELDS)
            -- per template_schema.toml's own explicit note, NPC_Generation.py fills these in at
            instancing time and never reads an authored value here, so a hand-authored one is
            silently discarded today with zero signal that it was ever a mistake. Deliberately
            doesn't flag "name" -- see TEMPLATE_FORBIDDEN_FIELDS' own comment.
        """
        for name, template in self.entity_templates.items():
            for field in TEMPLATE_FORBIDDEN_FIELDS:
                if field in template:
                    self.event_bus.publish(
                        "log_error",
                        f"entity_template '{name}' authors '{field}', which generation always "
                        f"overwrites at instancing time -- remove it.",
                    )

    # -----------------------------------------------------------------------------------------
    # Location/room references

    def _check_entity_entries(self, owner_label, entries):
        """!
        @brief Cross-checks a location/room's own "entities" list -- each entry names its
            template one of two mutually exclusive ways, exactly as _instance_entities reads
            them: "name" (self.entities, PLAYER_PLACEHOLDER exempted -- it's resolved to
            whichever entity is_player = true at instancing time, not a literal lookup) or
            "template" (self.entity_templates). Catches a bad reference in a room the player may
            never actually visit, not just one that gets instanced during a given playthrough/
            test run.
        """
        for entry in entries or []:
            if "template" in entry:
                template_name = entry.get("template")
                if template_name not in self.entity_templates:
                    self.event_bus.publish(
                        "log_error",
                        f"{owner_label} entities references unknown entity_template '{template_name}'.",
                    )
            else:
                entity_name = entry.get("name")
                if entity_name == PLAYER_PLACEHOLDER:
                    continue
                self._check_entity_name(owner_label, "entities", entity_name)

    def _validate_location_references(self):
        """!
        @brief Cross-checks every [[location]]'s own graph references: "start_room"/
            "return_to", each [[location.exit]]'s own "destination"/"arrival_room", each
            [[location.room.exit]]'s own "destination" (a sibling room in the same location),
            and every location/room's own "entities" list (see _check_entity_entries). Skips
            [[location.encounter]]'s own weighted-choice keys entirely -- see this module's own
            docstring for why that's deliberately unvalidatable.
        """
        for location_key, location in self.locations.items():
            label = f"location '{location_key}'"
            rooms = location.get("rooms", {})

            start_room = location.get("start_room")
            if start_room and start_room not in rooms:
                self.event_bus.publish("log_error", f"{label} start_room references unknown room '{start_room}'.")

            return_to = location.get("return_to")
            if return_to and return_to not in self.locations:
                self.event_bus.publish("log_error", f"{label} return_to references unknown location '{return_to}'.")

            self._check_entity_entries(label, location.get("entities"))

            for exit_entry in location.get("exit", []):
                destination = exit_entry.get("destination")
                if destination not in self.locations:
                    self.event_bus.publish(
                        "log_error", f"{label} exit references unknown destination location '{destination}'."
                    )
                    continue
                arrival_room = exit_entry.get("arrival_room")
                destination_rooms = self.locations[destination].get("rooms", {})
                if arrival_room and arrival_room not in destination_rooms:
                    self.event_bus.publish(
                        "log_error",
                        f"{label} exit to '{destination}' arrival_room references unknown room '{arrival_room}'.",
                    )

            for room_key, room in rooms.items():
                room_label = f"{label} room '{room_key}'"
                self._check_entity_entries(room_label, room.get("entities"))
                for room_exit in room.get("exit", []):
                    room_destination = room_exit.get("destination")
                    if room_destination not in rooms:
                        self.event_bus.publish(
                            "log_error",
                            f"{room_label} exit references unknown room '{room_destination}' "
                            f"(rooms only connect to siblings within the same location).",
                        )
