# LLDM — Action Resolution

Part of the [LLDM](../CLAUDE.md) docs — the turn pipeline and the multi-action penalty.

## Action resolution pipeline

`user_input_submitted` → `NLPCore` → `turn_detected {clauses: [{kind: "item", intent,
item_name} | {kind: "action", skill, score, target?}, ...], input}` → `DMCore` resolves every
entry in `clauses` → `round_resolved` (combat) or `action_resolved` (no combat) → `LLMCore`
narrates → `llm_response_ready` → GUI/Textual display it. `clauses` is always a list, even for
the common single-clause input, and always mixes item-interaction and skill/ability entries
freely — see "Multiple actions" for how more than one entry changes resolution. Each resolved
action-kind entry is a typed `ActionOutcome` (`DM_ActionOutcome.py`) — a tagged union
(`RolledOutcome`/`OutOfRangeOutcome`/`LanguageBarrierOutcome`/`MissingSpellMaterialsOutcome`/
`NotCraftableOutcome`/`MissingStationOutcome`/`MissingMaterialsOutcome`/`MovementOutcome`), not a
loosely-shaped dict — populated into the `action_resolved`/`round_resolved` envelope's own `"actions"` list
(the envelope itself stays a plain dict, like every other `EventBus` payload). A `RolledOutcome`
carries a list of `Effect`s (`DamageEffect`/`LootEffect`/`SummonEffect`/`CraftEffect`/
`RevealEffect`/`TeleportEffect`/`DefenderDetailsEffect`) instead of a fixed set of optional
fields, so a new kind of on-hit consequence is a new `Effect` subtype, not a new field every
outcome carries unused. `TeleportEffect` is `_apply_teleport_if_hit`'s own (`DM_Core.py`) --
an ability's `teleport_to_band`/`teleport_to_location` field relocates the player outright on
a successful cast (Dimension Door/Teleport, Pathfinder-mapping terms), the same "not really
against anyone" scope a summon already has.
`resolve_action`/`resolve_opposed_action` (`Combat_Resolution.py`) themselves keep returning a plain,
untyped roll dict — `DM_Rules.py`'s hidden-notice auto-roll (`_auto_roll_notice`) uses that raw
dict for an unrelated bool check with nothing to do with narration — so every narration-facing
call site builds its own `ActionOutcome` one layer up (`DM_Core.py`'s `_resolve_roll` and
friends, `DM_Crafting.py`'s `_try_craft_action`, `Combat_Actions.py`'s `resolve_behavior_action`).
`_on_turn_detected` and `_on_item_interaction_detected` both also call `_publish_party_status`,
which re-publishes `party_status_changed {"entities": self.entities}` so `GUICore`'s Party tab
redraws after anything that could have changed a party member's HP/equipment/inventory/
conditions.

Inside `DMCore._on_turn_detected`, an item-kind entry is resolved immediately, in clause order,
via the ordinary `_on_item_interaction_detected` (narrating separately, right away); an
action-kind entry goes through:
1. Resolves the acting skill's ability (weapon/spell/technique/innate) via
   `resolve_named_ability`/`select_ability_skill` if the matched name is an ability, else
   `find_attack_ability` for a bare skill.
2. If the ability has a range and the target is out of it (`is_in_range`), the action fails
   immediately as an `OutOfRangeOutcome` — no roll happens. Same shape, right alongside it: a
   `language_dependent` ability/skill (`_ability_requires_language`, see "Combat"'s own "Tags vs.
   conditions") against a target the player's own current language isn't shared with fails
   immediately as a `LanguageBarrierOutcome` — also no roll.
3. Otherwise resolves against `self.current_target` (see "Combat") — as a flat check against the
   target's own `[entity.test]` if one matches (ex: a chest's lock), else a flat check against
   the ability's own authored `difficulty` if it has one (ex: `spells.toml`'s `suggestion`/
   `fireball` — the number the caster needs to roll on the ability's own skill to pull it off at
   all, independent of the target; a target that actually wants to resist authors its own
   `[entity.test]` instead, which always wins when it matches), else the ordinary opposed roll
   against the defender's own best matching skill — or against an item-level `[entity.test]`
   target one level deeper (a container's contents or something already in inventory — see
   "Entity tests"), or with no target at all — see "Unopposed checks" below. Every dice roll here
   is reduced by this turn's own `dice_penalty` (see "Multiple actions").
4. On a hit, `calculate_damage` rolls damage, resolves the `bonus` field (plain number or
   `"user.<rule>"` reference into `rules.toml`), applies armor/resistance reduction and
   vulnerability bonus, and `apply_damage` applies net damage to HP; the `RolledOutcome` gets
   a `DamageEffect` appended to its own `effects` list. Damage itself is never reduced by
   `dice_penalty` — only the skill/action roll that earned it.
5. `apply_damage` also calls `evaluate_statuses(entity_name, "on_damage")` (see "Status and
   conditions").

Once every action-kind entry has resolved, `DMCore` decides `round_resolved` vs.
`action_resolved` — and, for combat, runs every other scene entity's own turn — exactly once
for the whole batch, not once per entry (see "Multiple actions").


## Ambiguous input: asking the model

`IntentClassifier`'s rules (opening words, speech verbs, hypotheticals, skill-match scores) decide
most lines on their own. For the lines they can only guess at, it asks the local model what the
line mainly is (`_adjudicate` → `IntentMatcher.adjudicate` → `AdHoc_Generation.py`'s
`adjudicate_player_input`): `action`, `speech`, `game_question` or `musing`. Only with someone
present, and in three cases:

- **declarative**: the rules called a line talk on its opening words alone, and it has no "?" and
  doesn't open on a question word ("let's go down that cut-through.", "i'll just grab something
  useful off it."). An `action` verdict sends it to the ordinary skill/item passes instead.
- **weak_turn**: every skill clause of the turn scored below `WEAK_TURN_SCORE` (0.6).
- **weak_quoted**: the same, for the action half of a line with quoted speech beside it
  (`_split_quoted_speech`); anything but `action` keeps the whole line as talk. Found by playtest:
  "casually reach out, tapping the heavy metal ring on his wrist" beside a quote rolled polearms
  ("reach") at 0.52. The `speech` kind's own description counts the small gestures that go with
  words (a smirk, a tap on the arm) as part of speaking.
- **not_understood**: nothing else claimed the line.

The model only picks the channel; the existing machinery still does the matching (`speech` →
dialogue, `game_question` → ADaM, `musing` → the not-understood reply). It's one enum-constrained
tool call with reasoning off and temperature 0 (at the client's default 0.7 the same line routed
differently run to run), told what the scene's people, the conversation partner and the latest
narration (`set_recent_narration`, fed from `llm_response_ready`) are. It answers in about 0.7s,
on roughly half of all lines. No answer (model unreachable, off-list reply) leaves the rules' call,
including the "!" rule for unclaimed barked lines. Each verdict is logged ("Adjudicated
ambiguous input (trigger): verdict"). A persuade/haggle/intimidate attempt counts as `action`,
and a remark made while talking to someone as `speech` — without saying so, "i'll bargain with
her over the cost of supper" lost its roll.

Measured on `tests/player_input_corpus.toml` mid-conversation (Pathfinder): talk reaching dialogue
42/52 → 49/52, with actions (40/49 acted on, 1 swallowed) and social-skill rolls (9/12)
unchanged. The unit tests stub the call module-wide (`_NO_ADJUDICATION`), so the corpus tests
measure the rules alone.


## Unopposed checks

A skill use with nothing resisting it — no target, no `[entity.test]` — used to roll against 0
and so could never fail (~800 playtest turns of searching, climbing and sneaking, every one a
success). Now `DMCore._untargeted_difficulty` asks the local model (`AdHoc_Generation.py`'s
`rate_difficulty`, one tool call constrained to an enum) to pick one of the setting's
`[[difficulty_tier]]` names from the attempt and the scene — `rules.toml` authors the numbers,
calibrated to this engine's plain d6 sums (2D untrained, 4D trained, 5D expert) rather than WEG's
attribute+skill pools: very easy 3, easy 6, moderate 10, difficult 15, very difficult 18, heroic 22
(at WEG's own 10/15/20 a 2D skill could never pass moderate — a playtest failed 13 of 20) — or
`"trivial"`, meaning no roll at all (`RolledOutcome.trivial`; narrated as simply happening). If
the model is unreachable, declines, or answers off the list, the skill's own optional
`default_difficulty` (a tier name in `skills.toml`) is used, else `[difficulty].fallback`; a
warning is logged when the model was unreachable. A setting that authors no tiers (`Rules/Zombie/`)
keeps the old difficulty 0. A named spell/technique cast at no one is never rated — it keeps its
authored automatic success (`spells.toml`: "summoning before a fight starts is trivial"). The call
runs on the game thread before the roll, with the model's reasoning turned off (`reasoning_effort =
"none"`: measured on gemma4, 5-15s and occasional token-limit failures with it on, under a second
with it off), so each unopposed check costs about a second; unit
tests never make it (`test_unit.py` stubs `_untargeted_difficulty` module-wide to the old 0, and
`TestUntargetedDifficulty` restores it against a stubbed chat client).


## Multiple actions

The player may attempt more than one action in a single turn — the West End Games D6
"multiple actions" rule: every action beyond the first, movement and speech excepted, costs
every one of that turn's actions a cumulative -1D (two actions: -1D each; three: -2D each;
...). Movement (`advance`/`retreat`) and speech (see "Dialogue") never reach
`_on_turn_detected` at all — they're their own diceless event/pipelines — so they're free by
construction.

**Item interactions count too.** Drawing a weapon, picking something up, giving/trading/
opening/using an item are all "an action" in the same sense swinging a sword is — a diceless
item interaction (examine/equip/unequip/drop/take/give/trade/open/close/use — see "Items and
movement as intents") costs a turn slot exactly like a skill/ability entry does. It just never
receives `dice_penalty` itself, since it never rolled anything to begin with (an item *test*
that does roll, ex: picking a lock, both counts *and* gets penalized).

**Detection.** `Intent_Classification.py`'s `IntentClassifier.classify` splits input into
clauses once (`split_action_clauses`, on `ACTION_CLAUSE_PATTERN`: `--`, `?`, `,`, `;`, `:`, and
the standalone words `"and"`/`"then"`, `\b`-anchored so a word merely containing one of those
substrings never splits), after save/load, inter-room movement, and location-to-location travel
have all had their whole-input shot (in that order — see "Location-to-location travel"). Each
clause is classified independently, in two passes:
1. **Item-interaction pass.** `detect_item_intent` runs per clause. `EXEMPT_ITEM_INTENTS`
   (`advance`/`retreat`/`formation_behind`/`formation_abreast`) publish their own free-standing
   `item_interaction_detected` immediately and never join the shared turn (so `"attack the wolf
   and retreat"` still lets the retreat through). Everything else resolving as an item
   interaction joins the shared clause list as a `{"kind": "item", ...}` entry; a clause that
   doesn't is deferred to pass 2.
2. **Dialogue, then skill/ability matching.** Dialogue detection runs once against the whole
   input, only once pass 1 found nothing at all, so a genuine item verb naming an entity (ex:
   `"give the sword to Anne"`) is never swallowed as dialogue. Whatever pass 1 didn't claim is
   matched via `map_to_action`/`map_to_target` per clause, joining the list as a `{"kind":
   "action", ...}` entry — a clause missing `confidence_threshold` is simply dropped, not
   reported as `action_not_understood` on its own. A plain single-clause input always resolves
   to a list of exactly one entry. Same-skill-multiplier phrasing (ex: "attack it twice") is
   out of scope — only distinct clauses are detected as distinct actions.

**Last-chance semantic routing.** Every intent gate above is a literal substring table
(`_keyword_gate`), which can only ever recognize phrasings someone thought to list:
`SCENE_QUERY_KEYWORDS` had `"who is here"` but not `"who all is here"`, `TRAVEL_KEYWORDS` had
`"head to "` but not `"head into"` — trivial paraphrases to a human, total misses to a substring
check, and a tail that doesn't converge however many get added (the observed failure: `"who all
is here"` reached `action_not_understood`, whose clarification prompt then invented three tavern
patrons). So `_finalize` consults one more matcher call, `map_to_intent`, against
`INTENT_PROTOTYPES` — a handful of example phrasings per routable intent, embedded in the same
space skill/item/target matching already uses — and on a confident hit publishes the *same* event
that intent's own keyword gate would have, so nothing downstream can tell the two producers
apart. **It runs only at the give-up point**, after both passes have declined, which is the whole
safety argument: it can never shadow a skill, item, or dialogue match that already succeeded, so
the worst case is a wrong answer where an honest non-answer would have been. It sits *ahead* of
`improvisation_requested` but must clear a higher bar (`intent_override_threshold`) to displace
it, because "after improvisation" would mean *never* for any input carrying a recognized item
verb — `DM_Improvisation.py`'s own decline path publishes `action_not_understood` itself, from
DMCore, where there is no matcher left to ask — and that overlap is the worst one to lose:
`EXAMINE_KEYWORDS`' own `"check out"` makes `"check out the room"` a recognized examine verb, so
it would reach ad hoc item generation and be asked to conjure "the room" as a takeable object.
Two things keep the false-positive rate down, and the *first* matters more than the second: a
deliberate `OTHER_INTENT` ("none of the above") prototype bucket, without which `argmax` picks a
real intent for literally every input on earth and an absolute cosine cutoff is the only defense;
and thresholds set above `confidence_threshold`, because the error costs run opposite to item
matching here — the right answer is usually *not* in the catalog, and a mis-routed travel
confidently narrates "there's no way through in that direction" where "I don't understand" was
correct, while a false negative costs exactly nothing (it's the old behavior). Scope is read-only
and low-stakes intents only; no item-named intent is routed this way. The two routes that *do*
change state — `travel` (a confident destination match really moves the player) and `rest` (the
clock) — are never taken from input containing a `?` (`QUESTION_BLOCKED_ROUTES`): anything
reaching the router already missed every keyword gate, so a question there is talk, not a command
(playtest: "is that argument about the docks…?" walked the player to the shipyard). Calibration is an
executable artifact, not prose —
`test_intent_router_separates_held_out_paraphrases_from_ordinary_actions` scores a held-out
battery through the real model, asserting the phrases aren't themselves prototypes so it measures
generalization rather than memorization.

**Resolution.** `dice_penalty = max(0, len(clauses) - 1)`, computed once per turn from the
combined item + action clause count and threaded through every dice-rolling action-kind entry:
`resolve_action`/`resolve_opposed_action` (`Combat_Resolution.py`) subtract whole dice (never pips) from
the *acting* entity's pool, floored at 0. For an opposed roll only the attacker's roll is
reduced — the defender's difficulty roll is computed before `dice_penalty` is applied.
`_on_turn_detected` loops every clause: item-kind entries resolve immediately via
`_on_item_interaction_detected`; action-kind entries resolve through the same phase helpers a
lone action always has, collecting into `player_actions`. The whole turn calls
`_resolve_combat_round` exactly once, after every action-kind entry resolves (tracked via
`engaged_combat_target`: an item-interaction/item-test-only turn must never trigger a round just
because `self.current_target` happens to already be hostile from an earlier turn). Item-kind
entries narrate separately (their own `item_interaction_resolved`, ahead of the batched
action-kind entries) rather than folding into one merged prompt.

`Narration_Prompts.describe_player_actions` describes every entry in `"actions"`, preceded by a line
naming the shared penalty whenever there's more than one, so narration reads as one character
splitting their attention rather than several independent, equally-precise attacks.

