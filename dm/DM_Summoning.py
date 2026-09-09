from dm.DM_Types import DMCoreProtocol


class SummoningMixin(DMCoreProtocol):
    """!
    @brief Temporary allies conjured by a spell/ability's own "summon" field (DMCore mixin --
        only ever composed into DMCore, never instantiated on its own; relies on
        self.entities/self.scenario_entities/self.persistent_entities/self.event_bus, set up
        by DMCore.__init__, plus RulesMixin's _instance_entities/get_band and
        ImprovisationMixin's remove_entity_from_scene). DM_Core.py's own _apply_summon_if_hit
        calls _summon_creature on a successfully cast summoning spell/ability; DM_Status.py's
        run_round_upkeep calls _expire_summon_if_due once per combat round for every living
        scene entity.

        Deliberately narrower than DM_Improvisation.py's own ad hoc creature conjuring:
        summon_spec always names a real, hand-authored [[entity]]/[[entity_template]]
        (Rules/Fantasy/creatures.toml's own "spectral wolf" is the shipped example) -- nothing
        here ever invents a creature via an LLM call. The same _instance_entities primitive
        (DM_Rules.py) every scenario/room's own static "entities" list is built on does the
        actual placement, so a summon gets the exact same disambiguation ("spectral wolf",
        "spectral wolf_2", ... if cast more than once) every other instanced entity does --
        reused via _resolve_one_encounter's own precedent (DM_Encounters.py), not
        DM_Improvisation.py's own bespoke _unique_entity_key/_place_new_entity pairing (that
        one exists specifically for an LLM-invented name with no real template to disambiguate
        against self.entity_occurrence_counts the ordinary way).

        Survives a save/load cycle like any other ad hoc entity: DM_Persistence.py's own
        _collect_ad_hoc_entities treats live self.scenario_entities membership as a
        reachability source in its own right (not just ground/inventory), and load_game
        re-appends a restored ad hoc entity's name back onto self.scenario_entities -- so does
        "summon_expires_in" itself, since the whole entity dict round-trips, not a whitelisted
        diff. This also fixed the same underlying gap for DM_Improvisation.py's own
        ADaM-conjured creatures/containers/traps, which shared it.
    """

    def _summon_creature(self, summon_spec):
        """!
        @brief Instances summon_spec's own named entity/template as a fresh, independent scene
            participant at the caster's own current band -- never claims self.current_target
            (an ally never should; see ImprovisationMixin's own _claim_current_target_if_free
            docstring for why only a *hostile* conjured creature does). Tags the new instance
            "ad_hoc" (there's no scenario/room-level "entities" list re-deriving it on a
            scenario/room revisit -- see CLAUDE.md's "Ad hoc entity creation and removal" and
            "Saving and loading" for how this makes it round-trip through a save anyway) and
            "summon_expires_in" (an integer countdown of combat rounds remaining, decremented
            by _expire_summon_if_due below).
        @param summon_spec A spell/ability's own "summon" table: {"name": <real entity>,
            "duration": <rounds>} or {"template": <real entity_template>, "duration": <rounds>}
            -- whichever key it carries is forwarded to _instance_entities unchanged, so a
            "template" entry still goes through NPC generation exactly like a scenario's own
            {template = ...} entry would.
        @return The new instance's own (possibly disambiguated) entity name, or None if
                summon_spec's own name/template doesn't resolve to anything real
                (_instance_entities' own log_error already reports why).
        """
        entry = {"band": self.get_band(self.player_name)}
        if "template" in summon_spec:
            entry["template"] = summon_spec["template"]
        else:
            entry["name"] = summon_spec.get("name")

        instanced = self._instance_entities([entry], party_pool=self.persistent_entities)
        if not instanced:
            return None

        name = instanced[0]
        instance = self.entities[name]
        instance["ad_hoc"] = True
        instance["summon_expires_in"] = summon_spec.get("duration", 1)
        self.scenario_entities.append(name)
        return name

    def _advance_pending_spawn(self, entity_name):
        """!
        @brief Counts down a corpse's own "pending_spawn" (stashed by DM_Combat.py's
            calculate_damage on a killing blow whose ability authored "create_spawn") once per
            combat round, instancing the named entity at the corpse's own band the round this
            reaches 0 -- the Pathfinder Wight/Shadow "kills become one of us" shape. Deliberately
            NOT built on top of _summon_creature above: a create_spawn creature is a permanent
            new enemy, not a temporary conjured ally, so it's instanced directly (the same
            _instance_entities primitive _summon_creature itself calls) without ever tagging
            "summon_expires_in" -- nothing here ever counts it back down or removes it the way
            _expire_summon_if_due does for a real summon. Still tagged "ad_hoc" for the same
            save/load reachability reason _summon_creature's own docstring explains (no scenario/
            room "entities" list re-derives it on a revisit). An entity with no "pending_spawn"
            at all is untouched.
        @param entity_name The name of the (possibly dead) entity to check -- called for every
            scenario entity regardless of current HP, unlike the rest of run_round_upkeep, since
            a corpse is exactly the entity this needs to keep ticking on.
        """
        entity = self.entities.get(entity_name)
        pending = entity.get("pending_spawn") if entity else None
        if pending is None:
            return
        pending["rounds_remaining"] -= 1
        if pending["rounds_remaining"] > 0:
            return
        del entity["pending_spawn"]
        instanced = self._instance_entities(
            [{"name": pending["name"], "band": pending["band"]}], party_pool=self.persistent_entities,
        )
        if not instanced:
            return
        spawn_name = instanced[0]
        self.entities[spawn_name]["ad_hoc"] = True
        self.scenario_entities.append(spawn_name)

    def _expire_summon_if_due(self, entity_name):
        """!
        @brief Counts down a temporary summon's own "summon_expires_in" (set by
            _summon_creature, above) once per combat round, removing it from the scene
            entirely (remove_entity_from_scene, ImprovisationMixin) the round this reaches 0
            -- a summon expiring means the *creature* is gone, not just one of its traits, so
            this is a real removal, not a condition dismiss. An entity with no
            "summon_expires_in" at all (everything hand-authored/ad hoc that isn't a summon)
            is untouched.
        @param entity_name The name of the entity to check.
        """
        entity = self.entities.get(entity_name)
        if entity is None or "summon_expires_in" not in entity:
            return
        entity["summon_expires_in"] -= 1
        if entity["summon_expires_in"] <= 0:
            self.remove_entity_from_scene(entity_name)
