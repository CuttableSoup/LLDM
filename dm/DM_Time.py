from dm.DM_Types import DMCoreProtocol


class TimeMixin(DMCoreProtocol):
    """!
    @brief The block clock underlying downtime (DMCore mixin -- only ever composed into
        DMCore, never instantiated on its own; relies on self.rules/self.entities/
        self.scenario_entities/self.event_bus/self.roll_dice/self.apply_healing/
        self._is_party_member/self.get_current_hp/self._current_environment/
        self._resolve_environment_block, set up by DMCore.__init__ or implemented by
        DM_Travel.py). See docs/downtime.md for the design this implements: self.current_block
        is a single monotonic counter of every 8-hour (by default) "block" elapsed since the
        scenario started -- round-tripped through save_game/load_game the same way
        round_number already is (see DM_Persistence.py) -- and every other time concept (day
        number, block-in-day, hour-of-day, day/night) is derived from it fresh rather than
        stored redundantly.
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
            everywhere in this design) once, then advances the clock one block at a time,
            rolling that same environment's own day/night encounter table (and, on a night
            block whose roll turns out hostile, a watch check) via _resolve_environment_block
            for every block spent -- the exact machinery grid travel's own per-block loop
            already uses, just against one fixed point instead of a line of travel, since
            nothing moves during rest. A location with no environment at all skips this
            entirely, same as before this existed. Deliberate simplification, matching grid
            travel's own: a hostile encounter (or a failed watch) doesn't cut the rest short
            or block the healing below -- pausing downtime for a real fight is a separate,
            still-deferred extension (see docs/downtime.md's "Not yet built").

            Once every block has elapsed, heals every living party member (is_player/is_party
            -- see _is_party_member) via the ordinary apply_healing call, scaled by their own
            "fortitude" skill -- the body's own recovery, picked over "medicine" (a caregiver
            treating someone else's wound). One aggregate roll per rester over the whole rest,
            not one per block -- fortitude's own dice/pips scale directly with blocks spent
            before the roll happens, so a longer rest's variance still grows the way rolling
            more dice actually would, the same "avoid swinginess from rolling repeatedly"
            reasoning crafting's own days_required already follows. Unaffected by however many
            of those blocks turned out hostile above -- resting through an ambush still heals
            exactly as much as an uneventful rest of the same length would.
        @param blocks How many blocks to spend resting (floored at 1).
        @return {healed: {entity_name: {healed, remaining_hp}}, time: get_time_state()}.
        """
        blocks = max(1, blocks)
        environment = self._current_environment()
        for _ in range(blocks):
            if environment:
                self._resolve_environment_block(environment)
            self.advance_blocks(1)
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
        return {"healed": healed, "time": time_state}
