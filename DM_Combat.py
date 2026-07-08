import random


class CombatMixin:
    """!
    @brief Dice rolling, opposed skill checks, damage resolution, and ability/behavior lookup
           (DMCore mixin -- only ever composed into DMCore, never instantiated on its own;
           relies on self.entities/self.rules/self.skills/self.event_bus, set up by
           DMCore.__init__). calculate_damage calls self.apply_damage (StatusMixin) to apply
           the net damage and trigger on_damage statuses; choose_behavior calls
           self.entity_matches_requirements (StatusMixin) to reuse the same
           {field, operator, value} requirement engine [[status]] uses.
    """

    def resolve_bonus(self, attacker_name, bonus):
        """!
        @brief Resolves a damage_value's bonus field, which may be a flat number or a
               "user.<rule>" reference (ex: "user.strength_damage") into a rules.toml formula.
        @param attacker_name The name of the entity dealing damage.
        @param bonus The bonus field from a damage_value table.
        @return The resolved flat bonus amount.
        """
        if isinstance(bonus, (int, float)):
            return bonus
        if not isinstance(bonus, str):
            return 0

        rule_name = bonus.split(".")[-1]
        formula = self.rules.get(rule_name)
        if not formula:
            self.event_bus.publish("log_warning", f"Unknown damage bonus reference: {bonus}")
            return 0

        skill_stats = self.entities.get(attacker_name, {}).get("skills", {}).get(formula.get("skill"), {"dice": 0})
        return skill_stats.get("dice", 0) // formula.get("divisor", 1)

    def resolve_damage_value(self, attacker_name, damage_value):
        """!
        @brief Rolls a damage_value's dice/pips and adds its resolved bonus.
        @param attacker_name The name of the entity dealing damage.
        @param damage_value A {dice, pips, bonus} table from an ability, weapon, or spell.
        @return The total rolled damage before any reduction.
        """
        dice = self.resolve_weapon_reference(attacker_name, damage_value.get("dice", 0), "dice")
        pips = self.resolve_weapon_reference(attacker_name, damage_value.get("pips", 0), "pips")
        if not isinstance(dice, (int, float)) or not isinstance(pips, (int, float)):
            self.event_bus.publish("log_warning", f"Unsupported damage dice/pips reference: {damage_value}")
            dice, pips = 0, 0

        bonus = self.resolve_bonus(attacker_name, damage_value.get("bonus", 0))
        return self.roll_dice(int(dice), int(pips)) + bonus

    def get_equipped_weapon(self, entity_name):
        """!
        @brief Finds the first of an entity's equipped items that deals damage.
        @param entity_name The name of the entity to check.
        @return The equipped weapon's entity table, or None if nothing equipped has a damage_value.
        """
        entity = self.entities.get(entity_name, {})
        for item_name in entity.get("equipped", {}).values():
            item = self.entities.get(item_name)
            if item and "damage_value" in item:
                return item
        return None

    def resolve_weapon_reference(self, attacker_name, value, field):
        """!
        @brief Resolves a damage_value's dice/pips field when it's the "user.weapon.<field>"
               indirection (ex: techniques.toml's cleave, whose damage scales with whatever
               weapon the attacker currently has equipped, rather than a fixed amount).
        @param attacker_name The name of the entity dealing damage.
        @param value The dice or pips field from a damage_value table.
        @param field Which field this is ("dice" or "pips"), matched against "user.weapon.<field>".
        @return value unchanged if it isn't that reference; otherwise the attacker's equipped
                weapon's matching field, or 0 if the attacker has no equipped weapon.
        """
        if value != f"user.weapon.{field}":
            return value
        weapon = self.get_equipped_weapon(attacker_name)
        if weapon is None:
            return 0
        return weapon.get("damage_value", {}).get(field, 0)

    def get_damage_reduction(self, defender_name, damage_tags):
        """!
        @brief Sums the rolled reduction against the given damage tags: the defender's own
               innate resistance_value/resistance_tags (ex: a fire elemental's inherent
               resistance to physical damage) plus the rolled armor value of any equipped
               items that resist the same tags. Both are static, tag-matched traits of the
               entity/item -- distinct from active_conditions, which represent temporary state
               gained/lost during play (see CLAUDE.md's tags-vs-conditions note).
        @param defender_name The name of the entity taking damage.
        @param damage_tags The damage tags of the incoming attack (ex: ["fire"]).
        @return The total damage reduction.
        """
        defender = self.entities.get(defender_name, {})
        reduction = 0

        resistance_value = defender.get("resistance_value")
        resistance_tags = defender.get("resistance_tags", [])
        if resistance_value and any(tag in resistance_tags for tag in damage_tags):
            reduction += self.roll_dice(resistance_value.get("dice", 0), resistance_value.get("pips", 0))

        for item_name in defender.get("equipped", {}).values():
            item = self.entities.get(item_name, {})
            armor_value = item.get("armor_value")
            armor_tags = item.get("armor_tags", [])
            if armor_value and any(tag in armor_tags for tag in damage_tags):
                reduction += self.roll_dice(armor_value.get("dice", 0), armor_value.get("pips", 0))

        return reduction

    def get_vulnerability_bonus(self, defender_name, damage_tags):
        """!
        @brief Rolls the extra damage a defender's own vulnerability_value/vulnerability_tags
               (ex: the fire elemental's vulnerability to "water") adds on a matching hit --
               the mirror image of resistance_value/resistance_tags in get_damage_reduction,
               just added to raw damage instead of subtracted. Innate to the entity only (no
               equipped-item counterpart, unlike armor's resistance side); a static, tag-matched
               trait rather than active_conditions' temporary state (see CLAUDE.md's
               tags-vs-conditions note).
        @param defender_name The name of the entity taking damage.
        @param damage_tags The damage tags of the incoming attack (ex: ["water"]).
        @return The rolled bonus damage, or 0 if no tag matches.
        """
        defender = self.entities.get(defender_name, {})
        vulnerability_value = defender.get("vulnerability_value")
        vulnerability_tags = defender.get("vulnerability_tags", [])
        if vulnerability_value and any(tag in vulnerability_tags for tag in damage_tags):
            return self.roll_dice(vulnerability_value.get("dice", 0), vulnerability_value.get("pips", 0))
        return 0

    def is_immune_to(self, defender_name, damage_tags):
        """!
        @brief Whether an entity's immunity_tags fully negate an incoming attack's damage tags
               (ex: a fire elemental's immunity to "fire"). Distinct from resistance/armor, which
               reduce damage by a rolled amount -- immunity is an absolute, tag-matched block,
               mirroring notes.txt's "poison damage tagged so undead are immune" example.
        @param defender_name The name of the entity taking damage.
        @param damage_tags The damage tags of the incoming attack (ex: ["fire"]).
        @return True if any damage tag matches the defender's immunity_tags.
        """
        immunity_tags = self.entities.get(defender_name, {}).get("immunity_tags", [])
        return any(tag in immunity_tags for tag in damage_tags)

    def calculate_damage(self, attacker_name, defender_name, ability):
        """!
        @brief Calculates and applies damage from an attacker's ability to a defender, including
               immunity, resistance/armor reduction, and vulnerability.
        @param attacker_name The name of the entity dealing damage.
        @param defender_name The name of the entity taking damage.
        @param ability A table with damage_value {dice, pips, bonus} and damage_tags, such as a weapon, spell, or innate ability.
        @return A dict describing the raw damage, reduction, vulnerability bonus, net damage, and the defender's remaining HP.
        """
        damage_value = ability.get("damage_value", {"dice": 0, "pips": 0, "bonus": 0})
        damage_tags = ability.get("damage_tags", [])

        raw_damage = self.resolve_damage_value(attacker_name, damage_value)
        if self.is_immune_to(defender_name, damage_tags):
            # An absolute block -- a matching immunity negates the hit entirely, so
            # vulnerability (which only matters once damage is actually getting through)
            # never applies alongside it.
            reduction = raw_damage
            vulnerability_bonus = 0
        else:
            reduction = self.get_damage_reduction(defender_name, damage_tags)
            vulnerability_bonus = self.get_vulnerability_bonus(defender_name, damage_tags)
        net_damage = max(0, raw_damage + vulnerability_bonus - reduction)
        remaining_hp = self.apply_damage(defender_name, net_damage)

        self.event_bus.publish(
            "log_info",
            f"{attacker_name} deals {raw_damage} raw damage to {defender_name}"
            f"{f' (+{vulnerability_bonus} vulnerability)' if vulnerability_bonus else ''}"
            f", reduced by {reduction} -> {net_damage} net damage."
        )
        return {
            "attacker": attacker_name,
            "defender": defender_name,
            "raw_damage": raw_damage,
            "reduction": reduction,
            "vulnerability_bonus": vulnerability_bonus,
            "net_damage": net_damage,
            "remaining_hp": remaining_hp,
        }

    def roll_dice(self, dice, pips):
        """!
        @brief Rolls the D6 dice pool and adds flat pips, per the D6 system.
        @param dice The number of six-sided dice to roll.
        @param pips The flat bonus added to the dice total.
        @return The total of the roll.
        """
        return sum(random.randint(1, 6) for _ in range(max(dice, 0))) + pips

    def resolve_action(self, entity_name, skill_name, difficulty=0):
        """!
        @brief Resolves the outcome of an entity using a skill against a difficulty.
        @param entity_name The name of the entity performing the action.
        @param skill_name The skill being used.
        @param difficulty The target number the roll must meet or beat. Defaults to 0 (auto-success) when not supplied.
        @return A dict describing the roll and whether it succeeded.
        """
        entity = self.entities.get(entity_name, {})
        skill_stats = entity.get("skills", {}).get(skill_name, {"dice": 1, "pips": 0})
        roll = self.roll_dice(skill_stats.get("dice", 1), skill_stats.get("pips", 0))
        success = roll >= difficulty
        self.event_bus.publish(
            "log_info",
            f"Resolved action: {entity_name} used {skill_name}, rolled {roll} vs difficulty {difficulty} -> {'success' if success else 'failure'}."
        )
        return {
            "entity": entity_name,
            "skill": skill_name,
            "roll": roll,
            "difficulty": difficulty,
            "success": success,
        }

    def get_opposing_skill(self, skill_name, defender_name):
        """!
        @brief Finds the defender's best (highest-rated) skill among a skill's opposing skills.
        @param skill_name The attacker's skill.
        @param defender_name The name of the defending entity.
        @return The defender's highest-rated matching opposing skill name, or None if it has none of them.
        """
        opposes = self.skills.get(skill_name, {}).get("opposes", [])
        defender_skills = self.entities.get(defender_name, {}).get("skills", {})
        best_skill = None
        best_rating = None
        for opposing_skill in opposes:
            stats = defender_skills.get(opposing_skill)
            if stats is None:
                continue
            # Pips convert to a die every 3 (see notes.txt), so rate skills on that common scale.
            rating = stats.get("dice", 0) * 3 + stats.get("pips", 0)
            if best_rating is None or rating > best_rating:
                best_rating = rating
                best_skill = opposing_skill
        return best_skill

    def resolve_opposed_action(self, attacker_name, skill_name, defender_name):
        """!
        @brief Resolves a skill roll opposed by a defending entity's matching skill.
        @param attacker_name The name of the acting entity.
        @param skill_name The skill being used by the attacker.
        @param defender_name The name of the opposing entity.
        @return A dict describing the roll, the opposing skill used (if any), and the outcome.
        """
        opposing_skill = self.get_opposing_skill(skill_name, defender_name)
        if opposing_skill:
            defender_stats = self.entities[defender_name]["skills"][opposing_skill]
            difficulty = self.roll_dice(defender_stats.get("dice", 1), defender_stats.get("pips", 0))
        else:
            difficulty = 0

        result = self.resolve_action(attacker_name, skill_name, difficulty)
        result["defender"] = defender_name
        result["opposing_skill"] = opposing_skill
        return result

    def find_attack_ability(self, entity_name, skill_name):
        """!
        @brief Finds the entity's equipped weapon or innate ability that uses the given skill and deals damage.
               An equipped weapon matching skill_name always wins over an ability/technique that
               also matches it (ex: gladstone's plain longsword swing over "cleave", both usable
               via "blades") -- there's no player-facing way to choose a technique over a basic
               attack on the same skill yet; see CLAUDE.md's cleave note.
        @param entity_name The name of the acting entity.
        @param skill_name The skill being used.
        @return The matching weapon/ability table (with damage_value and damage_tags), or None.
        """
        entity = self.entities.get(entity_name, {})

        for item_name in entity.get("equipped", {}).values():
            item = self.entities.get(item_name)
            if item and self.ability_matches_skill(item, skill_name) and "damage_value" in item:
                return item

        for ability in entity.get("abilities", []):
            ability = self.resolve_ability(ability)
            if ability and self.ability_matches_skill(ability, skill_name) and "damage_value" in ability:
                return ability

        return None

    def ability_matches_skill(self, ability, skill_name):
        """!
        @brief Whether an ability/weapon's "skill" field matches the given skill name -- either
               a single skill (ex: a weapon's own skill) or, for a multi-skill technique (ex:
               techniques.toml's cleave, usable via either "blades" or "axes"), a list any one
               of which counts as a match.
        @param ability The ability/weapon/spell/technique table to check.
        @param skill_name The skill being used.
        @return True if skill_name matches, directly or via list membership.
        """
        ability_skill = ability.get("skill")
        if isinstance(ability_skill, list):
            return skill_name in ability_skill
        return ability_skill == skill_name

    def resolve_ability(self, ability):
        """!
        @brief Resolves one entry from an entity's flat abilities list (mirroring how
               "inventory" is a flat list of item names) to its definition table. An entry
               is either a fully inlined table (ex: gladstone's "punch", wolf's "bite" --
               innate abilities unique to that one entity, not shared anywhere else) or a
               plain string naming a shared catalog entity (ex: gladstone's "fireball",
               which points at the standalone spell defined once in spells.toml and looked
               up here the same way equipped items are looked up by name via
               self.entities). Keeps that shared data in one place instead of requiring
               every caster to carry its own copy that can drift out of sync.
        @param ability Either an ability/spell/technique table, or a string name to look up.
        @return The resolved ability table, or None if a string reference doesn't match
                any loaded entity.
        """
        if isinstance(ability, str):
            return self.entities.get(ability)
        return ability

    def resolve_named_ability(self, entity_name, ability_name):
        """!
        @brief Checks whether ability_name literally names one of entity_name's own abilities
               (ex: NLPCore matched "I cleave through them" directly to the technique "cleave"
               rather than the plain skill "blades" it happens to share with an equipped
               weapon). This is what lets a named technique/spell win over
               find_attack_ability's equipped-weapon-first priority -- the exact ability is
               already known here, rather than inferred from a skill name afterward.
        @param entity_name The name of the acting entity.
        @param ability_name The candidate ability name (ex: action_detected's "skill" field).
        @return The resolved ability table if entity_name actually has it, else None.
        """
        entity = self.entities.get(entity_name, {})
        for ability in entity.get("abilities", []):
            resolved = self.resolve_ability(ability)
            if resolved and resolved.get("name") == ability_name:
                return resolved
        return None

    def select_ability_skill(self, entity_name, ability):
        """!
        @brief Picks which single skill to roll an ability with, when its "skill" field lists
               multiple options (ex: cleave's ["blades", "axes"]) -- the entity's highest-rated
               skill among them, using the same rating convention as get_opposing_skill
               (dice*3 + pips). A single-string "skill" is returned unchanged.
        @param entity_name The name of the entity attempting the ability.
        @param ability The ability table.
        @return The resolved skill name to roll, or None if the ability has no skill at all.
        """
        ability_skill = ability.get("skill")
        if not isinstance(ability_skill, list):
            return ability_skill

        entity_skills = self.entities.get(entity_name, {}).get("skills", {})
        best_skill = None
        best_rating = None
        for candidate in ability_skill:
            stats = entity_skills.get(candidate)
            if stats is None:
                continue
            rating = stats.get("dice", 0) * 3 + stats.get("pips", 0)
            if best_rating is None or rating > best_rating:
                best_rating = rating
                best_skill = candidate
        if best_skill is not None:
            return best_skill
        return ability_skill[0] if ability_skill else None

    def choose_behavior(self, entity_name):
        """!
        @brief Picks the first entry in an entity's [[entity.behavior]] list whose
               requirements are currently met, in declaration order -- the same
               {field, operator, value} requirement engine [[status]] already uses
               (entity_matches_requirements), just read from "behavior" instead of
               "status". Ex: creatures.toml's wolf has one behavior, "always attack
               while hp_per_remain >= 0.01", so it keeps attacking until it's
               effectively dead and then simply stops matching any behavior at all.
        @param entity_name The name of the entity choosing a behavior.
        @return The first matching behavior definition, or None if none match (or
                the entity has no behavior list at all).
        """
        for behavior in self.entities.get(entity_name, {}).get("behavior", []):
            if self.entity_matches_requirements(entity_name, behavior.get("requirements", [])):
                return behavior
        return None

    def resolve_behavior_action(self, entity_name, target_name):
        """!
        @brief Resolves an entity's currently-chosen behavior as an opposed action against a
               target. A behavior names a specific *action* (ex: creatures.toml's wolf names
               "bite", one of its own abilities) rather than a bare skill -- reusing
               resolve_named_ability + select_ability_skill, the exact same lookup the
               player's own named-technique path (ex: "cleave") already uses, rather than
               going through find_attack_ability's equipped-weapon-first priority. That
               priority exists to disambiguate a skill name shared by multiple things; a
               behavior already knows exactly which ability it means, so there's nothing
               to disambiguate.
        @param entity_name The name of the acting entity (ex: a wolf).
        @param target_name The name of the entity being acted against (ex: the player).
        @return The behavior's resolution result dict (same shape as resolve_opposed_action's,
                plus "damage" on a successful hit), or None if no behavior currently matches
                or its named action isn't actually one of the entity's own abilities.
        """
        behavior = self.choose_behavior(entity_name)
        if behavior is None:
            return None

        action_name = behavior.get("action")
        ability = self.resolve_named_ability(entity_name, action_name)
        if ability is None:
            self.event_bus.publish(
                "log_warning", f"{entity_name}'s behavior names unknown action '{action_name}'."
            )
            return None

        skill_name = self.select_ability_skill(entity_name, ability)
        result = self.resolve_opposed_action(entity_name, skill_name, target_name)

        if result["success"]:
            result["damage"] = self.calculate_damage(entity_name, target_name, ability)

        return result
