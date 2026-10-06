"""!
@file LLM_Backend.py
@brief Which LLM server every call goes to -- a local Ollama ("local"), or, for a machine
    that can't run a model itself, Google AI Studio ("google") or OpenRouter ("openrouter") --
    and how a request to it is shaped. Every caller (LLM_Client.py's call_chat_completion, LLM_Core.py's narration) reads
    the one current Backend from get_backend(); LLDM.py and tools/playtest.py pick it at boot
    with load_backend + set_backend. All three speak the same OpenAI-style chat/completions
    API, so a Backend only differs in its URL, model, key, and a few request fields.
"""

import os
import tomllib
import urllib.error
from dataclasses import dataclass

from paths import PROJECT_ROOT

# Per-machine, never committed (see .gitignore) -- it can hold an API key. llm_config.example.toml
# is the committed template.
CONFIG_PATH = os.path.join(PROJECT_ROOT, "llm_config.toml")
BACKEND_NAMES = ("local", "google", "openrouter")

OLLAMA_URL = "http://127.0.0.1:11434/v1/chat/completions"
OLLAMA_MODEL = "gemma4"

# Google AI Studio's OpenAI-compatible endpoint, with a free key from aistudio.google.com. It serves
# only the large Gemma 4 models -- gemma-4-26b-a4b-it and gemma-4-31b-it; every smaller one 404s.
# 26b-a4b is a mixture of experts (~4B active per token), so it runs at about a 4B model's speed:
# ~1s per adjudication and 1.5-1.7s per narration, measured 2026-10-05. 31b was overloaded that day.
GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GOOGLE_KEY_VARIABLE = "GEMINI_API_KEY"
GOOGLE_MODEL = "gemma-4-26b-a4b-it"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY_VARIABLE = "OPENROUTER_API_KEY"
# OpenRouter tries these in order, moving on when one is rate-limited or down (its "models"
# field, which takes at most three). Each was checked on 2026-10-05 against the real
# adjudication call and a narration, with reasoning off: Gemma 4 first for the same model
# family as the local default (upstream-limited that day -- the fallback cost 0.3s), then the
# two fastest correct ones (~1s per call). Not "openrouter/free": it picks a different model
# every call, some of them coding models, and about one in six rejected reasoning off.
OPENROUTER_FREE_MODELS = (
    "google/gemma-4-26b-a4b-it:free",
    "nvidia/nemotron-3.5-lightning:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
)
OPENROUTER_MAX_MODELS = 3

# How long gameplay waits for input adjudication (AdHoc_Generation.py's adjudicate_player_input)
# before the rules decide alone. Local: 0.3-0.65s measured with Ollama's second slot. Online
# adds the network round trip -- 0.5-1.1s measured through OpenRouter's free models, 0.85-1.25s
# (once 3.1s) through Google AI Studio.
LOCAL_ADJUDICATION_TIMEOUT = 1.5
OPENROUTER_ADJUDICATION_TIMEOUT = 2.5
GOOGLE_ADJUDICATION_TIMEOUT = 2.5

# How long an ad hoc generation call (AdHoc_Generation.py's create-an-item/creature/edit tool
# calls) waits before declining. Local calls took 4-8s in the 2026-10 brawler playtest, so the
# old shared 8s cut some off ("Couldn't work that out just now"); online ones answer in 1-2s.
LOCAL_GENERATION_TIMEOUT = 12
ONLINE_GENERATION_TIMEOUT = 8


@dataclass(frozen=True)
class Backend:
    """!
    @brief One LLM server and how to talk to it.
    @param name "local", "google", "openrouter", or "custom" (an explicit URL a caller passed -- see
        LLM_Client.call_chat_completion).
    @param fallback_models Tried after model, in order (OpenRouter only).
    @param sourcebook_grounding Whether narration prompts carry RAG excerpts of the sourcebook
        PDFs (LLM_Core.py's perform_rag) -- online, those excerpts go to a third party.
    """
    name: str
    api_url: str
    model: str
    fallback_models: tuple = ()
    api_key: str = ""
    adjudication_timeout: float = LOCAL_ADJUDICATION_TIMEOUT
    generation_timeout: float = LOCAL_GENERATION_TIMEOUT
    sourcebook_grounding: bool = True

    @property
    def launches_ollama(self):
        """!@brief Whether LLDM should start (and, if needed, install) Ollama for this backend."""
        return self.name == "local"

    def headers(self):
        """!@brief The request headers -- the key only ever goes to this backend's own URL."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.name == "openrouter":
            headers["X-Title"] = "LLDM"
        return headers

    def payload(self, messages, model=None, reasoning_effort=None, **fields):
        """!
        @brief The request body for messages plus fields (temperature, max_tokens, tools, ...).
        @param model Overrides this backend's model -- and drops its fallbacks, since the caller
            asked for that one model.
        @param reasoning_effort "none" turns a thinking model's hidden reasoning off for a quick
            pick (see LLM_Client.call_chat_completion). Ollama takes the OpenAI-style field;
            OpenRouter its own "reasoning" object; Google only "minimal" for Gemma -- the OpenAI
            value "none" isn't portable.
        """
        body = {"model": model or self.model, "messages": messages, **fields}
        if self.fallback_models and not model:
            body["models"] = [self.model, *self.fallback_models]
        if self.name == "openrouter":
            # Off by default online, narration included: with it on, a free model's thinking came
            # back as the narration itself ("Here's a thinking process: 1. Analyze User Input...")
            # and turns took 20-60s. Locally, Ollama keeps gemma4's thinking out of the reply.
            reasoning_effort = reasoning_effort or "none"
        if self.name == "google":
            # Always, narration included: Gemma 4 on Google rejects "none" ("Thinking budget is not
            # supported for this model"), and with no setting it thinks for 300-700 tokens a call --
            # 7-16s adjudications, and narration that opened with a <thought> block.
            reasoning_effort = "minimal"
        if reasoning_effort:
            if self.name == "openrouter":
                body["reasoning"] = {"enabled": False} if reasoning_effort == "none" else {"effort": reasoning_effort}
            else:
                body["reasoning_effort"] = reasoning_effort
        return body

    def failure_message(self, error):
        """!
        @brief What to tell the player when a request to this backend failed -- read from an
            HTTPError's body where the server says why (OpenRouter's free daily cap, a bad key).
        @param error The exception the request raised.
        """
        if self.name == "google":
            return _google_failure_message(error)
        if self.name != "openrouter":
            return "Could not connect to the local LLM."
        if isinstance(error, urllib.error.HTTPError):
            body = _read_error_body(error).lower()
            if error.code == 401:
                return (f"OpenRouter rejected the API key -- set {OPENROUTER_KEY_VARIABLE} or api_key "
                        "in llm_config.toml.")
            if error.code == 429 and "per-day" in body:
                return "OpenRouter's free daily request limit is used up; it resets at midnight UTC."
            if error.code == 429:
                return "The online models are busy right now; try again in a moment."
            if error.code == 402:
                return "OpenRouter needs credits on the account for this model."
        return "Could not reach OpenRouter."


def _read_error_body(error):
    """!@brief An HTTPError's body as text ("" if it can't be read)."""
    try:
        return error.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _google_failure_message(error):
    """!@brief Backend.failure_message for Google AI Studio."""
    code = error.code if isinstance(error, urllib.error.HTTPError) else None
    if code in (400, 401, 403) and "key" in _read_error_body(error).lower():
        return f"Google AI Studio rejected the API key -- set {GOOGLE_KEY_VARIABLE} or api_key in llm_config.toml."
    if code == 429:
        return "Google AI Studio's free quota is used up for now; try again later."
    if code in (500, 503):
        return "Google's Gemma model is overloaded right now; try again in a moment."
    return "Could not reach Google AI Studio."


def local_backend(section=None):
    """!@brief The Ollama backend, from llm_config.toml's optional [local] section."""
    section = section or {}
    return Backend(
        "local", section.get("url", OLLAMA_URL), section.get("model", OLLAMA_MODEL),
        generation_timeout=section.get("generation_timeout", LOCAL_GENERATION_TIMEOUT),
        sourcebook_grounding=section.get("sourcebook_grounding", True),
    )


def google_backend(section=None, environ=None):
    """!
    @brief The Google AI Studio backend, from llm_config.toml's optional [google] section. The key
        comes from the GEMINI_API_KEY environment variable first, then the section's api_key; an
        empty key still builds (the first request then fails with a clear message). No fallback
        models -- Google's endpoint has nothing like OpenRouter's "models" list.
    """
    section = section or {}
    environ = os.environ if environ is None else environ
    return Backend(
        "google", GOOGLE_URL, section.get("model", GOOGLE_MODEL),
        api_key=environ.get(GOOGLE_KEY_VARIABLE) or section.get("api_key", ""),
        adjudication_timeout=section.get("adjudication_timeout", GOOGLE_ADJUDICATION_TIMEOUT),
        generation_timeout=section.get("generation_timeout", ONLINE_GENERATION_TIMEOUT),
        sourcebook_grounding=section.get("sourcebook_grounding", True),
    )


def openrouter_backend(section=None, environ=None):
    """!
    @brief The OpenRouter backend, from llm_config.toml's optional [openrouter] section. The key
        comes from the OPENROUTER_API_KEY environment variable first, then the section's
        api_key; an empty key still builds (the first request then fails with a clear message).
    """
    section = section or {}
    environ = os.environ if environ is None else environ
    models = list(section.get("models") or OPENROUTER_FREE_MODELS)[:OPENROUTER_MAX_MODELS]
    return Backend(
        "openrouter", OPENROUTER_URL, models[0], tuple(models[1:]),
        api_key=environ.get(OPENROUTER_KEY_VARIABLE) or section.get("api_key", ""),
        adjudication_timeout=section.get("adjudication_timeout", OPENROUTER_ADJUDICATION_TIMEOUT),
        generation_timeout=section.get("generation_timeout", ONLINE_GENERATION_TIMEOUT),
        sourcebook_grounding=section.get("sourcebook_grounding", True),
    )


def load_backend(choice=None, config_path=CONFIG_PATH, environ=None):
    """!
    @brief The backend to run with: choice (a --llm flag) if given, else llm_config.toml's own
        "backend", else "local".
    @return The Backend.
    @raises ValueError for an unknown backend name; tomllib.TOMLDecodeError for a malformed file.
    """
    config = {}
    if config_path and os.path.exists(config_path):
        with open(config_path, "rb") as config_file:
            config = tomllib.load(config_file)
    name = choice or config.get("backend") or "local"
    if name not in BACKEND_NAMES:
        raise ValueError(f"Unknown LLM backend '{name}' -- expected one of {', '.join(BACKEND_NAMES)}.")
    if name == "openrouter":
        return openrouter_backend(config.get("openrouter"), environ)
    if name == "google":
        return google_backend(config.get("google"), environ)
    return local_backend(config.get("local"))


_current = local_backend()


def get_backend():
    """!@brief The backend every LLM call currently goes to."""
    return _current


def set_backend(backend):
    """!@brief Switches every LLM call to backend (boot-time, from load_backend)."""
    global _current
    _current = backend


def describe(backend):
    """!@brief One status line naming where narration will come from, for the boot log/GUI."""
    if backend.name == "local":
        return f"LLM: local Ollama ({backend.model})."
    service, variable = {
        "google": ("Google AI Studio", GOOGLE_KEY_VARIABLE),
        "openrouter": ("OpenRouter", OPENROUTER_KEY_VARIABLE),
    }.get(backend.name, (backend.name, "an API key"))
    models = ", ".join((backend.model, *backend.fallback_models))
    if not backend.api_key:
        return f"LLM: {service} ({models}) -- no API key found. Set {variable} or api_key in llm_config.toml."
    grounding = "" if backend.sourcebook_grounding else "; sourcebook excerpts off"
    return f"LLM: {service} ({models}{grounding})."
