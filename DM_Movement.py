from DM_Types import DMCoreProtocol


class MovementMixin(DMCoreProtocol):
    """!
    @brief Band positioning, player-issued advance/retreat, and range gating (DMCore mixin --
        only ever composed into DMCore, never instantiated on its own; relies on
        self.entities/self.rules/self.scenario/self.scenario_entities/self.player_name/
        self.current_target/self.event_bus, set up by DMCore.__init__).

        **Objective bands, not distance-from-player.** Every scenario entity -- the player
        included -- carries its own "band": a 1-indexed position in the scenario's own
        `bands` (a scenario-authored field, minimum 1, replacing the old, entirely unused
        `distance_multiplier`). Band 1 means nothing special on its own; what matters is the
        *gap* between two entities' band numbers (get_distance_between), and specifically
        whether that gap is 0 -- "the same band as the target" is what "melee only works in
        the same band as the target" (an explicit design decision, not a default) actually
        means. This replaced an earlier, short-lived version of this file where the player
        was a fixed anchor at 0 and everything else's position was stored relative to them --
        rejected because it made every other entity's distance-to-the-player-equidistant by
        construction; two enemies could never be at different effective ranges from each
        other, only from the player. Real per-entity band numbers fix that, and as a direct
        consequence "retreat" now has a real, sometimes surprising effect: moving away from
        one entity can move you *closer* to another one sitting on the opposite side (see
        advance_or_retreat).

        **Ranges are a single number, not a modifier.** An earlier version of this file also
        had accuracy modifiers (a `[[range_modifier]]` table: point-blank/short/medium/long,
        each with its own {dice, pips, bonus}) layered on top of range-gating. That's gone --
        every weapon/spell/ability now carries a single `range` (int, in bands): absent or 0
        means melee, usable only in the target's own band; a reach weapon (ex: items.toml's
        `spear`, `range = 1`) reaches one band further; a genuinely ranged weapon or spell
        (ex: `long bow`, `range = 6`) reaches however many bands its own data says. Being in
        range only ever gates whether the roll happens at all (is_in_range) -- it never
        changes the roll's difficulty. Simpler, and correct for what this needed: the
        modifier table's whole reason to exist was letting a *shot* be easier or harder at
        different distances, which stopped mattering the moment the design settled on bands
        being the only granularity that exists at all.

        **Movement is deterministic and flat-rate.** "Creatures can move one band in a turn
        unless otherwise specified": get_range_modifier-era speed-in-units is gone too --
        movement now spends an entity's own `speed` field (an int, defaulting to 1 band, only
        overridden on something unusually fast/slow) shifting *its own* band, never anyone
        else's. Only the player can choose to do this today via advance_or_retreat (see its
        own docstring for why -- creature/ally behavior never chooses to move; see
        DM_Combat.py's choose_behavior).

        **A room's own `bands` count is also its wall, unless it says otherwise.**
        `scenario["enclosed"]` (bool) controls whether move_entity's upper clamp is enforced:
        an enclosed room (ex: the arena, the tavern, the dungeon cellar) caps retreat at the
        room's own last band -- cornered, no further running, matching the "there's a wall"
        intuition an enclosed space actually has. An unenclosed one (ex: the open field
        scenario) has no upper clamp at all, so retreating can carry an entity arbitrarily far
        -- which is also, deliberately, the entire mechanism for "escaping" a fight: nothing
        else models fleeing the scene, because nothing else needs to. Once the gap between two
        entities exceeds every attack either of them has, neither can touch the other anymore,
        which *is* what getting away means. No band count is ever a lower bound -- band 1 is
        always the floor, "advance" just can't go tighter than being in the same band as
        whatever's already there.
    """

    def get_band(self, entity_name):
        """!
        @brief The entity's current band -- a 1-indexed position in the scenario's own bands,
            objective (not relative to the player or anything else).
        @param entity_name The entity to check.
        @return The entity's "band" field (1 if unset -- ex: an ad-hoc test entity with no
                scenario-authored starting band defaults to band 1, same as load_scenario
                already seeds for anything that doesn't specify one).
        """
        return self.entities.get(entity_name, {}).get("band", 1)

    def get_distance_between(self, entity_a, entity_b):
        """!
        @brief The gap between two entities, in bands -- just their two band numbers
            subtracted, since both are objective positions on the same scenario-wide scale
            (see this file's module docstring for why that's a deliberate departure from an
            earlier, player-anchored version).
        @param entity_a The first entity's name.
        @param entity_b The second entity's name.
        @return The absolute band gap between them (0 if they're in the same band).
        """
        return abs(self.get_band(entity_a) - self.get_band(entity_b))

    def _clamp_band(self, band):
        """!
        @brief Clamps a candidate band to a floor of 1 always, and additionally to the
            current scene's own "bands" count whenever its "enclosed" is true (the default
            when the field is missing -- safer to assume a room has walls than to silently
            let retreat escape an unenclosed-by-omission scenario). An unenclosed scene
            has no upper clamp at all -- see this file's module docstring for why that's the
            actual "escape" mechanism. "Current scene" is the current *room*'s own table for
            a multi-room dungeon (bands/enclosed are declared per-room there, not on
            self.scenario itself -- see DM_Rules.py's room-graph notes) or self.scenario
            directly for a plain single-room scenario. Pure -- doesn't touch any entity --
            so advance_or_retreat can preview a candidate move before committing to it.
        @param band The candidate band number.
        @return The clamped band number.
        """
        scene = self._current_room() or self.scenario
        band = max(1, band)
        if scene.get("enclosed", True):
            band = min(band, scene.get("bands", 1))
        return band

    def move_entity(self, entity_name, delta):
        """!
        @brief Shifts a single entity's own band by delta (positive = higher band number,
            negative = lower), clamped by _clamp_band.
        @param entity_name The entity to move.
        @param delta The signed amount to shift its band by.
        @return The entity's new band, or None if it isn't a real entity.
        """
        entity = self.entities.get(entity_name)
        if entity is None:
            return None
        entity["band"] = self._clamp_band(self.get_band(entity_name) + delta)
        return entity["band"]

    def advance_or_retreat(self, direction):
        """!
        @brief Resolves the player's own "advance"/"retreat" action: moves *only* the
            player's own band, toward or away from self.current_target, by up to the
            player's own "speed" (default 1). Every other entity's own band is untouched --
            they don't move, the player does -- but because gaps are computed from both
            sides' band numbers (get_distance_between), the player's own movement still
            changes their distance to *everyone* in the scene at once, sometimes in opposite
            directions: retreating from current_target can carry the player past band 1
            (impossible, clamped) or straight toward a different entity sitting on the other
            side, closing that gap even though "retreat" was the command. This is the direct
            payoff of objective bands over the earlier player-anchored version, which could
            never produce that outcome because every other entity moved in lockstep.

            If current_target is in the same band already (gap 0), "advance" has nothing to
            do -- already as close as physically possible, so it's a no-op rather than
            picking an arbitrary direction to move in anyway. "Retreat" from a tie still
            needs *some* direction, since opening the gap is well-defined even when there's
            no "toward" to invert; it prefers moving to a higher band number (arbitrary, but
            documented, not an accident) and falls back to a lower one only if that's already
            blocked -- ex: the player is tied with current_target *and* already pinned at an
            enclosed room's own ceiling, where "prefer higher" alone would silently do
            nothing (a real bug this exact case caught during testing: retreating while
            engaged at band 1 of a 4-band room kept trying to go to band 0, which doesn't
            exist, and every retreat silently no-opped).
        @param direction "advance" (closes the gap to current_target) or "retreat" (opens it).
        @return A list of {entity, before, after} dicts -- the gap to each living non-player
                scenario entity that actually changed, before/after in bands. Not what moved
                (only the player did); what changed as a result.
        """
        target_name = self.current_target
        if not target_name or target_name == self.player_name:
            return []

        before_gaps = {
            entity_name: self.get_distance_between(self.player_name, entity_name)
            for entity_name in self.scenario_entities
            if entity_name != self.player_name and self.get_current_hp(entity_name) > 0
        }

        player_band = self.get_band(self.player_name)
        gap_to_target = self.get_band(target_name) - player_band
        speed = self.entities.get(self.player_name, {}).get("speed", 1)

        if gap_to_target == 0:
            if direction == "advance":
                delta = 0
            elif self._clamp_band(player_band + speed) != player_band:
                delta = speed
            else:
                delta = -speed
        else:
            toward_target = 1 if gap_to_target > 0 else -1
            delta = (toward_target if direction == "advance" else -toward_target) * speed

        self.move_entity(self.player_name, delta)

        moved = []
        for entity_name, before in before_gaps.items():
            after = self.get_distance_between(self.player_name, entity_name)
            if after != before:
                moved.append({"entity": entity_name, "before": before, "after": after})
        return moved

    def is_in_range(self, attacker_name, defender_name, ability):
        """!
        @brief Whether attacker_name can currently reach defender_name with ability at all --
            a pure reachability gate, no difficulty change either way (see this file's
            module docstring for why the earlier per-tier accuracy modifier was dropped).
        @param attacker_name The name of the acting entity.
        @param defender_name The name of the target entity.
        @param ability The weapon/spell/innate-ability table being used, or None if this
            skill use isn't an attack at all (ex: a social check) -- always in range, since
            there's nothing physical to be out of reach of.
        @return True if reachable (ability is None, or the band gap is within ability's own
                "range", which defaults to 0 -- melee, same band only -- when absent).
        """
        if ability is None:
            return True
        max_range = ability.get("range", 0)
        return self.get_distance_between(attacker_name, defender_name) <= max_range
