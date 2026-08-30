# LLDM — Social, Attitudes, and Dialogue

Part of the [LLDM](../CLAUDE.md) docs — the three-axis attitude model and free-form talk.

## Social and attitudes

`get_attitude(entity, toward)` returns a three-value array (`disposition, threat, familiarity`,
nominally -100..100; a `name` override beats `supertype` beats `default`; no
`[entity.attitudes]` table defaults to all-neutral). Collapsed from an original six
(`disposition, trust, confidence, respect, obligation, intimacy`) after NLI zero-shot testing
(see "Dialogue sentiment") found only three axes reliably separate from each other when read off
dialogue tone — `confidence`/`intimacy` were kept and renamed `threat`/`familiarity` (same
sign/semantics: `threat` positive = safe/confident, negative = threatened/afraid; `familiarity`
positive = close/fond, negative = distant/repulsed); `trust` never separated from disposition,
`respect` collapsed back into disposition under testing, and `obligation` turned out to be
structurally event-driven rather than tone-driven (a debt/favor is a fact about what happened,
not a quality of how something was said) — see "Extended goals" for the fuller testing writeup.
`get_attitude_tier(value)` clamps to `[-150, 150]` and returns the first of seven
`[[attitude_tier]]` bands whose range contains it, in declaration order.
`describe_attitude(entity, toward)` renders all three axes as one sentence using each tier's own
phrase per axis.

`describe_character(entity_name, toward_name=None)` builds a flavor-text roster line from purely
descriptive TOML fields (`description`, `qualities`, `memories`, `quotes`) plus, when
`toward_name` is given, the attitude sentence above — deliberately excluding mechanical data.
`DMCore.__init__` builds this roster into the `scenario_loaded` payload; `_on_turn_detected` also
appends a fresh `DefenderDetailsEffect` to each `RolledOutcome`'s own `effects`.

`self.player_name` is resolved once in `__init__` via `_resolve_player_name()`, which scans
loaded templates for the one with `is_player = true` and raises `ValueError` if none is marked.

**Action-driven attitude drift.** A resolved player action — landing a hit, stealing something,
giving something away — nudges the target's own three-axis attitude toward the player, the same
"a 0..1 confidence/severity signal scales a per-axis delta" shape dialogue sentiment already
uses (below), just driven by what happened rather than tone of voice, and moving more than one
axis at once. `rules.toml`'s `[[attitude_event]]` table holds each event's own *full-strength*
per-axis deltas (`combat_hit`, `theft`, `favor`, `shared_enemy` today) — applied at
`magnitude = 1.0` (ex: a killing blow, or the single most valuable item `items.toml` authors);
an ordinary occurrence scales down from there. Each event authors only `disposition`/`threat`/
`familiarity` deltas now — `shared_enemy` in particular lost its only two non-disposition deltas
(`trust`/`respect`) when those axes were dropped, so it's disposition-only today; `favor` lost
its single largest value (`obligation = 20`), leaving a comparatively modest `familiarity` bump
in its place — an accepted consequence of the axis collapse, not rebalanced to compensate.
`DM_Social.py`'s `nudge_attitude_from_event(entity_name, toward_name, event_name, magnitude)`
looks up the named event and writes the scaled deltas into their own `action_attitude_deltas`
accumulator (`get_attitude` sums it elementwise alongside `attitude_deltas`, same as before) — a
no-op for an unknown event, a falsy magnitude, an entity with no `[entity.attitudes]` table at
all, an inanimate object (`supertype == "object"`), or an entity with no HP left (a dead entity
isn't aware of anything happening to it or nearby anymore, whether that's the killing blow
itself, a theft, a gift, or a battlefield bond forming), mirroring `is_hostile`'s own "nothing to
nudge" precedent for a tableless creature.

Four call sites, each computing its own 0..1 magnitude from context: `DM_Core.py`'s
`_apply_damage_if_hit` fires `combat_hit` after a landed player hit, scaled by
`net_damage / defender max_hp` — a graze barely registers, a near-kill measurably scares the
defender (the `threat` axis) even while `disposition` stays pinned at `is_hostile`'s own
floor; only the player's own attacks trigger this (an entity's own combat-turn attack,
`resolve_behavior_action`, never does — there's no player-side attitude to move). The same
method's own `_nudge_shared_enemy_bonds` then fires `shared_enemy`, at that same magnitude,
toward every *other* living scene entity that already considers the struck target a real enemy
(`is_hostile(observer, target_name)`) — "bonds made on the battlefield," deliberately not
restricted to allies/party members, so even a merely-wary bystander can start warming to the
player for fighting something the bystander already hates. Safe to call unconditionally over
every scene entity: a tableless creature's own `is_hostile` returns `True` regardless of
`target_name` (see "Combat"), but `nudge_attitude_from_event`'s own "no `[entity.attitudes]`
table" gate silently no-ops for exactly that case, so a mindless hostile creature never actually
accumulates a bond it has no data to hold. `DM_Inventory.py`'s `_resolve_transfer_intent` fires
`theft` (a `"take"` that actually moved something) or `favor` (a `"give"`) once a real transfer
completes against a real, distinct, *conscious* target (the shared HP gate above is what makes
`theft` specifically require the victim to actually be aware it's happening, rather than looting
an unconscious or dead body counting as a felt violation) — for either an item (scaled by its
own TOML `value`) or currency (scaled by the amount moved) — against `SIGNIFICANT_VALUE` (25), a
reference scale keeping most shipped items in the 0..1 range without clipping. Deliberately
excludes `"trade"` (a fair, paid exchange, not a violation or a gift) and never fires for the
player's own "already owned" self-transfer no-op (see "Items and movement as intents").

`action_attitude_deltas` is capped independently of `attitude_deltas` — `ACTION_ATTITUDE_DRIFT_CAP`
(60) rather than `TALK_ATTITUDE_DRIFT_CAP` (40) — a real betrayal or a real act of generosity can
move an axis further than words alone, and the two accumulators are tracked separately
specifically so each can enforce its own ceiling rather than sharing one. Round-trips through
save/load the same unconditional way `attitude_deltas` already does (`DM_Persistence.py`).


## Dialogue

Directly addressing someone (`"talk to the innkeeper"`, `"ask the guard about the road"`) is a
third diceless channel: there's no item involved, the addressee is resolved from the scene
rather than looked up, and the result is a generated in-character reply, not a structured
mechanical outcome. Distinct from a *skill-based* social check (persuade/intimidate/deceive) —
those still roll dice via `resolve_opposed_action` and narrate in third person as the omniscient
GM; free-form talking never rolls anything and speaks as the addressed entity.
`Intent_Classification.py`'s `detect_dialogue_intent` recognizes `DIALOGUE_KEYWORDS` phrases (`"talk to"`/`"ask"`/
`"tell"`/`"greet"`/...), checked after item-interaction detection has had its shot (so
`"give the sword to Anne"` is never swallowed as dialogue) and before skill matching. Once
detected, `IntentClassifier.classify` also calls the matcher's own `classify_sentiment(processed)`
(see "Dialogue sentiment" below) and publishes `dialogue_detected {input, score, sentiment}` with
no further resolution.

`DMCore._on_dialogue_detected` delegates to `DM_Dialogue.py`'s `DialogueMixin`:
`_resolve_dialogue_target` searches the input for any present entity's name (whole-word,
excluding the player), falling back to `_get_target_name()`'s default scene target if none is
named. `_resolve_dialogue` gates on the target being present/alive (`reason: "not_present"`)
and not an inanimate `"object"` (`reason: "cant_talk"`) — but deliberately **not** on hostility:
addressing a hostile entity is allowed (shouting mid-fight), and the model is free to read that
as hostile/dismissive in character rather than being denied outright. A found target's
attitude (all three axes) is nudged by the classified sentiments (`nudge_attitude`, see below)
before `persona`/`attitude` are attached for `LLMCore` to speak from — so the same turn's own
reply already reflects it.
Publishes `dialogue_resolved {target, input, found, present_entities, persona?, attitude?,
reason?}` — no `_publish_party_status()`, since dialogue never changes HP/equipment/inventory/
conditions (attitude drift isn't surfaced on the Party tab either, so this still holds).

**Dialogue sentiment.** The tone of what the player says nudges the addressed entity's own
attitude toward them — all three axes at once, each classified independently and independently
scored. Classified locally (`NLP_Core.py`'s `SentenceTransformerMatcher.classify_sentiment`/
`classify_threat`/`classify_familiarity`, one call per axis, all backed by the same separate NLI
(natural-language-inference) model (`NLI_MODEL_NAME`, `facebook/bart-large-mnli`) rather than
this class's own embedding model, a lexicon-based analyzer, or a purpose-trained sentiment
classification head: reading tone/threat/closeness out of an utterance needs broad,
compositional coverage across however a player might phrase something (ex: "get out of my
sight" — clearly hostile, but with no single word a dictionary lookup would flag), which only a
model built for real language understanding reliably provides. Each is run via Hugging Face's
`"zero-shot-classification"` pipeline: entailment is scored between the input and each axis's own
three candidate labels (as a hypothesis built from that axis's own hypothesis template),
normalized to a softmax over the three mutually-exclusive labels per axis. `classify_sentiment`
uses `SENTIMENT_CANDIDATE_LABELS`/`SENTIMENT_HYPOTHESIS_TEMPLATE`;
`classify_threat`/`classify_familiarity` share one `DIALOGUE_HYPOTHESIS_TEMPLATE` with their own
`THREAT_CANDIDATE_LABELS`/`FAMILIARITY_CANDIDATE_LABELS`. None of these are the library's own
bare defaults (`["negative", "neutral", "positive"]` + `"This example is {}."`) — the bare
defaults misread plain informational dialogue (ex: "do you know where the blacksmith is") as
negative/positive at `sentiment_confidence_threshold`'s own floor; the richer per-label phrasing
(ex: `"negative in tone"`/`"neutral or informational"`/`"positive in tone"` for sentiment) plus a
dialogue-framed hypothesis template were tuned against held-out sets spanning hostile/warm/
informational/sarcastic/valence-crossed lines and resolved this without needing to raise the
confidence threshold at all — `threat`/`familiarity` were validated less exhaustively than
disposition (a smaller, though still adversarial, test battery), which is worth keeping in mind
if either axis's real-play behavior looks off. Each `classify_*` method returns `(label, score)`
— normalized back to plain `"negative"`/`"positive"`, and the winning label's own entailment
probability — gated at the shared `sentiment_confidence_threshold` (0.5, "meaningfully more
confident than the ~0.33 a 3-way coin-flip would give") and short-circuited to `(None, score)`
whenever the model's own winning label is the neutral one, covering purely informational
dialogue as well as genuinely neutral phrasing. Still local inference — no network call —
deliberately not an LLM call: dialogue is the single most frequent player action, so adding LLM
latency to every turn was rejected in favor of a fast, local classifier (in practice, ~0.2-0.5s
per axis on CPU — roughly 3x that per dialogue turn now that three axes are classified instead
of one, still well within budget). `DM_Social.py`'s `nudge_attitude(entity_name, toward_name,
sentiments)` takes `sentiments`, a `{axis_name: (label, score)}` dict (an axis missing from the
dict, or with a falsy label/score, contributes 0), and applies a capped drift into
`entity["attitude_deltas"][toward_name]` across all three axes at once whose *magnitude* on each
axis is that axis's own `score` — the classifier's own confidence, already 0..1 — times
`SENTIMENT_INTENSITY_SCALE` (currently `1`, i.e. unscaled; a single tunable knob shared across all
three axes rather than a hand-tuned delta table), not a flat per-sentiment amount: a line the
classifier read as more intensely negative/positive moves that axis further than a mildly-worded
one. Clamped to `±TALK_ATTITUDE_DRIFT_CAP` (40) per axis — a cap on *accumulated drift*, not on
the resolved value, so sustained same-direction talk can still push a base value already close to
`is_hostile`'s `-100` disposition threshold across it (an intentional emergent outcome: insult
someone long enough and they turn on you). `get_attitude` adds `attitude_deltas` elementwise on
top of whichever name/supertype/default array it resolves, so `is_hostile`/`describe_attitude`/
the GUI all see the drifted value transparently, with no other call site changes. An entity with
no `[entity.attitudes]` table at all (ex: `arena.toml`'s wolf) stays hostile unconditionally
regardless of drift, since `is_hostile` short-circuits on the table's absence before ever reading
a disposition value. `attitude_deltas` is genuinely dynamic runtime state, so it round-trips
through save/load in the ordinary per-instance diff (`DM_Persistence.py`) for *every* entity, not
just generated/ad-hoc ones.

**Language barriers.** Every entity's own `languages` list (an entity field,
`entity_schema.toml`, absent entirely defaulting to `["common"]` — same as every entity shipped
today) is what a `_resolve_dialogue` addressee actually understands. `DM_Dialogue.py`'s
`_detect_language_barrier(target_name)` compares `set(player_languages)` against
`target_name`'s own list; any overlap at all resolves as ordinary dialogue. No overlap resolves
`{"found": True, "language_barrier": True, "target_language", "nonsense_phrase"}` instead of the
ordinary persona/attitude reply — the target is still present and willing to react, just unable
to understand the words, so `nudge_attitude` is deliberately skipped (a sentiment classifier
reads the *meaning* of an utterance, which the target never received). `target_language` is the
first of the target's own unshared languages; `nonsense_phrase` is looked up by matching that
name against `races.toml`'s own `[[race]].language` field (`None` if no race claims it, ex: a
scenario-authored language with no matching race entry). `LLMCore.generate_npc_dialogue`
branches on `language_barrier` to `_build_language_barrier_prompt`, instructing the model to
reply only with invented gibberish styled after `nonsense_phrase` (explicitly told not to reuse
it verbatim) rather than answering what was actually asked — persona/attitude still ground *tone*
(a hostile speaker's gibberish should still read as hostile), just never the content.

Each race in `races.toml` authors its own `language` (`human` → `"common"`, `elf` → `"elvish"`,
`dwarf` → `"dwarvish"`, `half-orc` → `"orcish"`, `halfling` → `"halfling"`) plus a
`nonsense_phrase` example of what it sounds like (human has none — every shipped entity already
defaults to knowing `"common"`, so a human-to-human barrier never arises with today's data).
`DM_CharacterCreation.py`'s `apply_character_creation` appends the chosen race's own `language`
onto the player template's existing `languages` list (deduped) alongside the point-buy skill
override, so an elf player knows `["common", "elvish"]` while a human re-adding `"common"` is a
no-op. This is opt-in for scenario/entity authors: nothing changes for existing data until an
NPC's own `languages` list is deliberately narrowed (ex: `["elvish"]` alone, no `"common"`) or a
player picks a race whose language that NPC doesn't share either.

**Room-level presence.** Every DM-published narration event carries `present_entities`: a
snapshot of `self.scenario_entities` at publish time. `LLMCore` tags each `context_window`
entry with this snapshot and `generate_npc_dialogue` uses `_filter_present_history(target)` to
ground a specific NPC's reply only in what that NPC has witnessed, rather than the DM's own
always-full, omniscient window (which stays untouched — the player's point of view is
deliberately still everything). An entity instanced mid-dungeon, or left behind in a previous
room, simply has no access to entries tagged before/without it. The exchange itself is still
appended to the *shared* `context_window`, so it becomes part of what everyone present has now
witnessed — letting a second NPC later recall what was just said to the first.

