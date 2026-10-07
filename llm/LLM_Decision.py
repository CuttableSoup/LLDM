"""!
@file LLM_Decision.py
@brief The structured decision (see CONTEXT.md): one synchronous LLM call constrained to a tool
    schema, answering with one accepted choice and its arguments, a decline, or "unavailable".
    Every function that asks the model to pick or fill something (resolution/AdHoc_Generation.py's
    item/creature/removal/edit/adjudication functions, resolution/NPC_Generation.py's
    generate_npc_stats) builds its own messages and schema, calls decide(), and shapes the result;
    none parses a tool call or handles the transport itself.

    The transport is LLM_Client.chat_completion -- a real HTTP adapter by default, swappable via
    LLM_Client.set_transport (tests/support.py's scripted_llm is the scripted one), so a test
    scripts the model's reply once and exercises the whole path, tool-call parsing and the
    "unavailable" failure path included.
"""
import json

from llm import LLM_Client
from llm.LLM_Backend import get_backend


def decline_tool_schema(description):
    """!
    @brief The shared "decline" function a tool schema offers alongside its own primary
        function(s) -- the model's own escape hatch for an implausible request, letting
        tool_choice="auto" pick between "do it" and "don't" rather than forcing a call
        regardless of plausibility.
    @param description Function-specific guidance on when to pick this over the primary one.
    @return One OpenAI-style function-schema dict.
    """
    return {
        "type": "function",
        "function": {
            "name": "decline",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    }


def extract_tool_call(response):
    """!
    @brief Pulls the function name and parsed arguments out of a raw chat-completion response.
    @param response The parsed JSON response body.
    @return (function_name, arguments_dict).
    @raises Exception on any malformed/missing shape -- decide() catches broadly.
    """
    tool_call = response["choices"][0]["message"]["tool_calls"][0]
    arguments = json.loads(tool_call["function"]["arguments"])
    return tool_call["function"]["name"], arguments


def decide(messages, tools, accepted_function_names, timeout=None,
           max_tokens=None, reasoning_effort=None, temperature=None):
    """!
    @brief Makes one structured decision: calls the model with a tool schema and resolves
        whether it picked one of accepted_function_names or effectively declined. tool_choice is
        always "auto".
    @param messages The full [system, user] messages list -- caller-built, since content differs
        per decision.
    @param tools The "tools" schema list (conventionally ending with a decline_tool_schema entry).
    @param accepted_function_names The set of function names the caller treats as success.
    @param timeout Seconds to wait; None is the backend's own generation_timeout.
    @param max_tokens Optional completion budget, forwarded only when given -- the reasoning model
        can spend the client's 1024 default on thinking before it ever reaches the tool call
        (finish_reason "length", no tool_calls), which reads as "unavailable".
    @param reasoning_effort Optional, forwarded only when given -- "none" for a quick enum pick
        that gains nothing from hidden reasoning (see LLM_Client.call_chat_completion).
    @param temperature Optional, forwarded only when given -- 0 for a classification that should
        answer the same way every time.
    @return (function_name, arguments) when function_name is in accepted_function_names.
            (None, reason) otherwise -- "unavailable" if the call or the tool-call parsing
            raised, else the arguments' own "reason" (or "declined") for an explicit decline or
            any unrecognized function name.
    """
    timeout = timeout or get_backend().generation_timeout
    extra = {"max_tokens": max_tokens} if max_tokens else {}
    if reasoning_effort:
        extra["reasoning_effort"] = reasoning_effort
    if temperature is not None:
        extra["temperature"] = temperature
    try:
        response = LLM_Client.chat_completion(
            None, messages, tools=tools, tool_choice="auto", timeout=timeout, **extra,
        )
        function_name, arguments = extract_tool_call(response)
    except Exception:
        return None, "unavailable"

    if function_name not in accepted_function_names:
        reason = arguments.get("reason", "declined") if isinstance(arguments, dict) else "declined"
        return None, reason

    return function_name, arguments
