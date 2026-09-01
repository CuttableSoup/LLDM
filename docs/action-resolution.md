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
`RevealEffect`/`DefenderDetailsEffect`) instead of a fixed set of optional fields, so a new kind
of on-hit consequence is a new `Effect` subtype, not a new field every outcome carries unused.
`resolve_action`/`resolve_opposed_action` (`DM_Combat.py`) themselves keep returning a plain,
untyped roll dict — `DM_Rules.py`'s hidden-notice auto-roll (`_auto_roll_notice`) uses that raw
dict for an unrelated bool check with nothing to do with narration — so every narration-facing
call site builds its own `ActionOutcome` one layer up (`DM_Core.py`'s `_resolve_roll` and
friends, `DM_Crafting.py`'s `_try_craft_action`, `DM_Combat.py`'s `resolve_behavior_action`).
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
   "Entity tests"), or with no target at all (difficulty 0). Every dice roll here is reduced by
   this turn's own `dice_penalty` (see "Multiple actions").
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

**Resolution.** `dice_penalty = max(0, len(clauses) - 1)`, computed once per turn from the
combined item + action clause count and threaded through every dice-rolling action-kind entry:
`resolve_action`/`resolve_opposed_action` (`DM_Combat.py`) subtract whole dice (never pips) from
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

`LLMCore._describe_player_actions` describes every entry in `"actions"`, preceded by a line
naming the shared penalty whenever there's more than one, so narration reads as one character
splitting their attention rather than several independent, equally-precise attacks.

