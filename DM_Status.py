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

    def get_comparable_value(self, entity_name, field):
        """!
        @brief Resolves a requirement's field name to a comparable value for an entity.
        @param entity_name The name of the entity to check.
        @param field The field name, either a derived value (ex: "hp_per_remain") or an entity attribute (ex: "supertype").
        @return The resolved value, or None if it can't be determined.
        """
        if field == "hp_per_remain":
            entity = self.entities.get(entity_name, {})
            max_hp = entity.get("max_hp", 0)
            if max_hp <= 0:
                return None
            return self.get_current_hp(entity_name) / max_hp
        return self.entities.get(entity_name, {}).get(field)

    def entity_matches_requirements(self, entity_name, requirements):
        """!
        @brief Checks whether an entity currently satisfies every comparison in a status's requirements.
        @param entity_name The name of the entity to check.
        @param requirements A list of {field, operator, value} comparisons, all of which must hold.
        @return True if every comparison is satisfied.
        """
        for comparison in requirements:
            compare = COMPARATORS.get(comparison.get("operator"))
            if compare is None:
                self.event_bus.publish("log_warning", f"Unknown requirement operator: {comparison.get('operator')}")
                return False

            actual_value = self.get_comparable_value(entity_name, comparison.get("field"))
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
            lock check, or an item's own hidden-property check), dispatching purely on which
            keys are present in outcome -- no "action" enum needed. A key of
            "dismiss_condition" removes that condition; a key of "condition" applies a new one
            (the same {condition, duration, dismiss} shape [[status]]'s own apply/test.fail
            blocks already use); a truthy "reveal" key applies the permanent "identified"
            condition (ex: the cursed dagger's arcane check) -- it doesn't say *what* was
            revealed, that's read back off the entity's own data (ex: its "tags" field) by
            whoever narrates it, once is_identified is true; a truthy "loot" key hands
            everything (currency + inventory) to the player via loot_entity. Any combination
            can be present at once, or the whole outcome can be empty/omitted for no consequence.
        @param entity_name The name of the entity the test was performed against.
        @param outcome The test's "pass" or "fail" table (or None/"" for no consequence).
        @return loot_entity's {currency, items} summary if "loot" applied, else None -- so the
                caller can narrate what was actually gained instead of leaving the LLM to guess.
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
        if outcome.get("loot"):
            return self.loot_entity(entity_name, self.player_name)
        return None

    def evaluate_statuses(self, entity_name, trigger):
        """!
        @brief Applies every status matching the given trigger that the entity currently qualifies for.
        @param entity_name The name of the entity to evaluate.
        @param trigger The trigger name to evaluate (ex: "on_damage").
        @return The list of status definitions that were applied.
        """
        matched_statuses = self.get_applicable_statuses(entity_name, trigger)
        for status in matched_statuses:
            apply_block = status.get("apply")
            if apply_block and apply_block.get("condition"):
                self.apply_condition(
                    entity_name,
                    apply_block["condition"],
                    duration=apply_block.get("duration"),
                    dismiss=apply_block.get("dismiss"),
                )
        return matched_statuses
