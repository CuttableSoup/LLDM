import resolution.Combat_Resolution as Combat_Resolution
from dm.DM_Types import DMCoreProtocol
from resolution.Program_Interpreter import run_program


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

    def get_current_hp(self, entity_name):
        """!
        @brief Gets an entity's current HP, initializing it from max_hp the first time it's needed.
        @param entity_name The name of the entity.
        @return The entity's current HP.
        """
        return Combat_Resolution.get_current_hp(self.entities, entity_name)

    def apply_damage(self, entity_name, amount, actor_name=None):
        """!
        @brief Subtracts damage from an entity's current HP, floored at 0, evaluates
            on_damage statuses, and runs the entity's own [entity.on_damage] program.
        @param entity_name The name of the entity taking damage.
        @param amount The amount of damage to apply.
        @param actor_name The entity that dealt the damage, if known -- ctx's own "actor" for
            on_damage; absent for damage with no real attacker.
        @return The entity's remaining HP.
        """
        return Combat_Resolution.apply_damage(self.entities, self.rules, self.event_bus, entity_name, amount, actor_name)

    def apply_healing(self, entity_name, amount, actor_name=None):
        """!
        @brief Adds HP to an entity, clamped at their own max_hp -- the inverse of
            apply_damage, but deliberately its own method rather than apply_damage called
            with a negative amount: apply_damage has no upper clamp at all (only ever needed
            a floor of 0, since incoming damage never had a reason to push past max_hp the
            other way). Still evaluates "on_damage" statuses afterward -- not to apply a new
            injury (healing only ever raises hp_per_remain, so no worse tier can newly match),
            but so a wound tier's condition that no longer holds (ex: "wounded" once healed
            back above 0.59) gets dismissed via evaluate_statuses' own stale-condition sweep.
            Also runs the entity's own [entity.on_heal] program.
        @param entity_name The name of the entity being healed.
        @param amount The amount of HP to restore.
        @param actor_name The entity that healed this one, if known -- ctx's own "actor" for
            on_heal.
        @return The entity's current HP after healing.
        """
        return Combat_Resolution.apply_healing(self.entities, self.rules, self.event_bus, entity_name, amount, actor_name)

    def get_active_conditions(self, entity_name):
        """!
        @brief entity_name's own active_conditions dict.
        @param entity_name The entity to check.
        @return The entity's active_conditions dict ({} if it has none).
        """
        return Combat_Resolution.get_active_conditions(self.entities, entity_name)

    def has_condition(self, entity_name, condition_name):
        """!
        @brief Whether entity_name currently has condition_name active.
        @param entity_name The entity to check.
        @param condition_name The condition name to look for.
        @return True if condition_name is in the entity's active_conditions.
        """
        return Combat_Resolution.has_condition(self.entities, entity_name, condition_name)

    def get_comparable_value(self, entity_name, field, opponent_name=None):
        """!
        @brief Resolves a requirement's field name to a comparable value for an entity.
        @param entity_name The name of the entity to check.
        @param field The field name, either a derived value (ex: "hp_per_remain",
            "distance_to_target", "has_condition:<name>", "opponent_has_condition:<name>") or
            an entity attribute (ex: "supertype").
        @param opponent_name The entity being acted against, if any -- only relevant to a
            derived field defined relative to an opponent (currently "distance_to_target" and
            "opponent_has_condition:<name>"); status requirements never pass one, since a
            status has no notion of an opponent at all.
        @return The resolved value, or None if it can't be determined (ex:
                "distance_to_target"/"opponent_has_condition:<name>" with no opponent_name given).
        """
        return Combat_Resolution.get_comparable_value(self.entities, entity_name, field, opponent_name)

    def entity_matches_requirements(self, entity_name, requirements, opponent_name=None):
        """!
        @brief Checks whether an entity currently satisfies every comparison in a status's
            (or a behavior's) requirements.
        @param entity_name The name of the entity to check.
        @param requirements A list of {field, operator, value} comparisons, all of which must hold.
        @param opponent_name The entity being acted against, if any -- only meaningful to
            choose_behavior's own callers (a status's own requirements have no opponent, so
            this is always None from evaluate_statuses/get_applicable_statuses); forwarded
            unchanged to get_comparable_value for an opponent-relative field like
            "distance_to_target".
        @return True if every comparison is satisfied.
        """
        return Combat_Resolution.entity_matches_requirements(
            self.entities, self.event_bus, entity_name, requirements, opponent_name,
        )

    def get_applicable_statuses(self, entity_name, trigger):
        """!
        @brief Finds every status definition for a given trigger whose requirements the entity currently meets.
        @param entity_name The name of the entity to check.
        @param trigger The trigger name to filter statuses by (ex: "on_damage").
        @return A list of matching status definitions.
        """
        return Combat_Resolution.get_applicable_statuses(self.entities, self.rules, self.event_bus, entity_name, trigger)

    def apply_condition(self, entity_name, condition_name, duration=None, length=None, dismiss=None):
        """!
        @brief Marks a condition as active on an entity.
        @param entity_name The name of the entity gaining the condition.
        @param condition_name The name of the condition, as defined in the [[condition]] table.
        @param duration Which clock the condition counts down against -- one of
            Combat_Resolution.CONDITION_DURATIONS, or the authoring-only "days" (Combat_
            Resolution.apply_condition's own conversion to "blocks", via self.rules below).
        @param length How many of "duration"'s own unit remain (unused/ignored for "permanent").
        @param dismiss What removes the condition (ex: "healing", "resurrection").
        """
        Combat_Resolution.apply_condition(
            self.entities, self.event_bus, entity_name, condition_name, duration, length, dismiss,
            rules=self.rules,
        )

    def dismiss_condition(self, entity_name, condition_name):
        """!
        @brief Removes a condition from an entity, if it's currently active. This is the
            general-purpose counterpart to apply_condition -- ex: a chest's "locked"
            condition, seeded from its template's [entity.conditions], gets dismissed via
            apply_test_outcome's "dismiss_condition" key once its [entity.test] is passed.
        @param entity_name The name of the entity losing the condition.
        @param condition_name The name of the condition to remove.
        @return True if the condition was present and removed, False otherwise.
        """
        return Combat_Resolution.dismiss_condition(self.entities, self.event_bus, entity_name, condition_name)

    def get_skill_group_members(self, name_or_names):
        """!
        @brief Expands a skill/group name (or a list of them) through rules.toml's own
            [[skill_group]] table -- {name, skills} entries standing in for the attribute
            layer this engine deliberately doesn't have (see get_condition_modifier's own
            applies_to / DM_Combat.py's get_equipped_skill_bonus, the two consumers).
        @param name_or_names A single skill/group name, or a list of them.
        @return A flat list of real skill names (groups expanded); a name matching no defined
            group passes through unchanged as a single-element result.
        """
        return Combat_Resolution.get_skill_group_members(self.rules, name_or_names)

    def get_condition_modifier(self, entity_name, skill_name=None):
        """!
        @brief Sums the {dice, pips, bonus} roll modifier of every one of entity_name's own
            active_conditions that has a matching entry in rules.toml's own [[condition]] table
            (ex: "stunned"'s modifier = {dice = -1, pips = 0, bonus = 0}). An active condition
            with no matching [[condition]] entry (ex: "locked"/"closed"/"hidden" -- presence
            flags authored on non-creature entities, never meant to affect a roll) contributes
            nothing. This is what makes the wound track's own conditions (see [[status]]) cost
            real dice instead of just narrating -- resolve_action/resolve_opposed_action
            (DM_Combat.py) fold this into every roll, for whichever entity is doing the rolling.
        @param entity_name The name of the entity to sum modifiers for.
        @param skill_name The skill being rolled, if known -- restricts a condition's own
            optional "applies_to" list to only those skills (see Combat_Resolution.py's own
            docstring); absent/None means only unscoped (no "applies_to") conditions apply.
        @return A {"dice", "pips", "bonus"} dict, each defaulting to 0 if nothing applies.
        """
        return Combat_Resolution.get_condition_modifier(self.entities, self.rules, entity_name, skill_name)

    def resolve_override_target(self, entity_name, candidates):
        """!
        @brief Resolves entity_name's own active override_target -- a [[condition]]'s own
            field hijacking WHO this entity's turn is aimed at (Pathfinder Confused/Dominate)
            -- to a real, currently-living target name. Folded into resolve_behavior_action
            (DM_Combat.py), which swaps its own target_name before choosing/resolving the
            actual attack, so everything downstream is unaffected by which name this returns.
        @param entity_name The name of the entity to check.
        @param candidates A pool of other currently-living scene entities an "override_target
            = \"random\"" condition picks from.
        @return The resolved target name, or None if nothing overrides (or the override can't
            currently resolve to anyone real).
        """
        return Combat_Resolution.resolve_override_target(self.entities, self.rules, entity_name, candidates)

    def get_concealment(self, entity_name):
        """!
        @brief The highest "miss_chance" of any of entity_name's own active_conditions with a
            matching [[condition]] entry authoring one -- the Pathfinder concealment/Invisible
            shape (a successful attack roll can still just miss outright). Folded into
            resolve_opposed_action (DM_Combat.py) unless the attacker's own ability authors
            "ignores_concealment".
        @param entity_name The name of the entity to check.
        @return The effective miss_chance (0-95), 0 if nothing applies.
        """
        return Combat_Resolution.get_concealment(self.entities, self.rules, entity_name)

    def is_action_prevented(self, entity_name):
        """!
        @brief Whether entity_name is currently unable to act on its own turn at all -- true
            if any of its own active_conditions has a matching rules.toml [[condition]] entry
            authoring prevents_action = true. Distinct from get_condition_modifier's own flat
            dice penalty: rules.toml's own "pinned" (maneuvers.toml's "pin", only ever applied
            to an already-grappled target) carries both a modifier -4 *and*
            prevents_action = true, matching Pathfinder's real "pinned" condition -- a pinned
            character can take essentially no physical action beyond trying to escape, not
            just a penalized one. Checked by DM_Core.py's _resolve_roll (the player's own
            turn -- ActionPreventedOutcome, no roll attempted) and DM_Combat.py's
            resolve_behavior_action (a creature/ally's own turn -- treated exactly like "no
            behavior currently matches", the same "doesn't act" outcome an entity with no
            matching [[entity.behavior]] entry already gets).
        @param entity_name The entity to check.
        @return True if entity_name cannot act this turn.
        """
        condition_defs = {c.get("name"): c for c in self.rules.get("condition", [])}
        return any(
            condition_defs.get(name, {}).get("prevents_action")
            for name in self.get_active_conditions(entity_name)
        )

    def get_condition_upkeep(self, entity_name):
        """!
        @brief Sums the per-round upkeep effect of every one of entity_name's own
            active_conditions that has a matching [[condition]] entry with an
            "upkeep_heal"/"upkeep_damage" field -- ex: "regenerating"'s
            upkeep_heal = {dice = 2, pips = 0, bonus = 0}. A condition whose own
            upkeep_blocked_by_tags overlaps entity_name's own "recent_damage_tags" (damage
            tags it was hit with since the last time this ran -- see calculate_damage,
            DM_Combat.py) is skipped entirely for this round -- ex: a troll's regeneration
            not firing the round it took fire damage.
        @param entity_name The name of the entity to sum upkeep for.
        @return A {"heal": {"dice", "pips", "bonus"}, "damage": {"dice", "pips", "bonus"}}
                dict, each defaulting to all-0 if nothing applies.
        """
        entity = self.entities.get(entity_name, {})
        active_conditions = self.get_active_conditions(entity_name)
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

        heal_total = self.roll_dice(upkeep["heal"]["dice"], upkeep["heal"]["pips"]) + upkeep["heal"]["bonus"]
        if heal_total > 0:
            self.apply_healing(entity_name, heal_total)

        damage_total = self.roll_dice(upkeep["damage"]["dice"], upkeep["damage"]["pips"]) + upkeep["damage"]["bonus"]
        if damage_total > 0:
            self.apply_damage(entity_name, damage_total)

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
            if self.get_current_hp(entity_name) <= 0:
                continue
            upkeep = self.get_condition_upkeep(entity_name)
            heal_total = self.roll_dice(
                upkeep["heal"]["dice"] * blocks, upkeep["heal"]["pips"] * blocks,
            ) + upkeep["heal"]["bonus"] * blocks
            if heal_total > 0:
                self.apply_healing(entity_name, heal_total)
            damage_total = self.roll_dice(
                upkeep["damage"]["dice"] * blocks, upkeep["damage"]["pips"] * blocks,
            ) + upkeep["damage"]["bonus"] * blocks
            if damage_total > 0:
                self.apply_damage(entity_name, damage_total)

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

            Also ticks every active_conditions entry whose own "duration" is "rounds" by one
            (Combat_Resolution.tick_condition_durations) -- ex: "surprised", applied by night
            watch (DM_Travel.py's _roll_night_watch) with length=1, expiring the first time this
            same entity's upkeep runs after gaining it, docs/downtime.md's "Night watch and
            surprise".

            Also ticks down every entry in the entity's own "ability_cooldowns"
            (Combat_Resolution.tick_ability_cooldowns) -- set whenever a behavior fires an
            ability authoring "cooldown_rounds" (DM_Combat.py's resolve_behavior_action),
            counted back down to 0 (removed entirely once it gets there) the same once-per-
            round cadence as the condition-duration tick just above.

            Also evaluates every [[status]] authoring trigger = "on_round" against entity_name
            (evaluate_proximity_statuses) -- the same proximity-apply shape "on_action" already
            uses for a Frightful-Presence-style aura, just checked once a round for every living
            entity instead of only the one that just landed a hit. This is the whole mechanism
            behind a persistent terrain hazard (Rules/Fantasy/reference/pathfinder_mapping.toml's
            "Persistent terrain/obstacle spells" row, ex: rules.toml's own "flame wall zone",
            matched by a status requirement naming spells.toml's "flame wall" entity): a status
            entry names a real entity (by "name", or any other stable field), and while that
            entity is alive in the scene, whoever shares its band each round gets the status's
            own "apply" condition -- authored with a short duration/length so it naturally lapses
            the moment they leave, rather than lingering once they step out.
        """
        for entity_name in list(self.scenario_entities):
            if self.get_current_hp(entity_name) <= 0:
                continue
            self.apply_round_upkeep(entity_name)
            self._run_round_upkeep_program(entity_name)
            self._expire_summon_if_due(entity_name)
            Combat_Resolution.tick_condition_durations(self.entities, self.event_bus, entity_name, "rounds")
            Combat_Resolution.tick_ability_cooldowns(self.entities, entity_name)
            self.evaluate_proximity_statuses(entity_name, "on_round")

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
        return self.has_condition(entity_name, "locked")

    def is_closed(self, entity_name):
        """!
        @brief Whether a container (ex: a chest) currently has the "closed" condition active.
            Mirrors is_locked exactly. Absent from active_conditions means not closed (open)
            by default, so any container with no [entity.conditions.closed] seeded in TOML
            is unaffected -- only items.toml's chest opts into this today.
        @param entity_name The name of the entity to check.
        @return True if "closed" is in the entity's active_conditions.
        """
        return self.has_condition(entity_name, "closed")

    def is_identified(self, entity_name):
        """!
        @brief Whether an entity (ex: the cursed dagger) has had a hidden property revealed by
            a passed [entity.test] whose outcome had a truthy "reveal" key (ex: an arcane
            check). Mirrors is_locked/is_closed exactly.
        @param entity_name The name of the entity to check.
        @return True if "identified" is in the entity's active_conditions.
        """
        return self.has_condition(entity_name, "identified")

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
        return self.has_condition(entity_name, "hidden")

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
        if requires and not self.has_condition(entity_name, requires):
            return False
        blocks = test.get("blocks_if_condition")
        if blocks and self.has_condition(entity_name, blocks):
            return False
        requirements = test.get("requirements")
        if requirements and not self.entity_matches_requirements(entity_name, requirements):
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
            _award_xp_for_defeat (DM_Combat.py) -- the same primitive a combat kill triggers,
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
            self.dismiss_condition(entity_name, dismiss_name)
        condition_name = outcome.get("condition")
        if condition_name:
            self.apply_condition(
                entity_name, condition_name,
                duration=outcome.get("duration"), length=outcome.get("length"),
                dismiss=outcome.get("dismiss"),
            )
        if outcome.get("reveal"):
            self.apply_condition(entity_name, "identified", duration="permanent", dismiss="")
        if outcome.get("xp"):
            self._award_xp_for_defeat(entity_name)
        effects = {}
        if outcome.get("loot"):
            effects["loot"] = self.loot_entity(entity_name, self.player_name)
        if outcome.get("damage"):
            ability = {"damage_value": outcome["damage"], "damage_tags": outcome.get("damage_tags", [])}
            effects["damage"] = self.calculate_damage(entity_name, self.player_name, ability)
        return effects or None

    def evaluate_proximity_statuses(self, actor_name, trigger):
        """!
        @brief Applies a status trigger that fires off the ACTING entity's own qualifying
            requirements but lands the resulting condition on every OTHER nearby living
            entity, rather than on the actor itself -- the Pathfinder "Fear aura/Frightful
            Presence" shape (Rules/Fantasy/reference/pathfinder_mapping.toml's
            creature_ability row), distinct from evaluate_statuses' own on_damage statuses,
            which always self-apply to whoever's HP just changed. A [[status]] entry opts into
            this shape by authoring trigger = "on_action" (the only trigger this checks;
            "on_damage" statuses are untouched and still only ever self-apply) with an "apply"
            block carrying two extra optional keys beyond the ones evaluate_statuses already
            reads: "radius" (bands, default 0 -- only entities sharing actor_name's own band,
            which already covers a melee-adjacent aura; a wider aura authors a larger number)
            and "side" (default "enemies", relative to actor_name, same vocabulary
            resolve_targets' own "targets" table uses -- "allies" or "all" also valid).
            requirements are checked against actor_name only, never the entities that end up
            gaining the condition. Deliberately doesn't run evaluate_statuses' own stale-
            condition dismissal sweep -- an "on_action" condition's own duration/length governs
            its expiry the ordinary way, and re-sweeping every nearby entity each time the
            actor acts again would dismiss a still-fresh application from a different actor's
            own aura sharing the same condition name.

            run_round_upkeep (DM_Status.py) also calls this once a round for every living scene
            entity with trigger = "on_round" -- the same requirements/apply/radius/side shape,
            just fired on a per-round cadence rather than off a landed hit, which is what makes
            a stationary object (ex: spells.toml's "flame wall") into a persistent terrain
            hazard: its own [[status]] entry's requirements match the hazard entity itself (by
            "name", same as any other field), and "apply" lands on whoever currently shares its
            band. Authoring the applied condition with a short duration/length (ex:
            rules.toml's own "flame wall zone", duration = "rounds"/length = 1) is what makes it
            a *zone* rather than a one-time blast -- it lapses on its own the moment an entity is
            no longer co-band, and is simply reapplied fresh each round for as long as they stay.
        @param actor_name The entity whose own action just happened (ex: a dragon that just
            landed a bite), or -- for "on_round" -- whichever living scene entity is currently
            being checked.
        @param trigger The trigger name to evaluate -- "on_action" (a landed hit) or "on_round"
            (once per combat round, per living scene entity) are the two shipped uses today.
        """
        for status in self.get_applicable_statuses(actor_name, trigger):
            apply_block = status.get("apply")
            if not apply_block or not apply_block.get("condition"):
                continue
            radius = apply_block.get("radius", 0)
            side = apply_block.get("side", "enemies")
            for target_name in self.scenario_entities:
                if target_name == actor_name or self.get_current_hp(target_name) <= 0:
                    continue
                if self.get_distance_between(actor_name, target_name) > radius:
                    continue
                if side == "enemies" and not self.is_hostile(target_name, actor_name):
                    continue
                if side == "allies" and self.is_hostile(target_name, actor_name):
                    continue
                self.apply_condition(
                    target_name, apply_block["condition"],
                    duration=apply_block.get("duration"), length=apply_block.get("length"),
                    dismiss=apply_block.get("dismiss"),
                )

    def evaluate_statuses(self, entity_name, trigger):
        """!
        @brief Applies every status matching the given trigger that the entity currently
            qualifies for, then dismisses any condition this same trigger's statuses previously
            applied whose requirements no longer hold (ex: a "wounded" gladstone healed back
            above 0.59 hp_per_remain, or one further hurt into "incapacitated" whose hp_per_remain
            no longer falls in "wounded"'s own 0.40-0.59 range). A condition is only eligible for
            this automatic sweep if it was stored with a falsy "dismiss" -- one stored with a
            named mechanism (ex: "dead"'s dismiss = "resurrection") is left alone; simple hp
            recovery shouldn't be able to undo it.
        @param entity_name The name of the entity to evaluate.
        @param trigger The trigger name to evaluate (ex: "on_damage").
        @return The list of status definitions that were applied.
        """
        return Combat_Resolution.evaluate_statuses(self.entities, self.rules, self.event_bus, entity_name, trigger)
