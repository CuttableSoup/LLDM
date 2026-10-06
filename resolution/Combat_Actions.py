"""!
@file Combat_Actions.py
@brief What a combatant does, as pure functions over a WorldContext -- damage with its kill
    consequences, target expansion, ability and behavior selection, challenge rating, XP, the
    lore check, and the status/range gates they depend on (action prevention, identification,
    proximity statuses, reach). Everything here is directly testable against a WorldContext and
    a fake CombatHooks, with no DMCore.

    Combat_Resolution.py holds the primitives (dice, conditions, damage maths); this module
    composes them. The few things it needs from outside that graph -- who is hostile to whom,
    the murder check on a kill, attitude nudges, moving an entity, and moving an item -- come
    through ctx.hooks (CombatHooks, below; DMCore supplies DMCoreCombatHooks). Keep that list
    short: a hook is a method on a sibling mixin this module can't reach on its own, and each
    one added makes this module a little less testable on its own.
"""

import re

import resolution.Combat_Resolution as Combat_Resolution
import resolution.Inventory_Resolution as Inventory_Resolution
from dm.DM_ActionOutcome import DamageEffect, MovementOutcome, TransferOutcome, rolled_outcome_from_roll
from resolution.Challenge_Rating import calculate_challenge_rating, calculate_party_challenge_rating, skill_rating
from resolution.Inventory_Resolution import SIGNIFICANT_VALUE

# Reserved [[entity.behavior]] action names -- resolve_behavior_action routes these straight to
# the hooks' move_toward_or_away instead of resolve_named_ability, so no real ability may ever
# be named "advance"/"retreat".
MOVEMENT_ACTIONS = {"advance", "retreat"}

# Reserved [[entity.behavior]] action names for an autonomous item transfer -- routed straight
# to _resolve_transfer_behavior instead of resolve_named_ability, so no real ability may ever be
# named "steal"/"gift" either. Mirrors DM_Inventory.py's own player-driven "take"/"give", just
# entity-initiated: "steal" moves the behavior entry's own "item" from target_name's inventory
# to entity_name's; "gift" moves it the other way.
TRANSFER_ACTIONS = {"steal", "gift"}


class CombatHooks:
    """!
    @brief What this module needs from outside the combat graph. DMCore supplies
        DMCoreCombatHooks (dm/Combat_Actions.py); a test supplies a fake.
    """

    def is_hostile(self, entity_name, toward_name):
        """!@return Whether entity_name is hostile toward toward_name (DM_Social.py)."""
        raise NotImplementedError

    def note_kill(self, killer, victim):
        """!@brief victim just died at killer's hand -- murder if killer's side started it (DM_Law.py)."""
        raise NotImplementedError

    def nudge_combat_hit_attitude(self, target_name, attacker_name, net_damage):
        """!@brief A landed hit's attitude consequences for the defender and anyone who saw it (DM_Core.py)."""
        raise NotImplementedError

    def nudge_attitude_from_event(self, entity_name, toward_name, event_name, magnitude):
        """!@brief An action-driven attitude nudge (DM_Social.py)."""
        raise NotImplementedError

    def move_toward_or_away(self, entity_name, opponent_name, direction):
        """!@brief Moves an entity along the bands, mounts and grid included (DM_Movement.py)."""
        raise NotImplementedError

    def transfer_item(self, from_name, to_name, item_name):
        """!@brief Moves one item between inventories (DM_Inventory.py)."""
        raise NotImplementedError


def is_action_prevented(ctx, entity_name):
    """!
    @brief Whether entity_name is currently unable to act on its own turn at all -- true
        if any of its own active_conditions has a matching conditions.toml [[condition]] entry
        authoring prevents_action = true. Distinct from get_condition_modifier's own flat
        dice penalty: conditions.toml's own "pinned" (maneuvers.toml's "pin", only ever applied
        to an already-grappled target) carries both a modifier -4 *and*
        prevents_action = true, matching Pathfinder's real "pinned" condition -- a pinned
        character can take essentially no physical action beyond trying to escape, not
        just a penalized one. Checked by DM_Core.py's _resolve_roll (the player's own
        turn -- ActionPreventedOutcome, no roll attempted) and Combat_Actions.py's
        resolve_behavior_action (a creature/ally's own turn -- treated exactly like "no
        behavior currently matches", the same "doesn't act" outcome an entity with no
        matching [[entity.behavior]] entry already gets).
    @param entity_name The entity to check.
    @return True if entity_name cannot act this turn.
    """
    condition_defs = {c.get("name"): c for c in ctx.rules.get("condition", [])}
    return any(
        condition_defs.get(name, {}).get("prevents_action")
        for name in Combat_Resolution.get_active_conditions(ctx, entity_name)
    )


def is_identified(ctx, entity_name):
    """!
    @brief Whether an entity (ex: the cursed dagger) has had a hidden property revealed by
        a passed [entity.test] whose outcome had a truthy "reveal" key (ex: an arcane
        check). Mirrors is_locked/is_closed exactly.
    @param entity_name The name of the entity to check.
    @return True if "identified" is in the entity's active_conditions.
    """
    return Combat_Resolution.has_condition(ctx, entity_name, "identified")


def evaluate_proximity_statuses(ctx, actor_name, trigger):
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
        statuses.toml's own "flame wall zone", duration = "rounds"/length = 1) is what makes it
        a *zone* rather than a one-time blast -- it lapses on its own the moment an entity is
        no longer co-band, and is simply reapplied fresh each round for as long as they stay.

        DM_Rules.py's own _evaluate_arrival_statuses (called from _enter_location and
        enter_room) also calls this once per present scene entity with trigger =
        "on_arrival" -- the Pathfinder "Glyph of Warding"/"Magic Mouth" shape: an ordinary
        [[entity]] hazard (placed permanently, no different from a room prop) whose own
        [[status]] entry matches it by "name", landing its "apply" condition on whoever just
        arrived, fired the moment a scene is entered rather than re-checked every combat
        round (there's no round clock running at all outside combat) or off a landed hit.

        A status's own optional "self_dismiss" (a condition name) is dismissed on
        actor_name itself once this call actually lands its "apply" condition on at least
        one nearby entity (never if nobody was in range this time) -- what makes a glyph
        spend itself the first time it actually catches someone, the Pathfinder "discharges
        once triggered" shape. Pairs with a has_condition:<name> requirement (ex: a hazard
        seeded with [entity.conditions.armed], required active, then named as its own
        "self_dismiss") -- the same "seed a flag, dismiss it once solved" shape items.toml's
        dart trap already uses via [entity.test], just spent automatically here instead of
        by a deliberate disarm attempt. Absent (every status shipped before this field
        existed) means this trigger keeps firing every time it's checked, same as "on_round"
        already does for a persistent hazard that's meant to keep re-triggering.
    @param actor_name The entity whose own action just happened (ex: a dragon that just
        landed a bite), or -- for "on_round"/"on_arrival" -- whichever living scene entity
        is currently being checked.
    @param trigger The trigger name to evaluate -- "on_action" (a landed hit), "on_round"
        (once per combat round, per living scene entity), and "on_arrival" (once per scene
        entry, per living scene entity) are the three shipped uses today.
    @return The set of target names a condition was actually applied to this call (possibly
        empty) -- "on_arrival"'s own caller (_evaluate_arrival_statuses, DM_Rules.py) uses
        this to resolve one implicit round of upkeep for exactly those targets immediately
        (see its own docstring for why outside a real combat round nothing else ever would).
        Every existing caller ("on_action"/"on_round") already ignored this method's return
        value entirely, so adding one is purely additive.
    """
    newly_affected = set()
    for status in Combat_Resolution.get_applicable_statuses(ctx, actor_name, trigger):
        apply_block = status.get("apply")
        if not apply_block or not apply_block.get("condition"):
            continue
        radius = apply_block.get("radius", 0)
        side = apply_block.get("side", "enemies")
        applied_to_anyone = False
        for target_name in ctx.scenario_entities:
            if target_name == actor_name or Combat_Resolution.get_current_hp(ctx, target_name) <= 0:
                continue
            if Combat_Resolution.get_distance_between(ctx, actor_name, target_name) > radius:
                continue
            if side == "enemies" and not ctx.hooks.is_hostile(target_name, actor_name):
                continue
            if side == "allies" and ctx.hooks.is_hostile(target_name, actor_name):
                continue
            Combat_Resolution.apply_condition(ctx, 
                target_name, apply_block["condition"],
                duration=apply_block.get("duration"), length=apply_block.get("length"),
                dismiss=apply_block.get("dismiss"),
            )
            applied_to_anyone = True
            newly_affected.add(target_name)
        if applied_to_anyone and status.get("self_dismiss"):
            Combat_Resolution.dismiss_condition(ctx, actor_name, status["self_dismiss"])
    return newly_affected


def is_in_range(ctx, attacker_name, defender_name, ability):
    """!
    @brief Whether attacker_name can currently reach defender_name with ability at all --
        a pure reachability gate, no difficulty change either way (see this file's
        module docstring for why the earlier per-tier accuracy modifier was dropped).
    @param attacker_name The name of the acting entity.
    @param defender_name The name of the target entity.
    @param ability The weapon/spell/innate-ability table being used, or None if this
        skill use isn't an attack at all (ex: a social check) -- always in range, since
        there's nothing physical to be out of reach of.
    @return True if reachable (ability is None, or the band gap is within ability's own
            "range", which defaults to 0 -- melee, same band only -- when absent).
    """
    if ability is None:
        return True
    max_range = ability.get("range", 0)
    return Combat_Resolution.get_distance_between(ctx, attacker_name, defender_name) <= max_range


def has_medium_access(ctx, attacker_name, defender_name, ability):
    """!
    @brief Whether attacker_name can physically engage defender_name at all, given each
        entity's own optional "medium" ("air"/"water"/"earth"; absent/"ground" -- the
        default every existing entity implicitly has, completely unaffected either way) --
        a second, independent reachability gate alongside is_in_range's own band-distance
        check, not a replacement for it. This is deliberately NOT a new spatial/elevation
        axis: the band model stays exactly one-dimensional, and nothing about *traversing*
        bands changes (there was never any terrain-blocking to bypass in the first place --
        every entity already crosses every band freely regardless of what's narrated
        there). What Fly/Swim-and-submerge/Burrow actually need mechanically is this: a
        grounded creature's melee can't connect with something airborne/submerged/
        underground unless it shares that medium itself -- the Pathfinder shape of "you
        can't full-attack a flying dragon with your sword," not a movement-cost question.
        A defender in "ground" (the default) is always reachable by everyone, unconditionally
        -- so the overwhelming majority of entities, which never author "medium" at all, are
        completely unaffected by this check regardless of what they fight.
    @param attacker_name The name of the acting entity.
    @param defender_name The name of the target entity.
    @param ability The weapon/spell/innate-ability table being used, or None (always
        reachable, same "nothing physical to be out of reach of" case is_in_range shares).
    @return True if defender_name is in "ground" (or has no "medium" authored at all);
            True if the ability has a "range" > 0 at all (any ranged/reach attack already
            crosses medium -- no new field needed on any existing weapon/spell, this reuses
            "range" exactly as authored today); True if attacker_name's own "medium"
            matches defender_name's; False otherwise (a melee-only, "ground"-medium
            attacker can't touch a non-"ground" defender).
    """
    if ability is None:
        return True
    defender_medium = ctx.entities.get(defender_name, {}).get("medium", "ground")
    if defender_medium == "ground":
        return True
    if ability.get("range", 0) > 0:
        return True
    attacker_medium = ctx.entities.get(attacker_name, {}).get("medium", "ground")
    return attacker_medium == defender_medium


def calculate_damage(ctx, attacker_name, defender_name, ability):
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
        Also the sole trigger point for create_spawn (an ability field, the Pathfinder Wight/
        Shadow "kills become one of us" shape): on that same real kill, if ability's own
        "create_spawn" = {name, delay_rounds, requirements} is present and defender_name
        matches "requirements" (ex: {field = "subtype", operator = "==", value = "humanoid"}
        -- entity_matches_requirements, the same check a behavior/[entity.test] already
        uses), a pending-spawn record is stashed directly on the corpse (defender_name)
        rather than a new global registry -- see DM_Summoning.py's own _advance_pending_spawn
        for how it actually resolves into a new entity a few rounds later. Deliberately not
        gated on is_hostile like XP is -- create_spawn is a property of the killing ability
        itself, unrelated to whether the kill was XP-worthy from the player's perspective.
    @param attacker_name The name of the entity dealing damage.
    @param defender_name The name of the entity taking damage.
    @param ability A table with damage_value {dice, pips, bonus} and damage_tags, such as a weapon, spell, or innate ability.
    @return A dict describing the raw damage, reduction, vulnerability bonus, net damage, and the defender's remaining HP.
    """
    previous_hp = Combat_Resolution.get_current_hp(ctx, defender_name)
    result = Combat_Resolution.calculate_damage(
        ctx, attacker_name, defender_name, ability,
    )
    killed = previous_hp > 0 and result["remaining_hp"] == 0
    if killed and ctx.hooks.is_hostile(defender_name, ctx.player_name):
        _award_xp_for_defeat(ctx, defender_name)
    if killed:
        # Murder, if the killer's side struck this victim first (DM_Law.py's note_assault).
        ctx.hooks.note_kill(attacker_name, defender_name)
    create_spawn = ability.get("create_spawn")
    if killed and create_spawn and Combat_Resolution.entity_matches_requirements(
        ctx, defender_name, create_spawn.get("requirements", []),
    ):
        ctx.entities[defender_name]["pending_spawn"] = {
            "name": create_spawn["name"],
            "band": Combat_Resolution.get_band(ctx, defender_name),
            "rounds_remaining": create_spawn.get("delay_rounds", 1),
        }
    return result


def resolve_targets(ctx, attacker_name, target_name, ability):
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
    for entity_name in ctx.scenario_entities:
        if entity_name == target_name or Combat_Resolution.get_current_hp(ctx, entity_name) <= 0:
            continue
        distance = Combat_Resolution.get_distance_between(ctx, entity_name, target_name)
        if distance > aoe:
            continue
        if side == "enemies" and not ctx.hooks.is_hostile(entity_name, attacker_name):
            continue
        if side == "allies" and ctx.hooks.is_hostile(entity_name, attacker_name):
            continue
        others.append((distance, entity_name))
    others.sort(key=lambda pair: pair[0])

    names = [target_name] + [name for _, name in others]
    if number and len(names) > number:
        names = names[:number]
    return names


def roll_initiative(ctx, entity_name):
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
    entity_skills = ctx.entities.get(entity_name, {}).get("skills", {})
    dice = 0
    pips = 0
    for term in ctx.rules.get("initiative", []):
        stats = entity_skills.get(term.get("skill"), {"dice": 0, "pips": 0})
        dice += stats.get("dice", 0)
        pips += stats.get("pips", 0)
    return Combat_Resolution.roll_dice(dice, pips)


def find_attack_ability(ctx, entity_name, skill_name):
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
    entity = ctx.entities.get(entity_name, {})

    for item_name in entity.get("equipped", {}).values():
        item = ctx.entities.get(item_name)
        if item and ability_matches_skill(ctx, item, skill_name):
            return item

    for ability in entity.get("abilities", []):
        ability = resolve_ability(ctx, ability)
        if ability and ability_matches_skill(ctx, ability, skill_name):
            return ability

    return None


def ability_matches_skill(ctx, ability, skill_name):
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


def _ability_requires_language(ctx, skill_name, ability):
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
    for name in ctx.skills.get(skill_name, {}).get("abilities", []):
        resolved = resolve_ability(ctx, name)
        if resolved and resolved.get("language_dependent"):
            return True
    return False


def resolve_ability(ctx, ability):
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
        return ctx.entities.get(ability)
    return ability


def resolve_named_ability(ctx, entity_name, ability_name):
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
    entity = ctx.entities.get(entity_name, {})
    for ability in entity.get("abilities", []):
        resolved = resolve_ability(ctx, ability)
        if resolved and resolved.get("name") == ability_name:
            return resolved
    if ability_name in ctx.universal_abilities:
        return ctx.entities.get(ability_name)
    return None


def select_ability_skill(ctx, entity_name, ability):
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

    entity_skills = ctx.entities.get(entity_name, {}).get("skills", {})
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


def choose_behavior(ctx, entity_name, opponent_name=None):
    """!
    @brief Picks the first entry in an entity's [[entity.behavior]] list whose
        requirements are currently met, in declaration order -- the same
        {field, operator, value} requirement engine [[status]] already uses
        (entity_matches_requirements), just read from "behavior" instead of
        "status". Ex: debug.toml's wolf checks a low-hp "retreat" entry first,
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
    for behavior in ctx.entities.get(entity_name, {}).get("behavior", []):
        if Combat_Resolution.entity_matches_requirements(ctx, entity_name, behavior.get("requirements", []), opponent_name):
            return behavior
    return None


def resolve_behavior_action(ctx, entity_name, target_name):
    """!
    @brief Resolves an entity's currently-chosen behavior against a target -- either a
        deliberate move (see below) or an opposed attack. A behavior names a specific
        *action* (ex: debug.toml's wolf names "bite", one of its own abilities)
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
        declaration-order list choose_behavior already walks -- see debug.toml's
        wolf/giant spider for the shipped example) or into deliberately
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
    @param target_name The entity being acted against (ex: the player) -- overridden
        outright if entity_name has its own active override_target (see below).
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
    if is_action_prevented(ctx, entity_name):
        return None

    # A [[condition]]'s own override_target (Combat_Resolution.resolve_override_target)
    # hijacks WHO this turn is aimed at, not whether it can act at all or which ability it
    # picks -- Pathfinder's Confused ("random") and the combat-relevant slice of Dominate
    # (a literal name, authored by whatever spell/effect applied the condition) are the
    # same primitive: everything downstream (behavior selection, range, roll, damage,
    # on-hit effects) runs completely unchanged against whichever name ends up here.
    # Resolved *before* choose_behavior so a behavior that picks between attack options by
    # distance (ex: debug.toml's bandit) judges that distance against the real target.
    override_target = Combat_Resolution.resolve_override_target(ctx, entity_name, [
        name for name in ctx.scenario_entities
        if name != entity_name and Combat_Resolution.get_current_hp(ctx, name) > 0
    ])
    if override_target:
        target_name = override_target

    behavior = choose_behavior(ctx, entity_name, target_name)
    if behavior is None:
        return None

    action_name = behavior.get("action")

    if action_name in MOVEMENT_ACTIONS:
        movement = ctx.hooks.move_toward_or_away(entity_name, target_name, action_name)
        return _movement_outcome(ctx, entity_name, action_name, movement)

    if action_name in TRANSFER_ACTIONS:
        return _resolve_transfer_behavior(ctx, 
            entity_name, target_name, action_name, behavior.get("item"), behavior.get("amount"),
        )

    ability = resolve_named_ability(ctx, entity_name, action_name)
    if ability is None:
        ctx.event_bus.publish(
            "log_warning", f"{entity_name}'s behavior names unknown action '{action_name}'."
        )
        return None

    if not has_medium_access(ctx, entity_name, target_name, ability):
        # Checked *before*, and separately from, is_in_range's own band-distance gate --
        # advancing (closing band distance) can never fix a medium mismatch (a grounded
        # wolf will never "catch up" to a flying bird no matter how many bands it closes),
        # so the ordinary out-of-range fallback (advance toward the target) would be
        # actively wrong here. No behavior currently lets this entity act at all this
        # round, the same "doesn't act" outcome an unmatched behavior list already gets.
        return None

    if not is_in_range(ctx, entity_name, target_name, ability):
        movement = ctx.hooks.move_toward_or_away(entity_name, target_name, "advance")
        return _movement_outcome(ctx, entity_name, "advance", movement)

    if ability.get("cooldown_rounds"):
        # Starts recharging the moment the ability is used, win or lose -- the Pathfinder
        # "breath weapon usable once every 1d4 rounds" shape. A behavior list gates back
        # off this same ability via the "ability_ready:<name>" derived requirement field
        # (Combat_Resolution.get_comparable_value) until tick_ability_cooldowns
        # (run_round_upkeep, DM_Status.py) counts it back down to 0.
        ctx.entities.setdefault(entity_name, {}).setdefault("ability_cooldowns", {})[ability["name"]] = ability["cooldown_rounds"]

    skill_name = select_ability_skill(ctx, entity_name, ability)
    roll = Combat_Resolution.resolve_opposed_action(ctx, entity_name, skill_name, target_name, ability=ability)
    result = rolled_outcome_from_roll(roll)

    if result.success:
        damage = calculate_damage(ctx, entity_name, target_name, ability)
        result.effects.append(DamageEffect(
            defender=damage["defender"], net_damage=damage["net_damage"],
            remaining_hp=damage["remaining_hp"],
        ))
        # "combat_hit"/"shared_enemy" attitude drift (DM_Core.py's own
        # _nudge_combat_hit_attitude) -- the same call-site shape _apply_damage_if_hit
        # already uses for the player's own attacks, generalized to any entity's resolved
        # attack (ex: an ally striking a shared foe, or a monster hitting the player).
        ctx.hooks.nudge_combat_hit_attitude(target_name, entity_name, damage.get("net_damage", 0))
        # "on_action" statuses (ex: Frightful Presence) -- fired on a landed hit, the
        # honest simplification of Pathfinder's "fires on the attack attempt" (see
        # evaluate_proximity_statuses, DM_Status.py).
        evaluate_proximity_statuses(ctx, entity_name, "on_action")

    return result


def _movement_outcome(ctx, entity_name, direction, movement):
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


def _resolve_transfer_behavior(ctx, entity_name, target_name, direction, item_name, amount=None):
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
        if ctx.entities.get(source_name, {}).get("currency", 0) <= 0:
            return None
        value = Inventory_Resolution.transfer_currency(ctx.entities, ctx.event_bus, source_name, destination_name, amount)
    else:
        if item_name not in ctx.entities.get(source_name, {}).get("inventory", []):
            return None
        ctx.hooks.transfer_item(source_name, destination_name, item_name)
        value = ctx.entities.get(item_name, {}).get("value", 0)

    event_name = "theft" if direction == "steal" else "favor"
    ctx.hooks.nudge_attitude_from_event(target_name, entity_name, event_name, min(1.0, value / SIGNIFICANT_VALUE))
    return TransferOutcome(entity=entity_name, direction=direction, item_name=item_name, target=target_name)


def _best_offense_package(ctx, entity_name):
    """!
    @brief entity_name's single best attack, as a matched (skill, dice, pips) package --
        every equipped item with a damage_value, plus every resolved ability
        (resolve_ability) with one (the same candidate pool find_attack_ability draws
        from, just not filtered to one particular skill_name), ranked by
        skill_rating(its own skill) + skill_rating(its own damage) together, not by
        damage alone -- a devastating hit from a skill this entity barely rolls isn't
        actually its best "package" the way get_challenge_rating's offense component
        needs (see Challenge_Rating.py's own module note): skill and damage have to come
        from the SAME candidate, never mixed independently from two different ones.
        select_ability_skill resolves a multi-skill "skill" field (ex: cleave's own
        ["blades", "axes"]) to whichever this entity actually rolls best. An entity with
        no damage-dealing weapon/ability at all (ex: a procedurally-generated NPC --
        DM_NpcGeneration.py deliberately never touches abilities/equipped, only skills)
        falls back to its own best-rated combat_role = "offense" skill with 0 damage,
        rather than reading as entirely unarmed for CR purposes -- a sharp "blades" rating
        still means something even before this entity is ever handed a specific sword
        object, the same way a real Pathfinder creature's attack bonus reflects its
        training, not just whatever it happens to be holding.
    @param entity_name The name of the entity to check.
    @return ({"dice", "pips"} for the winning candidate's own skill, dice, pips) -- dice/
        pips are 0 if entity_name has no damage-dealing weapon/ability at all (the skill
        itself still falls back per above), or none of its dice/pips fields actually
        resolve to a number (ex: an ability referencing "user.weapon.dice" on an entity
        with nothing equipped).
    """
    entity = ctx.entities.get(entity_name, {})
    candidates = [
        item for item in
        (ctx.entities.get(item_name) for item_name in entity.get("equipped", {}).values())
        if item and "damage_value" in item
    ]
    candidates += [
        ability for ability in
        (resolve_ability(ctx, entry) for entry in entity.get("abilities", []))
        if ability and "damage_value" in ability
    ]

    entity_skills = entity.get("skills", {})
    best_skill_stats, best_dice, best_pips, best_total = {}, 0, 0, -1
    for candidate in candidates:
        damage_value = candidate["damage_value"]
        dice = Combat_Resolution.resolve_weapon_reference(ctx, entity_name, damage_value.get("dice", 0), "dice")
        pips = Combat_Resolution.resolve_weapon_reference(ctx, entity_name, damage_value.get("pips", 0), "pips")
        if not isinstance(dice, (int, float)) or not isinstance(pips, (int, float)):
            continue
        skill_name = select_ability_skill(ctx, entity_name, candidate)
        skill_stats = entity_skills.get(skill_name, {}) if skill_name else {}
        total = skill_rating(skill_stats.get("dice", 0), skill_stats.get("pips", 0)) + skill_rating(dice, pips)
        if total > best_total:
            best_total = total
            best_skill_stats, best_dice, best_pips = skill_stats, dice, pips

    if best_total < 0:
        offense_candidates = [entity_skills.get(name, {}) for name in _skills_with_role(ctx, "offense")]
        best_skill_stats = max(
            offense_candidates, key=lambda stats: skill_rating(stats.get("dice", 0), stats.get("pips", 0)),
            default={},
        )
    return best_skill_stats, best_dice, best_pips


def _skills_with_role(ctx, combat_role):
    """!
    @brief Every skill name this setting's own skills.toml tags combat_role ==
        combat_role (ex: "defense", "resistive") -- get_challenge_rating's own way of
        finding "which skill is dodge"/"which skills are the saves" without ever
        hardcoding a setting-specific skill name in Python (see skills.toml's own
        combat_role comment on "athletics").
    @param combat_role "offense", "defense", or "resistive".
    @return A list of skill names (possibly empty, if this setting authors none).
    """
    return [name for name, skill in ctx.skills.items() if skill.get("combat_role") == combat_role]


def get_challenge_rating(ctx, entity_name):
    """!
    @brief A single number describing how powerful entity_name currently is -- see
        Challenge_Rating.py's calculate_challenge_rating for what it's built from. Reflects
        live state (current max_hp/skills/equipped gear/abilities), not a fixed character-
        creation-time value, so it changes across play as an entity is healed/hurt
        long-term, re-equipped, or gains an ability.
    @param entity_name The name of the entity to rate.
    @return The entity's challenge rating (an int), or 0 if entity_name doesn't exist.
    """
    entity = ctx.entities.get(entity_name)
    if entity is None:
        return 0
    offense_skill_stats, damage_dice, damage_pips = _best_offense_package(ctx, entity_name)
    entity_skills = entity.get("skills", {})
    defense_candidates = [entity_skills.get(name, {}) for name in _skills_with_role(ctx, "defense")]
    defense_stats = max(
        defense_candidates, key=lambda stats: skill_rating(stats.get("dice", 0), stats.get("pips", 0)),
        default={},
    )
    save_ratings = [entity_skills.get(name, {}) for name in _skills_with_role(ctx, "resistive")]
    return calculate_challenge_rating(
        offense_skill_stats, damage_dice, damage_pips, defense_stats, save_ratings, entity.get("max_hp", 0),
    )


def _resolve_lore_skill(ctx, target_name):
    """!
    @brief Which skill's own lore_types (skills.toml's [[skill]] field) matches
        target_name's supertype/subtype -- the domain skill a Pathfinder Knowledge check
        against this kind of creature would use (ex: "undead" -> miracles, "animal" ->
        survival), reusing Combat_Resolution.matches_supertype_or_subtype (the same
        OR-of-two-lists check damage_bonus_vs/dispel/cure already share) rather than a
        second lookup table.
    @param target_name The name of the entity being studied.
    @return The matching skill's own name, or None if no [[skill]] authors a matching
        lore_types at all (ex: an ordinary humanoid) -- opt-in, not universal.
    """
    target = ctx.entities.get(target_name, {})
    for skill_name, skill in ctx.skills.items():
        lore_types = skill.get("lore_types")
        if lore_types and Combat_Resolution.matches_supertype_or_subtype(target, lore_types):
            return skill_name
    return None


def _lore_tags(ctx, target_name):
    """!
    @brief The tag data a successful lore check actually reveals -- the same
        resistance_tags/immunity_tags/vulnerability_tags/damage_tags fields already driving
        this entity's own combat math (docs/combat.md's "Tags vs. conditions"), not a
        separately hand-authored "lore text" field.
    @param target_name The name of the entity being studied.
    @return A flat, deduplicated list of tag strings (possibly empty), in a fixed field
        order, skipping whichever fields the target never authored at all.
    """
    target = ctx.entities.get(target_name, {})
    tags = []
    for field in ("resistance_tags", "immunity_tags", "vulnerability_tags", "damage_tags"):
        for tag in target.get(field, []):
            if tag not in tags:
                tags.append(tag)
    return tags


def _resolve_lore_check_intent(ctx, input_text, resolved):
    """!
    @brief Handles "lore_check" -- Pathfinder's Knowledge-skill shape: recalling a
        currently-present creature's own weaknesses/abilities (see docs/extended-goals.md's
        "Knowledge checks revealing monster lore"). Deliberately resolved entirely outside
        _on_turn_detected's own clause list (Intent_Classification.py's own
        EXEMPT_ITEM_INTENTS) -- no dice_penalty threaded at all, and this never triggers
        _resolve_combat_round on its own, even mid-fight, unlike an ordinary action-kind
        clause.

        Which creature is meant is resolved the same "search the raw input for a
        currently-present entity's own name" way _resolve_mount_intent/
        _resolve_formation_intent already use (DM_Movement.py) -- no embedding match, since
        a creature's name either is or isn't literally said. Denied "not_present" if none is
        named; "no_lore_available" if no [[skill]] authors a lore_types matching the
        target's own supertype/subtype (_resolve_lore_skill) -- opt-in, not universal, the
        same precedent [bulk]/[[equip_slot]] already set for a setting/creature that never
        opts in. Already is_identified skips the roll entirely and just reports what's
        already known -- no need to re-earn already-learned knowledge, the same economy
        examining an already-identified item already has (DM_Status.py's is_identified).
        Otherwise a flat, non-opposed resolve_action against difficulty `10 +
        get_challenge_rating(target_name)` (the Pathfinder "DC = 10 + CR" shape). A pass
        applies the permanent "identified" condition and reveals the target's own tags
        (_lore_tags); a fail reports "check_failed".
    @param input_text The raw (lowercased, prefix-stripped) player input, searched for a
        currently-present entity's own name.
    @param resolved The item_interaction_resolved publisher closure from
        DMCore._on_item_interaction_detected.
    """
    candidates = [
        name for name in ctx.scenario_entities
        if name != ctx.player_name
        and re.search(rf"\b{re.escape(name.lower())}\b", input_text or "")
    ]
    if not candidates:
        resolved(False, reason="not_present")
        return
    target_name = candidates[0]

    lore_skill = _resolve_lore_skill(ctx, target_name)
    if not lore_skill:
        resolved(False, reason="no_lore_available", target=target_name)
        return

    if is_identified(ctx, target_name):
        resolved(True, target=target_name, skill=lore_skill, revealed=_lore_tags(ctx, target_name))
        return

    difficulty = 10 + get_challenge_rating(ctx, target_name)
    roll = Combat_Resolution.resolve_action(ctx, ctx.player_name, lore_skill, difficulty)
    if not roll["success"]:
        resolved(False, reason="check_failed", target=target_name, skill=lore_skill)
        return

    Combat_Resolution.apply_condition(ctx, target_name, "identified", duration="permanent")
    resolved(True, target=target_name, skill=lore_skill, revealed=_lore_tags(ctx, target_name))


def get_party_challenge_rating(ctx):
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
        get_challenge_rating(ctx, name)
        for name in ctx.scenario_entities
        if ctx.entities.get(name, {}).get("is_player") or ctx.entities.get(name, {}).get("is_party")
    )


def _award_xp_for_defeat(ctx, entity_name):
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
    formula = ctx.rules.get("xp", {})
    entity = ctx.entities.get(entity_name, {})
    base_xp = entity["exp"] if "exp" in entity else get_challenge_rating(ctx, entity_name)
    awarded = base_xp * formula.get("xp_multiplier", 1)

    party_members = [
        name for name in ctx.scenario_entities
        if ctx.entities.get(name, {}).get("is_player") or ctx.entities.get(name, {}).get("is_party")
    ]
    if not party_members:
        return
    if formula.get("divide_between_party"):
        awarded = awarded // len(party_members)
    if awarded <= 0:
        return

    for member_name in party_members:
        member = ctx.entities[member_name]
        member["exp"] = member.get("exp", 0) + awarded
    ctx.event_bus.publish(
        "log_info",
        f"{entity_name} defeated -- {awarded} XP awarded to {', '.join(party_members)}.",
    )
