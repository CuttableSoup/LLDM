# LLDM — Narration and LLM Integration

Part of the [LLDM](../CLAUDE.md) docs — narration triggers, Ollama wiring, RAG grounding.

## Narration

`LLMCore` subscribes to narration-relevant events, sharing outcome-text building
(`_describe_outcome` — also the one place that turns a successful summon's own `SummonEffect`
into an actual narrated line, "Summoning" above) and background-fetch plumbing
(`_queue_narration`/`_fetch_and_publish`):
- `scenario_loaded` → `generate_scene_intro` — once, from `DMCore.__init__`.
- `round_resolved` → `generate_round_response` — combat, once per round.
- `action_resolved` → `generate_response` — non-combat, once per skill use.
- `action_not_understood` → `generate_clarification_response` — acknowledges input that didn't
  resolve to any action.
- `item_interaction_resolved` → `generate_item_interaction_response` — covers examine/take/give/
  trade/open/close/use/equip/unequip/drop, room transitions, and location-to-location travel.
- `dialogue_resolved` → `generate_npc_dialogue` — a found target routes through
  `_queue_dialogue`; a denied one falls back to an ordinary `_queue_narration` explanation.
- `game_load_failed` → `generate_load_failed_response`.
- `help_resolved` → `generate_adam_response` — routes through `_queue_adam_response`, the one
  trigger here that never touches `context_window` at all.
- `encounter_triggered` → `generate_encounter_response` — a location/room's own random
  encounter roll (see "Random encounters"), the one trigger here that's never a response to
  something the player did.

The scenario/room setting and character roster are re-injected into the system message on every
request, so narration stays grounded even after the intro scrolls out of the rolling 100-message
`context_window`. `generate_npc_dialogue`'s own system message (built by
`_build_dialogue_system_message`) is different in kind, not just content — it speaks as the
addressed entity, grounded in `persona`/`attitude` plus that entity's own presence-filtered
history, never the standing GM framing.

Every `_queue_narration`/`_queue_dialogue` call's background fetch also publishes
`llm_debug_updated {"query", "response"}` alongside `llm_response_ready` — consumed only by
`GUICore`'s Debug tab, never stored in `context_window` itself.


## LLM integration

Endpoint is Ollama's OpenAI-compatible API (`http://127.0.0.1:11434/v1/chat/completions`,
`ollama serve`'s default). Ollama can have several models pulled at once, so
every request payload carries an explicit `"model"` field — `LLM_Client.py`'s own
`DEFAULT_MODEL` ("gemma4") and `LLM_Core.py`'s own `self.model`, each independently, mirroring
the same intentional non-sharing `_save_slot_dir`'s own module note documents. `/v1/models`
lists every locally pulled model (Ollama's native `/api/tags` is the same catalog, non-OpenAI-
shaped); a chat completion against a model name that hasn't been pulled 404s rather than
falling back to whatever's loaded.

`Ollama_Launcher.py`'s `ensure_ollama_running` is a best-effort local server bootstrap, called
once from `LLDM.py`'s own `main()` on a background daemon thread, started right after `GUICore`
is constructed (before `NLPCore`/`LLMCore`/`DMCore`) — specifically so its window already exists
for the thread's own log callback to report progress into (see "Booting the game"): a fast no-op
if something's already listening at `127.0.0.1:11434`. Otherwise it resolves an `ollama.exe` to
run —
preferring a real system install (`shutil.which("ollama")`) over a vendored one, so installing
Ollama for real later transparently takes over from a downloaded copy — and if neither exists
at all, downloads Ollama's own official portable Windows build straight from its GitHub
releases (`ollama-windows-amd64.zip`, resolved via the stable `.../releases/latest/download/...`
URL, so this always tracks whatever's currently latest) into `vendor/ollama/`, a gitignored,
per-machine directory exactly like `Saves/` — never committed, never shipped in the repo. The
download is verified against Ollama's own published `sha256sum.txt` before extracting;
`os.walk`-based `_find_executable` locates `ollama.exe` inside the extracted tree without
assuming a particular zip layout. Windows-only by design (`ollama.exe`, the win_amd64 asset,
`CREATE_NO_WINDOW`) — matches this project's own current platform (win32).

Once an executable is resolved, `ensure_ollama_running` spawns `ollama serve` and returns
immediately — deliberately not blocking on the new *process* actually becoming ready, since
`NLPCore`'s own `sentence-transformers` model load (the very next boot step, ~15-20s) already
gives a freshly-spawned Ollama plenty of time to come up in the background. The one-time
*install* step, by contrast, blocks whatever called `ensure_ollama_running` — there's no "just
try again later" fallback for a binary that doesn't exist on disk yet, and this only ever runs
once per machine (every later launch finds the already-extracted executable first). Because a
fresh machine has to download the ~1.5GB Ollama binary plus, by default, a ~9.6GB model pull
(`gemma4`'s own `:latest`/E4B tag) before this call would otherwise return, `main()` runs the
entire `ensure_ollama_running` call on a background daemon thread rather than blocking its own
startup on it — see "Booting the game" for why nothing in the app actually needs it to have
finished before a game can start. Every failure mode (no network, a failed checksum, an
unwritable `vendor/`, the process failing to launch) just logs and lets the app continue exactly
as it already would with no Ollama available at all — the same best-effort posture every other
LLM integration point in this codebase already follows. `main()` registers an `atexit` cleanup
that terminates the spawned process, but only the one this call itself started (checked via a
`nonlocal` variable the background thread assigns once `ensure_ollama_running` returns — `None`
until then, so a shutdown racing the bootstrap simply has nothing yet to clean up) — a
pre-existing Ollama instance (started by hand, or by another app) is never touched.

A running server alone doesn't mean narration will work — a chat completion against a model
name that hasn't been pulled 404s (see this section's own opening paragraph), so
`ensure_ollama_running` also calls `_ensure_model_pulled` right after resolving/spawning a
server, whichever branch reached that point. Unlike the server spawn itself, this step *does*
wait (up to `ready_timeout`, default 15s) for the server to actually answer — there's no way to
know what's pulled, let alone pull something missing, without talking to it — then checks
`GET /api/tags` (Ollama's own native listing, not the OpenAI-compat one) and, if `model` (default
`DEFAULT_MODEL`, `"gemma4"` — kept in sync by hand with `LLM_Client.py`/`LLM_Core.py`'s own same-
named defaults, the same duplicated-not-shared convention as everything else in this module)
isn't listed, streams `POST /api/pull` and relays Ollama's own NDJSON progress lines through
`log`, throttled to roughly every 10% per phase so it reads as a progress bar rather than a
flood. `_model_already_pulled` treats a bare request name (`"gemma4"`) as matching its own
implicit `":latest"` tag, since `/api/tags` always reports one even when none was given at pull
time. Every failure here (server never comes up, network error mid-pull, an unknown model name)
is the same best-effort "log and give up" as everything else in this module — the app's own
existing "Could not connect to the local LLM"/404 handling is still the real fallback if a model
genuinely never gets pulled.

`ensure_ollama_running`'s own `log` callback, as wired by `LLDM.py`'s `main()`, reports status
two ways: `event_bus.publish("log_info", ...)` (`Logger.py`'s ordinary console mirror) and
`GUICore.display_system_status` (a `"[System] ..."` line in the History pane, the same prefix
convention `display_game_saved`/`display_game_loaded`/`display_game_load_failed` already use).
`GUICore.display_system_status` is why it's constructed first among the three event-subscribing cores in
`main()` (ahead of `NLPCore`/`LLMCore`) rather than last — the background bootstrap thread's own
closure over `gui_core` needs it to already exist the moment the thread starts, and starting the
thread this early lets the window reach `mainloop()` (see `gui_core.start()`) without
waiting on `NLPCore`'s own ~15-20s model load either. The bootstrap thread reports progress
while `mainloop()` is already running, so the running loop picks up each history-pane update on
its own, the same way `LLM_Core.py`'s own background narration fetches touch `GUICore` from a
foreign thread. This is safe because none of `GUICore`'s own subscriptions
(`llm_response_ready`, `rules_loaded`, ...) can fire this early regardless of thread timing —
nothing publishes them until `DMCore` exists, and `DMCore` isn't constructed until well after
this point (see "Booting the game"). One consequence worth naming: the player can open
Character → Create... and start a scenario while the Ollama bootstrap is still mid-download —
narration during that window degrades to "Could not connect to the local LLM"
(`LLM_Core.py`'s own existing best-effort path) until the bootstrap catches up.


## RAG / sourcebook grounding

`LLM_Rag.py`'s `RagIndex` indexes every `*.pdf` under `Settings/Fantasy/` (a gitignored
directory), building its index on a daemon background thread; `query()` returns `[]` until
`self.ready` is `True`. Chunks/embeddings are cached to
`Settings/Fantasy/.rag_cache/<hash>.{chunks.json,embeddings.npy}`, keyed by a hash of every
source PDF's path/size/mtime.

Chunking is sentence-bounded (`_chunk_page_text`, capped at `MAX_CHUNK_WORDS`=180, dropping
fragments under `MIN_CHUNK_WORDS`=40). Retrieval is per-request, appended to that request's
system message only — never stored in `context_window`. `perform_rag` returns no chunks below
`confidence_threshold` (`0.3`).

The RAG query is the player's own raw input, not the full instruction-padded narration prompt —
embedding the padded prompt dilutes similarity enough to miss lore a bare-input query would find.
`generate_scene_intro` passes the scenario name+description instead (no player input exists
yet); `generate_load_failed_response` falls back to its own full prompt.

`vectorize_pdf.py` is a standalone CLI that builds this same cache ahead of time: `python
vectorize_pdf.py [pdf_or_dir] [--query "..."]`, defaulting to `Settings/Fantasy/`. Reuses
`RagIndex` directly via `RagIndex.wait_until_ready()`.

