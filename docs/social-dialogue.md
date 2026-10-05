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
descriptive TOML fields (`description`, `qualities`, `memories`, `voice`, `quotes`) plus, when
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
block-clock blocks, see `docs/downtime.md`'s "The block clock") is optional — an authored
`inject_directive`'s own `"duration"` field sets it (`suggestion`'s own `duration = 1` matches its
source rule's "a short while" flavor); absent means no expiry at all, persisting until overwritten
or manually cleared
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
specifically so each can enforce its own ceiling rather than sharing one. An `[[attitude_event]]`
may author its own `cap` instead (`"assaulted"`: 200, so being attacked out of the blue can push
anyone past `is_hostile`'s -100 — see "Attacking anyone" in `docs/combat.md`). A cap limits how
far *that* nudge may push; it never pulls back a total a wider-capped event already set, so an
ordinary `combat_hit` right after an assault can't snap -200 back to -60. Round-trips through
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
excluding the player). If none is named, it falls back to `_default_listener`: the first person
present who understands the player's current language, else `_get_target_name()`'s default scene
target, never a dead one. A playtest's first market vendor spoke only another tongue, so 80 turns
of unnamed talk came back as gibberish; another kept talking to the corpse of a bystander killed the
turn before. `_resolve_dialogue` gates on the target being alive (`reason: "dead"`, said outright so
the narrator doesn't have the body "gasping for air") and present (`reason: "not_present"`)
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
no `[entity.attitudes]` table at all (ex: `debug.toml`'s wolf) stays hostile unconditionally
regardless of drift, since `is_hostile` short-circuits on the table's absence before ever reading
a disposition value. `attitude_deltas` is genuinely dynamic runtime state, so it round-trips
through save/load in the ordinary per-instance diff (`DM_Persistence.py`) for *every* entity, not
just generated/ad-hoc ones.

**Language barriers.** Every entity's own `languages` list (an entity field,
`entity_schema.toml`, absent entirely defaulting to `["common"]` — same as every entity shipped
today) is what it understands. The player is understood if they share any language with the
target — unless they deliberately chose one: `current_language` (a runtime-only player-entity
field, absent until the player says "speak in elvish") narrows them to that one tongue, so
speaking Common so the elvish innkeeper can't follow is still a real choice. Without a choice, a
bilingual player just talks in whichever of their languages the listener knows. This used to be
"only the first known language counts", which a playtest broke: the Pathfinder default character
was made Varisian-first so polity-defaulted townsfolk would understand him, and from then on every
NPC left on the `"common"` default (authored ones with no `languages`, dialogue-promoted ones)
answered him in gibberish — no ordering of one list could satisfy both groups.
`_detect_language_barrier(target_name)` makes that comparison; a match resolves as ordinary
dialogue. No match resolves
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
player's own side is ever narrowed to one chosen tongue this way — a target's own multiple known
languages all still count toward whether *it* understands the player.

Which language the player has chosen is set via a new free-standing intent, `speak_language`
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


## Addressee resolution

`_resolve_dialogue_target` is now a literal scan (`_literal_dialogue_target`) with a default-target
fallback, split apart so the promotion gate can ask the unambiguous question "did the player name
someone who is actually here?" without the fallback masking the answer.

The literal scan checks three phrases per present entity — the `self.entities` key, the displayed
`name`, and an optional `aliases` list — where it previously checked only the key. That was a real
gap once instanced crowds existed: an instance keyed `sandpoint_townsfolk_2` but displayed as
"Fishmonger" was unaddressable by the only name the player ever sees. `aliases` is the same
mechanism `[[location.exit]]` already uses, applied to people, and it's what lets "greet the
barkeep" reach `Garridan Viskalai` without anyone authoring that phrasing as a keyword.

The fallback passes `include_background=True` — the one call site that does. A bare "ask about the
weather" landing on whichever townsperson is standing there is exactly right, even though the same
entity must never become the default "open it" target (see `docs/npc-generation.md`).

`_resolve_dialogue` also takes an optional `forced_target`, passed only by DMCore after it has just
materialized that entity into the scene. It bypasses resolution, never the gates — a promoted NPC
is present, alive, visible and not an object, so it passes them on its own merits. See
`docs/adam-improvisation.md`'s "Promotion on reference".


## Conversation partner

Only `DIALOGUE_KEYWORDS` or quoted speech used to reach an NPC, so a player talking naturally
("do you ever get tired of all this?", "let's find somewhere quieter") was sent to the skill pass.
The last fix stopped most of those turns rolling, but they still came back as narrator prose,
not a reply. DMCore now remembers who the player is talking to:
`conversation_partner = {"key", "idle_turns"}` or `None` (`DM_Dialogue.py`).

- **Set** by `_resolve_dialogue` whenever it finds a target, including across a language
  barrier (the player is still talking to them). Naming someone else switches it.
- **Read** by the addressee fallback: literal name → partner → default scene target. So "tell me
  what you know" mid-talk stays with whoever the player was already addressing.
- **Validated lazily** (`_current_conversation_partner`) against the same present/alive/not-hidden
  gates `_resolve_dialogue` uses; a failed check ends the conversation.
- **Ends** on `_enter_location`, or after `CONVERSATION_IDLE_TURNS` (3) turn-costing turns that
  weren't dialogue (`_tick_conversation_partner`, from `_on_turn_detected`). Free-standing intents
  don't count. Combat doesn't end it, since shouting mid-fight is still dialogue.
- **Round-trips** through save/load (`docs/persistence.md`).

Every change is published as `conversation_partner_updated {"partner": {"key", "name", "aliases"}
| None}`. NLPCore keeps the copy (`IntentClassifier.set_conversation_partner`), and while one
is set, `classify`'s dialogue gate also accepts `detect_implicit_speech` (`Intent_Classification.py`).
That check is structural and needs no model call. Any sentence that has a `?`, fails
`opens_like_an_action` (the `NON_ACTION_OPENERS` list the skill fallbacks already use to spot
banter, read after stripping a leading vocative like "gareth, "; an opening article counts too,
since "the wall thing i saw before" cast wall of fire and "then the rules are incomplete…" rolled
psionics into a bystander), or opens as an imperative
aimed at the speaker ("help me", "come help me", "join us") counts. Social-skill attempts
("persuade the captain to lend us his boat") open on their own verb, so they still reach the
skill pass and roll. Implicit dialogue is skipped when a free-standing intent already claimed a
clause: "what do you know about the troll" stays a lore check. The `dialogue_detected` payload
carries `implicit: true` for the logs and the playtest harness. With no partner, the same check
still runs whenever anyone besides the player is in the scene (`IntentClassifier.anyone_present`,
from `scene_roster_updated`). This lets a conversation *start* without "talk to": the line goes to
whoever `_resolve_dialogue_target` picks (a name in the input, or the scene's default person).
Before this, playtests showed 210 turns of talk to NPCs never reaching dialogue once. Only an
empty scene leaves unmarked talk to the skill pass. A line ending in "!" that nothing else
claims (no item, skill, or route) is said to whoever can hear it rather than coming back
not-understood: "stay right there!" and "keep your hands up!" were six of a brawler's fifteen
not-understood turns. An order given by name ("bram, attack the
goblin") still opens like an action. "i bet …" counts as a remark rather than a wager when the
next word is a pronoun or determiner (`BET_REMARK_FOLLOWERS`), so it no longer rolls gambling.
"i'm <verb>ing" is a declared action ("i'm knocking this stall over") unless the verb is stative
(`STATIVE_PROGRESSIVES`: "i'm starving", "i'm thinking").

A line that is partly speech and partly a declared action ("you call that a fight? punch bram.")
is split by `_split_speech_from_action`. Each sentence is judged on its own by
`detect_implicit_speech`, and the line becomes a `dialogue_detected` for the spoken sentences
plus a `turn_detected` for the rest, in the order written. This only happens when the action
half resolves to something real: a matched item, or a skill matched at
`MIXED_ACTION_MIN_SCORE` (0.5) or above. If not ("forget the lumber. let's find a private
place.", or an item verb naming nothing real), the whole line stays dialogue. A sentence
opening on an article or bare interjection (`SPEECH_FRAGMENT_OPENERS`: a/the/no/yeah/sure…) is a
fragment of the talk and never the action half: "a name, man. you gotta give me a name" had rolled
appraise on "a name, man.".

Quoted speech beside an action splits the same way (`_split_quoted_speech`): 'i yell "hey!" and
swing a fist at elara' is dialogue for the quoted words plus a turn for the rest, in the order
written. Here the rest is judged like any ordinary turn, with no `MIXED_ACTION_MIN_SCORE` bar,
since the quotes already mark the talk. The quote's own tag (`SPEECH_TAG_VERBS`: yell, shout,
snicker…) and any dialogue-keyword clause ('ask the guard "where is the inn?"') are never the
action. A brawler playtest lost all eleven of its shout-and-attack turns to dialogue before this. A clause that opens
on a gesture verb (`GESTURE_VERBS`: bow, nod, grin, shrug…) never reaches skill matching at all.
"(bows head dramatically)" had rolled missiles.


## Speech framing and voice

**Framing.** `Intent_Classification.py`'s `frame_speech` decides how the narrator should hear a
dialogue line, and `dialogue_detected` carries the result as `speech_form`/`utterance` (DMCore
passes both through on `dialogue_resolved`):

- `greet`: a dialogue keyword naming someone and nothing else ("talk to the fishmonger"). The
  prompt has the player approach, and the NPC speaks first. A keyword with nothing after it at
  all ("…and how can we tell?") greets no one and stays `verbatim`.
- `reported`: any other keyword-led line, restated in the second person from the keyword on
  ("ask about the kelp beds" → "You ask about the kelp beds."). A movement clause before the
  keyword is its own quiet intent, so it's left out.
- `verbatim`: the player's own words, quoted as said: a quoted span (the quote only; a span counts
  as speech at three or more words or ending in `.!?,` — a bare one- or two-word quote like 'the
  real "currents"' is a scare quote, see `speech_quotes`), implicit
  speech (see "Conversation partner"), or a keyword aimed back at the speaker ("tell me what you
  know").

Before this, the raw command itself was quoted as speech (`The player says: "talk to the
fishmonger"`), and the model invented a conversation to fit. `LLMCore._build_speech_prompt`
builds one prompt per form, always in the second person. The PC is "you" in every dialogue
prompt, including language-barrier and not-found ones. The model used to copy "the player"
straight into replies.

**Reply shape.** `_build_dialogue_system_message` lays the NPC out as "Who {target} is" (persona)
and "How {target} feels about you" (attitude). It asks for:

- mostly the NPC's own spoken words, in quotes, in everyday language
- at most one short action beat, with no scenery and no narrating the PC
- at most one speech tag
- length set by mood: guarded, busy or hostile gets a line or two; friendly gets up to three or
  four sentences

The existing rules (answer the question; don't repeat a deflection) are unchanged.

**Voice.** `voice` is an optional entity field: one line on how someone talks (register, dialect,
how much they say, verbal tics). `describe_character` emits it as "Voice: ...", and `quotes`
become "Lines in their voice: ..." — examples to match, not just trivia. Most people the player
actually talks to are generated, so `voice` is part of both narrated population
(`NARRATED_FREEFORM_FIELDS`, plus each setting's `[narration_population.limits].freeform`) and
the creature tool used for promotion (optional, not required). Without a voice, the prompt tells
the model to sound like an ordinary person of the NPC's station, not a storyteller.
`lost_coast`'s hand-authored NPCs are mostly unvoiced so far; only the opening `dockhand` has one.

`tools/playtest.py` flags three dialogue smells per turn: "the player" in the reply, a reply with
no quoted speech, and a meta parenthetical. It also logs each turn's `speech` forms and counts
implicit dialogue.

