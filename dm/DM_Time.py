import resolution.Combat_Resolution as Combat_Resolution
from dm.DM_Types import DMCoreProtocol


class TimeMixin(DMCoreProtocol):
    """!
    @brief The block clock underlying downtime (DMCore mixin -- only ever composed into
        DMCore, never instantiated on its own; relies on self.rules/self.entities/
        self.scenario_entities/self.event_bus/self.roll_dice/self.apply_healing/
        self.pending_downtime/self._is_party_member/self.get_current_hp/
        self._current_environment/self._resolve_environment_block/
        self._any_hostile_present/self._resume_pending_downtime/self.apply_downtime_upkeep,
        set up by DMCore.__init__ or implemented by DM_Travel.py/DM_Core.py/DM_Status.py). See
        docs/downtime.md for the design this
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
            docs/downtime.md's own worked example), and a "starting_year" of 1 -- day 0's own
            calendar year (_calendar_date_from_day) before any [[calendar_month]]-authoring
            setting overrides it (ex: Rules/Fantasy's own 4726, Golarion's current year).
        @return {hours_per_day, daylight_hours, blocks_per_day, starting_year}.
        """
        return self.rules.get(
            "time",
            {"hours_per_day": 24, "daylight_hours": 16, "blocks_per_day": 3, "starting_year": 1},
        )

    def get_time_state(self):
        """!
        @brief The current moment on the block clock, derived from self.current_block alone.
            Day/night is read off actual elapsed hours against [time]'s own daylight_hours,
            not a fixed block-index parity, so a setting whose daylight_hours doesn't evenly
            split blocks_per_day still resolves sensibly -- a block counts as daytime if it
            *starts* before daylight_hours; one straddling the dusk boundary reads as
            whichever it began in. "year"/"month"/"day_of_month" are only ever non-None for a
            setting that authors rules.toml's own [[calendar_month]] table (see
            _calendar_date_from_day) -- absent for one that doesn't (ex: Rules/Zombie/), the
            same "still works unauthored" fallback every other optional table already follows;
            "date_label" is a ready-made human-readable string either way, for narration to
            splice in directly rather than re-deriving this same day-vs-calendar branch itself.
        @return {day, block_in_day, hour, is_day, blocks_per_day, hours_per_day, year, month,
                day_of_month, date_label}.
        """
        time_rules = self._time_rules()
        blocks_per_day = max(1, time_rules.get("blocks_per_day", 3))
        hours_per_day = time_rules.get("hours_per_day", 24)
        hours_per_block = hours_per_day / blocks_per_day
        block_in_day = self.current_block % blocks_per_day
        hour = block_in_day * hours_per_block
        day = self.current_block // blocks_per_day
        calendar_date = self._calendar_date_from_day(day)
        if calendar_date:
            date_label = f"day {calendar_date['day_of_month']} of {calendar_date['month']}, Year {calendar_date['year']}"
        else:
            date_label = f"day {day}"
        return {
            "day": day,
            "block_in_day": block_in_day,
            "hour": hour,
            "is_day": hour < time_rules.get("daylight_hours", 16),
            "blocks_per_day": blocks_per_day,
            "hours_per_day": hours_per_day,
            "year": calendar_date["year"] if calendar_date else None,
            "month": calendar_date["month"] if calendar_date else None,
            "day_of_month": calendar_date["day_of_month"] if calendar_date else None,
            "date_label": date_label,
        }

    def is_daytime(self):
        """!@brief Whether the current block counts as daytime -- see get_time_state."""
        return self.get_time_state()["is_day"]

    def _calendar_date_from_day(self, day_number):
        """!
        @brief Converts an absolute day count (get_time_state()'s own "day") into a calendar
            date against rules.toml's own [[calendar_month]] table (an ordered list of {name,
            days} entries -- Rules/Fantasy's own table matches Golarion's real calendar, 12
            months/365 days/no leap year, but nothing here assumes that specific shape; any
            ordered month list works the same way). day_number 0 is the 1st of the first
            authored month, [time]'s own "starting_year" (default 1, ex: Rules/Fantasy's own
            4726 -- see _time_rules); the count wraps into a new year once every authored
            month's own "days" (summed, not hardcoded to 365) has been used up, repeating the
            same month order every year.
        @param day_number An absolute day count (get_time_state()'s own "day").
        @return {"year", "month", "day_of_month"} (1-indexed day_of_month), or None if this
            setting authors no [[calendar_month]] table at all (or one summing to 0 days,
            which can't be divided into).
        """
        months = self.rules.get("calendar_month", [])
        days_per_year = sum(month.get("days", 0) for month in months)
        if not months or days_per_year <= 0:
            return None
        starting_year = self._time_rules().get("starting_year", 1)
        year = day_number // days_per_year + starting_year
        day_in_year = day_number % days_per_year
        for month in months:
            month_days = month.get("days", 0)
            if day_in_year < month_days:
                return {"year": year, "month": month.get("name", ""), "day_of_month": day_in_year + 1}
            day_in_year -= month_days
        return None  # unreachable -- day_in_year < days_per_year always lands in some month

    def get_calendar_date(self):
        """!@brief The current calendar date -- see _calendar_date_from_day."""
        return self._calendar_date_from_day(self.get_time_state()["day"])

    def _day_of_year_from_calendar_date(self, month_name, day_of_month):
        """!
        @brief The inverse of _calendar_date_from_day's own month-walk -- how many days into
            the year month_name's day_of_month falls, against rules.toml's own
            [[calendar_month]] table. Used only to seed self.current_block from a scenario's
            own optional "start_month"/"start_day" (see _seed_starting_date) -- ordinary block-
            clock advancement never calls this, only the one-time initial placement does.
        @param month_name A [[calendar_month]] "name" (case-sensitive, exactly as authored).
        @param day_of_month 1-indexed day within that month.
        @return A 0-indexed day-of-year offset, or None if this setting authors no
            [[calendar_month]] table at all, month_name doesn't match any entry, or
            day_of_month falls outside that month's own "days".
        """
        months = self.rules.get("calendar_month", [])
        day_of_year = 0
        for month in months:
            month_days = month.get("days", 0)
            if month.get("name") == month_name:
                if not (1 <= day_of_month <= month_days):
                    return None
                return day_of_year + (day_of_month - 1)
            day_of_year += month_days
        return None

    def _seed_starting_date(self):
        """!
        @brief Sets self.current_block from the scenario's own optional [scenario]
            "start_month"/"start_day" (ex: lost_coast.toml's own Erastus 15) -- called exactly
            once, from DMCore.__init__ right after load_scenario_definition (which is what
            populates self.scenario/self.rules["calendar_month"] in the first place), never
            from load_game (which restores current_block from the save file instead -- see
            DM_Persistence.py's own docstring for why re-running scenario-derived defaults on
            top of a restored save would silently clobber it). A scenario authoring neither
            field (every scenario shipped before this existed) leaves current_block at its
            already-set default (0 -- the 1st of the first authored month, [time]'s own
            starting_year) exactly as before. "start_day" defaults to 1 (the 1st of
            "start_month") when only "start_month" is authored. An unresolvable date (an
            unknown month name, an out-of-range day, or a setting with no [[calendar_month]]
            table at all to resolve against) logs an error and leaves current_block at its
            default rather than guessing.
        """
        start_month = self.scenario.get("start_month")
        if start_month is None:
            return
        start_day = self.scenario.get("start_day", 1)
        day_of_year = self._day_of_year_from_calendar_date(start_month, start_day)
        if day_of_year is None:
            self.event_bus.publish(
                "log_error",
                f"Scenario's own start_month/start_day ({start_month!r}/{start_day!r}) doesn't "
                f"resolve against this setting's own [[calendar_month]] table -- ignored.",
            )
            return
        self.current_block = day_of_year * max(1, self._time_rules().get("blocks_per_day", 3))

    def advance_blocks(self, blocks=1):
        """!
        @brief Advances the block clock by blocks (floored at 0 -- there's no such thing as
            time moving backward). The one and only place self.current_block is ever
            mutated, mirroring round_number's own single incrementing site
            (_resolve_combat_round, DM_Combat.py). Also expires any planted prompt_directive
            whose own countdown runs out this many blocks (_expire_prompt_directives) -- the
            one other piece of state that ticks against this same clock.
        @param blocks How many blocks elapse.
        @return The resulting get_time_state().
        """
        blocks = max(0, blocks)
        self.current_block += blocks
        self._expire_prompt_directives(blocks)
        self._tick_conditions_by_block(blocks)
        return self.get_time_state()

    def _tick_conditions_by_block(self, blocks):
        """!
        @brief Ticks every entity's own active_conditions whose "duration" is "blocks" down by
            however many blocks just elapsed (Combat_Resolution.tick_condition_durations),
            dismissing any that reach 0 -- blocks' own counterpart to run_round_upkeep's
            "rounds" tick and enter_room's "rooms" tick. Same global "every entity, regardless
            of presence in the current scene" scope _expire_prompt_directives already uses,
            since a block-scale duration (measured in hours, not the current fight) isn't tied
            to who's actually on-screen the way a round-scale one is.

            Also advances every active_conditions entry's own "periodic_test" countdown by
            blocks (Combat_Resolution.tick_periodic_tests) -- a disease's own "Frequency 1/day"
            self-save (converted from "days" via [time].blocks_per_day), the same global,
            presence-independent scope as the duration tick just above: a disease keeps
            progressing on a party member who's stepped out of the current scene entirely.
        @param blocks How many blocks just elapsed.
        """
        if blocks <= 0:
            return
        for entity_name in list(self.entities):
            Combat_Resolution.tick_condition_durations(self.entities, self.event_bus, entity_name, "blocks", blocks)
            Combat_Resolution.tick_periodic_tests(self.entities, self.rules, self.event_bus, entity_name, "blocks", blocks)

    def _expire_prompt_directives(self, blocks):
        """!
        @brief Decrements every entity's own planted prompt_directive's "expires_in_blocks"
            (if it has one) by blocks elapsed, clearing the directive entirely once it reaches
            0 or below. Social_Resolution.py's set_prompt_directive plants a directive with no
            expiry at all by default (persists until overwritten or manually cleared, exactly
            as before this existed); an authored "inject_directive" program op can instead give
            it a real "duration" in blocks -- its own bespoke field, distinct from (and ticked
            separately from -- see _tick_conditions_by_block, below) a [[condition]]'s own
            "duration"/"length" pair, since a prompt_directive isn't a condition at all.
        @param blocks How many blocks just elapsed.
        """
        if blocks <= 0:
            return
        for entity in self.entities.values():
            directive = entity.get("prompt_directive")
            if not directive or "expires_in_blocks" not in directive:
                continue
            directive["expires_in_blocks"] -= blocks
            if directive["expires_in_blocks"] <= 0:
                entity["prompt_directive"] = None

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
            interruption -- the actual fortitude-scaled party healing roll, plus
            apply_downtime_upkeep's own condition-driven tick (ex: a regenerating creature's
            own upkeep_heal, otherwise only ever applied mid-combat) for every living scene
            entity, deferred until now instead of running right after a single bulk
            advance_blocks call. Clears self.pending_downtime first, same reasoning
            _finish_pending_travel follows.
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

        self.apply_downtime_upkeep(blocks)

        return {"interrupted": False, "healed": healed, "blocks_spent": blocks, "time": time_state}
