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
The one genuinely dynamic exception: if the entity's own `prompt_directive` (a plain
`{"text", "source", "expires_in_blocks"?}` dict) is set, its text is appended too — "Currently
privately convinced (planted by ...): '...'". This is the general "inject material into an
NPC's prompt" mechanism: `resolution/Social_Resolution.py`'s `set_prompt_directive(entities,
entity_name, text, source_name, duration_blocks=None)` writes it, plugged into the
`on_pass`/`on_fail` program language (see "Action resolution") as an `inject_directive` op
(`resolution/Program_Interpreter.py`) — `spells.toml`'s `suggestion` is the shipped example,
whose own `on_pass = { do = "inject_directive", entity = "target", duration = 1 }` omits a
literal `text` so the op falls back to `ctx["input"]`, the caster's own raw turn text (threaded
in by `DM_Core.py`'s `_run_ability_outcome_program` specifically for this — the `[entity.test]`
attachment point is *not* threaded the same way, since an item/lock has no NPC prompt to affect).
Because `describe_character` already backs every NPC-facing prompt except live combat/behavior-
turn narration (the `scenario_loaded` roster, `DefenderDetailsEffect` on every resolved roll,
and free-form dialogue's own `persona` field, `DM_Dialogue.py`'s `_resolve_dialogue`), a planted
directive reaches the very turn it lands *and* every later dialogue turn with that NPC, for free.
One directive at a time (a later plant overwrites, never stacks). `duration_blocks` (in
block-clock blocks, see "Downtime") is optional — an authored `inject_directive`'s own
`"duration"` field sets it (`suggestion`'s own `duration = 1` matches its source rule's "a short
while" flavor); absent means no expiry at all, persisting until overwritten or manually cleared
(ADaM's own ad hoc entity-edit path already can, incidentally). `DMCore.advance_blocks`'s own
`_expire_prompt_directives` (`DM_Time.py`) decrements every planted directive's countdown by
however many blocks just elapsed and clears it once that reaches zero — the same bespoke,
bolted-on-per-mechanism countdown idiom `"summon_expires_in"`/`"surprised"` already use, rather
than the (never-enforced) `duration` field a `[[condition]]` itself carries.
Round-trips through save/load the same unconditional per-instance way `current_language` already
does (`DM_Persistence.py`).
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
(`trust`/`respect`) when those axes were dropped, so it's disposition-only today, deliberately
smaller than `favor`'s own deltas ("watching someone fight your enemy is a lighter touch than
being on the receiving end of a real gift" — `rules.toml`'s own comment). `favor` itself did
lose its single largest value (`obligation = 20`) in the collapse, leaving it noticeably weaker
than its own negative mirror, `theft` — since fixed by restoring roughly that lost weight into
`disposition`/`familiarity` instead, so `favor` now mirrors `theft`'s own magnitude exactly
(give vs. take being the direct positive/negative mirror of the same mechanic, with no
principled reason for one to carry less emotional weight than the other).
`DM_Social.py`'s `nudge_attitude_from_event(entity_name, toward_name, event_name, magnitude)`
looks up the named event and writes the scaled deltas into their own `action_attitude_deltas`
accumulator (`get_attitude` sums it elementwise alongside `attitude_deltas`, same as before) — a
no-op for an unknown event, a falsy magnitude, an entity with no `[entity.attitudes]` table at
all, an inanimate object (`supertype == "object"`), or an entity with no HP left (a dead entity
isn't aware of anything happening to it or nearby anymore, whether that's the killing blow
itself, a theft, a gift, or a battlefield bond forming), mirroring `is_hostile`'s own "nothing to
nudge" precedent for a tableless creature.

`DM_Core.py`'s `_nudge_combat_hit_attitude(target_name, attacker_name, net_damage)` is the shared
call-site shape behind `combat_hit`/`shared_enemy`: it fires `combat_hit` on `target_name`'s own
attitude toward `attacker_name`, scaled by `net_damage / target_name`'s `max_hp` — a graze barely
registers, a near-kill measurably scares the defender (the `threat` axis) even while
`disposition` stays pinned at `is_hostile`'s own floor — then calls its own
`_nudge_shared_enemy_bonds(target_name, attacker_name, magnitude)`, which fires `shared_enemy`,
at that same magnitude, toward every *other* living scene entity that already considers the
struck target a real enemy (`is_hostile(observer, target_name)`) — "bonds made on the
battlefield," deliberately not restricted to allies/party members, so even a merely-wary
bystander can start warming to `attacker_name` for fighting something the bystander already
hates. Safe to call unconditionally over every scene entity: a tableless creature's own
`is_hostile` returns `True` regardless of `target_name` (see "Combat"), but
`nudge_attitude_from_event`'s own "no `[entity.attitudes]` table" gate silently no-ops for
exactly that case, so a mindless hostile creature never actually accumulates a bond it has no
data to hold. Two call sites share this shape, `attacker_name` being whichever side actually
landed the hit: `_apply_damage_if_hit` after a landed *player* hit, and `DM_Combat.py`'s
`resolve_behavior_action` after any *other* entity's own successful combat-turn attack (ex: a
monster hitting the player, or an ally striking a shared foe) — a generalization
`_nudge_shared_enemy_bonds` needed no changes of its own to support, since it already looped
every other living scene entity generically. Stays one-directional either way: only the victim's
attitude toward the attacker moves, never the reverse — an attacker's own feelings toward its
target are already fully authored via `[[entity.behavior]]`/`[entity.attitudes]`, the data that
decided it was attacking in the first place, so an automatic reciprocal nudge on the attacker's
own side would be redundant with something already hand-authored.

`theft`/`favor` aren't player-only anymore: `DM_Inventory.py`'s `_resolve_transfer_intent` still
covers the player's own `take`/`give` intents, and `DM_Combat.py`'s `TRANSFER_ACTIONS`/
`_resolve_transfer_behavior` cover the NPC side — reserved `[[entity.behavior]]` action names
`"steal"`/`"gift"` (parallel to `MOVEMENT_ACTIONS`' own `"advance"`/`"retreat"`), naming which
item to move via the behavior entry's own `"item"` field (`"currency"`, the same reserved
sentinel `_resolve_transfer_intent` already uses, moves currency instead — `"amount"` caps how
much, since `transfer_currency`'s own unset default is "everything the source has," too
punishing for an ambush the player didn't choose to walk into). Fires the identical
`theft`/`favor` nudge either way, just with entity_name (not the player) as the mover.
`creatures.toml`'s `"pickpocket"` is the shipped worked example — no attack ability at all,
just a `"steal"` (a modest, capped sum) behavior entry that gives way to `"retreat"` the moment
it actually takes a hit.

On the player's own side, `_resolve_transfer_intent` fires
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
confidence threshold at all — `threat`/`familiarity` were originally validated less exhaustively
than disposition by hand; `test_unit.py`'s `TestGameBoot` now carries a real, live-model
regression test for each (the same deliberately valence-crossed cases this section's own tuning
notes already named — "your skill with that blade is terrifying..." for threat, an "I've known
you my whole life" vs. "I don't know you" pair for familiarity), so a future embedding/label/
template change that quietly breaks either axis gets caught the same way the sentiment axis's
own regression test already catches drift there. Each `classify_*` method returns `(label, score)`
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
today) is what it understands. The player's own side of the comparison, though, is a single
persistent choice, not the full list: `DM_Dialogue.py`'s `_current_language()` returns
`current_language` (a runtime-only player-entity field, absent until the player deliberately
switches) or, absent that, the first entry of the player's own `languages` — chargen's own
ordering (`"common"` first, the chosen race's language appended after). This is deliberate: a
community plausibly converges on one shared tongue, and re-deciding which language is in use on
every single exchange would be tedious for no narrative payoff, so a bilingual player isn't
treated as speaking every known language at once just because they know more than one.
`_detect_language_barrier(target_name)` compares that one active language against
`target_name`'s own full list; a match resolves as ordinary dialogue. No match resolves
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
(a hostile speaker's gibberish should still read as hostile), just never the content. Only the
player's own side is ever narrowed to one active tongue this way — a target's own multiple known
languages all still count toward whether *it* understands the player, since there's no equivalent
"which one is it currently speaking" ambiguity on that side.

Which language is currently active is switched via a new free-standing intent, `speak_language`
(`nlp/Intent_Classification.py`'s `SPEAK_LANGUAGE_KEYWORDS`: "speak in ", "switch to speaking ",
"start speaking " — phrases, not a bare "speak ", to avoid colliding with the `linguistics`
skill's own "speak" keyword, same reasoning `DIALOGUE_KEYWORDS`' own "speak to "/"speak with "
already follow). Recognized and resolved the same way party formation is: `EXEMPT_ITEM_INTENTS`
publishes it free-standing (no turn cost, the same "speech is free" treatment dialogue itself
gets), and `DM_Dialogue.py`'s `_resolve_language_intent` — not NLPCore — figures out *which*
language is named, by searching the raw input for one of the player's own known `languages` (the
same "search input for a known name" pattern `_resolve_formation_intent` already uses for a
party member's own name). Naming a language the player doesn't actually know (or naming nothing
recognizable at all) is declined outright (`reason: "unknown_language"`), never guessed at.
`current_language` round-trips through save/load the same unconditional per-instance way
`attitude_deltas` already does (`DM_Persistence.py`).

Each race in `races.toml` authors its own `language` (`human` → `"common"`, `elf` → `"elvish"`,
`dwarf` → `"dwarvish"`, `half-orc` → `"orcish"`, `halfling` → `"halfling"`) plus a
`nonsense_phrase` example of what it sounds like (human has none — every shipped entity already
defaults to knowing `"common"`, so a human-to-human barrier never arises with today's data).
`DM_CharacterCreation.py`'s `apply_character_creation` appends the chosen race's own `language`
onto the player template's existing `languages` list (deduped) alongside the point-buy skill
override, so an elf player knows `["common", "elvish"]` while a human re-adding `"common"` is a
no-op. This is opt-in for scenario/entity authors: nothing changes for existing data until an
NPC's own `languages` list is deliberately narrowed (ex: `["elvish"]` alone, no `"common"`) or a
player picks a race (or later switches, via `speak_language`) to a language that NPC doesn't
share either.

**Language-dependent abilities and skill checks.** Free-form dialogue is diceless, so the barrier
above only ever gates its flavor text. A named ability/spell/technique or a skill-based social
check (persuade/deceive) can additionally require a shared language to function *at all* —
opt-in via `language_dependent = true` on an ability entry (`entity_schema.toml`, the same
fixed-classification role `damage_tags`/`armor_tags` already play, see `docs/combat.md`'s "Tags
vs. conditions" — deliberately not reusing `damage_tags` itself, since that field only ever feeds
the damage-reduction pipeline and many language-dependent checks deal no damage at all).
`DM_Combat.py`'s `_ability_requires_language(skill_name, ability)` checks the resolved ability's
own flag when one was named; for a bare skill use with no named ability (ex: "persuade the
guard" resolves `skill_name="charisma"` with no ability, since `find_attack_ability` deliberately
never scans *universal* abilities like `charm`), it falls back to checking the skill's own
declared `abilities` list (`skills.toml`, ex: `charisma` → `["charm"]`). `DM_Core.py`'s
`_resolve_roll` checks this right alongside `is_in_range`: no shared language auto-fails the
ability outright as a `LanguageBarrierOutcome`, no roll attempted at all — the same "can't do it,
don't roll" precedent `is_in_range` already sets. `maneuvers.toml`'s `charm` carries the flag
(warm words only land if understood); its own `intimidate` doesn't (a raised weapon needs no
shared tongue).

**Room-level presence.** Every DM-published narration event carries `present_entities`: a
snapshot of `self.scenario_entities` at publish time. `LLMCore` tags each `context_window`
entry with this snapshot and `generate_npc_dialogue` uses `_filter_present_history(target)` to
ground a specific NPC's reply only in what that NPC has witnessed, rather than the DM's own
always-full, omniscient window (which stays untouched — the player's point of view is
deliberately still everything). An entity instanced mid-dungeon, or left behind in a previous
room, simply has no access to entries tagged before/without it. The exchange itself is still
appended to the *shared* `context_window`, so it becomes part of what everyone present has now
witnessed — letting a second NPC later recall what was just said to the first.

