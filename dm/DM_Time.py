from dm.DM_Types import DMCoreProtocol


class TimeMixin(DMCoreProtocol):
    """!
    @brief The block clock underlying downtime (DMCore mixin -- only ever composed into
        DMCore, never instantiated on its own; relies on self.rules/self.entities/
        self.scenario_entities/self.event_bus/self.roll_dice/self.apply_healing/
        self.pending_downtime/self._is_party_member/self.get_current_hp/
        self._current_environment/self._resolve_environment_block/
        self._any_hostile_present/self._resume_pending_downtime, set up by DMCore.__init__ or
        implemented by DM_Travel.py/DM_Core.py). See docs/downtime.md for the design this
        implements: self.current_block is a single monotonic counter of every 8-hour (by
        default) "block" elapsed since the scenario started -- round-tripped through
        save_game/load_game the same way round_number already is (see DM_Persistence.py) --
        and every other time concept (day number, block-in-day, hour-of-day, day/night) is
        derived from it fresh rather than stored redundantly.
    """

    def _time_rules(self):
        """!
        @brief rules.toml's own [time] table, defaulted the same shape every other optional
            rules.toml table falls back to when a setting doesn't author one (ex: [xp]) --
            24 hours/day, 16 of them daylight, 3 blocks/day (an 8-hour block, matching
            docs/downtime.md's own worked example).
        @return {hours_per_day, daylight_hours, blocks_per_day}.
        """
        return self.rules.get(
            "time", {"hours_per_day": 24, "daylight_hours": 16, "blocks_per_day": 3},
        )

    def get_time_state(self):
        """!
        @brief The current moment on the block clock, derived from self.current_block alone.
            Day/night is read off actual elapsed hours against [time]'s own daylight_hours,
            not a fixed block-index parity, so a setting whose daylight_hours doesn't evenly
            split blocks_per_day still resolves sensibly -- a block counts as daytime if it
            *starts* before daylight_hours; one straddling the dusk boundary reads as
            whichever it began in.
        @return {day, block_in_day, hour, is_day, blocks_per_day, hours_per_day}.
        """
        time_rules = self._time_rules()
        blocks_per_day = max(1, time_rules.get("blocks_per_day", 3))
        hours_per_day = time_rules.get("hours_per_day", 24)
        hours_per_block = hours_per_day / blocks_per_day
        block_in_day = self.current_block % blocks_per_day
        hour = block_in_day * hours_per_block
        return {
            "day": self.current_block // blocks_per_day,
            "block_in_day": block_in_day,
            "hour": hour,
            "is_day": hour < time_rules.get("daylight_hours", 16),
            "blocks_per_day": blocks_per_day,
            "hours_per_day": hours_per_day,
        }

    def is_daytime(self):
        """!@brief Whether the current block counts as daytime -- see get_time_state."""
        return self.get_time_state()["is_day"]

    def advance_blocks(self, blocks=1):
        """!
        @brief Advances the block clock by blocks (floored at 0 -- there's no such thing as
            time moving backward). The one and only place self.current_block is ever
            mutated, mirroring round_number's own single incrementing site
            (_resolve_combat_round, DM_Combat.py).
        @param blocks How many blocks elapse.
        @return The resulting get_time_state().
        """
        self.current_block += max(0, blocks)
        return self.get_time_state()

    def rest(self, blocks=1):
        """!
        @brief Downtime rest (docs/downtime.md): consults the current location's own
            environment (_current_environment, DM_Travel.py -- None for a location with no
            "grid" field, or one whose grid point falls in an unmapped world_map.toml gap,
            both the same "absence of an environment" default that's what "safe" looks like
            everywhere in this design) once, then advances the clock one block at a time via
            _advance_pending_rest, rolling that same environment's own day/night encounter
            table (and, on a night block whose roll turns out hostile, a watch check) via
            _resolve_environment_block -- the exact machinery grid travel's own per-block loop
            already uses, just against one fixed point instead of a line of travel, since
            nothing moves during rest. A location with no environment at all skips this
            entirely.

            A hostile block pauses the rest (self.pending_downtime, "kind": "rest") exactly
            like grid travel now does (see docs/downtime.md's "Pausing for a fight") --
            without needing a scene of its own the way a mid-journey ambush does, since rest
            already happens at a real, already-loaded location. Denied outright (reason
            "downtime_interrupted") if a previously-paused trip/rest hasn't cleared yet,
            opportunistically resumed first in case its blocker was removed some other way.

            Once every block has actually elapsed, heals every living party member
            (is_player/is_party -- see _is_party_member) via the ordinary apply_healing call,
            scaled by their own "fortitude" skill -- the body's own recovery, picked over
            "medicine" (a caregiver treating someone else's wound). One aggregate roll per
            rester over the whole rest, not one per block -- fortitude's own dice/pips scale
            directly with blocks spent before the roll happens, so a longer rest's variance
            still grows the way rolling more dice actually would, the same "avoid swinginess
            from rolling repeatedly" reasoning crafting's own days_required already follows.
            Unaffected by however many of those blocks turned out hostile above -- resting
            through an ambush (once it's actually cleared) still heals exactly as much as an
            uneventful rest of the same length would.
        @param blocks How many blocks to spend resting (floored at 1).
        @return {"interrupted": True, "reason": "downtime_interrupted"} if denied outright;
                {"interrupted": True} if this call paused partway through; otherwise
                {"interrupted": False, "healed": {entity_name: {healed, remaining_hp}},
                "blocks_spent": blocks, "time": get_time_state()}.
        """
        if self.pending_downtime and not self._any_hostile_present():
            self._resume_pending_downtime()
        if self.pending_downtime:
            return {"interrupted": True, "reason": "downtime_interrupted"}

        blocks = max(1, blocks)
        self.pending_downtime = {"kind": "rest", "blocks_total": blocks, "blocks_done": 0}
        return self._advance_pending_rest()

    def _advance_pending_rest(self):
        """!
        @brief Runs (or resumes) self.pending_downtime's own per-block loop from wherever
            "blocks_done" left off -- rest's counterpart to DM_Travel.py's own
            _advance_pending_travel, against _current_environment's single fixed point rather
            than a line, so no per-block position math is needed here at all. A block whose
            own roll turns out hostile updates "blocks_done" and returns immediately
            ({"interrupted": True}) instead of continuing -- no encounter site to enter, since
            rest already happens at a real location. Running every remaining block clean hands
            off to _finish_pending_rest for the actual healing.
        @return _finish_pending_rest's own result, or {"interrupted": True}.
        """
        pending = self.pending_downtime
        environment = self._current_environment()
        for i in range(pending["blocks_done"], pending["blocks_total"]):
            hostile = False
            if environment:
                hostile = self._resolve_environment_block(environment)
            self.advance_blocks(1)
            if hostile:
                pending["blocks_done"] = i + 1
                return {"interrupted": True}

        return self._finish_pending_rest()

    def _finish_pending_rest(self):
        """!
        @brief Completes a self.pending_downtime rest once every block has cleared without
            interruption -- the actual healing roll, deferred until now instead of running
            right after a single bulk advance_blocks call. Clears self.pending_downtime first,
            same reasoning _finish_pending_travel follows.
        @return {"interrupted": False, "healed": {...}, "blocks_spent": int, "time": {...}} --
                exactly the fields intents/rest.py's narrate_rest expects, plus "blocks_spent"
                for load-bearing use by the resume path (DM_Core.py's _resume_pending_downtime).
        """
        pending = self.pending_downtime
        blocks = pending["blocks_total"]
        self.pending_downtime = None
        time_state = self.get_time_state()

        healed = {}
        for entity_name in self.scenario_entities:
            if not self._is_party_member(entity_name) or self.get_current_hp(entity_name) <= 0:
                continue
            fortitude = self.entities.get(entity_name, {}).get("skills", {}).get(
                "fortitude", {"dice": 0, "pips": 0},
            )
            amount = self.roll_dice(
                fortitude.get("dice", 0) * blocks, fortitude.get("pips", 0) * blocks,
            )
            remaining_hp = self.apply_healing(entity_name, amount)
            healed[entity_name] = {"healed": amount, "remaining_hp": remaining_hp}
        return {"interrupted": False, "healed": healed, "blocks_spent": blocks, "time": time_state}
