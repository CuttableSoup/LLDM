from dm.DM_Types import DMCoreProtocol


class TimeMixin(DMCoreProtocol):
    """!
    @brief The block clock underlying downtime (DMCore mixin -- only ever composed into
        DMCore, never instantiated on its own; relies on self.rules/self.entities/
        self.scenario_entities/self.event_bus/self.roll_dice/self.apply_healing/
        self._is_party_member/self.get_current_hp, set up by DMCore.__init__). See
        docs/downtime.md for the design this implements: self.current_block is a single
        monotonic counter of every 8-hour (by default) "block" elapsed since the scenario
        started -- round-tripped through save_game/load_game the same way round_number
        already is (see DM_Persistence.py) -- and every other time concept (day number,
        block-in-day, hour-of-day, day/night) is derived from it fresh rather than stored
        redundantly. Day/night and rest are the only two things built on this clock so far;
        travel/environments/watch stay unbuilt (see docs/downtime.md's own "Not yet built"
        note).
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
        @brief Downtime rest (docs/downtime.md): advances the clock by blocks, then heals
            every living party member (is_player/is_party -- see _is_party_member) via the
            ordinary apply_healing call, scaled by their own "fortitude" skill -- the body's
            own recovery, picked over "medicine" (a caregiver treating someone else's
            wound). One aggregate roll per rester over the whole rest, not one per block --
            fortitude's own dice/pips scale directly with blocks spent before the roll
            happens, so a longer rest's variance still grows the way rolling more dice
            actually would, the same "avoid swinginess from rolling repeatedly" reasoning
            crafting's own days_required already follows. No environment/watch check yet
            (see docs/downtime.md) -- every rest today is exactly as safe as resting
            somewhere with no active environment always will be.
        @param blocks How many blocks to spend resting (floored at 1).
        @return {healed: {entity_name: {healed, remaining_hp}}, time: get_time_state()}.
        """
        blocks = max(1, blocks)
        time_state = self.advance_blocks(blocks)
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
