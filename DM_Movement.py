import re

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
        else's. The player triggers this via advance_or_retreat (see its own docstring);
        a creature/ally triggers the exact same math via move_toward_or_away, either because
        its own `[[entity.behavior]]` explicitly names `action = "advance"`/`"retreat"` (ex:
        a self-preserving animal fleeing once badly hurt) or because resolve_behavior_action
        (DM_Combat.py) falls back to it automatically when the behavior it did choose names an
        attack that can't currently reach its target -- closing the distance instead of simply
        not acting.

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
        # A freeform location (no active room) has no bands of its own at all -- pinned to a
        # single enclosed band 1, which is what makes advance/retreat there always a no-op,
        # consistent with "no real positioning" (see DM_Rules.py's _enter_location).
        scene = self._current_room() or {"enclosed": True, "bands": 1}
        band = max(1, band)
        if scene.get("enclosed", True):
            band = min(band, scene.get("bands", 1))
        return band

    def _apply_party_formation(self):
        """!
        @brief Snaps every currently-scened party member's own band to a fixed offset from
            the player's own band -- ex: arena.toml/crypt.toml's own thane (follow_offset = 0)
            walks abreast, crypt.toml's own anne (follow_offset = -1) trails one band behind,
            favoring her own ranged spellwork over standing in melee. Called anywhere the
            *player's* own band
            changes (advance_or_retreat, enter_room's own arrival) -- never from a creature/
            ally's own combat-turn movement (move_toward_or_away), which stays purely
            tactical (chasing/fleeing its own opponent) and is deliberately left free to drift
            out of formation until the player's next move snaps it back. This is a flat
            teleport to the target band, not a speed-limited move_entity call -- keeping
            formation is bookkeeping, not an action that costs a turn.

            follow_offset is read straight off the entity instance (defaulting to 0 -- walking
            abreast -- for a party member that doesn't set one), the same per-instance field
            every other mutable entity stat lives on, so a future player command (ex: "stay
            behind me, anne") only ever needs to write entities["anne"]["follow_offset"] = -2
            to take effect on the very next move -- no new mechanism required, just not wired
            to any player-issued command yet.
        """
        player_band = self.get_band(self.player_name)
        for entity_name in self.scenario_entities:
            if entity_name == self.player_name:
                continue
            entity = self.entities.get(entity_name, {})
            if not entity.get("is_party"):
                continue
            offset = entity.get("follow_offset", 0)
            entity["band"] = self._clamp_band(player_band + offset)

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

    def _resolve_move_delta(self, entity_name, opponent_name, direction):
        """!
        @brief The shared "which way, how far" math behind both advance_or_retreat (the
            player's own move) and move_toward_or_away (a creature/ally's own move):
            entity_name's signed band shift, up to its own "speed" (default 1), toward or
            away from opponent_name.

            If opponent_name is in the same band already (gap 0), "advance" has nothing to
            do -- already as close as physically possible, so it's a no-op rather than
            picking an arbitrary direction to move in anyway. "Retreat" from a tie still
            needs *some* direction, since opening the gap is well-defined even when there's
            no "toward" to invert; it prefers moving to a higher band number (arbitrary, but
            documented, not an accident) and falls back to a lower one only if that's already
            blocked -- ex: entity_name is tied with opponent_name *and* already pinned at an
            enclosed room's own ceiling, where "prefer higher" alone would silently do
            nothing (a real bug this exact case caught during testing: retreating while
            engaged at band 1 of a 4-band room kept trying to go to band 0, which doesn't
            exist, and every retreat silently no-opped).
        @param entity_name The entity that will move.
        @param opponent_name The entity being moved toward/away from.
        @param direction "advance" (closes the gap) or "retreat" (opens it).
        @return The signed band delta to pass to move_entity.
        """
        entity_band = self.get_band(entity_name)
        gap = self.get_band(opponent_name) - entity_band
        speed = self.entities.get(entity_name, {}).get("speed", 1)

        if gap == 0:
            if direction == "advance":
                return 0
            if self._clamp_band(entity_band + speed) != entity_band:
                return speed
            return -speed

        toward_opponent = 1 if gap > 0 else -1
        return (toward_opponent if direction == "advance" else -toward_opponent) * speed

    def advance_or_retreat(self, direction):
        """!
        @brief Resolves the player's own "advance"/"retreat" action: moves the player's own
            band, toward or away from self.current_target, by up to the player's own "speed"
            (default 1) -- see _resolve_move_delta for the shared distance/tie-breaking math
            -- then snaps every party member's own band back into formation around the
            player's new position (_apply_party_formation). Every *other* entity's own band
            is untouched -- an enemy doesn't move just because the player did -- but because
            gaps are computed from both sides' band numbers (get_distance_between), the
            player's own movement still changes their distance to *everyone* in the scene at
            once, sometimes in opposite directions: retreating from current_target can carry
            the player past band 1 (impossible, clamped) or straight toward a different
            entity sitting on the other side, closing that gap even though "retreat" was the
            command. This is the direct payoff of objective bands over the earlier
            player-anchored version, which could never produce that outcome because every
            other entity moved in lockstep.
        @param direction "advance" (closes the gap to current_target) or "retreat" (opens it).
        @return A list of {entity, before, after} dicts -- the gap to each living non-player
                scenario entity that actually changed, before/after in bands. Not what moved
                (only the player and the party formation did); what changed as a result.
        """
        target_name = self.current_target
        if not target_name or target_name == self.player_name:
            return []

        before_gaps = {
            entity_name: self.get_distance_between(self.player_name, entity_name)
            for entity_name in self.scenario_entities
            if entity_name != self.player_name and self.get_current_hp(entity_name) > 0
        }

        self.move_entity(self.player_name, self._resolve_move_delta(self.player_name, target_name, direction))
        self._apply_party_formation()

        moved = []
        for entity_name, before in before_gaps.items():
            after = self.get_distance_between(self.player_name, entity_name)
            if after != before:
                moved.append({"entity": entity_name, "before": before, "after": after})
        return moved

    def move_toward_or_away(self, entity_name, opponent_name, direction):
        """!
        @brief The creature/ally counterpart to advance_or_retreat -- moves entity_name's own
            band toward/away from opponent_name by up to its own "speed", using the exact
            same distance/tie-breaking math (_resolve_move_delta). Unlike advance_or_retreat,
            which is always relative to the player's own current_target, this takes
            opponent_name directly so it works for any entity against whichever opponent
            resolve_behavior_action already resolved for it (a hostile entity's own player
            target, or an ally's shared current_target) -- called either because a behavior
            entry explicitly names `action = "advance"`/`"retreat"` (ex: fleeing once badly
            hurt), or as resolve_behavior_action's own fallback when an attack it chose can't
            currently reach opponent_name.
        @param entity_name The entity that will move.
        @param opponent_name The entity being moved toward/away from.
        @param direction "advance" or "retreat".
        @return {"opponent", "before", "after"} (the band gap to opponent_name, before/after
                the move), or None if entity_name or opponent_name isn't a real, distinct
                entity.
        """
        if not entity_name or not opponent_name or entity_name == opponent_name:
            return None
        if entity_name not in self.entities or opponent_name not in self.entities:
            return None

        before = self.get_distance_between(entity_name, opponent_name)
        self.move_entity(entity_name, self._resolve_move_delta(entity_name, opponent_name, direction))
        after = self.get_distance_between(entity_name, opponent_name)
        return {"opponent": opponent_name, "before": before, "after": after}

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

    def _resolve_room_transition_intent(self, direction, resolved):
        """!
        @brief Handles "move" -- taking a declared exit to a different room *within the current
            location* (see DM_Rules.py's [[location.room]] notes and _find_room_exit). Denied
            (reason "no_exit") if the current room has no exit at all in that direction --
            including a single-room location (ex: arena/tavern), whose one room simply declares
            none, same as a "close" aimed at a creature fails "not_openable"; denied (reason
            "wrong_band") if that direction exists in this room but not from the player's
            current band; denied (reason "blocked_by_enemies") if any living hostile creature
            remains in the current room -- a dungeon-crawl convention: a room is cleared before
            moving past it, not slipped around while something is still actively fighting the
            player. See _resolve_travel_intent, below, for the location-to-location counterpart
            of this same move.
        @param direction "forward"/"back"/"left"/"right", from NLPCore's direction match.
        @param resolved The item_interaction_resolved publisher closure from
            DMCore._on_item_interaction_detected.
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

    def _resolve_location_exit(self, input_text):
        """!
        @brief Finds the current location's own declared [[location.exit]] named in input_text
            -- searching for any exit's destination location's own "name" (or one of its
            "aliases"), whole-word and case-insensitive, present in input_text. Same
            "search the raw input for a known name" pattern _resolve_formation_intent already
            uses for a party member's own name, just against multi-word phrases.
        @param input_text The raw (lowercased, prefix-stripped) player input.
        @return The matched [[location.exit]] table, or None if no destination is named.
        """
        location = self.locations.get(self.current_location_key, {})
        for exit_def in location.get("exit", []):
            destination = self.locations.get(exit_def.get("destination"), {})
            candidates = [destination.get("name", "")] + list(exit_def.get("aliases", []))
            for phrase in candidates:
                if phrase and re.search(rf"\b{re.escape(phrase.lower())}\b", input_text or ""):
                    return exit_def
        return None

    def _resolve_travel_intent(self, input_text, resolved):
        """!
        @brief Handles "travel" -- taking a declared [[location.exit]] to a different
            [[location]] entirely, the location-graph counterpart to
            _resolve_room_transition_intent's own room-graph move. Unlike a room's exits
            (band-gated, a fixed direction vocabulary), a location's exits are reachable by
            naming where you want to go (see _resolve_location_exit), from anywhere within the
            current location. Falls back to the current location's own "return_to" (a generic
            "leave"/"go outside" phrase) if no destination is named at all -- denied (reason
            "no_exit") if that's also absent (ex: town_square, the top of the graph, has
            nowhere to "leave" to).

            Hostile gate: never blocks a move taken from a location's own freeform "entities"
            space (self.rooms empty) -- an open square is non-linear on purpose, so ducking
            into a shop mid-fight is allowed. Always blocks one taken from inside a
            [[location.room]] (self.rooms populated) -- the exact same "blocked_by_enemies"
            check _resolve_room_transition_intent already runs, scoped to this one room's own
            occupants, whether the destination is another room in the same location or a jump
            to a different location entirely.
        @param input_text The raw (lowercased, prefix-stripped) player input.
        @param resolved The item_interaction_resolved publisher closure from
            DMCore._on_item_interaction_detected.
        """
        exit_def = self._resolve_location_exit(input_text)
        if exit_def is not None:
            destination_key = exit_def["destination"]
            arrival_room = exit_def.get("arrival_room")
            arrival_band = exit_def.get("arrival_band", 1)
        else:
            return_to = self.locations.get(self.current_location_key, {}).get("return_to")
            if not return_to or return_to not in self.locations:
                resolved(False, reason="no_exit")
                return
            destination_key = return_to
            arrival_room = None
            arrival_band = 1

        if self.rooms:
            for entity_name in self.scenario_entities:
                if entity_name == self.player_name:
                    continue
                if self.is_hostile(entity_name, self.player_name) and self.get_current_hp(entity_name) > 0:
                    resolved(False, reason="blocked_by_enemies")
                    return

        self._enter_location(destination_key, arrival_room=arrival_room, arrival_band=arrival_band)
        new_location = self.locations.get(self.current_location_key, {})
        new_room = self._current_room()
        resolved(
            True,
            location_name=new_location.get("name", ""),
            location_description=new_location.get("description", ""),
            room_name=new_room.get("name", "") if new_room else "",
            room_description=new_room.get("description", "") if new_room else "",
            characters=self._describe_scenario_characters(),
        )

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
        @param resolved The item_interaction_resolved publisher closure from
            DMCore._on_item_interaction_detected.
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
