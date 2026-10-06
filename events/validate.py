"""!
@file validate.py
@brief Checks an event payload against its declared schema (events/schemas.py) -- unknown keys,
    missing required keys, and loosely the type of each value. Pure: it returns a list of
    problems rather than raising, so a caller decides what a problem means (the test bus turns
    any into a failure; production never calls this).

    The check is deliberately loose about types -- it catches a key renamed on one side or a
    list sent where a string is read, not every nuance -- and strict about keys, since a key
    that exists on only one side of the bus is the drift this exists to catch: a consumer's
    payload.get("amount") silently reading None because the producer now says "price".
"""

import types
import typing


def _matches(annotation, value):
    """!@brief Whether value plausibly fits annotation (str/int/float/bool/list/dict/None/unions/Any)."""
    if annotation is typing.Any or annotation is object:
        return True
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        return any(_matches(arg, value) for arg in typing.get_args(annotation))
    if origin in (typing.NotRequired, typing.Required):
        return _matches(typing.get_args(annotation)[0], value)
    if annotation is type(None) or annotation is None:
        return value is None
    if origin is not None:
        annotation = origin
    if annotation is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if annotation is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(annotation, type):
        return isinstance(value, annotation)
    return True


def validate(schema, payload, path=""):
    """!
    @param schema A TypedDict class, or a plain type (ex: str) for an event whose payload is not a dict.
    @param payload What was published.
    @return A list of human-readable problems (empty when the payload fits).
    """
    where = path or getattr(schema, "__name__", str(schema))
    if not typing.is_typeddict(schema):
        return [] if _matches(schema, payload) else [f"{where}: expected {schema}, got {type(payload).__name__}"]
    if not isinstance(payload, dict):
        return [f"{where}: expected a dict, got {type(payload).__name__}"]
    problems = []
    hints = typing.get_type_hints(schema)
    for key in payload:
        if key not in hints:
            problems.append(f"{where}: unknown key {key!r}")
    for key in schema.__required_keys__:
        if key not in payload:
            problems.append(f"{where}: missing required key {key!r}")
    for key, annotation in hints.items():
        if key in payload and not _matches(annotation, payload[key]):
            problems.append(f"{where}.{key}: {type(payload[key]).__name__} doesn't fit {annotation}")
    return problems
