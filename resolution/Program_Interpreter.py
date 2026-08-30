"""!
@file Program_Interpreter.py
@brief The pure `do`/`if` engine behind the skill & entity effect language (see
    docs/design/skill_effect_language.md) -- run_program(node, ctx, entities, rules, event_bus)
    walks a TOML-authored program (a list of steps, or a single inline step) and performs
    whatever it says, entirely through the same pure primitives Combat_Resolution.py/
    Social_Resolution.py already expose. Never a DMCore mixin -- callers reach in from both
    DMCore mixins (DM_Core.py's ability/test on_pass/on_fail, DM_Status.py's on_round_upkeep,
    DM_Rules.py's on_enter) and from Combat_Resolution.py's own apply_damage/apply_healing
    (pure-to-pure, for on_damage/on_heal) -- a self-reading interpreter could never be called
    from the latter (see the design doc's own "Module shape" for why this was corrected from an
    earlier self-reading-mixin draft).

    A program runs against ctx = {"actor": <entity name or None>, "target": <entity name or
    None>, ...}. Exactly two roles exist everywhere on purpose -- which entity "target" (or
    "actor") actually means changes by attachment point, not by anything this module knows
    about; see the design doc's own "Evaluation context"/"Attachment points" tables. A role
    missing from ctx (ex: no "actor" on a round-upkeep tick) resolves any reference to it as a
    quiet no-op, matching the existing convention that a derived field resolves to None under
    inapplicable conditions rather than erroring.

    Error handling is split by kind, deliberately: a structurally malformed step (an unknown
    `do` name, a missing required arg, an unparseable condition string) raises immediately --
    this is new, authored TOML, not legacy data, and it matches load_scenario_definition's own
    "fatal on purpose" precedent for a missing scenario. A step whose *entity reference* doesn't
    resolve at evaluation time (ex: entity = "target" with no target in this ctx) is a quiet
    no-op instead, matching the existing convention that a derived field resolves to None under
    inapplicable conditions rather than erroring.
"""

import re

import resolution.Combat_Resolution as Combat_Resolution
import resolution.Inventory_Resolution as Inventory_Resolution
import resolution.Social_Resolution as Social_Resolution
# Reserved role tokens ctx is always keyed by, and the only values entity/toward/from/to args
# may name -- see PLAYER_PLACEHOLDER (DM_Rules.py) for the same reserved-token precedent.
ROLES = ("actor", "target")

_COMPARISON_PATTERN = re.compile(
    r"^(?P<role>actor|target)\.(?P<field>[\w:]+)\s+(?P<op>>=|<=|==|!=|not_in|in|>|<)\s+(?P<value>.+)$"
)
_VALUE_REF_PATTERN = re.compile(r"^(?P<role>actor|target)\.(?P<field>[\w:]+)$")


def _parse_literal(text):
    """!
    @brief Parses a one-line condition string's own literal value -- a bool, an int, a float,
        a quoted string, a plain bracketed list of any of those, or (falling through) the raw
        text unchanged. Deliberately not ast.literal_eval/eval -- this is untrusted-shape-wise
        authored TOML, not code, and a small hand-rolled parser is enough for the vocabulary
        this language actually needs.
    @param text The literal's own raw text (already stripped of surrounding whitespace).
    @return The parsed Python value.
    """
    text = text.strip()
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [] if not inner else [_parse_literal(item) for item in inner.split(",")]
    return text


def _parse_comparison(expr):
    """!
    @brief Parses one "<role>.<field> <op> <literal>" condition string.
    @param expr The condition string (ex: "target.threat < -50").
    @return (role, field, operator, value).
    @raises ValueError if expr doesn't match the expected shape at all -- a structurally
            malformed step, not a missing-entity runtime gap.
    """
    match = _COMPARISON_PATTERN.match(expr.strip())
    if not match:
        raise ValueError(f"Malformed condition expression: {expr!r}")
    return match.group("role"), match.group("field"), match.group("op"), _parse_literal(match.group("value"))


def _evaluate_comparison(expr, ctx, entities):
    """!
    @brief Evaluates one "<role>.<field> <op> <literal>" comparison string against ctx/entities,
        via the same get_comparable_value/COMPARATORS engine [[status]]/[[entity.behavior]]
        requirements already use (Combat_Resolution.py) -- so every derived field
        (hp_per_remain, has_condition:<name>, distance_to_target, ...) this project has already
        built works here for free.
    @param expr The condition string.
    @param ctx {"actor", "target", ...}.
    @param entities The live entities dict.
    @return True if the comparison holds; False if the role has no entity in this ctx, the
            field doesn't resolve (None), or the operator is unknown.
    """
    role, field, operator, value = _parse_comparison(expr)
    entity_name = ctx.get(role)
    if entity_name is None:
        return False
    opponent_role = "target" if role == "actor" else "actor"
    opponent_name = ctx.get(opponent_role)
    actual = Combat_Resolution.get_comparable_value(entities, entity_name, field, opponent_name)
    compare = Combat_Resolution.COMPARATORS.get(operator)
    if compare is None or actual is None:
        return False
    return compare(actual, value)


def evaluate_condition(condition, ctx, entities):
    """!
    @brief Evaluates an `if` condition -- either a single one-line comparison string, or a
        {all|any|none: [...]} boolean combination of them (still just a list of the same
        one-line strings -- see the design doc's own "Condition expressions").
    @param condition A comparison string, or a {"all"|"any"|"none": [expr, ...]} table.
    @param ctx {"actor", "target", ...}.
    @param entities The live entities dict.
    @return True/False.
    @raises ValueError if condition is neither shape.
    """
    if isinstance(condition, str):
        return _evaluate_comparison(condition, ctx, entities)
    if isinstance(condition, dict):
        if "all" in condition:
            return all(evaluate_condition(sub, ctx, entities) for sub in condition["all"])
        if "any" in condition:
            return any(evaluate_condition(sub, ctx, entities) for sub in condition["any"])
        if "none" in condition:
            return not any(evaluate_condition(sub, ctx, entities) for sub in condition["none"])
    raise ValueError(f"Malformed condition: {condition!r}")


def resolve_role(role_token, ctx):
    """!
    @brief Resolves an `entity`/`toward`/`from`/`to` arg -- always one of the two reserved role
        tokens, never a literal entity name -- to the actual entity name for this ctx.
    @param role_token "actor" or "target".
    @param ctx {"actor", "target", ...}.
    @return The resolved entity name, or None if that role has no entity in this ctx (a quiet
            no-op case for the caller, not an error).
    @raises ValueError if role_token isn't one of the two reserved tokens at all -- a
            structurally malformed step.
    """
    if role_token not in ROLES:
        raise ValueError(f"Invalid role token (expected 'actor'/'target'): {role_token!r}")
    return ctx.get(role_token)


def resolve_value(value, ctx, entities):
    """!
    @brief Resolves a step arg that may be a bare "<role>.<field>" value reference (ex:
        magnitude = "actor.roll_margin") -- anywhere else a step expects a value, this is the
        one place that reference is recognized and resolved, via the same get_comparable_value
        engine evaluate_condition uses. Anything else (a plain number, a literal string with no
        role prefix, ...) is returned unchanged.
    @param value The raw arg value from the step's own table.
    @param ctx {"actor", "target", ...}.
    @param entities The live entities dict.
    @return The resolved value.
    """
    if not isinstance(value, str):
        return value
    match = _VALUE_REF_PATTERN.match(value)
    if not match:
        return value
    role, field = match.group("role"), match.group("field")
    entity_name = ctx.get(role)
    if entity_name is None:
        return None
    opponent_role = "target" if role == "actor" else "actor"
    return Combat_Resolution.get_comparable_value(entities, entity_name, field, ctx.get(opponent_role))


def _require(step, key):
    """!@brief Fetches a required op arg, raising a clear error if it's missing entirely."""
    if key not in step:
        raise ValueError(f"Program step missing required arg {key!r}: {step!r}")
    return step[key]


def _op_condition(step, ctx, entities, rules, event_bus):
    """!@brief `condition` -- applies a condition to entity/toward/from/to's resolved role."""
    entity_name = resolve_role(_require(step, "entity"), ctx)
    if entity_name is None:
        return
    Combat_Resolution.apply_condition(
        entities, event_bus, entity_name, _require(step, "name"),
        duration=step.get("duration"), dismiss=step.get("dismiss"),
    )


def _op_dismiss_condition(step, ctx, entities, rules, event_bus):
    """!@brief `dismiss_condition` -- removes a condition from entity's resolved role."""
    entity_name = resolve_role(_require(step, "entity"), ctx)
    if entity_name is None:
        return
    Combat_Resolution.dismiss_condition(entities, event_bus, entity_name, _require(step, "name"))


def _op_attitude(step, ctx, entities, rules, event_bus):
    """!@brief `attitude` -- nudges entity's attitude toward toward via a named attitude_event."""
    entity_name = resolve_role(_require(step, "entity"), ctx)
    toward_name = resolve_role(_require(step, "toward"), ctx)
    if entity_name is None or toward_name is None:
        return
    magnitude = resolve_value(_require(step, "magnitude"), ctx, entities)
    Social_Resolution.nudge_attitude_from_event(entities, rules, entity_name, toward_name, step["event"], magnitude)


def _op_damage(step, ctx, entities, rules, event_bus):
    """!
    @brief `damage` -- deals real damage (via calculate_damage, same immunity/resistance/
        vulnerability path a weapon hit takes) to entity's resolved role. The attacker for
        bonus-formula resolution purposes is ctx's own "actor" if this program has one, else
        entity itself -- the same self-inflicted convention DM_Inventory.py's own poison-item
        handling already uses when there's no real third party dealing the damage.
    """
    entity_name = resolve_role(_require(step, "entity"), ctx)
    if entity_name is None:
        return
    attacker_name = ctx.get("actor") or entity_name
    ability = {
        "damage_value": {"dice": step.get("dice", 0), "pips": step.get("pips", 0), "bonus": step.get("bonus", 0)},
        "damage_tags": step.get("tags", []),
    }
    Combat_Resolution.calculate_damage(entities, rules, event_bus, attacker_name, entity_name, ability)


def _op_heal(step, ctx, entities, rules, event_bus):
    """!@brief `heal` -- restores HP to entity's resolved role."""
    entity_name = resolve_role(_require(step, "entity"), ctx)
    if entity_name is None:
        return
    amount = Combat_Resolution.roll_dice(step.get("dice", 0), step.get("pips", 0)) + step.get("bonus", 0)
    Combat_Resolution.apply_healing(entities, rules, event_bus, entity_name, amount)


def _op_transfer_item(step, ctx, entities, rules, event_bus):
    """!@brief `transfer_item` -- moves one named item from `from`'s resolved role to `to`'s."""
    from_name = resolve_role(_require(step, "from"), ctx)
    to_name = resolve_role(_require(step, "to"), ctx)
    if from_name is None or to_name is None:
        return
    Inventory_Resolution.transfer_item(entities, event_bus, from_name, to_name, _require(step, "item"))


def _op_transfer_currency(step, ctx, entities, rules, event_bus):
    """!
    @brief `transfer_currency` -- moves currency from `from`'s resolved role to `to`'s. `amount`
        is optional; omitted, it moves everything `from` is carrying (maneuvers.toml's own
        "sleight of hand" -- a pickpocket takes whatever's in the purse, not a fixed cut).
    """
    from_name = resolve_role(_require(step, "from"), ctx)
    to_name = resolve_role(_require(step, "to"), ctx)
    if from_name is None or to_name is None:
        return
    Inventory_Resolution.transfer_currency(entities, event_bus, from_name, to_name, step.get("amount"))


# do -> handler(step, ctx, entities, rules, event_bus). A small dict registry, not an if/elif
# chain -- adding an op later is one function plus one registry entry (see the design doc's own
# "Module shape").
OP_HANDLERS = {
    "condition": _op_condition,
    "dismiss_condition": _op_dismiss_condition,
    "attitude": _op_attitude,
    "damage": _op_damage,
    "heal": _op_heal,
    "transfer_item": _op_transfer_item,
    "transfer_currency": _op_transfer_currency,
}


def run_program(node, ctx, entities, rules, event_bus):
    """!
    @brief Runs one program -- a list of steps, or a single inline step (TOML's array-of-tables
        idiom collapses to a bare list here; a single-step program is just one table, per the
        design doc's own "Surface syntax"). Each step is either a conditional (`if`/`then`/
        `else`) or an action (`do` plus that op's own args); `then`/`else` nest the same two
        shapes, so a branch running more than one action is just an array there too.
    @param node The program -- a step (dict), a list of steps, or None/falsy (a no-op).
    @param ctx {"actor": <entity name or None>, "target": <entity name or None>, ...}.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param event_bus The EventBus, forwarded to every op needing one.
    @raises ValueError for any structurally malformed step (unknown `do`, missing required arg,
            unparseable condition, a step with neither `do` nor `if`) -- see this module's own
            docstring for why that's fatal rather than silently skipped.
    """
    if not node:
        return
    if isinstance(node, list):
        for step in node:
            run_program(step, ctx, entities, rules, event_bus)
        return
    if not isinstance(node, dict):
        raise ValueError(f"Invalid program step: {node!r}")

    if "if" in node:
        branch = node.get("then") if evaluate_condition(node["if"], ctx, entities) else node.get("else")
        run_program(branch, ctx, entities, rules, event_bus)
        return

    if "do" in node:
        handler = OP_HANDLERS.get(node["do"])
        if handler is None:
            raise ValueError(f"Unknown program op: {node['do']!r}")
        handler(node, ctx, entities, rules, event_bus)
        return

    raise ValueError(f"Program step must have 'do' or 'if': {node!r}")
