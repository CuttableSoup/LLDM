# LLDM — Testing

Part of the [LLDM](../CLAUDE.md) docs — the offline/live test split and the Textual headless mirror.

## Textual mirror (headless testing)

`Textual_Core.py` subscribes to the same events `GUI_Core` displays and adds its own `Input`
widget publishing `user_input_submitted`, so the app can be driven and asserted on headlessly
(`app.run_test()`/`Pilot`) without Tkinter or a display.

Practical constraints when touching this file:
1. Don't name an attribute `self._ready` — Textual's `App` reserves that name internally.
2. Pre-mount events (`DMCore` publishes `rules_loaded` synchronously during `__init__`, which
   can precede `compose()`) are buffered and flushed in `on_mount`.
3. `RichLog.lines` only reflects content once its tab is active — activate it
   (`tabbed_content.active = "tab_id"`, then `await pilot.pause()`) before reading a background
   tab.
4. Writes can arrive from a foreign thread (`LLMCore`'s background fetch). `call_safely` wraps
   everything through `self.call_from_thread`, falling back to a direct call.
5. Pilot has no `.type()` in the installed Textual version (8.2.8) — build a key list (`["space"
   if c == " " else c for c in text]`) and pass it to `pilot.press(*keys)`.
6. Joining a background thread from an `async def` must go through `await
   asyncio.to_thread(thread.join)`, not a bare `t.join()`, or the event loop deadlocks.


## Testing

- **`test_unit.py`** (`tests/`) — offline `unittest.TestCase` classes: one representative test per
  genuinely distinct mechanism/branch, not one per edge case or flavor variant of an
  already-covered code path. `TestGameBoot` and `TestNlpConfidenceThreshold` load the real
  `sentence-transformers` model via `setUpClass`, narrowed to what actually needs it
  (confidence-threshold/keyword-fallback scoring, real embedding registration).
  `TestIntentClassification` covers `Intent_Classification.py`'s gate/precedence order instead
  — `IntentClassifier` exercised directly against `FakeMatcher` (a canned `IntentMatcher`
  double defined alongside it), no model load, EventBus, or DMCore needed. Most other classes
  share fixture setup via `DMTestCase` (`scenario_name` class attribute, plus
  `_capture`/`_capture_any` helpers) and `LLMTestCase`.
- **`test_integration.py`** (`tests/`) — every test needing a real, running Ollama, gated on
  `_ollama_reachable()` so they skip together when nothing's listening on `127.0.0.1:11434`.
  Nothing has to already be running, though: importing this module calls
  `Ollama_Launcher.ensure_ollama_running()` once, synchronously, before any skip gate evaluates
  (module-level code, not a fixture, since the gates themselves are `@unittest.skipUnless`/
  `@pytest.mark.skipif` decorators evaluated at import time) — the same bootstrap `LLDM.py`'s own
  `main()` runs, just blocking here instead of backgrounded so the gates see a live server by the
  time they check, and torn back down at process exit via the same "only ever terminate the
  process this call itself started" `atexit` precedent `main()` uses. `_LivePipelineTestCase`'s
  own optional `character` class attribute is forwarded straight into
  `DMCore`'s `character` param. `TestNpcGenerationLive` is a plain `unittest.TestCase` (no
  NLPCore/LLMCore) since NPC generation runs synchronously during `DMCore`'s own construction —
  a real tool-calling round trip. The pure fitting math and DMCore-side wiring both live in
  `test_unit.py` instead (patching `NPC_Generation._real_call_chat_completion` with a
  deterministic fake), so most of NPC generation stays covered by the fast offline suite — only
  the "does the configured model actually return a valid tool call" question needs a live
  Ollama.

`python -m pytest -q` runs both files; `python -m pytest -q tests/test_unit.py` runs the fast,
offline subset only.

