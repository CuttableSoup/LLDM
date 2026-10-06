import resolution.Combat_Resolution as Combat_Resolution
from dm.DM_Types import DMCoreProtocol
from resolution.Program_Interpreter import run_program
import resolution.Combat_Actions as Combat_Actions


class StatusMixin(DMCoreProtocol):
    """!
    @brief HP, the status/condition system, and entity tests (DMCore mixin -- only ever
        composed into DMCore, never instantiated on its own; relies on
        self.entities/self.rules/self.event_bus/self.player_name, set up by
        DMCore.__init__). The actual roll/damage/condition computation lives in
        Combat_Resolution.py, a pure module taking entities/rules/event_bus explicitly
        (see its own module docstring) -- every method below that used to hold that logic is
        now a thin wrapper forwarding self.entities/self.rules/self.event_bus, so no caller
        anywhere else in the codebase needed to change. What stays here instead is
        orchestration that reaches into a *different* mixin (apply_test_outcome's own
        loot_entity/calculate_damage calls, run_round_upkeep's own _expire_summon_if_due) or
        wasn't part of the extracted graph (get_condition_upkeep/apply_round_upkeep, the
        is_locked/is_closed/is_identified/is_hidden/is_test_available presence checks).
        Inherits DMCoreProtocol purely so type checkers can resolve these shared attributes/
        cross-mixin methods -- see DM_Types.py.
    """

    def get_condition_upkeep(self, entity_name):
        """!
        @brief Sums the per-round upkeep effect of every one of entity_name's own
            active_conditions that has a matching [[condition]] entry with an
            "upkeep_heal"/"upkeep_damage" field -- ex: "regenerating"'s
            upkeep_heal = {dice = 2, pips = 0, bonus = 0}. A condition whose own
            upkeep_blocked_by_tags overlaps entity_name's own "recent_damage_tags" (damage
            tags it was hit with since the last time this ran -- see calculate_damage,
            Combat_Actions.py) is skipped entirely for this round -- ex: a troll's regeneration
            not firing the round it took fire damage.
        @param entity_name The name of the entity to sum upkeep for.
        @return A {"heal": {"dice", "pips", "bonus"}, "damage": {"dice", "pips", "bonus"}}
                dict, each defaulting to all-0 if nothing applies.
        """
        entity = self.entities.get(entity_name, {})
        active_conditions = Combat_Resolution.get_active_conditions(self.world, entity_name)
        recent_damage_tags = entity.get("recent_damage_tags", set())
        condition_defs = {c.get("name"): c for c in self.rules.get("condition", [])}
        totals = {
            "heal": {"dice": 0, "pips": 0, "bonus": 0},
            "damage": {"dice": 0, "pips": 0, "bonus": 0},
        }
        for condition_name in active_conditions:
            condition_def = condition_defs.get(condition_name)
            if not condition_def:
                continue
            blocked_by = condition_def.get("upkeep_blocked_by_tags", [])
            if blocked_by and any(tag in blocked_by for tag in recent_damage_tags):
                continue
            for key in ("heal", "damage"):
                effect = condition_def.get(f"upkeep_{key}")
                if not effect:
                    continue
                totals[key]["dice"] += effect.get("dice", 0)
                totals[key]["pips"] += effect.get("pips", 0)
                totals[key]["bonus"] += effect.get("bonus", 0)
        return totals

    def apply_round_upkeep(self, entity_name):
        """!
        @brief Applies one round's worth of upkeep to a single entity -- rolls and applies
            get_condition_upkeep's own heal/damage totals (a regeneration/fast-healing-style
            condition heals; a future bleed/poison-with-onset condition would damage the same
            way), then clears "recent_damage_tags" so the next round starts fresh. The one
            generic per-round hook every condition-driven periodic effect shares -- see
            run_round_upkeep for the actual per-round entry point.
        @param entity_name The name of the entity to apply upkeep to.
        """
        entity = self.entities.get(entity_name)
        if entity is None:
            return
        upkeep = self.get_condition_upkeep(entity_name)
        entity["recent_damage_tags"] = set()

        heal_total = Combat_Resolution.roll_dice(upkeep["heal"]["dice"], upkeep["heal"]["pips"]) + upkeep["heal"]["bonus"]
        if heal_total > 0:
            Combat_Resolution.apply_healing(self.world, entity_name, heal_total)

        damage_total = Combat_Resolution.roll_dice(upkeep["damage"]["dice"], upkeep["damage"]["pips"]) + upkeep["damage"]["bonus"]
        if damage_total > 0:
            Combat_Resolution.apply_damage(self.world, entity_name, damage_total)

    def apply_downtime_upkeep(self, blocks):
        """!
        @brief The downtime counterpart to apply_round_upkeep -- condition-driven upkeep (ex:
            "regenerating"'s own upkeep_heal, creatures.toml's troll) previously only ever
            ticked during an active combat round (run_round_upkeep), so a regenerating
            creature never actually healed between scenes or during freeform (non-combat)
            play, no matter how much in-fiction time passed. Called from DM_Time.py's own
            _finish_pending_rest once a rest actually completes, against every living scene
            entity (not just is_player/is_party -- a creature's own regeneration isn't a party
            privilege, the same scope run_round_upkeep already uses). One aggregate roll per
            entity over the whole span (dice/pips/bonus scaled by blocks before a single roll,
            not one roll per block) -- the same "avoid swinginess from rolling repeatedly"
            reasoning rest()'s own fortitude healing already follows. Deliberately doesn't
            touch "recent_damage_tags" the way apply_round_upkeep does -- nothing takes fresh
            damage during rest, so whatever it already held (ex: fire damage from a fight right
            before making camp) correctly keeps suppressing a tag-blocked condition through the
            rest too, not just the round it happened in.
        @param blocks How many blocks this upkeep spans.
        """
        if blocks <= 0:
            return
        for entity_name in list(self.scenario_entities):
            if Combat_Resolution.get_current_hp(self.world, entity_name) <= 0:
                continue
            upkeep = self.get_condition_upkeep(entity_name)
            heal_total = Combat_Resolution.roll_dice(
                upkeep["heal"]["dice"] * blocks, upkeep["heal"]["pips"] * blocks,
            ) + upkeep["heal"]["bonus"] * blocks
            if heal_total > 0:
                Combat_Resolution.apply_healing(self.world, entity_name, heal_total)
            damage_total = Combat_Resolution.roll_dice(
                upkeep["damage"]["dice"] * blocks, upkeep["damage"]["pips"] * blocks,
            ) + upkeep["damage"]["bonus"] * blocks
            if damage_total > 0:
                Combat_Resolution.apply_damage(self.world, entity_name, damage_total)

    def run_round_upkeep(self):
        """!
        @brief Applies one round's worth of upkeep (see apply_round_upkeep) to every living
            entity currently in the scene -- the generic per-round hook
            Rules/Fantasy/reference/pathfinder_mapping.toml flagged as the shared
            prerequisite for Bleed/Regeneration/Fast Healing/poison-with-onset. Called once
            per round, after every actor's own turn has already resolved (see
            _resolve_combat_round, DM_Core.py), so a condition's own upkeep_blocked_by_tags
            can already see whatever damage tags landed this same round before deciding
            whether to fire. A dead entity (hp <= 0) is skipped entirely -- upkeep never
            revives anything on its own.

            Also counts down any temporary summon's own "summon_expires_in"
            (_expire_summon_if_due, DM_Summoning.py) -- unrelated to condition-driven upkeep,
            just sharing the same "once per round, per living scene entity" cadence rather
            than a second pass over the same list. Iterates a snapshot (list(...), not
            self.scenario_entities directly), since a summon expiring this same call removes
            itself from that live list mid-iteration.

            Also counts down any corpse's own "pending_spawn" (_advance_pending_spawn, DM_
            Summoning.py -- the Pathfinder Wight/Shadow create_spawn shape) -- deliberately
            called BEFORE the hp<=0 skip below, unlike everything else in this loop: a corpse is
            exactly the entity a pending spawn needs to keep ticking on, so it can't be filtered
            out the way a dead entity's own ordinary upkeep already is.

            Also ticks every active_conditions entry whose own "duration" is "rounds" by one
            (Combat_Resolution.tick_condition_durations) -- ex: "surprised", applied by night
            watch (DM_Travel.py's _roll_night_watch) with length=1, expiring the first time this
            same entity's upkeep runs after gaining it, docs/downtime.md's "Night watch and
            surprise".

            Also advances every active_conditions entry's own "periodic_test" countdown by one
            round (Combat_Resolution.tick_periodic_tests) -- a poison's own "Frequency 1/round"
            self-save (a disease's "1/day" cadence instead ticks off DM_Time.py's block clock,
            see _tick_conditions_by_block), rolled and applied the same round its onset/interval
            elapses in.

            Also ticks down every entry in the entity's own "ability_cooldowns"
            (Combat_Resolution.tick_ability_cooldowns) -- set whenever a behavior fires an
            ability authoring "cooldown_rounds" (Combat_Actions.py's resolve_behavior_action),
            counted back down to 0 (removed entirely once it gets there) the same once-per-
            round cadence as the condition-duration tick just above.

            Also evaluates every [[status]] authoring trigger = "on_round" against entity_name
            (evaluate_proximity_statuses) -- the same proximity-apply shape "on_action" already
            uses for a Frightful-Presence-style aura, just checked once a round for every living
            entity instead of only the one that just landed a hit. This is the whole mechanism
            behind a persistent terrain hazard (Rules/Fantasy/reference/pathfinder_mapping.toml's
            "Persistent terrain/obstacle spells" row, ex: statuses.toml's own "flame wall zone",
            matched by a status requirement naming spells.toml's "flame wall" entity): a status
            entry names a real entity (by "name", or any other stable field), and while that
            entity is alive in the scene, whoever shares its band each round gets the status's
            own "apply" condition -- authored with a short duration/length so it naturally lapses
            the moment they leave, rather than lingering once they step out.
        """
        for entity_name in list(self.scenario_entities):
            self._advance_pending_spawn(entity_name)
            if Combat_Resolution.get_current_hp(self.world, entity_name) <= 0:
                continue
            self.apply_round_upkeep(entity_name)
            self._run_round_upkeep_program(entity_name)
            self._expire_summon_if_due(entity_name)
            Combat_Resolution.tick_condition_durations(self.world, entity_name, "rounds")
            Combat_Resolution.tick_periodic_tests(self.world, entity_name, "rounds")
            Combat_Resolution.tick_ability_cooldowns(self.world, entity_name)
            Combat_Actions.evaluate_proximity_statuses(self.world, entity_name, "on_round")

    def _run_round_upkeep_program(self, entity_name):
        """!
        @brief Runs entity_name's own [entity.on_round_upkeep] program, alongside the ordinary
            per-condition upkeep loop above -- no "actor" role for this trigger, same as
            on_enter, since a per-round tick isn't "done by" anyone.
        @param entity_name The entity ticking over this round.
        """
        program = self.entities.get(entity_name, {}).get("on_round_upkeep")
        if program:
            run_program(program, {"actor": None, "target": entity_name}, self.entities, self.rules, self.event_bus)

    def is_locked(self, entity_name):
        """!
        @brief Whether an entity (ex: a chest) currently has the "locked" condition active.
        @param entity_name The name of the entity to check.
        @return True if "locked" is in the entity's active_conditions.
        """
        return Combat_Resolution.has_condition(self.world, entity_name, "locked")

    def is_closed(self, entity_name):
        """!
        @brief Whether a container (ex: a chest) currently has the "closed" condition active.
            Mirrors is_locked exactly. Absent from active_conditions means not closed (open)
            by default, so any container with no [entity.conditions.closed] seeded in TOML
            is unaffected -- only items.toml's chest opts into this today.
        @param entity_name The name of the entity to check.
        @return True if "closed" is in the entity's active_conditions.
        """
        return Combat_Resolution.has_condition(self.world, entity_name, "closed")

    def is_hidden(self, entity_name):
        """!
        @brief Whether an entity (ex: items.toml's dart trap) currently has the "hidden"
            condition active -- seeded by its own [entity.conditions.hidden] and dismissed by
            a passed [entity.notice] auto-roll (see RulesMixin._auto_roll_notice). Mirrors
            is_locked/is_closed/is_identified exactly. _describe_scenario_characters
            (DM_Rules.py) checks this to keep a still-hidden entity out of the roster the LLM
            narrates from, so it isn't spoiled before the player would actually notice it.
        @param entity_name The name of the entity to check.
        @return True if "hidden" is in the entity's active_conditions.
        """
        return Combat_Resolution.has_condition(self.world, entity_name, "hidden")

    def is_test_available(self, entity_name, test, skill_name):
        """!
        @brief Whether an entity's [entity.test] can currently be attempted with the given
            skill. Gates the test on the entity's *current* active_conditions, not just
            whether the skill matches -- without this, ex: an already-picked chest's
            [entity.test] would keep re-triggering on repeat attempts (harmless only by
            accident, since there'd be nothing left to loot), and a "jammed" condition
            applied on a failed attempt would have no actual effect on future ones.
        @param entity_name The name of the entity being tested (ex: a chest).
        @param test The entity's test table ({difficulty, skill, requires_condition,
            blocks_if_condition, requirements, pass, fail}).
        @param skill_name The skill the player is attempting to use.
        @return True if skill_name matches test["skill"], test["requires_condition"] (if set)
                is currently active, test["blocks_if_condition"] (if set) is not, and
                test["requirements"] (if set) is satisfied by entity_matches_requirements --
                the same requirements engine [[status]]/[[entity.behavior]] already use, letting
                a test gate on more than one named condition's presence/absence (ex: an HP tier,
                an attribute, or a boolean combination of several checks).
        """
        if skill_name not in test.get("skill", []):
            return False
        requires = test.get("requires_condition")
        if requires and not Combat_Resolution.has_condition(self.world, entity_name, requires):
            return False
        blocks = test.get("blocks_if_condition")
        if blocks and Combat_Resolution.has_condition(self.world, entity_name, blocks):
            return False
        requirements = test.get("requirements")
        if requirements and not Combat_Resolution.entity_matches_requirements(self.world, entity_name, requirements):
            return False
        return True

    def apply_test_outcome(self, entity_name, outcome):
        """!
        @brief Applies the pass/fail consequence of an entity's [entity.test] (ex: a chest's
            lock check, a trap's disarm/dodge attempt, or an item's own hidden-property
            check), dispatching purely on which keys are present in outcome -- no "action"
            enum needed. A key of "dismiss_condition" removes that condition; a key of
            "condition" applies a new one (the same {condition, duration, length, dismiss}
            shape [[status]]'s own "apply" block already uses); a truthy "reveal" key applies the
            permanent "identified" condition (ex: the cursed dagger's arcane check) -- it
            doesn't say *what* was revealed, that's read back off the entity's own data (ex:
            its "tags" field) by whoever narrates it, once is_identified is true; a truthy
            "loot" key hands everything (currency + inventory) to the player via loot_entity;
            a "damage" key ({dice, pips, bonus}, same shape as any weapon/spell's own
            damage_value) deals real damage to the player via calculate_damage -- ex: a
            trap's failed disarm/dodge attempt -- reusing the exact same immunity/resistance/
            vulnerability and evaluate_statuses("on_damage") path a weapon hit already takes,
            rather than a separate one-off HP subtraction; a truthy "xp" key awards XP via
            _award_xp_for_defeat (Combat_Actions.py) -- the same primitive a combat kill triggers,
            just from this call site instead, so surviving/disarming a trap (ex: items.toml's
            dart trap/scythe trap, both `dismiss_condition = "armed"` + `xp = true` on their
            own [entity.test.pass]) is worth XP the same principled way defeating a hostile
            creature already is, rather than a bespoke "if trap" branch anywhere. Deliberately
            opt-in (unlike a combat kill, which is unconditional the moment a hostile entity's
            HP hits 0) -- most [entity.test]s (ex: a chest's lock) aren't "surviving a threat"
            at all, so this has to be authored, not inferred from subtype == "trap" or any
            other property. Naturally single-fire, no extra bookkeeping needed: once "armed" is
            dismissed, is_test_available's own requires_condition gate makes this same test
            permanently unavailable, so a disarmed trap can never re-fire this a second time.
            Any combination of the above can be present at once, or the whole outcome can be
            empty/omitted for no consequence.
        @param entity_name The name of the entity the test was performed against -- also the
            nominal "attacker" for a "damage" key (ex: the trap itself), purely so
            resolve_damage_value has something to resolve a flat/no bonus against; traps
            aren't expected to carry their own skills the way a creature would.
        @param outcome The test's "pass" or "fail" table (or None/"" for no consequence).
        @return A dict with "loot" (loot_entity's {currency, items} summary) and/or "damage"
                (calculate_damage's own result dict) present only for whichever keys actually
                fired, or None if outcome was empty -- so the caller can narrate exactly what
                happened instead of leaving the LLM to guess.
        """
        if not outcome:
            return None
        dismiss_name = outcome.get("dismiss_condition")
        if dismiss_name:
            Combat_Resolution.dismiss_condition(self.world, entity_name, dismiss_name)
        condition_name = outcome.get("condition")
        if condition_name:
            Combat_Resolution.apply_condition(self.world, 
                entity_name, condition_name,
                duration=outcome.get("duration"), length=outcome.get("length"),
                dismiss=outcome.get("dismiss"),
            )
        if outcome.get("reveal"):
            Combat_Resolution.apply_condition(self.world, entity_name, "identified", duration="permanent", dismiss="")
        if outcome.get("xp"):
            Combat_Actions._award_xp_for_defeat(self.world, entity_name)
        effects = {}
        if outcome.get("loot"):
            effects["loot"] = self.loot_entity(entity_name, self.player_name)
        if outcome.get("damage"):
            ability = {"damage_value": outcome["damage"], "damage_tags": outcome.get("damage_tags", [])}
            effects["damage"] = Combat_Actions.calculate_damage(self.world, entity_name, self.player_name, ability)
        return effects or None

