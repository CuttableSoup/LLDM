import resolution.Combat_Resolution as Combat_Resolution
from resolution.Challenge_Rating import calculate_challenge_rating, calculate_party_challenge_rating, skill_rating
from dm.DM_ActionOutcome import DamageEffect, MovementOutcome, TransferOutcome, rolled_outcome_from_roll
from dm.DM_Inventory import SIGNIFICANT_VALUE
from dm.DM_Types import DMCoreProtocol

# Reserved [[entity.behavior]] action names -- resolve_behavior_action routes these straight to
# move_toward_or_away (DM_Movement.py) instead of resolve_named_ability, so no real ability may
# ever be named "advance"/"retreat".
MOVEMENT_ACTIONS = {"advance", "retreat"}

# Reserved [[entity.behavior]] action names for an autonomous item transfer -- routed straight
# to _resolve_transfer_behavior instead of resolve_named_ability, so no real ability may ever be
# named "steal"/"gift" either. Mirrors DM_Inventory.py's own player-driven "take"/"give", just
# entity-initiated: "steal" moves the behavior entry's own "item" from target_name's inventory
# to entity_name's; "gift" moves it the other way.
TRANSFER_ACTIONS = {"steal", "gift"}


class CombatMixin(DMCoreProtocol):
    """!
    @brief Dice rolling, opposed skill checks, damage resolution, and ability/behavior lookup
        (DMCore mixin -- only ever composed into DMCore, never instantiated on its own;
        relies on self.entities/self.rules/self.skills/self.event_bus, set up by
        DMCore.__init__). The actual roll/damage computation lives in Combat_Resolution.py,
        a pure module taking entities/rules/skills/event_bus explicitly (see its own module
        docstring) -- every method below that used to hold that logic is now a thin wrapper
        forwarding self.entities/self.rules/self.skills/self.event_bus, so no caller anywhere
        else in the codebase needed to change. choose_behavior calls
        self.entity_matches_requirements (StatusMixin, itself now a thin Combat_Resolution.py
        wrapper too) to reuse the same {field, operator, value} requirement engine [[status]]
        uses; resolve_behavior_action calls self.move_toward_or_away (MovementMixin) for a
        deliberate `action = "advance"`/`"retreat"` behavior entry, or as its own fallback
        when a chosen attack can't currently reach its target; self.is_action_prevented
        (StatusMixin) to skip its turn entirely when it can't act at all; and, for a
        deliberate `action = "steal"`/`"gift"` behavior entry, self.transfer_item
        (InventoryMixin) plus self.nudge_attitude_from_event (SocialMixin) to fire the same
        "theft"/"favor" nudge the player's own "take"/"give" already fires. Inherits
        DMCoreProtocol purely so type checkers can resolve these shared attributes/cross-mixin
        methods -- see DM_Types.py.
    """

    def resolve_bonus(self, attacker_name, bonus):
        """!
        @brief Resolves a damage_value's bonus field, which may be a flat number or a
            "user.<rule>" reference (ex: "user.strength_damage") into a rules.toml formula.
        @param attacker_name The name of the entity dealing damage.
        @param bonus The bonus field from a damage_value table.
        @return The resolved flat bonus amount.
        """
        return Combat_Resolution.resolve_bonus(self.entities, self.rules, self.event_bus, attacker_name, bonus)

    def resolve_damage_value(self, attacker_name, damage_value):
        """!
        @brief Rolls a damage_value's dice/pips and adds its resolved bonus.
        @param attacker_name The name of the entity dealing damage.
        @param damage_value A {dice, pips, bonus} table from an ability, weapon, or spell.
        @return The total rolled damage before any reduction.
        """
        return Combat_Resolution.resolve_damage_value(self.entities, self.rules, self.event_bus, attacker_name, damage_value)

    def get_equipped_weapon(self, entity_name):
        """!
        @brief Finds the first of an entity's equipped items that deals damage.
        @param entity_name The name of the entity to check.
        @return The equipped weapon's entity table, or None if nothing equipped has a damage_value.
        """
        return Combat_Resolution.get_equipped_weapon(self.entities, entity_name)

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
        return Combat_Resolution.resolve_weapon_reference(self.entities, attacker_name, value, field)

    def get_damage_reduction(self, defender_name, damage_tags):
        """!
        @brief Sums the rolled reduction against the given damage tags: the defender's own
            innate resistance_value/resistance_tags (ex: a fire elemental's inherent
            resistance to physical damage) plus the rolled armor value of any equipped
            items that resist the same tags. Both are static, tag-matched traits of the
            entity/item -- distinct from active_conditions, which represent temporary state
            gained/lost during play (see CLAUDE.md's tags-vs-conditions note).

            Each side also has its own optional bypass list (resistance_bypass_tags on the
            defender, armor_bypass_tags on the equipped item) -- if any of the incoming
            damage_tags matches one of *those*, that side's reduction is skipped entirely for
            this hit, regardless of whether resistance_tags/armor_tags would otherwise match
            (ex: a creature with resistance_tags = ["physical"] but
            resistance_bypass_tags = ["magic"] still resists an ordinary sword, but a
            damage_tags = ["slashing", "magic"] hit bypasses that resistance outright -- the
            Pathfinder "DR 10/magic" shape). Absent/empty on every entity that doesn't author
            it, so this is purely additive over existing data.
        @param defender_name The name of the entity taking damage.
        @param damage_tags The damage tags of the incoming attack (ex: ["fire"]).
        @return The total damage reduction.
        """
        return Combat_Resolution.get_damage_reduction(self.entities, defender_name, damage_tags)

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
        return Combat_Resolution.get_vulnerability_bonus(self.entities, defender_name, damage_tags)

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
        return Combat_Resolution.is_immune_to(self.entities, defender_name, damage_tags)

    def calculate_damage(self, attacker_name, defender_name, ability):
        """!
        @brief Calculates and applies damage from an attacker's ability to a defender, including
            immunity, resistance/armor reduction, and vulnerability. Also records
            ability's own damage_tags onto defender_name's own "recent_damage_tags" (a plain
            set, never persisted -- see DM_Persistence.py's own whitelisted save fields) --
            consumed and cleared once per round by run_round_upkeep (DM_Status.py), so a
            condition's own upkeep_blocked_by_tags can tell whether this entity was hit with a
            matching damage type this round (ex: a troll's regeneration not firing the round
            it took fire damage). Recorded whenever this function runs at all (any landed
            hit), regardless of net_damage -- a fully-resisted-to-zero fire hit still counts
            as "touched by fire" for this purpose, the same simplification real Pathfinder
            regeneration makes (fire/acid suppress it outright, not just when damage gets
            through).
            Also the sole trigger point for XP: if this hit is what brings defender_name from
            positive HP down to 0 (a real kill, not a second hit against an already-dead
            corpse) and defender_name is_hostile toward the player, _award_xp_for_defeat runs
            before returning -- see that method and rules.toml's own [xp] table.
        @param attacker_name The name of the entity dealing damage.
        @param defender_name The name of the entity taking damage.
        @param ability A table with damage_value {dice, pips, bonus} and damage_tags, such as a weapon, spell, or innate ability.
        @return A dict describing the raw damage, reduction, vulnerability bonus, net damage, and the defender's remaining HP.
        """
        previous_hp = self.get_current_hp(defender_name)
        result = Combat_Resolution.calculate_damage(
            self.entities, self.rules, self.event_bus, attacker_name, defender_name, ability,
        )
        if previous_hp > 0 and result["remaining_hp"] == 0 and self.is_hostile(defender_name, self.player_name):
            self._award_xp_for_defeat(defender_name)
        return result

    def resolve_targets(self, attacker_name, target_name, ability):
        """!
        @brief Expands a single rolled-against target_name into the full set of entities an
            ability's hit/on_pass/on_fail actually lands on -- just [target_name] for the vast
            majority of abilities (no authored "targets" table at all, entity_schema.toml's
            {number, aoe, side}), which is exactly today's unchanged single-target behavior.
            target_name itself is always the first entry (it's who the roll was actually
            resolved against -- range/opposed-skill/language checks all already ran against
            it specifically), then "aoe" (int, bands; 0 = target_name's own band) widens the
            search to every other living scene entity within that many bands of it
            (get_distance_between, nearest-first), "side" filters that widened pool
            ("enemies", the default, matching every existing weapon/technique's implicit
            behavior; "allies"; or "all" for an indiscriminate blast that doesn't check
            hostility at all, ex: fire not caring who it burns), and "number" (0 = unlimited)
            caps the combined, target_name-inclusive list. "enemies"/"allies" are resolved via
            is_hostile(candidate, attacker_name) -- relative to whoever is actually casting/
            swinging, not hardcoded to the player, so an NPC's own area attack/aura discriminates
            correctly too. Covers three distinct authored shapes with one field, not three:
            techniques.toml's cleave ({number = 3, aoe = 0}, side defaulting to "enemies") hits
            up to 3 other enemies sharing target_name's own band; an indiscriminate blast (ex:
            fireball) authors {aoe = 5, side = "all", number = 0}; a discriminating area effect
            (ex: a Pathfinder-style channeling that only touches allies) authors
            {aoe = <radius>, side = "allies"}.

            "side" = "self" is a fourth, short-circuiting case: always exactly
            [attacker_name], ignoring target_name/aoe/number entirely -- a personal ward or
            self-buff shouldn't require the player to name themselves (so it works even with
            no current_target at all, ex: cast outside combat), and mustn't spill onto an
            adjacent ally the way an ordinary {aoe = 0, side = "allies"} still could if one
            happens to share target_name's own band.
        @param attacker_name The name of the acting entity (whose own hostility/allegiance
            "enemies"/"allies" is resolved relative to, and who "self" always resolves to).
        @param target_name self.current_target, or None.
        @param ability The resolved weapon/spell/technique table, or None.
        @return [None] if target_name is falsy and the ability isn't "self"-sided (an
                untargeted ability still runs its own on_pass/on_fail program exactly once,
                against no one); [attacker_name] if the ability's "targets" authors
                side = "self"; otherwise a list of at least [target_name], widened/filtered/
                capped per the ability's own "targets" table if it authors one.
        """
        targets_spec = ability.get("targets") if ability else None
        if targets_spec and targets_spec.get("side") == "self":
            return [attacker_name]
        if not target_name:
            return [None]
        if not targets_spec:
            return [target_name]

        aoe = targets_spec.get("aoe", 0)
        number = targets_spec.get("number", 0)
        side = targets_spec.get("side", "enemies")

        others = []
        for entity_name in self.scenario_entities:
            if entity_name == target_name or self.get_current_hp(entity_name) <= 0:
                continue
            distance = self.get_distance_between(entity_name, target_name)
            if distance > aoe:
                continue
            if side == "enemies" and not self.is_hostile(entity_name, attacker_name):
                continue
            if side == "allies" and self.is_hostile(entity_name, attacker_name):
                continue
            others.append((distance, entity_name))
        others.sort(key=lambda pair: pair[0])

        names = [target_name] + [name for _, name in others]
        if number and len(names) > number:
            names = names[:number]
        return names

    def roll_dice(self, dice, pips):
        """!
        @brief Rolls the D6 dice pool and adds flat pips, per the D6 system.
        @param dice The number of six-sided dice to roll.
        @param pips The flat bonus added to the dice total.
        @return The total of the roll.
        """
        return Combat_Resolution.roll_dice(dice, pips)

    def roll_initiative(self, entity_name):
        """!
        @brief Rolls an entity's initiative for turn ordering: every skill named in rules.toml's
            [[initiative]] list (today, dodge + observation) has its dice/pips pooled together
            and rolled once -- not compared statically via the dice*3+pips rating convention
            get_opposing_skill/select_ability_skill use, since initiative is meant to vary
            round to round rather than just rank a fixed pair of skills. A skill the entity
            lacks defaults to the same untrained 0D/0 pips resolve_action already defaults to
            -- an entity with none of the pooled skills simply rolls 0 (still resolvable, just
            never wins a tie against anyone with even one die in one of them).
        @param entity_name The name of the entity rolling initiative.
        @return The rolled initiative total.
        """
        entity_skills = self.entities.get(entity_name, {}).get("skills", {})
        dice = 0
        pips = 0
        for term in self.rules.get("initiative", []):
            stats = entity_skills.get(term.get("skill"), {"dice": 0, "pips": 0})
            dice += stats.get("dice", 0)
            pips += stats.get("pips", 0)
        return self.roll_dice(dice, pips)

    def resolve_action(self, entity_name, skill_name, difficulty=0, dice_penalty=0):
        """!
        @brief Resolves the outcome of an entity using a skill against a difficulty. A skill
            entity_name's own [entity.skills] doesn't list at all is untrained -- 0D/0 pips,
            not a token 1D -- so an unlisted skill can only ever succeed against difficulty 0
            (an auto-success check, ex: a non-opposed "examine"-style test with no real gate).
        @param entity_name The name of the entity performing the action.
        @param skill_name The skill being used.
        @param difficulty The target number the roll must meet or beat. Defaults to 0 (auto-success) when not supplied.
        @param dice_penalty Whole dice subtracted from entity_name's own pool before rolling,
            floored at 0 dice (pips are never touched) -- the West End Games D6 multiple-
            actions rule: attempting more than one action in a turn costs every one of those
            actions -1D per action beyond the first (see DM_Core.py's own multi-action
            docstring for where this is computed). 0 (the default) is every existing call
            site's behavior, unchanged -- movement/speech/item-interactions never pass this at
            all, and a lone action always resolves at full dice. Also folds in
            get_condition_modifier(entity_name) (StatusMixin) -- ex: "stunned"'s -1D -- into
            the same dice/pips pool, floored at 0 dice the same way dice_penalty is; its own
            "bonus" is added straight to the final roll, after dice are rolled.
        @return A dict describing the roll and whether it succeeded.
        """
        return Combat_Resolution.resolve_action(
            self.entities, self.rules, self.event_bus, entity_name, skill_name, difficulty, dice_penalty,
        )

    def get_opposing_skill(self, skill_name, defender_name):
        """!
        @brief Finds the defender's best (highest-rated) skill among a skill's opposing skills.
        @param skill_name The attacker's skill.
        @param defender_name The name of the defending entity.
        @return The defender's highest-rated matching opposing skill name, or None if it has none of them.
        """
        return Combat_Resolution.get_opposing_skill(self.entities, self.skills, skill_name, defender_name)

    def resolve_opposed_action(self, attacker_name, skill_name, defender_name, dice_penalty=0):
        """!
        @brief Resolves a skill roll opposed by a defending entity's matching skill. Range
            (see DM_Movement.py's is_in_range) is checked by the caller *before* this is
            reached at all -- it's a pure reachability gate now, not a difficulty modifier,
            so nothing here needs to know about distance.
        @param attacker_name The name of the acting entity.
        @param skill_name The skill being used by the attacker.
        @param defender_name The name of the opposing entity.
        @param dice_penalty Forwarded to resolve_action for attacker_name's own roll only --
            the defender isn't the one splitting their attention across multiple actions this
            turn, so defender_stats' own roll below is never penalized by dice_penalty
            regardless of this value (see resolve_action's own docstring for the West End
            Games rule this implements). The defender's own active_conditions still apply to
            their roll, via get_condition_modifier (StatusMixin) -- ex: a stunned defender
            still rolls their opposing skill at -1D, same as if they'd been the one acting.
        @return A dict describing the roll, the opposing skill used (if any), and the outcome.
        """
        return Combat_Resolution.resolve_opposed_action(
            self.entities, self.rules, self.skills, self.event_bus,
            attacker_name, skill_name, defender_name, dice_penalty,
        )

    def find_attack_ability(self, entity_name, skill_name):
        """!
        @brief Finds the entity's equipped weapon or owned ability matching the given skill --
            the shared "which specific thing is entity_name using" lookup for both an attack's
            own damage roll (DM_Core.py's _apply_damage_if_hit, which separately re-checks
            "damage_value" in ability before dealing damage) and its post-roll on_pass/on_fail
            program lookup -- one method covering both the equipped-weapon/owned-ability
            lookup and the post-roll on_pass/on_fail lookup. Deliberately no "damage_value" gate
            here anymore -- a purely non-damaging owned ability (ex: a trained, non-universal
            maneuver with only an on_pass condition) has to be findable here too, not just a
            weapon. An equipped weapon matching skill_name always wins over an ability/technique
            that also matches it (ex: gladstone's plain longsword swing over "cleave", both
            usable via "blades") -- there's no player-facing way to choose a technique over a
            basic attack on the same skill yet; see CLAUDE.md's cleave note. Never scans
            skill-listed *universal* abilities (ex: "trip") -- that ambiguity (several maneuvers,
            one skill) is exactly what resolve_named_ability's own exact-name-match fallback
            exists to avoid guessing at; a universal ability only ever becomes named_ability by
            being matched by name.
        @param entity_name The name of the acting entity.
        @param skill_name The skill being used.
        @return The matching weapon/ability table, or None.
        """
        entity = self.entities.get(entity_name, {})

        for item_name in entity.get("equipped", {}).values():
            item = self.entities.get(item_name)
            if item and self.ability_matches_skill(item, skill_name):
                return item

        for ability in entity.get("abilities", []):
            ability = self.resolve_ability(ability)
            if ability and self.ability_matches_skill(ability, skill_name):
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

    def _ability_requires_language(self, skill_name, ability):
        """!
        @brief Whether skill_name/ability needs a shared language to work at all -- the
            language_dependent opt-in tag (entity_schema.toml, same fixed-classification role
            damage_tags/armor_tags already play, see CLAUDE.md's "Tags vs. conditions") checked
            by _resolve_roll (DM_Core.py) right alongside is_in_range.
        @param skill_name The skill being used.
        @param ability The resolved weapon/spell/technique table from _resolve_roll, or None.
        @return True if ability itself is flagged, or -- when no ability was actually resolved
                (ex: "persuade the guard" resolves skill_name="charisma" with ability=None,
                since find_attack_ability deliberately never scans *universal* abilities like
                "charm" -- see its own docstring) -- if any ability the skill declares in its
                own skills.toml "abilities" list (ex: charisma -> ["charm"]) is flagged. False
                for an unlisted/unresolvable skill or an ability/skill with no such abilities.
        """
        if ability is not None:
            return bool(ability.get("language_dependent"))
        for name in self.skills.get(skill_name, {}).get("abilities", []):
            resolved = self.resolve_ability(name)
            if resolved and resolved.get("language_dependent"):
                return True
        return False

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

            Falls back to a *universal* ability if entity_name doesn't own ability_name itself --
            any name appearing in some [[skill]]'s own "abilities" field (self.universal_abilities,
            built once at load time -- DM_Rules.py's load_rules) is usable by any entity, no
            ownership check at all, the tabletop "you don't have to be trained to try a combat
            maneuver" precedent. This only ever succeeds on an exact name match -- if NLP only
            matched the bare skill name (too vague to name a specific maneuver), nothing here
            fires and the roll proceeds as an ordinary skill check with no attached effect;
            there's no principled way to guess which of several same-skill maneuvers a vague
            phrase meant, so this deliberately never guesses.
        @param entity_name The name of the acting entity.
        @param ability_name The candidate ability name (ex: action_detected's "skill" field).
        @return The resolved ability table if entity_name owns it, or it's a universal ability,
                else None.
        """
        entity = self.entities.get(entity_name, {})
        for ability in entity.get("abilities", []):
            resolved = self.resolve_ability(ability)
            if resolved and resolved.get("name") == ability_name:
                return resolved
        if ability_name in self.universal_abilities:
            return self.entities.get(ability_name)
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
            rating = skill_rating(stats.get("dice", 0), stats.get("pips", 0))
            if best_rating is None or rating > best_rating:
                best_rating = rating
                best_skill = candidate
        if best_skill is not None:
            return best_skill
        return ability_skill[0] if ability_skill else None

    def choose_behavior(self, entity_name, opponent_name=None):
        """!
        @brief Picks the first entry in an entity's [[entity.behavior]] list whose
            requirements are currently met, in declaration order -- the same
            {field, operator, value} requirement engine [[status]] already uses
            (entity_matches_requirements), just read from "behavior" instead of
            "status". Ex: arena.toml's wolf checks a low-hp "retreat" entry first,
            then falls back to "always attack while hp_per_remain >= 0.01", so it
            keeps attacking (or fleeing) until it's effectively dead and then simply
            stops matching any behavior at all.
        @param entity_name The name of the entity choosing a behavior.
        @param opponent_name The entity it would act against, if any -- forwarded to
            entity_matches_requirements purely so a requirement can reference the
            opponent-relative "distance_to_target" field (ex: a creature with both a
            melee and a ranged attack choosing between them by the current gap); no
            shipped behavior data uses this yet, since resolve_behavior_action's own
            implicit "advance when the chosen attack can't reach" fallback already
            covers the common single-attack case without it.
        @return The first matching behavior definition, or None if none match (or
                the entity has no behavior list at all).
        """
        for behavior in self.entities.get(entity_name, {}).get("behavior", []):
            if self.entity_matches_requirements(entity_name, behavior.get("requirements", []), opponent_name):
                return behavior
        return None

    def resolve_behavior_action(self, entity_name, target_name):
        """!
        @brief Resolves an entity's currently-chosen behavior against a target -- either a
            deliberate move (see below) or an opposed attack. A behavior names a specific
            *action* (ex: arena.toml's wolf names "bite", one of its own abilities)
            rather than a bare skill -- reusing resolve_named_ability + select_ability_skill,
            the exact same lookup the player's own named-technique path (ex: "cleave")
            already uses, rather than going through find_attack_ability's
            equipped-weapon-first priority. That priority exists to disambiguate a skill name
            shared by multiple things; a behavior already knows exactly which ability it
            means, so there's nothing to disambiguate.

            `action = "advance"`/`"retreat"` are reserved, not looked up as abilities at all
            -- MOVEMENT_ACTIONS routes straight to move_toward_or_away (DM_Movement.py), the
            explicit way a behavior entry opts into self-preservation (ex: fleeing once
            hp_per_remain drops low, checked ahead of an attack entry in the same
            declaration-order list choose_behavior already walks -- see arena.toml's
            wolf/crypt.toml's giant spider for the shipped example) or into deliberately
            closing distance
            regardless of what's in range. `action = "steal"`/`"gift"` are reserved the same
            way -- routed to _resolve_transfer_behavior instead, an autonomous item transfer
            (the behavior entry's own "item" field names what moves) that fires the same
            "theft"/"favor" attitude nudge DM_Inventory.py's player-driven "take"/"give"
            already fires, just entity-initiated.

            Otherwise, range-checked exactly like the player's own attacks (see is_in_range
            in DM_Movement.py) -- but unlike a denied player attack (which just fails with
            "out_of_range", no roll, same turn), an entity whose chosen attack can't currently
            reach target_name moves toward it instead of doing nothing: closing the distance
            is what any of these would obviously do rather than stand still out of reach, and
            unlike fleeing (which needs an author's judgment call about which creatures value
            their own life) there's no reason this needs to be opted into per entity.
        @param entity_name The name of the acting entity (ex: a wolf).
        @param target_name The name of the entity being acted against (ex: the player).
        @return A MovementOutcome if the chosen behavior was a deliberate move, or was an
            attack that had to close distance instead; a TransferOutcome if it was a "steal"/
            "gift"; a RolledOutcome (with a DamageEffect on a successful hit) on a normal
            attack; or None if entity_name currently can't act at all (is_action_prevented,
            ex: "pinned"), no behavior currently matches, its named action isn't actually one
            of the entity's own abilities, a "steal"/"gift" named an item not actually present
            in the source's own inventory, or a
            move (deliberate or fallback) had nowhere valid to happen (ex: target_name isn't
            a real entity). A successful hit also nudges target_name's own attitude toward
            entity_name (DM_Core.py's _nudge_combat_hit_attitude -- see docs/social-dialogue.md's
            "Action-driven attitude drift"), the same "combat_hit"/"shared_enemy" shape the
            player's own attacks already trigger.
        """
        if self.is_action_prevented(entity_name):
            return None

        behavior = self.choose_behavior(entity_name, target_name)
        if behavior is None:
            return None

        action_name = behavior.get("action")

        if action_name in MOVEMENT_ACTIONS:
            movement = self.move_toward_or_away(entity_name, target_name, action_name)
            return self._movement_outcome(entity_name, action_name, movement)

        if action_name in TRANSFER_ACTIONS:
            return self._resolve_transfer_behavior(
                entity_name, target_name, action_name, behavior.get("item"), behavior.get("amount"),
            )

        ability = self.resolve_named_ability(entity_name, action_name)
        if ability is None:
            self.event_bus.publish(
                "log_warning", f"{entity_name}'s behavior names unknown action '{action_name}'."
            )
            return None

        if not self.is_in_range(entity_name, target_name, ability):
            movement = self.move_toward_or_away(entity_name, target_name, "advance")
            return self._movement_outcome(entity_name, "advance", movement)

        skill_name = self.select_ability_skill(entity_name, ability)
        roll = self.resolve_opposed_action(entity_name, skill_name, target_name)
        result = rolled_outcome_from_roll(roll)

        if result.success:
            damage = self.calculate_damage(entity_name, target_name, ability)
            result.effects.append(DamageEffect(
                defender=damage["defender"], net_damage=damage["net_damage"],
                remaining_hp=damage["remaining_hp"],
            ))
            # "combat_hit"/"shared_enemy" attitude drift (DM_Core.py's own
            # _nudge_combat_hit_attitude) -- the same call-site shape _apply_damage_if_hit
            # already uses for the player's own attacks, generalized to any entity's resolved
            # attack (ex: an ally striking a shared foe, or a monster hitting the player).
            self._nudge_combat_hit_attitude(target_name, entity_name, damage.get("net_damage", 0))

        return result

    def _movement_outcome(self, entity_name, direction, movement):
        """!
        @brief Wraps move_toward_or_away's own {"opponent", "before", "after"} return into a
            typed MovementOutcome, or None if the move had nowhere valid to happen.
        @param entity_name The entity that moved.
        @param direction "advance" or "retreat".
        @param movement move_toward_or_away's own return value.
        @return A MovementOutcome, or None if movement was falsy.
        """
        if not movement:
            return None
        return MovementOutcome(
            entity=entity_name, direction=direction,
            opponent=movement.get("opponent"), before=movement.get("before"), after=movement.get("after"),
        )

    def _resolve_transfer_behavior(self, entity_name, target_name, direction, item_name, amount=None):
        """!
        @brief Resolves a behavior entry's own "steal"/"gift" action -- an NPC autonomously
            moving one named item (or currency) between itself and target_name, via the same
            transfer_item/transfer_currency primitives DM_Inventory.py's own player-driven
            "take"/"give" already use (_resolve_transfer_intent), just entity-initiated
            instead of player-initiated. "steal" moves item_name from target_name's own
            inventory to entity_name's; "gift" moves it the other way. item_name == "currency"
            (the same reserved sentinel _resolve_transfer_intent already uses) moves currency
            instead of an inventory item -- amount (from the behavior entry's own "amount"
            field) caps how much, same as transfer_currency's own default (None moves
            everything the source has, which a hand-authored pickpocket-style behavior should
            usually override with a modest number rather than cleaning the target out in one
            swipe). Fires the same "theft"/"favor" attitude nudge the player-driven path fires
            too -- target_name's own attitude toward entity_name, scaled by the moved item's
            own TOML "value" (or the currency amount actually moved) against
            SIGNIFICANT_VALUE, the identical reference scale (DM_Inventory.py) -- an NPC
            pickpocketing the player should sour the player's opinion of *them* exactly the
            way the reverse already would.
        @param entity_name The acting entity (ex: a pickpocket NPC).
        @param target_name The other party (ex: the player).
        @param direction "steal" or "gift".
        @param item_name The item entity's own name, or "currency", from the behavior entry's
            own "item" field.
        @param amount Only meaningful for item_name == "currency" -- how much to move; None
            moves everything the source has.
        @return A TransferOutcome once something actually moved; None (the same "nothing valid
            to happen" precedent move_toward_or_away's own fallback shares) if item_name is
            missing entirely, isn't actually present in the source's own inventory, or the
            source has no currency to move -- a "gift" naming something this entity doesn't
            have, or a "steal" naming something target_name doesn't have, simply does nothing
            rather than erroring.
        """
        if not item_name:
            return None
        source_name = target_name if direction == "steal" else entity_name
        destination_name = entity_name if direction == "steal" else target_name

        if item_name == "currency":
            if self.entities.get(source_name, {}).get("currency", 0) <= 0:
                return None
            value = self.transfer_currency(source_name, destination_name, amount)
        else:
            if item_name not in self.entities.get(source_name, {}).get("inventory", []):
                return None
            self.transfer_item(source_name, destination_name, item_name)
            value = self.entities.get(item_name, {}).get("value", 0)

        event_name = "theft" if direction == "steal" else "favor"
        self.nudge_attitude_from_event(target_name, entity_name, event_name, min(1.0, value / SIGNIFICANT_VALUE))
        return TransferOutcome(entity=entity_name, direction=direction, item_name=item_name, target=target_name)

    def _best_damage_dice_pips(self, entity_name):
        """!
        @brief The dice/pips of entity_name's single best damage-dealing weapon/ability, by
            skill_rating -- every equipped item with a damage_value, plus every resolved
            ability (resolve_ability) with one: the same candidate pool find_attack_ability
            draws from, just not filtered down to one particular skill_name, since nothing
            here is about to be rolled -- there's no "which skill" to disambiguate by, only
            "which single number best represents this entity's damage output" (powers
            get_challenge_rating's own damage component).
        @param entity_name The name of the entity to check.
        @return (dice, pips) of the best candidate, or (0, 0) if it has no damage-dealing
            weapon/ability at all, or none of its dice/pips fields actually resolve to a
            number (ex: an ability referencing "user.weapon.dice" on an entity with nothing
            equipped).
        """
        entity = self.entities.get(entity_name, {})
        candidates = [
            item for item in
            (self.entities.get(item_name) for item_name in entity.get("equipped", {}).values())
            if item and "damage_value" in item
        ]
        candidates += [
            ability for ability in
            (self.resolve_ability(entry) for entry in entity.get("abilities", []))
            if ability and "damage_value" in ability
        ]

        best_dice, best_pips, best_rating = 0, 0, 0
        for candidate in candidates:
            damage_value = candidate["damage_value"]
            dice = self.resolve_weapon_reference(entity_name, damage_value.get("dice", 0), "dice")
            pips = self.resolve_weapon_reference(entity_name, damage_value.get("pips", 0), "pips")
            if not isinstance(dice, (int, float)) or not isinstance(pips, (int, float)):
                continue
            rating = skill_rating(dice, pips)
            if rating > best_rating:
                best_dice, best_pips, best_rating = dice, pips, rating
        return best_dice, best_pips

    def get_challenge_rating(self, entity_name):
        """!
        @brief A single number describing how powerful entity_name currently is -- see
            Challenge_Rating.py's calculate_challenge_rating for what it's built from.
            Reflects live state (current max_hp/skills/equipped gear/abilities), not a fixed
            character-creation-time value, so it changes across play as an entity is healed/
            hurt long-term, re-equipped, or gains an ability.
        @param entity_name The name of the entity to rate.
        @return The entity's challenge rating (an int), or 0 if entity_name doesn't exist.
        """
        entity = self.entities.get(entity_name)
        if entity is None:
            return 0
        damage_dice, damage_pips = self._best_damage_dice_pips(entity_name)
        return calculate_challenge_rating(
            entity.get("skills", {}), entity.get("max_hp", 0), damage_dice, damage_pips,
        )

    def get_party_challenge_rating(self):
        """!
        @brief The whole party's challenge rating -- every is_player/is_party entity actually
            in play right now, its own get_challenge_rating summed (Challenge_Rating.py's
            calculate_party_challenge_rating). Filtered through self.scenario_entities, not a
            blind is_player/is_party scan of self.entities -- self.entities can still hold an
            *uninstanced* is_party template that isn't part of the live scenario (see the same
            note on GUI_Core.py's own Party-tab filtering, CLAUDE.md's "Architecture"), and
            that must not count just for existing there.
        @return The party's combined challenge rating (an int).
        """
        return calculate_party_challenge_rating(
            self.get_challenge_rating(name)
            for name in self.scenario_entities
            if self.entities.get(name, {}).get("is_player") or self.entities.get(name, {}).get("is_party")
        )

    def _award_xp_for_defeat(self, entity_name):
        """!
        @brief Awards XP for a just-neutralized threat to every current party member -- one
            shared primitive, two call sites, each deciding independently *when* entity_name
            actually counts as neutralized rather than duplicating this method's own math:
            calculate_damage (above), unconditionally the moment a hostile entity's HP first
            reaches 0, and apply_test_outcome (DM_Status.py), only when the matched
            [entity.test] outcome carries a truthy "xp" key (ex: items.toml's dart trap/scythe
            trap, surviving or disarming being just as real a threat neutralized as a kill,
            authored the same declarative way loot/reveal/damage already are rather than a
            special "if trap" branch anywhere in this method itself).

            The base award is entity_name's own "exp" field if it authored one at all
            (entity_schema.toml -- a deliberate custom value, even 0), else its live
            get_challenge_rating -- the same "no explicit exp" default every shipped
            creature/npc uses today (a trap's own get_challenge_rating is a poor stand-in for
            "how dangerous was this," since it has no skills/damage-dealing ability the usual
            way, so every shipped trap authors an explicit "exp" instead). Multiplied by
            rules.toml's own [xp] xp_multiplier, then either split evenly across the party
            (divide_between_party = true, floor division so an uneven split never grants
            fractional XP) or credited to each member in full (false). No-ops if nobody
            currently in self.scenario_entities is_player/is_party -- mirrors
            get_party_challenge_rating's own filter exactly, so a defeat that happens to leave
            no live party member (shouldn't happen in practice) can't raise on an empty list.
        @param entity_name The name of the entity that was just neutralized.
        """
        formula = self.rules.get("xp", {})
        entity = self.entities.get(entity_name, {})
        base_xp = entity["exp"] if "exp" in entity else self.get_challenge_rating(entity_name)
        awarded = base_xp * formula.get("xp_multiplier", 1)

        party_members = [
            name for name in self.scenario_entities
            if self.entities.get(name, {}).get("is_player") or self.entities.get(name, {}).get("is_party")
        ]
        if not party_members:
            return
        if formula.get("divide_between_party"):
            awarded = awarded // len(party_members)
        if awarded <= 0:
            return

        for member_name in party_members:
            member = self.entities[member_name]
            member["exp"] = member.get("exp", 0) + awarded
        self.event_bus.publish(
            "log_info",
            f"{entity_name} defeated -- {awarded} XP awarded to {', '.join(party_members)}.",
        )
