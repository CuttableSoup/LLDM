"""!
@file LLM_Client.py
@brief A small, stateless, synchronous helper for talking to the current LLM backend's
    OpenAI-compatible chat/completions endpoint (a local Ollama or OpenRouter -- see
    LLM_Backend.py). Deliberately standalone -- not shared with LLM_Core.py's own
    async fetch_from_llm (see NPC_Generation.py's own notes on why): that call always runs on
    its own background thread and must never raise (it always publishes llm_response_ready,
    even on failure); this one is called synchronously, in place, by whatever needs the result
    immediately (NPC_Generation.py's generate_npc_stats, during scenario/room loading), and
    must raise cleanly on any failure so its caller's own fallback path can take over.
"""

import json
import urllib.request

from llm.LLM_Backend import Backend, get_backend

DEFAULT_TIMEOUT = 20
# The model for an explicit api_url that isn't the current backend's own (see
# call_chat_completion) -- Ollama's OpenAI-compat endpoint 400s without a "model" field.
DEFAULT_MODEL = "gemma4"

# The synchronous seam (see LLM_Decision.py): chat_completion calls this instead of
# call_chat_completion's own HTTP request while one is set. None -- the real HTTP adapter -- is
# the default; tests/support.py's scripted_llm installs a scripted one.
_transport = None


def set_transport(transport):
    """!
    @brief Installs a scripted transport (any callable taking call_chat_completion's own
        arguments and returning a parsed response body), or None to restore the real HTTP one.
    @return The transport that was installed before, so a caller can put it back.
    """
    global _transport
    previous, _transport = _transport, transport
    return previous


def chat_completion(api_url, messages, **options):
    """!
    @brief The synchronous seam every structured decision goes through: the scripted transport if
        one is installed, else call_chat_completion's real HTTP request. Passes exactly the
        options the caller gave, so a scripted transport sees the same call a real one would.
    """
    return (_transport or call_chat_completion)(api_url, messages, **options)


def call_chat_completion(
    api_url, messages, tools=None, tool_choice=None, model=None, temperature=0.7,
    max_tokens=1024, timeout=DEFAULT_TIMEOUT, reasoning_effort=None,
):
    """!
    @brief Posts one chat/completions request and returns the parsed JSON response.
    @param api_url None (every caller's default) for the current backend (LLM_Backend.py's
        get_backend), whose key and model fallbacks then apply -- or an explicit endpoint URL,
        which gets a plain request with no key (a key only ever goes to its own backend).
    @param messages The OpenAI-style messages list ({"role", "content"} dicts).
    @param tools Optional OpenAI-style "tools" list (function-calling schema).
    @param tool_choice Optional "tool_choice" value (ex: "auto") -- only meaningful alongside
        tools.
    @param model Overrides the backend's model (and its fallbacks); None for the backend's own.
    @param temperature/max_tokens Standard OpenAI-style sampling params.
    @param reasoning_effort Optional reasoning control, sent only when given -- "none" turns a
        thinking model's hidden reasoning off for a quick enum pick (measured on gemma4: a
        difficulty rating went from 5-15s, sometimes cut off at max_tokens mid-thought, to
        under a second). The backend picks the field that carries it (Backend.payload).
    @param timeout Seconds to wait before giving up -- a hard requirement here (unlike
        fetch_from_llm's own unbounded call), since a caller of this function is blocking
        synchronously, in place, potentially on the GUI thread; a hung server must not be able
        to freeze the whole app indefinitely.
    @return The parsed JSON response body.
    @raises Exception (network error, non-2xx response, invalid JSON) -- callers are expected
        to catch broadly and fall back, not to inspect the specific error type.
    """
    backend = get_backend()
    if api_url is not None and api_url != backend.api_url:
        backend = Backend("custom", api_url, model or DEFAULT_MODEL)
    fields = {"temperature": temperature, "max_tokens": max_tokens}
    if tools is not None:
        fields["tools"] = tools
    if tool_choice is not None:
        fields["tool_choice"] = tool_choice
    payload = backend.payload(messages, model=model, reasoning_effort=reasoning_effort, **fields)

    request = urllib.request.Request(
        backend.api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=backend.headers(),
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))
