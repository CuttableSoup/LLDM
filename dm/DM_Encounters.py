from dm.DM_Types import DMCoreProtocol
from resolution.NPC_Generation import resolve_varied_value


class EncounterMixin(DMCoreProtocol):
    """!
    @brief Resolves a location/room's own [[location.encounter]]/[[location.room.encounter]]
        table on entry (DMCore mixin -- only ever composed into DMCore, never instantiated on
        its own; relies on self.entities/self.entity_templates/self.scenario_entities/
        self.persistent_entities/self.event_bus, set up by DMCore.__init__, plus RulesMixin's
        _instance_entities/get_band, SocialMixin's is_hostile, and ImprovisationMixin's
        _claim_current_target_if_free). Called from DM_Rules.py's _enter_location, once per
        location/room entry -- see CLAUDE.md's own "Random encounters" section.

        "encounter" is a weighted-choice list -- the exact same
        [ { "choice" = weight }, ... ] shape NPC_Generation.py's resolve_varied_value already
        resolves for an [[entity_template]]'s own hint/qualities.race (see template_schema.toml's
        "Varied values") -- reused directly, not a new probability mechanism. Each key resolves
        the same way an ordinary entities-list entry already would: a real [[entity]]/
        [[entity_template]] name is instanced exactly like any other {name = ...}/
        {template = ...} entry, friendly or hostile decided entirely by that entity's own
        [entity.attitudes]/[[entity.behavior]] data (no separate "kind" field trying to
        redeclare what the referenced entity already says about itself); the reserved key
        "nothing" is a deliberate no-op, no narration; anything else is used directly as a
        flavor narration beat, no entity created (same shape ad hoc generation's own
        describe_scenery already produces).
    """

    def _resolve_location_encounter(self, table):
        """!
        @brief Resolves every [[location.encounter]]/[[location.room.encounter]] entry on
            table whose own "trigger" is "on_enter" -- the only trigger this v1 shape supports
            ("ambient", a repeating per-turn roll, is deferred -- see location_schema.toml).
        @param table The now-active location's own table (freeform) or its now-active room's
            own table (room-based) -- whichever _enter_location just populated. A no-op if
            table is falsy (ex: a brand-new location with no [[location.room]] entered before
            any room table exists) or carries no "encounter" list at all.
        """
        if not table:
            return
        for entry in table.get("encounter", []):
            if entry.get("trigger") == "on_enter":
                self._resolve_one_encounter(entry)

    def _resolve_one_encounter(self, entry):
        """!
        @brief Rolls one [[location.encounter]] entry's own weighted "encounter" list and
            applies whatever it resolves to -- see this class's own docstring for the three
            possible outcomes.
        @param entry One [[location.encounter]]/[[location.room.encounter]] table
            ({"name", "trigger", "encounter"}).
        """
        choices = entry.get("encounter", [])
        if not choices:
            return
        result = resolve_varied_value(choices)
        if result == "nothing":
            return

        if result in self.entities or result in self.entity_templates:
            key = "name" if result in self.entities else "template"
            instanced = self._instance_entities(
                [{key: result, "band": self.get_band(self.player_name)}], party_pool=self.persistent_entities,
            )
            for name in instanced:
                self.scenario_entities.append(name)
                if self.is_hostile(name, self.player_name):
                    self._claim_current_target_if_free(name)
            self.event_bus.publish("encounter_triggered", {
                "description": None,
                "entity_name": instanced[0] if instanced else None,
                "present_entities": list(self.scenario_entities),
            })
        else:
            self.event_bus.publish("encounter_triggered", {
                "description": result, "entity_name": None,
                "present_entities": list(self.scenario_entities),
            })
