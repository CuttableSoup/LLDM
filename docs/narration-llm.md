# LLDM — Narration and LLM Integration

Part of the [LLDM](../CLAUDE.md) docs — narration triggers, LLM backends (Ollama/OpenRouter), RAG grounding.

## Narration

`LLMCore` subscribes to narration-relevant events, sharing outcome-text building
(`describe_outcome` — also the one place that turns a successful summon's own `SummonEffect`
into an actual narrated line, "Summoning" above) and background-fetch plumbing
(`_queue`/`_fetch_and_publish`):
- `scenario_loaded` → `generate_scene_intro` — once, from `DMCore.__init__`.
- `round_resolved` → `generate_round_response` — combat, once per round.
- `action_resolved` → `generate_response` — non-combat, once per skill use.
- `action_not_understood` → `generate_clarification_response` — only for a line with nothing to
  resolve (`reason` "musing", or no reason): a short in-character acknowledgment. An *attempt* the
  engine couldn't resolve (`reason` "unresolved_action", "unmatched", "no_seller",
  "improvisation_declined"/"_unavailable"), and an item denial of `not_present`/`no_recipient`,
  is never narrated: `failed_attempt` sends a `player_notice` (`FAILED_ATTEMPT_MESSAGES`)
  that the GUIs show as a `[System]` line and the player rephrases. Nothing enters the context
  window and no time passes — found by playtest: an unresolved "I'll buy the lantern" was
  narrated as the shopkeeper handing it over, which then stood in history as if it happened.
- `item_interaction_resolved` → `generate_item_interaction_response` — covers examine/take/give/
  trade/open/close/use/equip/unequip/drop, room transitions, and location-to-location travel.
- `dialogue_resolved` → `generate_npc_dialogue` — a found target routes through
  `_queue`; a denied one falls back to an ordinary `_queue` explanation.
- `game_load_failed` → `generate_load_failed_response`.
- `help_resolved` → `generate_adam_response` — routes through `_queue`, the one
  trigger here that never touches `context_window` at all.
- `scene_query_resolved` → `generate_scene_query_response` — a free-standing "what do I see"/
  "who is here" question (see `docs/adam-improvisation.md`'s "Scene queries"), routed through
  `_queue`; answered in the ordinary GM voice, grounded the same strict way ADaM is,
  and (unlike ADaM) does join `context_window`.
- `encounter_triggered` → `generate_encounter_response` — a location/room's own random
  encounter roll (see "Random encounters"), the one trigger here that's never a response to
  something the player did.

The scenario/room setting and character roster are re-injected into the system message on every
request, so narration stays grounded even after the intro scrolls out of the rolling 100-message
`context_window`. So is `location_rule`: the player is at the engine's own current scene
(`scene_roster_updated`'s `"scene_name"`) and stays there — only the game moves the player — with
the location's real exits listed (`location_exits_updated`). Exempt only for a real
`item_interaction:move`/`travel`, whose whole job is arriving. Without it the narrator walked a
playtest's player into a tavern and then underground ruins over a few clarification/skill replies
while the engine never left the market, so every later turn described a place the game wasn't in.

Every skill/round narration (`describe_player_actions`) also tells the narrator to narrate what
the player actually wrote — the skill only names the dice — and that the player's gear is exactly
the payload's `"player_gear"` (DMCore's equipped items). A playtest's "grab the finest jar of
spices" mismatched to *polearms*; told only "Skill used: polearms", the narrator handed over a
polearm the player never owned, and the player LLM swung it for fifteen turns. `generate_npc_dialogue`'s own system message (built by
`dialogue_system_message`) is different in kind, not just content — it speaks as the
addressed entity, grounded in `persona`/`attitude` plus that entity's own presence-filtered
history, never the standing GM framing.

Every reply has "the user"/"the user's" rewritten to "you"/"your" (`address_player_as_you`)
before it's shown or stored. The chat API calls the player "user", and one "Finn stares at the
user" in the history was imitated by nearly every reply for thirty turns.

**Denial-path grounding.** Every trigger above that can fire with *nothing* real to narrate —
`generate_clarification_response` (no action matched at all), `generate_item_interaction_response`
's own `"found": false` branch (a locked/absent/unaffordable item), and `generate_npc_dialogue`'s
own `"found": false` branch (no one by that name present) — is the one place the model has the
least real state to work from, and so the easiest place for it to fill the gap with an invented
person, item, or explanation (ex: narrating that an unaddressable NPC "just stepped away with
someone" rather than simply saying they aren't here). Each of these three prompts explicitly
forbids that rather than relying on the standing system message alone, the same
belt-and-suspenders convention `generate_load_failed_response`'s own "without inventing what the
save might have contained" and `"open"`'s own "describing only what's actually there" already
followed before this was made consistent across every denial path. This is deliberately a
per-prompt discipline, not a blanket system-message rule — ordinary narration (a resolved roll, a
combat round, an opened container's real contents) already has real state behind it and loses
nothing by staying free to add sensory color; only the "nothing resolved" paths needed the
explicit guardrail. Scene query and ADaM (above) already carried this same discipline from the
day they shipped, being built around exactly this risk from the start.

Every `_queue`/`_queue` call's background fetch also publishes
`llm_debug_updated {"query", "response"}` alongside `llm_response_ready` — consumed only by
`GUICore`'s Debug tab, never stored in `context_window` itself.

**Publishing order.** Each fetch runs on its own thread, so a short prompt can come back before a
longer one queued ahead of it (found by playtest: a guard's arrest demand was narrated before the
attack it was about). Every queue site takes a ticket on the game thread
(`_take_publish_ticket`), and `_fetch_and_publish` holds its reply until every earlier ticket has
published (`_publish_in_order`), so replies — and the assistant turns appended to
`context_window` — land in the order they were queued. A reply waits at most
`PUBLISH_ORDER_TIMEOUT` (90s) for an earlier one, so one hung request can't stall the game.
`scene_narration_ready`'s extraction runs after the ordered part, never holding up the next reply.


## LLM integration

**Backends.** Every request goes to the current backend — `LLM_Backend.py`'s `get_backend()`,
picked at boot by `load_backend` (`--llm local|google|openrouter` on `LLDM.py`/`tools/playtest.py`,
else `llm_config.toml`'s `backend`, else `local`; the file is gitignored, and
`llm_config.example.toml` is its template). All three speak the same OpenAI-style
chat/completions API, so a `Backend` only differs in URL, model, key and a few request fields
(`Backend.payload`/`headers`): `LLM_Client.call_chat_completion` (reached by every structured decision through
`LLM_Decision.decide` and the `LLM_Client.chat_completion` seam) and `LLMCore._request_completion` both read it per request.

- `local` — Ollama, launched (and if needed installed) by `Ollama_Launcher.py`, below.
- `google` — Gemma 4 on Google AI Studio's OpenAI-compatible endpoint, for a machine that can't
  run a model; no Ollama is started at all. Key from `GEMINI_API_KEY`, else `[google].api_key`.
  Only the large Gemma 4 models are served (`gemma-4-26b-a4b-it`, the default — a mixture of
  experts with ~4B active per token, ~1s per adjudication and 1.5–1.7s per narration — and
  `gemma-4-31b-it`; every smaller name 404s). Every request carries `reasoning_effort: "minimal"`:
  Google rejects `"none"` for Gemma, and with no setting it thinks for 300–700 tokens a call
  (7–16s adjudications, narration opening with a `<thought>` block). No fallback models.
- `openrouter` — free models through OpenRouter; no Ollama is started either. The key
  comes from `OPENROUTER_API_KEY`, else the file's `[openrouter].api_key`. Requests carry
  OpenRouter's `models` fallback list (at most three, tried in order when one is rate-limited
  or down — default `OPENROUTER_FREE_MODELS`, checked 2026-10-05) and its own
  `reasoning: {enabled: false}` on every request, narration included (with reasoning on, a free
  model's thinking came back as the narration), in place of the `reasoning_effort: "none"` Ollama
  takes. Free Gemma 4 here runs on one quota shared by every OpenRouter user, usually exhausted.
  Not `openrouter/free`: it picks a different model per call, and about one in six rejected
  reasoning off.

Input adjudication waits `adjudication_timeout` (1.5s local, 2.5s online — the network round
trip); ad hoc generation waits `generation_timeout` (12s local, where calls take 4–8s; 8s online). A failed online request tells the player why (`Backend.failure_message`: a free quota
used up, a rejected key, busy models). `sourcebook_grounding = false` keeps RAG excerpts
(below) out of prompts, since online they reach a third party.

Locally, the endpoint is Ollama's OpenAI-compatible API (`http://127.0.0.1:11434/v1/chat/completions`,
`ollama serve`'s default). Ollama can have several models pulled at once, so
every request payload carries an explicit `"model"` field — the backend's own (`LLM_Backend.py`'s
`OLLAMA_MODEL`, "gemma4"; `LLM_Client.py`'s `DEFAULT_MODEL` only covers an explicit URL that isn't
the backend's). `/v1/models`
lists every locally pulled model (Ollama's native `/api/tags` is the same catalog, non-OpenAI-
shaped); a chat completion against a model name that hasn't been pulled 404s rather than
falling back to whatever's loaded.

**Context budgeting.** The model's own context window covers the prompt and the reply
*together*, and this endpoint gives no way to raise it — Ollama's `/v1/chat/completions`
silently ignores `num_ctx` (verified against a live server: passing it changes neither
`prompt_tokens` nor the cap), so the only lever on this side of the wire is keeping the prompt
small enough to leave room to answer inside it. `context_window`'s own cap is **100 messages**,
which has no idea how big a message is, so a long session grew prompts to ~4000 tokens against
a 4096 ceiling and left ~95 for the reply: every narration came back
`finish_reason="length"` — silently truncated mid-sentence, which nothing was checking for —
and roughly half the time the model spent that sliver without emitting any content at all and
returned an empty string, which was then published verbatim as the turn's narration *and*
appended to `context_window` as an assistant turn. A blank turn from a pipeline that had
resolved the action perfectly well. `_fit_history` fixes the cause, dropping the oldest entries
until the system message plus what's left clears `RESPONSE_TOKEN_RESERVE`; it's a separate axis
from the 100-message cap rather than a replacement, since that one bounds what the game
*remembers* while this bounds what any single request *sends* (a long scene keeps its full
history for `_filter_present_history` and later turns, it just stops putting all of it on the
wire at once). At least one entry always survives even if it alone blows the budget — sending
the prompt that actually prompted this turn and letting the model truncate beats sending a bare
system message with no player action in it. The reserve is measured, not guessed: an ordinary
2-3 sentence narration runs ~550 completion tokens, and capping at 512 visibly truncated one
mid-sentence. Behind that, `_fetch_and_publish` retries once on an empty completion, then once more
with only the system message and this turn's own prompt (a playtest got two instant empties on a
14 KB request, which wasn't a starved context, and an identical retry just repeated it). If it is
still empty, it publishes a `System:` notice rather than a blank turn and **never stores it in
`context_window`** — an empty assistant turn isn't something the scene witnessed, and keeping it
would spend budget on nothing and teach the model that empty replies belong here. This is a
per-symptom guard, not a substitute for the budget: if empties start appearing again, the prompt
has outgrown its room and `_fit_history` is what needs revisiting.

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
(`LLM_Core.py`'s own existing best-effort path) until the bootstrap catches up. The bootstrap
only runs for the `local` backend; `LLDM.py` posts `LLM_Backend.describe`'s one-line status
(which backend and models, or a missing OpenRouter key) either way.

An Ollama this module spawns runs with `SERVER_ENVIRONMENT` (a value the user already set wins):
`OLLAMA_NUM_PARALLEL=2`, so input adjudication never queues behind a narration still generating
(measured 5.5–6.3s queued vs 0.3–0.4s), and `OLLAMA_KEEP_ALIVE=-1`, so the model isn't unloaded
after five idle minutes (a reload takes ~9s, far past the adjudication timeout).


## RAG / sourcebook grounding

`LLM_Rag.py`'s `RagIndex` indexes every `*.pdf` under `Settings/Fantasy/` by default (a
gitignored directory), building its index on a daemon background thread; `query()` returns `[]`
until `self.ready` is `True`. Chunks/embeddings are cached to
`Settings/Fantasy/.rag_cache/<hash>.{chunks.json,embeddings.npy}`, keyed by a hash of every
source PDF's path/size/mtime. `LLMCore.set_setting(setting)` (`LLDM.py`'s `start_game`, before
`DMCore` is constructed) repoints this at `Settings/<setting>/` instead — each setting keeps its
own sourcebooks/cache (ex: `Settings/Pathfinder/` holds the Golarion PDFs backing the
`lost_coast` scenario's Sandpoint/Magnimar content, since those never shipped generic-Fantasy
lore in the first place); a setting with no matching `Settings/<setting>/` directory just yields
an empty index, same as an empty/missing `Settings/Fantasy/` would.

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


## The scene roster

`system_message` injects `" Characters: " + join(self.scenario_characters)` into **every**
narration system message. That attribute used to be assigned only by `generate_scene_intro` (on
`scenario_loaded`, once per playthrough) and `load_state` — so it described the scenario's
*starting* scene forever: walk from Sandpoint's market into the tavern and every later narration
was still told the market's cast was standing there.

`DM_Rules.py`'s `_publish_scene_roster` fixes that with a `scene_roster_updated` event, published
from every site that mutates `scenario_entities` — `_enter_location`, `enter_room`, ad hoc
placement and removal, encounters, summoning, `load_game`, plus a cheap backstop at the top of
DMCore's own turn and dialogue handlers. A dirty guard on the produced prose roster is what makes
calling it freely affordable, and means a future mutation site that forgets its hook degrades to
"stale until the player's next turn" rather than breaking. It's keyed on the prose rather than the
name list deliberately, so an entity edit that only rewrites a description still reaches the
narrator.

The payload also carries `"population"` (`{sentences, hint}`), which LLMCore reads in
`scene_length_instruction` to write scene-setting narrations longer, and to ask for a populated
scene, in locations that opt in to narration-driven population (`docs/npc-generation.md`); it is
part of the dirty key, so moving between an opted-in and an ordinary location republishes even if
the cast is unchanged.

The payload's `"characters"` half is LLMCore's (`_on_scene_roster_updated`, which does nothing
but repoint `scenario_characters`); its `"entities"` half is NLPCore's, feeding
`set_present_entities` (see `docs/adam-improvisation.md`'s "Promotion on reference"). Deliberately
its own event rather than another key on each narration payload: there are ~10 such publish sites
sharing no helper, and each would recompute `describe_character` for the whole scene every turn
regardless of whether anything changed.
