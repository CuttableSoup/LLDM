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
            table whose own "trigger" is "on_enter" -- checked once, the moment this location/
            room is entered. See _resolve_ambient_encounter for the "ambient" (repeating
            per-turn) trigger this table can carry instead.
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

    def _resolve_ambient_encounter(self):
        """!
        @brief Rolls every [[location.encounter]]/[[location.room.encounter]] entry on the
            *current* location/room's own table whose own "trigger" is "ambient" -- a
            repeating per-turn roll, unlike "on_enter"'s own once-per-arrival check
            (_resolve_location_encounter). Called once per real player turn
            (_on_turn_detected, DM_Core.py, right after clauses are confirmed non-empty),
            never while a hostile is already present (_any_hostile_present) -- an ambient
            flavor beat piling a second, uninvited fight on top of an already-active one would
            be nonsensical, the same "don't interrupt what's already happening" reasoning
            grid travel/rest's own hostile-block pause already follows elsewhere for a
            different kind of interruption. Frequency control is entirely the authored
            table's own job (ex: "nothing" at a heavy weight) -- no separate probability gate
            layered on top, the same "the weighted-choice table already decides how often"
            precedent [[location.encounter]] already sets for "on_enter".
        """
        table = self._current_room() or self.locations.get(self.current_location_key, {})
        if not table or self._any_hostile_present():
            return
        for entry in table.get("encounter", []):
            if entry.get("trigger") == "ambient":
                # Forced True here (unlike _resolve_location_encounter's own "on_enter" call,
                # and DM_Travel.py's per-block environment roll) -- this is the one encounter
                # context with no natural pause to justify a synchronous LLM call: it fires on
                # an arbitrary player turn, not a deliberate location-entry/travel-block action,
                # so a generate=true entity_template referenced here must always take
                # NPC_Generation.py's instant offline-fallback path instead of ever blocking on
                # Ollama. See CLAUDE.md/docs/npc-generation.md's own synchronous-call notes.
                self._resolve_one_encounter(entry, skip_llm_generation=True)

    def _resolve_one_encounter(self, entry, skip_llm_generation=False):
        """!
        @brief Rolls one [[location.encounter]] entry's own weighted "encounter" list and
            applies whatever it resolves to -- see this class's own docstring for the three
            possible outcomes.
        @param entry One [[location.encounter]]/[[location.room.encounter]] table
            ({"name", "trigger", "encounter"}).
        @param skip_llm_generation Forwarded to _instance_entities -- see its own docstring.
            True only from _resolve_ambient_encounter; "on_enter" (_resolve_location_encounter)
            and DM_Travel.py's per-block environment roll both leave this False, since both are
            already a deliberate, expected pause point (location entry / spending a travel
            block), unlike an ambient roll's arbitrary per-turn timing.
        @return True if this roll instanced at least one entity hostile to the player --
            DM_Travel.py's own night-watch check (_roll_night_watch) uses this to know whether
            a night block's roll is even worth a watch check against; every other existing
            caller (_resolve_location_encounter) simply ignores it, exactly as it ignored this
            method returning nothing before.
        """
        choices = entry.get("encounter", [])
        if not choices:
            return False
        result = resolve_varied_value(choices)
        if result == "nothing":
            return False

        if result in self.entities or result in self.entity_templates:
            key = "name" if result in self.entities else "template"
            instanced = self._instance_entities(
                [{key: result, "band": self.get_band(self.player_name)}],
                party_pool=self.persistent_entities, skip_llm_generation=skip_llm_generation,
            )
            hostile = False
            for name in instanced:
                self.scenario_entities.append(name)
                # No location/room "entities" list names this instance -- unlike a
                # hand-authored occupant, nothing re-derives its presence on a later
                # save/load beyond the generic "ad hoc" path (_collect_ad_hoc_entities,
                # DM_Persistence.py), so it needs that same flag to keep its live hp/
                # active_conditions instead of silently resetting to template stats on
                # reload -- load-bearing now that a hostile roll here can pause travel/rest
                # across multiple turns (see docs/downtime.md's "Pausing for a fight").
                self.entities[name]["ad_hoc"] = True
                if self.is_hostile(name, self.player_name):
                    self._claim_current_target_if_free(name)
                    hostile = True
            self.event_bus.publish("encounter_triggered", {
                "description": None,
                "entity_name": instanced[0] if instanced else None,
                "present_entities": list(self.scenario_entities),
            })
            return hostile
        else:
            self.event_bus.publish("encounter_triggered", {
                "description": result, "entity_name": None,
                "present_entities": list(self.scenario_entities),
            })
            return False
