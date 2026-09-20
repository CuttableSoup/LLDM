# LLDM — ADaM and Ad Hoc Improvisation

Part of the [LLDM](../CLAUDE.md) docs — out-of-character help, ad hoc creation/removal/editing, summoning.

## ADaM (out-of-character help)

`"ADaM"` (Artificial Dungeon and Master) is a fourth diceless channel — a reserved,
always-available, explicitly out-of-character persona the player can address for guidance
(skills/abilities, scene re-description, exits, command guidance), never an in-fiction one.
Unlike Dialogue, there's no addressee to resolve and no way to deny it — it always resolves, and
is unconditionally free: it never joins the shared multi-action turn economy.

`Intent_Classification.py`'s `ADAM_NAME_PATTERN` (`\badam\b`, case-insensitive) is checked as its own
whole-input, pre-clause-split reserved word — right after save/load detection, ahead of both
inter-room direction detection and the item-interaction pass — so it reaches the help channel
rather than being swallowed by `DIALOGUE_KEYWORDS` or an item verb first. Reserves the literal
name "adam" the same way `PLAYER_PLACEHOLDER` reserves "player" — a known, accepted tradeoff.
Publishes `help_detected {input}`.

`DM_Help.py`'s `HelpMixin._on_help_detected` gathers a fresh snapshot of live state every time
it fires (no memory of past exchanges — see below) and publishes `help_resolved`: the player's
skills/abilities/equipped/inventory, the current scene's name/description, the present-character
roster, and `exits` (the current room's own exits, if a room is active, followed by the current
location's own `[[location.exit]]` list, resolved to friendly destination names — `[]` if
neither exists, ex: a location with no rooms and no declared exits of its own). No
`_publish_party_status()` for the ordinary informational path — except when a removal, creature
conjuring, or edit actually goes through (see "Ad hoc entity creation and removal"), the three
exceptions on purpose.

**Deliberately excluded from `context_window`.** Every other narration trigger appends both
prompt and reply to `LLMCore`'s shared rolling window. ADaM's own `generate_adam_response`/
`_queue_adam_response` instead send a standalone `[system, user]` request to
`_fetch_and_publish(..., store_in_context=False)`, appending neither. Two reasons: (1) tone — a
meta/OOC exchange left in the shared window risks the GM later parroting mechanical facts
in-fiction; (2) budget — ADaM's dense payloads (full skill lists, exits) would otherwise crowd
the finite 100-message window. The cost: ADaM has no memory of its own past replies — each
invocation is a fresh request built from whatever `DM_Help.py` gathers off live state.


## Scene queries

A fifth diceless channel, alongside ADaM: a free-standing, read-only question about the current
scene ("what do I see", "who is here", "describe the room") that needs no "ADaM" address at all.
Before this existed, a bare scene question with no recognized item verb fell through to
`action_not_understood`/`generate_clarification_response` (no live-state grounding beyond the
standing scenario/roster system message) or, worse, got misread as an `"examine"` with no
matching item and reached `DM_Improvisation.py`'s ad hoc item generation — inventing something
that was never actually in the scene. `Intent_Classification.py`'s `SCENE_QUERY_KEYWORDS`
(`"what do i see"`, `"who is here"`, `"describe the room"`, ...) is checked as its own
whole-input reserved gate, right after `ADAM_NAME_PATTERN` and ahead of item-interaction
detection — so `"adam, what do i see"` still reaches the ADaM channel (ADaM is checked first
either way), while a bare `"what do i see"` reaches this one instead. Deliberately long,
distinguishing phrases, never a bare `"look"`/`"search"`/`"spot"`/`"notice"`/`"see"`/`"find"` —
`observation`'s own `skills.toml` keywords list every one of those as a single word, so a genuine
`"search the room for hidden traps"` (an actual perception check, meant to roll dice) is never
swallowed here first.

`DM_Help.py`'s `_on_scene_query_detected` answers from the same kind of live ground-truth
snapshot ADaM's own `_on_help_detected` gathers (present roster, scene description, exits) plus
one field ADaM's own payload gained alongside it: `_describe_ground_items()` (every loose item
currently sitting in `_current_ground_items()`, `is_hidden`-filtered the same way the present
roster already is) — the concrete gap that made "what do I see" specifically unanswerable before,
even through ADaM. Deliberately its own handler, not a call into `_on_help_detected`: this
channel never runs the `REMOVAL_KEYWORDS`/`CREATURE_KEYWORDS`/`EDIT_KEYWORDS` gates ADaM's own
higher-risk mutation paths do, and never reports the player's own mechanical state
(skills/abilities/equipped/inventory) — irrelevant to "what does the room look like."

`LLMCore.generate_scene_query_response`/`_build_scene_query_system_message`/`_queue_scene_query`
mirror ADaM's own trio, but differ in exactly the two ways the design calls for: the system
message speaks as the ordinary in-fiction Game Master, not ADaM's own explicit out-of-character
persona (while keeping the identical strict "use only the facts given below; never invent people,
items, or exits that aren't listed" grounding instruction ADaM's own message already uses), and
the exchange *does* join the shared `context_window` (tagged with this request's own
`present_entities`, like any ordinary narration turn) rather than staying excluded the way ADaM's
own is — "there's a locked chest here" is exactly the kind of fact a later turn should be able to
build on, unlike ADaM's own mechanical/meta payload.

## Ad hoc entity creation and removal

ADaM's second capability: improvising the world itself, not just explaining it.
`AdHoc_Generation.py` is the pure LLM-calling half —
`generate_ad_hoc_item`/`generate_ad_hoc_creature`/`decide_entity_removal`/`decide_entity_edit`,
each an OpenAI-style tool call (`LLM_Client.py`, synchronous, raises on failure) offering a
primary function plus a shared `decline` escape hatch, `tool_choice="auto"`. Every function
defaults to declining on any failure — never fabricating an item/creature/removal/edit when the
LLM is unreachable — on a tighter 8s timeout than NPC generation's 20s (this can fire far more
often mid-session). `DM_Improvisation.py`'s `ImprovisationMixin` is the DMCore-touching glue.

**Two risk tiers.** (1) **Automatic fallback**, no ADaM address needed (low risk): plain item
creation, extended to a container/trap (a `generate_ad_hoc_item` subtype carrying its own
minimal `[entity.test]`) and to ambient scenery detail (`describe_scenery`, no entity created).
(2) **ADaM-gated**, behind explicitly naming ADaM (higher risk — can affect combat balance or
mutate any entity): removal (`removal_candidate`), creature conjuring (`creature_candidate`),
editing (`edit_candidate`) — each gated by its own cheap local keyword pre-check
(`Intent_Classification.py`'s `REMOVAL_KEYWORDS`/`CREATURE_KEYWORDS`/`EDIT_KEYWORDS`, attached to
`help_detected`'s payload only once `ADAM_NAME_PATTERN` matched), with the LLM's own
`tool_choice="auto"`/`decline` still the real arbiter.

**Creation.** `Intent_Classification.py` tracks any clause matching `IMPROVISABLE_INTENTS`
(`examine`/`take`/`give`/`equip`/`unequip`/`use`/`drop`/`trade`) whose `map_to_item` found
nothing. If the whole turn resolves to nothing at all, the first such candidate is published as
`improvisation_requested {intent, phrase, input}` instead of `action_not_understood`.
`ImprovisationMixin._on_improvisation_requested` calls `generate_ad_hoc_item`
(enum-constrained `subtype`/`equip_slot`/`lock_skill`/`disarm_skill`/`damage_tag`, built from
real in-use values for reliability with small local models). On decline/failure: publishes
`action_not_understood` — the same outcome as without this feature. On success: tags the entity
`ad_hoc = True` (drives its save/load treatment — see "Persistence"), stores it into
`self.entities`, publishes `item_catalog_updated`.

The same tool call offers `describe_scenery(description)` for ambient detail with no entity
created — a pure flavor beat published as `item_interaction_resolved {found: True,
description}`.

**Containers and traps.** `subtype = "container"`/`"trap"` carry a minimal LLM-authorable
version of `items.toml`'s hand-authored `chest`/`dart trap` shape: a locked container gets
`active_conditions = {"closed", "locked"}` plus a `[entity.test]` whose `pass` dismisses it and
`fail` applies `"jammed"`; a trap gets `active_conditions = {"armed"}` plus a `[entity.test]`
whose `fail` deals real damage — skipping `[entity.notice]`/`"hidden"` since it was only
conjured because the player already found it. `_resolve_test_skill` falls back to
`lock_skill`/`disarm_skill` → `"finesse"` → dropping the test block entirely, so a conjured
container/trap can never land permanently unopenable/undisarmable.

Unlike a plain item, a container/trap becomes a live, targetable scene participant
(`SCENE_PLACED_SUBTYPES`), via `_place_and_register_scene_entity(name, entity, insert_front=True,
claim_target=True)` — inserted at the *front* of `self.scenario_entities` (so
`_get_target_name()` picks it immediately) and claiming `self.current_target` via
`_claim_current_target_if_free` (needed since scene-level `[entity.test]` checks resolve against
`current_target`, not `_get_target_name()`), but never stealing the target from a fight already
engaged. Hostile creature conjuring (below) calls the same helper with `insert_front=False,
claim_target=is_hostile(name, player_name)` — the two ad hoc placement sites share this one
place/register/maybe-claim sequence (on top of `_place_new_entity`, `DM_Rules.py`, the lower
primitive both this and ordinary scenario/room instancing build on) rather than each
hand-rolling it.

An item can opt into `"use"` (`usable = true`, `healing`/`poison` `{dice, pips}`) via
`is_healing`/`is_poisonous` flags — the tool schema explicitly tells the model a plausible
fraction of consumables should come back poisonous, not a guaranteed free heal.

**Placement.** The model's `location` choice (`"ground"`/`"inventory"`) is narrative judgment,
but which one actually works depends on the triggering intent. Every placement that lands in an
inventory goes through `DM_Inventory.py`'s `place_new_item` (see "Inventory and currency") and
then re-dispatches through the ordinary, unchanged `_on_item_interaction_detected` uniformly —
`PLAYER_CENTRIC_INTENTS` (`give`/`equip`/`unequip`/`use`/`drop`) always land in the player's own
inventory this way regardless of the model's own `location` choice; `GROUND_AWARE_INTENTS`
(`examine`/`take`) honor it — `"ground"` appends to `_current_ground_items()` instead and
re-dispatches the same way, `"inventory"` places into the player's own inventory exactly like a
player-centric intent does. `TARGET_CENTRIC_INTENTS` (`trade` alone) ignores `location` and
stocks the entity into the current scene target's own inventory via the same `place_new_item`
call — this is what lets a shopkeeper sell "most general goods" without every item being
pre-authored (`Rules/Fantasy/scenarios/debug.toml`'s own `general_store` area); short-circuits to a decline if there's no
current scene target to sell from. `_on_item_interaction_detected` itself resolves "examine"/
"take" directly against the player whenever the item is already sitting in their own inventory
(checked ahead of the locked/closed-target gates, not just the source/destination resolution, so
an unrelated locked container elsewhere in the scene never blocks examining something the player
already possesses — see "Items and movement as intents") — which is what lets every one of these
placements redispatch through the same unchanged pipeline with no bespoke narration of its own.

**Removal.** `DM_Help.py`'s `_on_help_detected` calls `_attempt_entity_removal` first when
flagged — builds the real universe of removable names (every scene/ground/inventory entity,
`player_name` excluded) as an enum constraint for `decide_entity_removal`, plus a narrower
`hostile_entities` subset (`is_hostile` + a live-HP check) passed alongside it — without this,
a real model complies with "get rid of that wolf, this fight is too hard" unconditionally,
turning a free out-of-character request into a dice-free win; `decide_entity_removal`'s own
prompt is what actually leans on this subset to decline. A real removal is
folded into `help_resolved` as `"removed"` and triggers `_publish_party_status()` — one of the
three exceptions to "ADaM never mutates state" (creature conjuring and editing, below, are the
other two). `remove_entity_from_scene(name)` strips `name` from
every entity/ground/room list and records it in `self.removed_entities` so it can never respawn
(`DM_Rules.py`'s `_instance_entities` checks this set). Deliberately does **not**
`del self.entities[name]` — left orphaned/unreferenced, same precedent as a fully-consumed item,
which is also what makes it self-clean out of future saves (collection is by *reachability*).
Hard guard: refuses if `name == player_name`. Removing a container also orphans its own
inventory contents — intended.

**Creature/NPC conjuring.** `_attempt_creature_conjuring` resolves `target_cr =
get_challenge_rating(player_name)` (a single-target framing, not real NPC generation's
party-pool math) and calls `generate_ad_hoc_creature`: one tool call constrained to 1-2 real
`npc_keywords.toml` archetypes, a `disposition` enum (`hostile`/`wary`/`neutral`/`friendly` —
`hostile` maps to exactly `-100`, the real combat threshold), and a `power` enum
(`weak`/`moderate`/`strong`, a multiplier on `target_cr`). No second LLM round trip for stats —
the keyword choice is reused directly with `NPC_Generation.py`'s deterministic
`fit_skills_to_cr`. **Only `"hostile"`** gets an inline attack ability plus a
flee-under-0.4-hp_per_remain-then-attack behavior pair, mirroring `debug.toml`'s wolf; a
wary/neutral/friendly conjured NPC is dialogue-only. Placement mutates
`self.entities`/`self.scenario_entities` directly (since `_instance_entities` only runs at load
time) — `entity_id`/`band` (player's current band), `ad_hoc = True`. A hostile creature also
claims `self.current_target` unless a fight is already engaged, so conjuring mid-combat never
silently retargets the player or flips the round-vs-action narration choice. Folded into
`help_resolved` as `"created_creature"` and triggers `_publish_party_status()`, same as removal
and editing.

**Entity editing.** `_attempt_entity_edit` builds the same editable-name universe as removal and
asks `decide_entity_edit` to pick one and change it, or decline. Scope is narrow: a full
`new_description` rewrite plus `apply_condition`/`dismiss_condition` — never raw mechanical
fields like `skills`/`damage_value`. A description change tags `entity["edited"] = True` (see
"Persistence") and republishes `item_catalog_updated`; folded into `help_resolved` as `"edited"`
and triggers `_publish_party_status()`.

**Persistence.** An ad hoc entity has no static TOML template to re-derive from on reload, so
`DM_Persistence.py`'s `_collect_ad_hoc_entities` saves every *reachable* one's complete dict
under `"ad_hoc_entities"` — reachable meaning a live `self.scenario_entities` participant (a
conjured creature/container/trap, or a temporary summon — see "Summoning"), present in some
ground list, or sitting in some known instance's own inventory/equipped mapping.
`"recent_damage_tags"` (`calculate_damage`, `DM_Combat.py`) is stripped from the copied dict
first — a plain Python `set`, not JSON-serializable, and deliberately ephemeral regardless (see
"Status and conditions"'s own per-round upkeep note). `load_game` restores each entity with a
full dict replacement alongside `"ground"`, republishes `item_catalog_updated` once, then
re-appends every name the save's own `"scenario_entities"` remembered that a fresh
scenario/room re-instancing didn't already reproduce (exactly the ad hoc ones — a hand-authored
entity's own presence already re-derives correctly from static data) back onto the live
`self.scenario_entities`, guarded on the name actually resolving to something in `self.entities`
so a stale reference is dropped rather than left dangling. `"removed_entities"` round-trips too,
restored *before* scenario/room loading runs so a removed entity can't respawn. An *edited*
hand-authored entity still has a real template, so only `entity["edited"] = True` is needed to
make `save_game` include `"description"` in its diff and `load_game` restore it.

**NLPCore catch-up.** `SentenceTransformerMatcher`'s `item_embeddings`/`item_indices` are
otherwise only ever built once, from `"rules_loaded"` — an ad hoc entity created/restored after
that would be permanently unreachable to `map_to_item` without `item_catalog_updated`: `NLPCore`
forwards each entry in that event's payload to `IntentClassifier.register_item`, which delegates
to the matcher's own `register_item`, encoding the `{name, description}` pair and appending onto
the existing tensor/index list (`torch.cat`) rather than rebuilding from scratch. Every
capability above that introduces a name or description change republishes this event.


## Summoning

A spell/technique/innate ability's own `summon = {"name"|"template", "duration"}` field
(`entity_schema.toml`) opts a successful cast into conjuring a temporary ally, alongside (or
instead of) dealing damage — `spells.toml`'s `summon spectral wolf` (`creatures.toml`'s own
`spectral wolf`, on gladstone's `abilities` list) is the shipped example.
`DM_Core.py`'s `_apply_summon_if_hit`, called from `_finish_rolled_outcome` (the single post-roll
step `_on_turn_detected` calls once per action-kind clause) right after `_apply_damage_if_hit`,
fires whenever the turn's own `named_ability` carries a `summon` table
and the roll succeeded — regardless of `target_name`/`via_test`, since a summon isn't "against"
anyone the way damage is: casting with no `current_target` at all resolves as an ordinary flat,
automatic `resolve_action` (difficulty 0, unaffected by the ability's own authored `difficulty`,
which only ever gates the targeted branch below — see "Action resolution"); casting against a
live hostile `current_target` resolves as a flat check against `summon spectral wolf`'s own
authored `difficulty` (10) instead, the caster's `arcane` vs. that fixed number, same as every
other spell that authors one — no bespoke "resist a summon" mechanic, and no opposed roll
against the target at all unless the target itself authors a matching `[entity.test]`.

Unlike `DM_Improvisation.py`'s own ad hoc creature conjuring, a summon never invents anything —
`DM_Summoning.py`'s `_summon_creature` always instances a real, hand-authored `[[entity]]`/
`[[entity_template]]`, via the exact same `_instance_entities` primitive (`DM_Rules.py`) every
scenario/room's own static `entities` list is built on (reusing `_resolve_one_encounter`'s own
precedent, `DM_Encounters.py` — not `DM_Improvisation.py`'s bespoke `_unique_entity_key`/
`_place_and_register_scene_entity` pairing, which exists specifically for an LLM-invented name
with nothing real to disambiguate against `self.entity_occurrence_counts` the ordinary way). The
new instance
is placed at the caster's own current band, tagged `ad_hoc = True`, given a `summon_expires_in`
(whole combat rounds remaining), and appended to `self.scenario_entities` — but never claims
`self.current_target` (an ally never should, same as `_claim_current_target_if_free`'s own
hostile-only rule). Whether the summon actually fights *for* the caster is entirely down to its
own `[entity.attitudes]`/`[[entity.behavior]]` data, same as any other entity: `spectral wolf`'s
own flat-positive `default` attitude is what keeps it from reading as hostile-by-default (no
`[entity.attitudes]` table at all defaults to hostile — see "Combat"), and its own
`[[entity.behavior]]` entry is what makes it actually attack `self.current_target` each round.

`run_round_upkeep` (`DM_Status.py`) also counts down every living scene entity's own
`summon_expires_in` each combat round (`_expire_summon_if_due`, `DM_Summoning.py`), sharing the
same per-round cadence condition-driven upkeep already uses rather than a second pass over
`scenario_entities` — iterated as a snapshot (`list(self.scenario_entities)`), since an expiring
summon removes itself from that same live list mid-loop. A summon reaching 0 is removed from the
scene entirely via `remove_entity_from_scene` (`DM_Improvisation.py`), not a condition dismissed
— the creature itself is gone, not just one of its traits. There's no engine concept of a "round"
outside combat, so a summon cast with no fight underway doesn't start counting down until an
actual combat round happens.

**Save/load.** A summoned creature (`ad_hoc = True`) round-trips through save/load like any
other ad hoc entity — `_collect_ad_hoc_entities` treats live `scenario_entities` membership as
its own reachability source, and `load_game` re-appends a restored ad hoc entity's name back
onto `self.scenario_entities` (see "Saving and loading"'s own "Persistence" note), including
its own `summon_expires_in`, since the whole entity dict round-trips, not a whitelisted diff —
the same mechanism covers `DM_Improvisation.py`'s own ADaM-conjured creatures/containers/traps
too, not just summons.

**`_apply_damage_if_hit`'s own gating.** A resolved ability only appends a `DamageEffect` to the
`RolledOutcome`'s own `effects` if it actually carries a `damage_value` field
(`"damage_value" in ability`, not just a truthy `ability`) — a named ability with none (a
summon, or any non-damaging spell) never rolls through `calculate_damage`'s own
`{"dice": 0, "pips": 0, "bonus": 0}` default and picks up a spurious zero-damage `DamageEffect`
just because it resolved against a target that was present.


## Promotion on reference

The problem this closes: a location can describe a populated street while instancing nobody, so
the narrator is asked to describe a crowd that doesn't exist — and obliges, inventing people the
player then can't talk to. `Rules/Pathfinder/`'s own `sandpoint` hub was exactly this, an
`entities = []` beneath a description promising market vendors. Background crowds (see
`docs/npc-generation.md`) are the primary fix; promotion is what covers the rest, when the player
reaches for someone the authored crowd doesn't include.

**The rule the whole feature rests on: the narrator never writes to the world.** Narration is
downstream of state and stays there. What triggers promotion is always the player's own literal
words. If the narrator invents a hooded figure, no hooded figure exists until the *player* says
"talk to the hooded figure."

Four layers decide, cheapest and most literal first, each able to veto alone
(`DM_Core.py`'s `_promote_addressed_npc`):

1. **Did the player address anyone at all?** `Intent_Classification.py`'s
   `extract_address_phrase` takes the remainder after whichever `DIALOGUE_KEYWORDS` phrase
   matched earliest (they're all prefixes), declines on a question opener or a pronoun, strips
   articles, and takes up to 3 words. Published as `address_phrase` on `dialogue_detected`,
   which carried no notion of *who* before. This is keyword-shaped machinery in a file that
   moved away from keyword tables, and it's acceptable only because it **fails closed**: an
   unrecognized shape returns `None`, which means no promotion, which means today's behavior.
   Nothing is created because it guessed well; things are only not created because it guessed
   badly. Known limitation: an addressee named *before* the keyword ("walk up to the merchant
   and ask what he is selling") isn't found — recovering it would mean guessing which earlier
   noun was some other verb's object, which fails open.
2. **Does anyone present match it literally?** `DM_Dialogue.py`'s `_literal_dialogue_target`,
   now scanning three phrases per entity — the instance key, the displayed `name`, and an
   optional `aliases` list. The key alone was a real gap once crowds existed: an instance keyed
   `sandpoint_townsfolk_2` but displayed as "Fishmonger" was unaddressable by the only name the
   player ever sees. `aliases` is the same mechanism `[[location.exit]]` uses for "the tavern"
   reaching "The White Deer Tavern and Inn", applied to people.
3. **Does anyone present match it semantically?** `NLP_Core.py`'s `map_to_present_entity`,
   against a scene-scoped bank `set_present_entities` replaces wholesale on every
   `scene_roster_updated` — mirroring `set_destinations`/`map_to_destination` exactly. Its
   threshold is deliberately the most permissive in the file (~0.45) because its error costs run
   backwards from everywhere else: a false positive merely suppresses promotion (today's
   behavior), while a false negative materializes a second barkeep beside the real one. When in
   doubt, decide the person is already here. The *global* `map_to_target` bank is the wrong tool
   — never scene-filtered, so "the merchant" would match a merchant three towns away.
4. **Safety gates, then the model's own right to decline** —
   `ImprovisationMixin._attempt_dialogue_promotion`. All checked before any network call:
   dialogue channel only; refused while any live hostile is anywhere in the scene (stricter than
   `_target_is_engaged`, which only inspects `current_target` — nobody wanders into a knife
   fight to sell you fruit); at most one per turn (structural — `classify` returns immediately
   after appending `dialogue_detected`); refused once the scene already holds
   `MAX_PROMOTED_PER_SCENE` ad hoc entities, a ceiling derived from live state so it needs no
   persistence and survives reload for free; and a result carrying `abilities`/`behavior` is
   **declined, not defanged** — a hostile result means the model misread the request, and
   stripping its teeth would leave an NPC whose description doesn't match what it is.

`resolution/AdHoc_Generation.py`'s `generate_referenced_npc` is the generator, a sibling of
`generate_ad_hoc_creature` reusing `_build_creature_tool_schema`, `_call_tool_or_decline`,
`_decline_tool_schema` and the shared `_build_creature_entity` unchanged. Only the framing and
the disposition enum differ: it's passed `NON_HOSTILE_DISPOSITIONS`, a **schema-level**
constraint rather than a post-filter, and since only a `hostile` creature is ever given
abilities/behavior, that single narrowing is what makes an automatically-materialized NPC
structurally incapable of affecting combat. The prompt carries the address phrase, the scene
description, the present roster, and `DMCore.recent_narration` — and is told to decline if the
phrase refers to someone already listed present, the third backstop behind layers 2 and 3.

**One field the model will not reliably give you.** Measured live against the shipped gemma4,
`create_creature` comes back with a good `name` and valid `keywords` but **no `description`** on
roughly seven of every eight calls, despite the schema marking it required. That is not a prompt
problem — enumerating the fields in the function description, documenting each property, and
narrowing the tool to `describe_npc`'s own proven three-field arity were each measured, and none
of them moved it; the difference appears to be field count, and `create_creature` asks for five.
Left alone it meant promotion declining as `"incomplete"` almost every time, for a reason having
nothing to do with whether the person belonged in the scene. So `_build_creature_entity` takes a
`fallback_description`, and `generate_referenced_npc` supplies one built from the player's own
noun phrase: "the blacksmith" becomes "A blacksmith." Nothing is invented — it is a restatement
of what the player already said was there. The fallback is scoped to this path deliberately;
`generate_ad_hoc_creature` has only a free-text request to work from, so it still declines on a
dropped description exactly as before. `test_integration.py`'s own `TestReferencedNpcLive` is what
holds this, since only a real model exhibits the behavior at all.

`DMCore.recent_narration` is a `deque(maxlen=RECENT_NARRATION_TURNS)` fed by subscribing to
`llm_response_ready` (DMCore's only cross-thread subscription — LLMCore publishes from a daemon
thread; `deque.append` under `maxlen` is atomic in CPython, so no lock, but nothing heavier than
an append belongs in that handler). It lives on DMCore rather than reading `LLMCore.context_window`
because DMCore holds no reference to LLMCore and needs the text synchronously, mid-decision. It
persists as its own `dm_state.json` key. **It grounds a promoted NPC's flavor and never triggers
one** — see the rule above.

**One narration, not two.** A promoted entity is already in `self.entities`/`scenario_entities`
by the time `_resolve_dialogue(..., forced_target=...)` runs, and passes every existing gate
(present, alive, not hidden, not an `"object"`) on its own merits — so the turn flows into
`generate_npc_dialogue`'s ordinary `found: True` branch with a real persona and attitude. No
second event fires. When promotion declines, the turn resolves exactly as it did before this
existed, landing on the grounded "there's no one like that here" narration that
`docs/narration-llm.md`'s "Denial-path grounding" describes — that denial is the floor beneath
this feature, not something it replaces.
