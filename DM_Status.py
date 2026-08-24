from DM_Types import DMCoreProtocol

COMPARATORS = {
    ">": lambda actual, value: actual > value,
    "<": lambda actual, value: actual < value,
    ">=": lambda actual, value: actual >= value,
    "<=": lambda actual, value: actual <= value,
    "==": lambda actual, value: actual == value,
    "!=": lambda actual, value: actual != value,
    "in": lambda actual, value: actual in value,
    "not_in": lambda actual, value: actual not in value,
}


class StatusMixin(DMCoreProtocol):
    """!
    @brief HP, the status/condition system, and entity tests (DMCore mixin -- only ever
        composed into DMCore, never instantiated on its own; relies on
        self.entities/self.rules/self.event_bus/self.player_name, set up by
        DMCore.__init__). entity_matches_requirements is also relied on by CombatMixin's
        choose_behavior, which reuses this same {field, operator, value} requirement engine
        for [[entity.behavior]] rather than duplicating it. Inherits DMCoreProtocol purely
        so type checkers can resolve these shared attributes/cross-mixin methods -- see
        DM_Types.py.
    """

    def get_current_hp(self, entity_name):
        """!
        @brief Gets an entity's current HP, initializing it from max_hp the first time it's needed.
        @param entity_name The name of the entity.
        @return The entity's current HP.
        """
        entity = self.entities.get(entity_name, {})
        if "hp" not in entity:
            entity["hp"] = entity.get("max_hp", 0)
        return entity["hp"]

    def apply_damage(self, entity_name, amount):
        """!
        @brief Subtracts damage from an entity's current HP, floored at 0, and evaluates on_damage statuses.
        @param entity_name The name of the entity taking damage.
        @param amount The amount of damage to apply.
        @return The entity's remaining HP.
        """
        entity = self.entities.get(entity_name)
        if entity is None:
            return 0
        current_hp = self.get_current_hp(entity_name)
        entity["hp"] = max(0, current_hp - amount)
        self.event_bus.publish("log_info", f"{entity_name} takes {amount} damage ({current_hp} -> {entity['hp']} HP).")
        self.evaluate_statuses(entity_name, "on_damage")
        return entity["hp"]

    def apply_healing(self, entity_name, amount):
        """!
        @brief Adds HP to an entity, clamped at their own max_hp -- the inverse of
            apply_damage, but deliberately its own method rather than apply_damage called
            with a negative amount: apply_damage has no upper clamp at all (only ever needed
            a floor of 0, since incoming damage never had a reason to push past max_hp the
            other way). Still evaluates "on_damage" statuses afterward -- not to apply a new
            injury (healing only ever raises hp_per_remain, so no worse tier can newly match),
            but so a wound tier's condition that no longer holds (ex: "wounded" once healed
            back above 0.59) gets dismissed via evaluate_statuses' own stale-condition sweep.
        @param entity_name The name of the entity being healed.
        @param amount The amount of HP to restore.
        @return The entity's current HP after healing.
        """
        entity = self.entities.get(entity_name)
        if entity is None:
            return 0
        current_hp = self.get_current_hp(entity_name)
        max_hp = entity.get("max_hp", current_hp)
        entity["hp"] = min(max_hp, current_hp + amount)
        self.event_bus.publish("log_info", f"{entity_name} heals {amount} HP ({current_hp} -> {entity['hp']} HP).")
        self.evaluate_statuses(entity_name, "on_damage")
        return entity["hp"]

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
        if field == "hp_per_remain":
            entity = self.entities.get(entity_name, {})
            max_hp = entity.get("max_hp", 0)
            if max_hp <= 0:
                return None
            return self.get_current_hp(entity_name) / max_hp
        if field == "distance_to_target":
            # Not used by any shipped [[entity.behavior]] yet (the implicit "advance when the
            # chosen attack can't reach" fallback in resolve_behavior_action covers the common
            # case without any TOML authorship) -- available for a creature that needs to
            # *choose* between more than one attack option by range instead, ex: a creature
            # with both a melee and a ranged attack picking the ranged one while the gap is
            # still open, falling to melee once it closes. None with no opponent_name at all,
            # same as a malformed/unresolvable requirement -- never accidentally matches.
            if opponent_name is None:
                return None
            return self.get_distance_between(entity_name, opponent_name)
        if field.startswith("has_condition:"):
            # Boolean presence check against entity_name's own active_conditions -- pair with
            # operator = "==", value = true (or false) in a requirement. Ex: a creature that
            # should stand down entirely while paralyzed rather than roll 0 dice and "act"
            # harmlessly (see get_condition_modifier, which only zeroes the roll, not the
            # turn) can gate its attack behavior on { field = "has_condition:paralyzed",
            # operator = "==", value = false }.
            condition_name = field[len("has_condition:"):]
            return condition_name in self.entities.get(entity_name, {}).get("active_conditions", {})
        if field.startswith("opponent_has_condition:"):
            # Same check, against opponent_name's own active_conditions instead -- lets a
            # behavior react to what state its *target* is in (ex: pressing the attack while
            # they're stunned, or favoring a fleeing/frightened target over a healthy one).
            # None with no opponent_name given, same as distance_to_target above -- never
            # accidentally matches a status requirement, which never passes one.
            if opponent_name is None:
                return None
            condition_name = field[len("opponent_has_condition:"):]
            return condition_name in self.entities.get(opponent_name, {}).get("active_conditions", {})
        return self.entities.get(entity_name, {}).get(field)

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
        for comparison in requirements:
            compare = COMPARATORS.get(comparison.get("operator"))
            if compare is None:
                self.event_bus.publish("log_warning", f"Unknown requirement operator: {comparison.get('operator')}")
                return False

            actual_value = self.get_comparable_value(entity_name, comparison.get("field"), opponent_name)
            if actual_value is None or not compare(actual_value, comparison.get("value")):
                return False

        return True

    def get_applicable_statuses(self, entity_name, trigger):
        """!
        @brief Finds every status definition for a given trigger whose requirements the entity currently meets.
        @param entity_name The name of the entity to check.
        @param trigger The trigger name to filter statuses by (ex: "on_damage").
        @return A list of matching status definitions.
        """
        return [
            status for status in self.rules.get("status", [])
            if status.get("trigger") == trigger
            and self.entity_matches_requirements(entity_name, status.get("requirements", []))
        ]

    def apply_condition(self, entity_name, condition_name, duration=None, dismiss=None):
        """!
        @brief Marks a condition as active on an entity.
        @param entity_name The name of the entity gaining the condition.
        @param condition_name The name of the condition, as defined in the [[condition]] table.
        @param duration How long the condition lasts (ex: "fleeting", "scene", "permanent").
        @param dismiss What removes the condition (ex: "healing", "resurrection").
        """
        entity = self.entities.get(entity_name)
        if entity is None:
            return
        active_conditions = entity.setdefault("active_conditions", {})
        active_conditions[condition_name] = {"duration": duration, "dismiss": dismiss}
        self.event_bus.publish("log_info", f"{entity_name} gains condition '{condition_name}'.")

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
        active_conditions = self.entities.get(entity_name, {}).get("active_conditions", {})
        if condition_name not in active_conditions:
            return False
        del active_conditions[condition_name]
        self.event_bus.publish("log_info", f"{entity_name} loses condition '{condition_name}'.")
        return True

    def get_condition_modifier(self, entity_name):
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
        @return A {"dice", "pips", "bonus"} dict, each defaulting to 0 if nothing applies.
        """
        active_conditions = self.entities.get(entity_name, {}).get("active_conditions", {})
        condition_defs = {c.get("name"): c.get("modifier", {}) for c in self.rules.get("condition", [])}
        total = {"dice": 0, "pips": 0, "bonus": 0}
        for condition_name in active_conditions:
            modifier = condition_defs.get(condition_name)
            if not modifier:
                continue
            total["dice"] += modifier.get("dice", 0)
            total["pips"] += modifier.get("pips", 0)
            total["bonus"] += modifier.get("bonus", 0)
        return total

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
        active_conditions = entity.get("active_conditions", {})
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

    def run_round_upkeep(self):
        """!
        @brief Applies one round's worth of upkeep (see apply_round_upkeep) to every living
            entity currently in the scene -- the generic per-round hook
            Rules/Fantasy/reference/pathfinder_conversion.md flagged as the shared
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
        """
        for entity_name in list(self.scenario_entities):
            if self.get_current_hp(entity_name) <= 0:
                continue
            self.apply_round_upkeep(entity_name)
            self._expire_summon_if_due(entity_name)

    def is_locked(self, entity_name):
        """!
        @brief Whether an entity (ex: a chest) currently has the "locked" condition active.
        @param entity_name The name of the entity to check.
        @return True if "locked" is in the entity's active_conditions.
        """
        return "locked" in self.entities.get(entity_name, {}).get("active_conditions", {})

    def is_closed(self, entity_name):
        """!
        @brief Whether a container (ex: a chest) currently has the "closed" condition active.
            Mirrors is_locked exactly. Absent from active_conditions means not closed (open)
            by default, so any container with no [entity.conditions.closed] seeded in TOML
            is unaffected -- only items.toml's chest opts into this today.
        @param entity_name The name of the entity to check.
        @return True if "closed" is in the entity's active_conditions.
        """
        return "closed" in self.entities.get(entity_name, {}).get("active_conditions", {})

    def is_identified(self, entity_name):
        """!
        @brief Whether an entity (ex: the cursed dagger) has had a hidden property revealed by
            a passed [entity.test] whose outcome had a truthy "reveal" key (ex: an arcane
            check). Mirrors is_locked/is_closed exactly.
        @param entity_name The name of the entity to check.
        @return True if "identified" is in the entity's active_conditions.
        """
        return "identified" in self.entities.get(entity_name, {}).get("active_conditions", {})

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
        return "hidden" in self.entities.get(entity_name, {}).get("active_conditions", {})

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
            blocks_if_condition, pass, fail}).
        @param skill_name The skill the player is attempting to use.
        @return True if skill_name matches test["skill"], test["requires_condition"] (if set)
                is currently active, and test["blocks_if_condition"] (if set) is not.
        """
        if skill_name not in test.get("skill", []):
            return False
        active_conditions = self.entities.get(entity_name, {}).get("active_conditions", {})
        requires = test.get("requires_condition")
        if requires and requires not in active_conditions:
            return False
        blocks = test.get("blocks_if_condition")
        if blocks and blocks in active_conditions:
            return False
        return True

    def apply_test_outcome(self, entity_name, outcome):
        """!
        @brief Applies the pass/fail consequence of an entity's [entity.test] (ex: a chest's
            lock check, a trap's disarm/dodge attempt, or an item's own hidden-property
            check), dispatching purely on which keys are present in outcome -- no "action"
            enum needed. A key of "dismiss_condition" removes that condition; a key of
            "condition" applies a new one (the same {condition, duration, dismiss} shape
            [[status]]'s own "apply" block already uses); a truthy "reveal" key applies the
            permanent "identified" condition (ex: the cursed dagger's arcane check) -- it
            doesn't say *what* was revealed, that's read back off the entity's own data (ex:
            its "tags" field) by whoever narrates it, once is_identified is true; a truthy
            "loot" key hands everything (currency + inventory) to the player via loot_entity;
            a "damage" key ({dice, pips, bonus}, same shape as any weapon/spell's own
            damage_value) deals real damage to the player via calculate_damage -- ex: a
            trap's failed disarm/dodge attempt -- reusing the exact same immunity/resistance/
            vulnerability and evaluate_statuses("on_damage") path a weapon hit already takes,
            rather than a separate one-off HP subtraction. Any combination can be present at
            once, or the whole outcome can be empty/omitted for no consequence.
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
                duration=outcome.get("duration"), dismiss=outcome.get("dismiss"),
            )
        if outcome.get("reveal"):
            self.apply_condition(entity_name, "identified", duration="permanent", dismiss="")
        effects = {}
        if outcome.get("loot"):
            effects["loot"] = self.loot_entity(entity_name, self.player_name)
        if outcome.get("damage"):
            ability = {"damage_value": outcome["damage"], "damage_tags": outcome.get("damage_tags", [])}
            effects["damage"] = self.calculate_damage(entity_name, self.player_name, ability)
        return effects or None

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
        matched_statuses = self.get_applicable_statuses(entity_name, trigger)
        matched_conditions = set()
        for status in matched_statuses:
            apply_block = status.get("apply")
            if apply_block and apply_block.get("condition"):
                self.apply_condition(
                    entity_name,
                    apply_block["condition"],
                    duration=apply_block.get("duration"),
                    dismiss=apply_block.get("dismiss"),
                )
                matched_conditions.add(apply_block["condition"])

        active_conditions = self.entities.get(entity_name, {}).get("active_conditions", {})
        for status in self.rules.get("status", []):
            if status.get("trigger") != trigger:
                continue
            apply_block = status.get("apply")
            condition_name = apply_block.get("condition") if apply_block else None
            if not condition_name or condition_name in matched_conditions:
                continue
            active_entry = active_conditions.get(condition_name)
            if active_entry is not None and not active_entry.get("dismiss"):
                self.dismiss_condition(entity_name, condition_name)

        return matched_statuses
