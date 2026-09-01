"""!
@file Social_Resolution.py
@brief Pure, DMCore-independent counterpart to DM_Social.py's own action-driven attitude drift
    -- nudge_attitude_from_event, plus apply_capped_drift, the shared accumulate-and-clamp
    primitive it and DM_Social.py's own dialogue-tone nudge_attitude both build on -- as plain
    functions over an explicit entities/rules dict rather than DMCore instance methods. Mirrors
    Combat_Resolution.py's own "pure module, DMCore reaches in" shape (see that module's own
    docstring): built specifically so Program_Interpreter.py's own "attitude" op (see
    docs/design/skill_effect_language.md) can call this without a DMCore instance -- the same
    reason Combat_Resolution.py exists at all.

    DM_Social.py's own nudge_attitude_from_event/_apply_capped_drift keep their existing method
    names/signatures -- both become thin wrappers forwarding self.entities/self.rules, so no
    caller anywhere else in the codebase changes at all. nudge_attitude (dialogue-tone drift)
    deliberately stays a DM_Social.py-only method, not part of this extraction -- see the design
    doc's own "Prerequisite: pure cores for attitude and transfer" for why only
    nudge_attitude_from_event (of DM_Social.py's several attitude methods) needed to move: it's
    the one Program_Interpreter.py's own "attitude" op has to reach with no DMCore instance in
    hand.

    set_prompt_directive is a sibling primitive for the same reason (Program_Interpreter.py's own
    "inject_directive" op reaching in with no DMCore instance), not an extraction of an existing
    DM_Social.py method -- nothing on the DMCore side calls it directly, only the op does, so it
    gets no thin-wrapper counterpart there.
"""

import resolution.Combat_Resolution as Combat_Resolution
# Mirrors DM_Social.py's own ATTITUDE_AXES/ACTION_ATTITUDE_DRIFT_CAP exactly -- see that
# module's own module-level comments for the fuller rationale (the six-to-three axis collapse,
# and why action-driven drift gets a wider cap than talk-driven drift). Owned here now, the same
# "pure module owns the constant, the mixin wrapper doesn't redefine it" convention
# Combat_Resolution.py's own COMPARATORS already sets -- DM_Social.py imports these back rather
# than re-declaring them, so test_unit.py's own `from DM_Social import ...` keeps resolving
# unchanged.
ATTITUDE_AXES = ("disposition", "threat", "familiarity")
TALK_ATTITUDE_DRIFT_CAP = 40
ACTION_ATTITUDE_DRIFT_CAP = 60


def apply_capped_drift(entities, entity_name, toward_name, deltas_key, axis_deltas, cap):
    """!
    @brief Shared accumulate-and-clamp primitive behind both nudge_attitude (DM_Social.py, talk-
        driven) and nudge_attitude_from_event (below, action-driven) -- adds axis_deltas
        elementwise onto entities[entity_name][deltas_key][toward_name] (creating either as
        needed), clamping each axis independently to +/-cap. A no-op for a missing entity; an
        all-zero axis_deltas entry is skipped per-axis rather than writing a spurious zero.
    @param entities The live entities dict.
    @param entity_name The entity whose attitude is drifting.
    @param toward_name The entity it's drifting toward.
    @param deltas_key "attitude_deltas" (talk) or "action_attitude_deltas" (action).
    @param axis_deltas A three-value list, ordered like ATTITUDE_AXES, to add elementwise.
    @param cap The max |value| this accumulator may hold on any one axis.
    """
    entity = entities.get(entity_name)
    if entity is None:
        return
    deltas = entity.setdefault(deltas_key, {})
    axis = deltas.setdefault(toward_name, [0, 0, 0])
    for index, delta in enumerate(axis_deltas):
        if delta:
            axis[index] = max(-cap, min(cap, axis[index] + delta))


def get_attitude(entities, entity_name, toward_name):
    """!
    @brief Resolves entity_name's three-value attitude array toward toward_name -- a specific
        name override, then a supertype override, then the entity's default -- plus whatever
        runtime drift has accumulated toward toward_name specifically (talk-driven
        attitude_deltas and action-driven action_attitude_deltas, added elementwise on top).
        Pure mirror of DM_Social.py's own SocialMixin.get_attitude (needs only entities, never
        self.rules) -- extracted so Combat_Resolution.py's own get_comparable_value can resolve
        a one-line condition string's own "disposition"/"threat"/"familiarity" field (ex:
        docs/design/skill_effect_language.md's own "target.threat < -50") with no DMCore
        instance in hand, the same reason nudge_attitude_from_event lives here at all. DM_Social.py's
        own method becomes a thin wrapper forwarding self.entities, so no caller anywhere else
        in the codebase changes.
    @param entities The live entities dict.
    @param entity_name The name of the entity whose attitude is being read.
    @param toward_name The name of the entity being regarded.
    @return The [disposition, threat, familiarity] attitude array.
    """
    entity = entities.get(entity_name, {})
    attitudes = entity.get("attitudes", {})

    base = None
    for override in attitudes.get("name", []):
        if toward_name in override:
            base = override[toward_name]
            break

    if base is None:
        toward_supertype = entities.get(toward_name, {}).get("supertype")
        for override in attitudes.get("supertype", []):
            if toward_supertype in override:
                base = override[toward_supertype]
                break

    if base is None:
        base = attitudes.get("default", [0, 0, 0])

    result = list(base)
    talk_deltas = entity.get("attitude_deltas", {}).get(toward_name)
    if talk_deltas:
        result = [value + delta for value, delta in zip(result, talk_deltas)]
    action_deltas = entity.get("action_attitude_deltas", {}).get(toward_name)
    if action_deltas:
        result = [value + delta for value, delta in zip(result, action_deltas)]
    return result


def set_prompt_directive(entities, entity_name, text, source_name=None):
    """!
    @brief Plants (or overwrites) entity_name's own persistent prompt_directive -- free text
        later read back by DM_Social.py's own describe_character, so a successfully-cast effect
        (ex: spells.toml's "suggestion") actually shapes what that NPC says/does in every future
        narration prompt built from its persona, not just this turn's own narration line. Mirrors
        nudge_attitude_from_event's own "nothing to affect" gates: a no-op for a missing entity,
        an inanimate object (supertype == "object"), or an entity with no HP left (a dead entity
        has no mind left to plant anything in). One active directive at a time -- a second
        successful suggestion overwrites the first rather than stacking, the simplest thing that
        works.
    @param entities The live entities dict.
    @param entity_name The entity receiving the directive.
    @param text The free-text directive itself.
    @param source_name Who planted it, if known (ctx's own "actor") -- attributed in the stored
        record so narration can credit the right party, but never required (ex: a
        scenario-authored on_enter program with no "actor" in its own ctx).
    """
    entity = entities.get(entity_name)
    if entity is None or entity.get("supertype") == "object":
        return
    if Combat_Resolution.get_current_hp(entities, entity_name) <= 0:
        return
    entity["prompt_directive"] = {"text": text, "source": source_name}


def nudge_attitude_from_event(entities, rules, entity_name, toward_name, event_name, magnitude):
    """!
    @brief Applies a small, capped, persistent drift to entity_name's own three-axis attitude
        toward toward_name, driven by a resolved action rather than dialogue tone -- see
        DM_Social.py's own fuller docstring (unchanged) for the full rationale and call sites.
        A no-op for a missing entity/event/magnitude, an entity with no [entity.attitudes] table
        at all, an inanimate object (supertype == "object"), or an entity with no HP left.
    @param entities The live entities dict.
    @param rules The loaded rules dict (read for its own [[attitude_event]] table).
    @param entity_name The entity whose attitude is drifting.
    @param toward_name The entity it's drifting toward.
    @param event_name An [[attitude_event]] entry's own "name" (ex: "combat_hit", "theft").
    @param magnitude How strongly this occurrence counts, 0..1 -- scales every axis delta the
        matched event declares.
    """
    if not magnitude:
        return
    entity = entities.get(entity_name)
    if entity is None or "attitudes" not in entity or entity.get("supertype") == "object":
        return
    if Combat_Resolution.get_current_hp(entities, entity_name) <= 0:
        return
    event = next(
        (candidate for candidate in rules.get("attitude_event", []) if candidate.get("name") == event_name),
        None,
    )
    if not event:
        return
    axis_deltas = [event.get(axis, 0) * magnitude for axis in ATTITUDE_AXES]
    apply_capped_drift(entities, entity_name, toward_name, "action_attitude_deltas", axis_deltas, ACTION_ATTITUDE_DRIFT_CAP)
