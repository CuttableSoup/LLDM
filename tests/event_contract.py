"""!
@file event_contract.py
@brief The event contract, enforced in tests only (events/schemas.py declares it; production
    never checks it): a validating EventBus that fails the test the moment a payload on a
    schema'd event doesn't fit, and a static check that every key a consumer reads is one the
    schema declares.
"""

import ast
import inspect
import textwrap

from Event_Bus import EventBus
from events.schemas import CLAUSE_SCHEMAS, SCHEMAS
from events.validate import validate


class EventContractError(AssertionError):
    pass


class ValidatingEventBus(EventBus):
    """!
    @brief EventBus that checks every payload published on a schema'd event against SCHEMAS (and
        each turn_detected clause against its own kind's schema), raising EventContractError on a
        mismatch so the producer's own test fails at the line that published it. strict = False
        turns it off for a test that publishes a deliberately malformed payload.
    """

    def __init__(self):
        super().__init__()
        self.strict = True

    def publish(self, event_type, message):
        if self.strict and event_type in SCHEMAS:
            problems = validate(SCHEMAS[event_type], message, path=event_type)
            if event_type == "turn_detected" and isinstance(message, dict):
                for index, clause in enumerate(message.get("clauses", [])):
                    schema = CLAUSE_SCHEMAS.get(clause.get("kind")) if isinstance(clause, dict) else None
                    if schema is None:
                        problems.append(f"turn_detected.clauses[{index}]: unknown clause kind")
                    else:
                        problems += validate(schema, clause, path=f"turn_detected.clauses[{index}]")
            if problems:
                raise EventContractError("; ".join(problems))
        super().publish(event_type, message)


def consumed_keys(function, param="data"):
    """!
    @brief Every string key function reads from its payload parameter -- param.get("k"),
        param["k"], and "k" in param -- found statically. A key read here that no schema
        declares is the consumer half of drift: it can only ever see None.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    keys = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == param
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            keys.add(node.args[0].value)
        elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == param
              and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
            keys.add(node.slice.value)
        elif (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.In)
              and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)
              and isinstance(node.comparators[0], ast.Name) and node.comparators[0].id == param):
            keys.add(node.left.value)
    return keys
