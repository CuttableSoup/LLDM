import os
import re
import tkinter as tk
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch
import resolution.Combat_Resolution as Combat_Resolution
from dm.DM_Core import DMCore
from tests.event_contract import ValidatingEventBus
from llm import LLM_Client
from llm.LLM_Core import LLMCore
from nlp.NLP_Core import NLPCore, SentenceTransformerMatcher
import resolution.Ability_Effects as Ability_Effects


@contextmanager
def scripted_llm(transport=None, **mock_options):
    """!
    @brief The scripted adapter at LLM_Client's synchronous seam (see llm/LLM_Decision.py): while
        the block runs, every structured decision is answered by transport -- any callable
        taking (api_url, messages, **options) and returning a parsed response body -- or, with no
        transport, by a MagicMock built from mock_options (return_value=..., side_effect=...).
    @return The transport actually installed, so a test can assert on its calls.
    """
    transport = transport if transport is not None else MagicMock(**mock_options)
    previous = LLM_Client.set_transport(transport)
    try:
        yield transport
    finally:
        LLM_Client.set_transport(previous)


def script_llm(test, transport):
    """!@brief scripted_llm for the rest of one test -- undone by addCleanup, so no `with` block is needed."""
    previous = LLM_Client.set_transport(transport)
    test.addCleanup(LLM_Client.set_transport, previous)
    return transport


def _new_tk_root_with_retry(attempts=3, delay=0.5):
    """!
    @brief Constructs a real tk.Tk() root, retrying on TclError -- creating a Tk() root is,
        on this environment, an occasionally-flaky operation independent of how many other
        Tk() roots have been created in this process (observed even with only one Tk() root
        created in an entire test run, so it isn't purely a cumulative-churn issue reducing
        Tk() creations elsewhere already helps with, just a residual one worth retrying
        directly). Every TestCase class that needs a real Tk() root for setUpClass should
        call this instead of tk.Tk() directly, so a single transient failure doesn't error
        out an entire test class at once.
    @param attempts How many times to try before giving up and letting the last TclError raise.
    @param delay Seconds to wait between attempts.
    @return A real, constructed tk.Tk() instance.
    """
    import time
    for attempt in range(attempts):
        try:
            return tk.Tk()
        except tk.TclError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def apply_effects(core, result, skill_name, named_ability, ability, target_name, via_test=False, input_text=None):
    """!@brief Ability_Effects.apply_ability_effects for the player's own turn -- the one interface
        every ability-effect test crosses (resolution/Ability_Effects.py)."""
    Ability_Effects.apply_ability_effects(
        core.world, core.player_name, result, skill_name, named_ability, ability, target_name, via_test, input_text,
    )


# Rating an unopposed check's difficulty is a live model call (DMCore._untargeted_difficulty),
# never made from a unit test: every test sees the old difficulty-0 behavior, and the tests of
# the rating itself (TestUntargetedDifficulty) restore REAL_UNTARGETED_DIFFICULTY and stub the
# chat client instead.
REAL_UNTARGETED_DIFFICULTY = DMCore._untargeted_difficulty


_UNRATED_DIFFICULTY = patch.object(DMCore, "_untargeted_difficulty", lambda self, skill_name, input_text: (0, False))


# Likewise asking the model what an ambiguous line is (SentenceTransformerMatcher.adjudicate): every
# test sees "no answer", so the rules stand -- the corpus tests measure the rules alone.
# TestInputAdjudication covers the call itself against a stubbed chat client.
_NO_ADJUDICATION = patch.object(SentenceTransformerMatcher, "adjudicate", lambda self, *args, **kwargs: None)


def setUpModule():
    _UNRATED_DIFFICULTY.start()
    _NO_ADJUDICATION.start()


def tearDownModule():
    _UNRATED_DIFFICULTY.stop()
    _NO_ADJUDICATION.stop()


class DMTestCase(unittest.TestCase):
    """Shared setUp for tests that just need a fresh DMCore over a real scenario.
    Subclasses set scenario_name to pick which one, and override setUp (calling
    super().setUp() first) to also capture events or otherwise extend the fixture.
    start_location overrides which of "debug" scenario's own many areas to land in --
    None (the default) leaves it at "debug".toml's own start_location ("arena_grounds"),
    which is what every subclass that never overrides either attribute relies on."""
    scenario_name = "debug"
    start_location = "arena_grounds"
    setting = "Fantasy"

    def setUp(self):
        self.event_bus = ValidatingEventBus()
        self.dm_core = DMCore(
            self.event_bus, scenario_name=self.scenario_name, start_location=self.start_location,
            setting=self.setting,
        )

    def _stub_roll_dice(self, roll_result):
        """Forces every dice roll anywhere in the resolution graph (Combat_Resolution.py) to
        the same flat total, regardless of dice/pips -- the same convenience a bare
        `self.dm_core.roll_dice = lambda ...` gave back when roll_dice was still a DMCore
        method every call site reached through self. Patched at the module level since
        Combat_Resolution.py's own internal callers (resolve_action, calculate_damage, ...)
        now call the bare module function directly, not self.roll_dice -- restored via
        addCleanup so it can't leak into a later test."""
        original = Combat_Resolution.roll_dice
        Combat_Resolution.roll_dice = lambda dice, pips: roll_result
        self.addCleanup(setattr, Combat_Resolution, "roll_dice", original)

    def _load_ad_hoc_scenario(self, entities, bands=None, enclosed=True):
        """Swaps in a throwaway [[location]] (freeform if bands is None, else one
        [[location.room]] with the given bands/enclosed) and loads it -- the ad-hoc-scenario
        equivalent of directly authoring a scenario TOML file, for a test that just needs a
        specific, minimal entity roster rather than any of the real shipped scenarios. Mirrors
        DM_Rules.py's own [[location]] shape exactly, just built in Python instead of TOML."""
        if bands is None:
            location = {"key": "ad_hoc", "entities": entities}
        else:
            location = {
                "key": "ad_hoc", "start_room": "ad_hoc_room",
                "rooms": {"ad_hoc_room": {"key": "ad_hoc_room", "bands": bands, "enclosed": enclosed, "entities": entities}},
            }
        self.dm_core.locations = {"ad_hoc": location}
        self.dm_core.scenario = {"start_location": "ad_hoc"}
        self.dm_core.load_scenario()

    def _capture(self, event_name):
        events = []
        self.event_bus.subscribe(event_name, events.append)
        return events

    def _capture_any(self, *event_names):
        events = []
        for name in event_names:
            self.event_bus.subscribe(name, events.append)
        return events


class LLMTestCase(unittest.TestCase):
    """Shared setUp for tests that just need a fresh LLMCore with RAG disabled."""

    def setUp(self):
        self.event_bus = ValidatingEventBus()
        # rag_source_dir points at a real directory with no PDFs in it, so RagIndex's
        # background build returns immediately (see LLMCore.__init__'s docstring) instead of
        # every test here kicking off a real, potentially minutes-long index build against
        # whatever's actually in Settings/Fantasy/.
        self.llm_core = LLMCore(self.event_bus, rag_source_dir=os.path.join("Rules", "Fantasy"))


class FakeMatcher:
    """!
    @brief Test-only IntentMatcher adapter -- returns pre-configured (name, score) tuples for
        exact clause-text lookups instead of running any real embedding model, so
        TestIntentClassification can exercise IntentClassifier's own gate/precedence order at
        full speed, with no SentenceTransformer load. Unmapped text always misses (None, 0.0),
        the same "confidently below threshold" shape SentenceTransformerMatcher returns for
        genuinely unmatched input. Real adapter: NLP_Core.py's SentenceTransformerMatcher --
        two adapters is what justifies IntentMatcher as a real seam rather than a hypothetical
        one authored just in case.
    """

    def __init__(self, actions=None, items=None, targets=None, sentiments=None, threats=None, familiarities=None, modifiers=None, intents=None, destinations=None, present_entities=None, intent_override=0.65):
        self._actions = actions or {}
        self._items = items or {}
        self._targets = targets or {}
        self._intents = intents or {}
        self._destinations = destinations or {}
        # Address-phrase -> (present entity key, score), for the promotion gate's own
        # "is someone here already called that?" check (see map_to_present_entity).
        self._present_entities = present_entities or {}
        # Mirrors SentenceTransformerMatcher.intent_override_threshold, so the two-tier
        # strict= gate (IntentClassifier._route_intent) is exercisable with no model loaded.
        self._intent_override = intent_override
        self._sentiments = sentiments or {}
        self._threats = threats or {}
        self._familiarities = familiarities or {}
        # Modifier names to literally match/strip (see match_modifier) -- longest first, same
        # convention SentenceTransformerMatcher's own self.modifier_names keeps.
        self._modifier_names = sorted(modifiers or [], key=len, reverse=True)

    def on_rules_loaded(self, data):
        pass

    def register_item(self, name, description, targetable=False):
        pass

    def match_modifier(self, processed_text):
        for name in self._modifier_names:
            match = re.search(rf"\b{re.escape(name)}\b", processed_text)
            if match:
                stripped = processed_text[:match.start()] + processed_text[match.end():]
                return name, re.sub(r"\s+", " ", stripped).strip()
        return None, processed_text

    def map_to_action(self, processed_text):
        return self._actions.get(processed_text, (None, 0.0))

    def map_to_item(self, processed_text):
        return self._items.get(processed_text, (None, 0.0))

    def map_to_target(self, processed_text):
        return self._targets.get(processed_text, (None, 0.0))

    def map_to_intent(self, processed_text, strict=False):
        intent, score = self._intents.get(processed_text, (None, 0.0))
        if strict and score < self._intent_override:
            return None, score
        return intent, score

    def set_destinations(self, destinations):
        pass

    def map_to_destination(self, processed_text):
        return self._destinations.get(processed_text, (None, 0.0))

    # {processed text: verdict} -- set directly by a test; anything unlisted gets no answer. A
    # verdict is a kind ("speech") or the full {"kind", "game_action", "item"} dict.
    adjudications = {}

    def adjudicate(self, text, present_names=(), partner=None, recent_narration=""):
        self.adjudicated = getattr(self, "adjudicated", []) + [(text, tuple(present_names), partner, recent_narration)]
        return self.adjudications.get(text)

    def set_present_entities(self, entities):
        pass

    def map_to_present_entity(self, processed_text):
        return self._present_entities.get(processed_text, (None, 0.0))

    def classify_sentiment(self, processed_text):
        return self._sentiments.get(processed_text, (None, 0.0))

    def classify_threat(self, processed_text):
        return self._threats.get(processed_text, (None, 0.0))

    def classify_familiarity(self, processed_text):
        return self._familiarities.get(processed_text, (None, 0.0))


class _RecordingBus(ValidatingEventBus):
    """!@brief A ValidatingEventBus that also keeps every event it publishes, in order."""

    def __init__(self):
        super().__init__()
        self.log = []

    def publish(self, event_type, message=None):
        self.log.append((event_type, message))
        super().publish(event_type, message)


def _llm_unreachable(*args, **kwargs):
    raise ConnectionError("no model in a scripted session unless the test scripts one")


class ScriptedSession:
    """!
    @brief The whole offline pipeline in one object: a real DMCore and NLPCore on a contract-checking
        bus, classification by a FakeMatcher the test scripts, and the LLM a scripted transport (by
        default one that is unreachable, so every structured decision answers "unavailable" and the
        rules stand). No model load, no network. say() is the one interface: type a line, get back
        everything published because of it, in order.

        The lines the matcher is scripted with are exact (see FakeMatcher: it looks up the processed
        clause text), so a test scripts just the lines it types. This does not replace the
        real-model corpus tests, which measure classification quality.
    """

    def __init__(self, test, matcher=None, llm=_llm_unreachable, scenario_name="debug",
                 start_location="arena_grounds", setting="Fantasy"):
        self.bus = _RecordingBus()
        self.matcher = matcher or FakeMatcher()
        script_llm(test, llm)
        self.nlp = NLPCore(self.bus, self.matcher)  # first: it must hear rules_loaded
        self.core = DMCore(self.bus, scenario_name=scenario_name, start_location=start_location, setting=setting)

    def say(self, text):
        """!@return Every (event_name, payload) published because the player typed text, in order."""
        start = len(self.bus.log)
        self.bus.publish("user_input_submitted", text)
        return self.bus.log[start:]

    @staticmethod
    def names(events):
        """!@return Just the event names of say()'s result, in order."""
        return [name for name, _ in events]

    @staticmethod
    def payloads(events, name):
        """!@return The payloads of one kind of event in say()'s result."""
        return [payload for event, payload in events if event == name]
