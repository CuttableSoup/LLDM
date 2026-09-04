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

# ---------------------------------------------------------------------------------------------
# Field-shape/type spec -- entity_schema.toml's own documented Python type for every flat,
# non-compound [[entity]]/[[entity_template]] field (shared identically by both namespaces;
# template_schema.toml's own "Varied values" list is a closed, explicit set of *specific*
# fields, not a blanket "anything on a template can be a range/weighted-choice instead" rule --
# _check_field_type's own varied-value tolerance, below, covers that set without needing a
# second spec table). A field absent from this dict entirely is either compound (its own
# dedicated _check_* method, further down) or genuinely freeform ([entity.qualities]'s own
# leaf values, entity_schema.toml's own comment: "Freeform keys... simply what the shipped data
# happens to use") and deliberately never type-checked at all.
SCALAR_FIELD_TYPES = {
    "name": str, "supertype": str, "subtype": str, "description": str,
    "is_player": bool, "is_party": bool,
    "max_hp": (int, float), "currency": (int, float), "bulk": (int, float),
    "max_bulk": (int, float), "value": (int, float), "exp": (int, float),
    "speed": (int, float), "travel_speed": (int, float), "follow_offset": (int, float),
    "usable": bool, "charges": (int, float), "replace_with": str,
    "current_language": str, "provides_station": str, "mount": (str, list),
    # "range"/"difficulty"/"language_dependent"/"equip_slot" are ability-shaped fields --
    # checked once by _check_ability_shape instead, called against both an entity's own
    # top-level fields (in case it's itself a weapon/spell/technique) and every resolved
    # inline ability, so they're deliberately not duplicated here.
}
# Every field documented as a flat list of strings.
LIST_OF_STRING_FIELDS = (
    "languages", "memories", "quotes", "inventory",
    "damage_tags", "armor_tags", "armor_bypass_tags",
    "resistance_tags", "resistance_bypass_tags", "immunity_tags", "vulnerability_tags", "tags",
)
# Every field documented as a rollable {dice, pips, bonus} table -- see _check_dice_table.
DICE_TABLE_FIELDS = ("damage_value", "armor_value", "resistance_value", "vulnerability_value")

# The legal values for a condition apply site's own "duration" -- one more than Combat_
# Resolution.CONDITION_DURATIONS, since "days" is legal to *author* (apply_condition converts
# it to "blocks" the moment it's actually applied) even though it never exists as a live,
# stored value -- see that module's own CONDITION_DURATIONS comment.
CONDITION_DURATIONS = ("rounds", "rooms", "blocks", "days", "permanent")


class ValidationMixin(DMCoreProtocol):
    """!
    @brief Load-time checks over everything load_rules/load_scenario_definition just loaded
        (DMCore mixin -- only ever composed into DMCore, never instantiated on its own; relies
        on self.entities/self.entity_templates/self.locations/self.skills/self.rules/
        self.event_bus, set up by DMCore.__init__). See docs/data-conventions.md's own
        "Load-time validation" and docs/extended-goals.md's "The TOML rule set itself needs
        standardizing" for the design this resolves.

        Two kinds of check, both non-blocking -- a problem is a single "log_error" publish,
        never a raised exception, the same "malformed data degrades quietly, on purpose"
        convention load_rules' own per-file try/except and DM_Rules.py's pre-existing
        _validate_equipped_slots already follow:
        - *Referential integrity* (the original pass) -- does a name/skill/room/location a
          field claims to point at actually resolve to something real. Deliberately skips
          [[location.encounter]]'s own weighted-choice keys -- a real entity/entity_template
          name, the reserved "nothing", or pure flavor text are indistinguishable without
          knowing the author's intent (see location_schema.toml's own comment on that
          three-way shape).
        - *Field shape/type* (_validate_entity_shapes/_validate_location_shapes) -- does a
          field actually hold the Python type/structure entity_schema.toml/template_schema.
          toml/location_schema.toml document for it (ex: damage_value is a {dice, pips, bonus}
          table, not a bare number; [[entity.behavior]] is a list of {requirements, action}
          tables). Driven by the module-level SCALAR_FIELD_TYPES/LIST_OF_STRING_FIELDS/
          DICE_TABLE_FIELDS specs plus a handful of dedicated _check_* methods for compound
          shapes, rather than one hand-written check per field -- a new documented field only
          ever needs one new spec entry (or, for something genuinely freeform like
          [entity.qualities]'s own leaf values, deliberately no entry at all). An
          entity_template's own generation-input fields (target_cr/cr_multiplier/variance/
          hint) may legally hold NPC_Generation.py's own varied-value shape ({min, max} or a
          weighted-choice list, template_schema.toml's own "Varied values") instead of a fixed
          value -- _is_varied_value_shape recognizes and tolerates that everywhere a scalar is
          checked, not just on those four fields, since a real entity would never coincidentally
          author one by accident.

        Entirely setting-agnostic -- every check below reads only whatever's currently loaded
        into self.entities/self.entity_templates/self.locations/self.skills/self.rules for
        whichever setting booted, no Fantasy-specific assumption anywhere, so it applies
        unchanged to Rules/Zombie/ too. Inherits DMCoreProtocol purely so type checkers can
        resolve these shared attributes/cross-mixin methods -- see DM_Types.py.
    """

    def validate_loaded_data(self):
        """!
        @brief Runs every check, once per full (re)load -- called from
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
        self._validate_entity_shapes()
        self._validate_location_shapes()
        self._validate_status_shapes()

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

    # -----------------------------------------------------------------------------------------
    # Field shape/type -- entities/entity_templates

    def _log(self, owner_label, message):
        """!@brief Shared one-line log_error publish -- every shape check below ends with
        this, so the "label + message" join only has to be written once."""
        self.event_bus.publish("log_error", f"{owner_label} {message}")

    def _is_varied_value_shape(self, value):
        """!
        @brief True if value is NPC_Generation.py's own resolve_varied_value shape -- a
            {"min", "max"} range, or a non-empty list of single-key tables (a weighted-choice
            list) -- which an entity_template's own generation-input fields may legally use
            instead of a fixed value (template_schema.toml's own "Varied values"). Never
            itself resolved here, just recognized so a template's real use of it is never
            flagged as the wrong type. Tolerated everywhere a scalar is type-checked below, not
            gated to only the specific fields template_schema.toml names -- a real entity/
            entity_template would never coincidentally author this exact shape by accident, so
            the extra tolerance elsewhere costs nothing.
        @param value The candidate value.
        @return True if value matches either varied-value shape.
        """
        if isinstance(value, dict):
            return set(value.keys()) == {"min", "max"}
        if isinstance(value, list) and value:
            return all(isinstance(entry, dict) and len(entry) == 1 for entry in value)
        return False

    def _check_field_type(self, owner_label, field_label, value, expected_types):
        """!
        @brief Publishes one log_error if value is present but isn't an instance of
            expected_types (and isn't a tolerated varied-value shape -- see
            _is_varied_value_shape). Absent (None) is always fine here -- every entity field is
            optional unless some *other* part of the engine actively requires it (ex: "name",
            already covered by load_rules indexing entities by it); this check only ever
            objects to the wrong *type* being present, never to a field simply being omitted.
        @param owner_label Human-readable text identifying where this field lives.
        @param field_label The field's own name (or a dotted path, ex: "[entity.skills].blades")
            for the log message.
        @param value The candidate value.
        @param expected_types A type, or a tuple of types (isinstance's own second argument).
        """
        if value is None or isinstance(value, expected_types) or self._is_varied_value_shape(value):
            return
        expected_names = expected_types.__name__ if isinstance(expected_types, type) else (
            "/".join(t.__name__ for t in expected_types)
        )
        self._log(
            owner_label,
            f"{field_label} should be a {expected_names}, got {type(value).__name__}.",
        )

    def _check_string_list(self, owner_label, field_label, value):
        """!
        @brief Publishes one log_error if value is present but isn't a flat list of strings
            (ex: "languages", "damage_tags") -- see LIST_OF_STRING_FIELDS.
        """
        if value is None:
            return
        if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
            self._log(owner_label, f"{field_label} should be a list of strings.")

    def _check_dice_table(self, owner_label, field_label, value):
        """!
        @brief Publishes one log_error if value is present but isn't a {dice, pips, bonus}
            table -- "bonus" a number or a "user.<rule_name>" string reference
            (Combat_Resolution.resolve_bonus's own shape; the referential half of that check --
            does <rule_name> actually exist -- already lives in _check_damage_bonus, above).
            "dice"/"pips" are usually numbers too, but on an ability's own damage_value only,
            may instead be the literal strings "user.weapon.dice"/"user.weapon.pips" (resolved
            off the attacker's own equipped weapon at roll time instead of a fixed number --
            entity_schema.toml's own damage_value comment, techniques.toml's "cleave" is the
            shipped example) -- tolerated here too rather than only on damage_value
            specifically, since no [entity.skills]/armor/resistance/vulnerability entry would
            ever coincidentally match that exact string by accident. Used for damage_value/
            armor_value/resistance_value/vulnerability_value (DICE_TABLE_FIELDS) and every
            [entity.skills] entry.
        """
        if value is None:
            return
        if not isinstance(value, dict):
            self._log(owner_label, f"{field_label} should be a {{dice, pips, bonus}} table.")
            return
        for key in ("dice", "pips"):
            entry = value.get(key)
            # The one literal string Combat_Resolution.py's own resolve_weapon_reference
            # actually matches -- anything else, string or not, silently resolves to 0 damage
            # at roll time (its own "Unsupported damage dice/pips reference" log_warning), so a
            # near-miss (ex: a typo, or the wrong field's own string) is exactly the kind of
            # mistake this check exists to catch, not something to tolerate.
            ok = entry is None or isinstance(entry, (int, float)) or entry == f"user.weapon.{key}"
            if not ok:
                self._log(
                    owner_label, f"{field_label}.{key} should be a number (or \"user.weapon.{key}\").",
                )
        bonus = value.get("bonus")
        if bonus is not None and not isinstance(bonus, (int, float, str)):
            self._log(owner_label, f"{field_label}.bonus should be a number or a \"user.<rule>\" string.")

    def _check_skills_table(self, owner_label, entity):
        """!@brief [entity.skills] -- a table of skill_name -> {dice, pips} (DICE_TABLE_FIELDS'
        own shape, minus "bonus", which no skill entry ever authors)."""
        skills = entity.get("skills")
        if skills is None:
            return
        if not isinstance(skills, dict):
            self._log(owner_label, "[entity.skills] should be a table.")
            return
        for skill_name, value in skills.items():
            self._check_dice_table(owner_label, f"[entity.skills].{skill_name}", value)

    def _check_equipped_table(self, owner_label, entity):
        """!@brief [entity.equipped] -- a table of slot -> item entity name (a string; whether
        that name actually resolves is _validate_entity_references' own job)."""
        equipped = entity.get("equipped")
        if equipped is None:
            return
        if not isinstance(equipped, dict) or not all(isinstance(v, str) for v in equipped.values()):
            self._log(owner_label, "[entity.equipped] should be a table of slot -> item name.")

    def _check_attitude_axes(self, owner_label, field_label, axes, allow_varied):
        """!
        @brief One disposition/threat/familiarity array -- exactly 3 entries, each a number
            (or, for an entity_template's own array, optionally a varied-value shape per axis --
            template_schema.toml's own worked "default" example mixes fixed and varied axes in
            the very same array).
        """
        if axes is None:
            return
        if not isinstance(axes, list) or len(axes) != 3:
            self._log(owner_label, f"{field_label} should be a list of exactly 3 numbers.")
            return
        for index, value in enumerate(axes):
            ok = isinstance(value, (int, float)) or (allow_varied and self._is_varied_value_shape(value))
            if not ok:
                self._log(owner_label, f"{field_label}[{index}] should be a number.")

    def _check_attitudes_table(self, owner_label, entity, allow_varied):
        """!@brief [entity.attitudes] -- "default" plus the [[entity.attitudes.supertype]]/
        [[entity.attitudes.name]] override lists (TOML's own "list of single-key tables" shape
        for those two -- see entity_schema.toml's own comment on why)."""
        attitudes = entity.get("attitudes")
        if attitudes is None:
            return
        if not isinstance(attitudes, dict):
            self._log(owner_label, "[entity.attitudes] should be a table.")
            return
        self._check_attitude_axes(owner_label, "[entity.attitudes].default", attitudes.get("default"), allow_varied)
        for axis_name in ("supertype", "name"):
            overrides = attitudes.get(axis_name)
            if overrides is None:
                continue
            if not isinstance(overrides, list):
                self._log(owner_label, f"[entity.attitudes.{axis_name}] should be a list of tables.")
                continue
            for override in overrides:
                if not isinstance(override, dict):
                    self._log(owner_label, f"[[entity.attitudes.{axis_name}]] entry should be a table.")
                    continue
                for key, value in override.items():
                    self._check_attitude_axes(
                        owner_label, f"[[entity.attitudes.{axis_name}]].{key}", value, allow_varied,
                    )

    def _is_valid_requirement_entry(self, requirement):
        """!
        @brief Whether one entry of a "requirements" list is a shape entity_matches_requirements
            (Combat_Resolution.py) actually accepts -- either a plain {field, operator, value}
            comparison, or a nested {"all"|"any"|"none": [...]} boolean combination of more such
            entries (recursive), the same shape Program_Interpreter.evaluate_condition already
            gives program `if`-steps.
        @param requirement One requirements-list entry.
        @return True if it's a valid comparison or a valid boolean-combinator table.
        """
        if not isinstance(requirement, dict):
            return False
        for combinator in ("all", "any", "none"):
            if combinator in requirement:
                sub_entries = requirement[combinator]
                return isinstance(sub_entries, list) and all(
                    self._is_valid_requirement_entry(sub) for sub in sub_entries
                )
        return {"field", "operator", "value"} <= requirement.keys()

    def _check_behavior_list(self, owner_label, entity):
        """!@brief [[entity.behavior]] -- a list of {requirements, action, ...} tables, each
        requirement itself either a {field, operator, value} table or a nested
        {"all"|"any"|"none": [...]} boolean combination (entity_matches_requirements' own
        shape, Combat_Resolution.py)."""
        behaviors = entity.get("behavior")
        if behaviors is None:
            return
        if not isinstance(behaviors, list):
            self._log(owner_label, "[[entity.behavior]] should be a list.")
            return
        for index, behavior in enumerate(behaviors):
            label = f"{owner_label} [[entity.behavior]][{index}]"
            if not isinstance(behavior, dict):
                self._log(label, "should be a table.")
                continue
            requirements = behavior.get("requirements")
            if requirements is not None:
                if not isinstance(requirements, list):
                    self._log(label, "requirements should be a list.")
                else:
                    for req_index, requirement in enumerate(requirements):
                        if not self._is_valid_requirement_entry(requirement):
                            self._log(
                                label,
                                f"requirements[{req_index}] should be a {{field, operator, value}} table, "
                                f"or a nested {{\"all\"|\"any\"|\"none\": [...]}} table.",
                            )
            if "action" in behavior and not isinstance(behavior["action"], str):
                self._log(label, "action should be a string.")
            if "amount" in behavior and not isinstance(behavior["amount"], (int, float)):
                self._log(label, "amount should be a number.")

    def _check_targets_table(self, owner_label, ability):
        """!@brief An ability's own "targets" -- {number, aoe, side}."""
        targets = ability.get("targets")
        if targets is None:
            return
        if not isinstance(targets, dict):
            self._log(owner_label, "targets should be a table.")
            return
        for field_name in ("number", "aoe"):
            if field_name in targets and not isinstance(targets[field_name], (int, float)):
                self._log(owner_label, f"targets.{field_name} should be a number.")
        if "side" in targets and not isinstance(targets["side"], str):
            self._log(owner_label, "targets.side should be a string.")

    def _check_summon_shape(self, owner_label, summon):
        """!@brief An ability's own "summon" -- exactly one of "name"/"template", plus an
        optional numeric "duration". The referential half (does that name/template actually
        resolve) already lives in _validate_entity_references, above -- this only checks the
        structural "exactly one of the two" shape and duration's own type."""
        if summon is None:
            return
        if not isinstance(summon, dict):
            self._log(owner_label, "summon should be a table.")
            return
        has_name, has_template = "name" in summon, "template" in summon
        if has_name == has_template:  # neither, or both -- either way, not "exactly one"
            self._log(owner_label, "summon should author exactly one of \"name\"/\"template\".")
        if "duration" in summon and not isinstance(summon["duration"], (int, float)):
            self._log(owner_label, "summon.duration should be a number.")

    def _check_duration_length(self, owner_label, duration, length, field_label="duration"):
        """!
        @brief A condition apply site's own "duration"/"length" pair -- shared by every place
            one can be authored: [[status]]'s own "apply" block (_validate_status_shapes,
            below), a "condition" program op (_check_program_condition_durations, below), and
            an entity's own seeded [entity.conditions.X] starting state
            (_check_entity_conditions_shape, below). duration must be one of CONDITION_
            DURATIONS; length must be a positive number for every denomination except
            "permanent", which carries no countdown at all and so needs none.
        @param owner_label Human-readable text identifying where this apply site lives.
        @param duration The apply site's own "duration" value (None if never authored at all --
            a silent no-op, same as every other optional field this module checks).
        @param length The apply site's own "length" value.
        @param field_label What to call "duration" in a logged message, when the field isn't
            literally named "duration" at this particular apply site (ex: summon.duration).
        """
        if duration is None:
            return
        if duration not in CONDITION_DURATIONS:
            self._log(owner_label, f"{field_label} should be one of {CONDITION_DURATIONS}.")
            return
        if duration != "permanent" and not (isinstance(length, (int, float)) and length > 0):
            self._log(owner_label, f'{field_label} "{duration}" requires a positive numeric length.')

    def _walk_program_steps(self, program):
        """!
        @brief Yields every step dict inside an authored program -- a single inline step, a
            flat list of steps, or steps nested under "then"/"else" (Program_Interpreter.py's
            own "if" branching shape). Narrowly scoped to support
            _check_program_condition_durations below -- not a general op/arg validator, so it
            doesn't check "do" names or any other step field.
        @param program A program value as authored (None, a dict, or a list of dicts).
        """
        if program is None:
            return
        if isinstance(program, list):
            for item in program:
                yield from self._walk_program_steps(item)
            return
        if not isinstance(program, dict):
            return
        yield program
        yield from self._walk_program_steps(program.get("then"))
        yield from self._walk_program_steps(program.get("else"))

    def _check_program_condition_durations(self, owner_label, program):
        """!@brief Validates every "condition" op's own duration/length inside program (an
        entity/ability's own on_pass/on_fail/on_round_upkeep/on_enter/on_damage/on_heal)."""
        for step in self._walk_program_steps(program):
            if step.get("do") == "condition":
                self._check_duration_length(f"{owner_label} condition op", step.get("duration"), step.get("length"))

    def _check_materials_shape(self, owner_label, materials):
        """!@brief A "materials" list -- [{item, quantity}, ...]. The referential half (does
        "item" actually resolve) already lives in _check_materials, above."""
        if materials is None:
            return
        if not isinstance(materials, list):
            self._log(owner_label, "materials should be a list.")
            return
        for index, material in enumerate(materials):
            if not isinstance(material, dict):
                self._log(owner_label, f"materials[{index}] should be a table.")
                continue
            if "item" in material and not isinstance(material["item"], str):
                self._log(owner_label, f"materials[{index}].item should be a string.")
            if "quantity" in material and not isinstance(material["quantity"], (int, float)):
                self._log(owner_label, f"materials[{index}].quantity should be a number.")

    def _check_ability_shape(self, owner_label, ability):
        """!
        @brief Every type/shape check shared by anything ability-shaped -- a standalone
            weapon/spell/technique/maneuver catalog entity, or a resolved inline ability table
            (an entity's own "abilities" list entry, already run through resolve_ability).
            Called once against an entity's own top-level fields (in case it's itself one of
            these) and once per resolved ability -- entity_schema.toml documents these as the
            same field set either way, so one shared check covers both attachment points with
            no special-casing for which kind of thing ability actually is.
        @param owner_label Human-readable text identifying where this ability lives.
        @param ability The entity dict, or a resolved ability table.
        """
        self._check_dice_table(owner_label, "damage_value", ability.get("damage_value"))
        self._check_string_list(owner_label, "damage_tags", ability.get("damage_tags"))
        if "range" in ability and not isinstance(ability["range"], (int, float)):
            self._log(owner_label, "range should be a number.")
        if "difficulty" in ability and not isinstance(ability["difficulty"], (int, float)):
            self._log(owner_label, "difficulty should be a number.")
        if "language_dependent" in ability and not isinstance(ability["language_dependent"], bool):
            self._log(owner_label, "language_dependent should be a boolean.")
        self._check_targets_table(owner_label, ability)
        self._check_summon_shape(owner_label, ability.get("summon"))
        self._check_materials_shape(owner_label, ability.get("materials"))
        self._check_program_condition_durations(owner_label, ability.get("on_pass"))
        self._check_program_condition_durations(owner_label, ability.get("on_fail"))

    def _check_entity_conditions_shape(self, owner_label, conditions):
        """!@brief An entity's own seeded [entity.conditions.X] starting state -- copied
        verbatim into active_conditions at instancing time (DM_Rules.py's _instance_entities),
        so each entry's own duration/length gets exactly the apply-site shape check."""
        if conditions is None:
            return
        if not isinstance(conditions, dict):
            self._log(owner_label, "conditions should be a table.")
            return
        for condition_name, entry in conditions.items():
            if not isinstance(entry, dict):
                self._log(owner_label, f'conditions.{condition_name} should be a table.')
                continue
            self._check_duration_length(f"{owner_label} conditions.{condition_name}", entry.get("duration"), entry.get("length"))

    def _check_template_generation_fields(self, owner_label, template):
        """!@brief entity_template-only generation-input fields (template_schema.toml's own
        "Generation inputs") -- target_cr/cr_multiplier/variance/hint. Each may legally be a
        varied-value shape instead of a fixed one (see _is_varied_value_shape); target_cr may
        also be the literal strings "player"/"party"."""
        target_cr = template.get("target_cr")
        if target_cr is not None and not (
            isinstance(target_cr, (int, float)) or target_cr in ("player", "party")
            or self._is_varied_value_shape(target_cr)
        ):
            self._log(owner_label, "target_cr should be a number, \"player\", \"party\", or a varied value.")
        for field_name in ("cr_multiplier", "variance"):
            value = template.get(field_name)
            if value is not None and not (isinstance(value, (int, float)) or self._is_varied_value_shape(value)):
                self._log(owner_label, f"{field_name} should be a number or a varied value.")
        hint = template.get("hint")
        if hint is not None and not (isinstance(hint, str) or self._is_varied_value_shape(hint)):
            self._log(owner_label, "hint should be a string or a varied value.")

    def _validate_entity_shapes(self):
        """!
        @brief Runs every field-shape/type check above against both self.entities and
            self.entity_templates -- SCALAR_FIELD_TYPES/LIST_OF_STRING_FIELDS/DICE_TABLE_FIELDS
            for the flat fields, then one dedicated call per compound shape
            ([entity.skills]/[entity.equipped]/[entity.attitudes]/[[entity.behavior]]), plus
            _check_ability_shape once against the entity's own top-level fields and once per
            resolved entry in its own "abilities" list (which, alongside on_pass/on_fail,
            covers every "condition" op's own duration/length -- see
            _check_program_condition_durations), plus _check_entity_conditions_shape for a
            seeded [entity.conditions.X] starting state and a direct
            _check_program_condition_durations pass over the entity's own on_round_upkeep/
            on_enter/on_damage/on_heal. entity_templates additionally get
            _check_template_generation_fields and allow_varied=True on their own attitudes
            axes (template_schema.toml's own "Varied values").
        """
        for namespace_label, namespace in (("entity", self.entities), ("entity_template", self.entity_templates)):
            is_template = namespace_label == "entity_template"
            for name, entity in namespace.items():
                label = f"{namespace_label} '{name}'"

                for field_name, expected_types in SCALAR_FIELD_TYPES.items():
                    self._check_field_type(label, field_name, entity.get(field_name), expected_types)
                for field_name in LIST_OF_STRING_FIELDS:
                    self._check_string_list(label, field_name, entity.get(field_name))
                for field_name in DICE_TABLE_FIELDS:
                    self._check_dice_table(label, field_name, entity.get(field_name))

                self._check_ability_shape(label, entity)
                self._check_skills_table(label, entity)
                self._check_equipped_table(label, entity)
                self._check_attitudes_table(label, entity, allow_varied=is_template)
                self._check_behavior_list(label, entity)
                self._check_entity_conditions_shape(label, entity.get("conditions"))
                for program_field in ("on_round_upkeep", "on_enter", "on_damage", "on_heal"):
                    self._check_program_condition_durations(label, entity.get(program_field))

                for ability in entity.get("abilities", []):
                    resolved = self.resolve_ability(ability)
                    if resolved is None:
                        continue
                    ability_name = resolved.get("name", ability if isinstance(ability, str) else "?")
                    self._check_ability_shape(f"{label} ability '{ability_name}'", resolved)

                if is_template:
                    self._check_template_generation_fields(label, entity)

    def _validate_status_shapes(self):
        """!@brief [[status]]'s own "apply" block -- {condition, duration, length, dismiss}."""
        for status in self.rules.get("status", []):
            apply_block = status.get("apply")
            if not apply_block:
                continue
            label = f"status '{status.get('name', '?')}'"
            self._check_duration_length(label, apply_block.get("duration"), apply_block.get("length"))

    # -----------------------------------------------------------------------------------------
    # Field shape/type -- locations/rooms

    LOCATION_STRING_FIELDS = ("key", "name", "description", "kind", "start_room", "return_to")
    ROOM_STRING_FIELDS = ("key", "name", "description")

    def _check_grid(self, owner_label, grid):
        """!@brief A [[location]]'s own optional "grid" -- {x, y}, both numbers (DM_Travel.py's
        own grid-travel model)."""
        if grid is None:
            return
        if not isinstance(grid, dict) or not all(isinstance(grid.get(axis), (int, float)) for axis in ("x", "y")):
            self._log(owner_label, "grid should be a {x, y} table of numbers.")

    def _validate_location_shapes(self):
        """!
        @brief Field-shape/type checks over every [[location]]/[[location.room]] -- string
            fields (LOCATION_STRING_FIELDS/ROOM_STRING_FIELDS), "grid", a room's own "bands"
            (number)/"enclosed" (boolean), and every [[location.exit]]/[[location.room.exit]]'s
            own "destination" (string)/"aliases" (list of strings)/"band"/"arrival_band"
            (numbers). Deliberately doesn't re-check "entities" here -- _check_entity_entries
            (the referential pass) already walks its own {name, band}/{template, band} shape
            while resolving it.
        """
        for location_key, location in self.locations.items():
            label = f"location '{location_key}'"
            for field_name in self.LOCATION_STRING_FIELDS:
                self._check_field_type(label, field_name, location.get(field_name), str)
            self._check_grid(label, location.get("grid"))

            for exit_entry in location.get("exit", []):
                if "destination" in exit_entry and not isinstance(exit_entry["destination"], str):
                    self._log(label, "[[location.exit]] destination should be a string.")
                self._check_string_list(label, "[[location.exit]] aliases", exit_entry.get("aliases"))
                if "arrival_band" in exit_entry and not isinstance(exit_entry["arrival_band"], (int, float)):
                    self._log(label, "[[location.exit]] arrival_band should be a number.")

            for room_key, room in location.get("rooms", {}).items():
                room_label = f"{label} room '{room_key}'"
                for field_name in self.ROOM_STRING_FIELDS:
                    self._check_field_type(room_label, field_name, room.get(field_name), str)
                if "bands" in room and not isinstance(room["bands"], (int, float)):
                    self._log(room_label, "bands should be a number.")
                if "enclosed" in room and not isinstance(room["enclosed"], bool):
                    self._log(room_label, "enclosed should be a boolean.")
                for room_exit in room.get("exit", []):
                    if "destination" in room_exit and not isinstance(room_exit["destination"], str):
                        self._log(room_label, "[[location.room.exit]] destination should be a string.")
                    for field_name in ("band", "arrival_band"):
                        if field_name in room_exit and not isinstance(room_exit[field_name], (int, float)):
                            self._log(room_label, f"[[location.room.exit]] {field_name} should be a number.")
