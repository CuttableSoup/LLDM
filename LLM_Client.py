"""!
@file LLM_Client.py
@brief A small, stateless, synchronous helper for talking to Ollama's OpenAI-compatible
    chat/completions endpoint. Deliberately standalone -- not shared with LLM_Core.py's own
    async fetch_from_llm (see NPC_Generation.py's own notes on why): that call always runs on
    its own background thread and must never raise (it always publishes llm_response_ready,
    even on failure); this one is called synchronously, in place, by whatever needs the result
    immediately (NPC_Generation.py's generate_npc_stats, during scenario/room loading), and
    must raise cleanly on any failure so its caller's own fallback path can take over.
"""

import json
import urllib.request

DEFAULT_TIMEOUT = 20
# Unlike LM Studio (which infers the model from whatever's the one thing currently loaded),
# Ollama's OpenAI-compat endpoint 400s without an explicit "model" field, since a single Ollama
# instance can have many models pulled at once -- every caller either accepts this default or
# threads its own override through, mirroring api_url's own pattern.
DEFAULT_MODEL = "gemma4"


def call_chat_completion(
    api_url, messages, tools=None, tool_choice=None, model=DEFAULT_MODEL, temperature=0.7,
    max_tokens=1024, timeout=DEFAULT_TIMEOUT,
):
    """!
    @brief Posts one chat/completions request and returns the parsed JSON response.
    @param api_url The full chat/completions endpoint URL.
    @param messages The OpenAI-style messages list ({"role", "content"} dicts).
    @param tools Optional OpenAI-style "tools" list (function-calling schema).
    @param tool_choice Optional "tool_choice" value (ex: "auto") -- only meaningful alongside
        tools.
    @param model The Ollama model tag to target (ex: "gemma4") -- Ollama, unlike LM Studio,
        requires this on every request.
    @param temperature/max_tokens Standard OpenAI-style sampling params.
    @param timeout Seconds to wait before giving up -- a hard requirement here (unlike
        fetch_from_llm's own unbounded call), since a caller of this function is blocking
        synchronously, in place, potentially on the GUI thread; a hung Ollama must not be able
        to freeze the whole app indefinitely.
    @return The parsed JSON response body.
    @raises Exception (network error, non-2xx response, invalid JSON) -- callers are expected
        to catch broadly and fall back, not to inspect the specific error type.
    """
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice

    request = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))
