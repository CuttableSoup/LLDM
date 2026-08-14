# LLDM

An autonomous dungeon master: the player types free-text actions, NLP maps them to a skill,
a simplified D6 (West End Games) engine rolls dice and resolves outcomes, and a local LLM
(currently Gemma via LM Studio at `http://127.0.0.1:1234`) narrates what happened. Skills,
entities, items, spells, rules, and scenarios are all data-driven via TOML, organized into
"settings" — self-contained sibling directories under `Rules/` (`Rules/Fantasy/`,
`Rules/Zombie/`), each independently scanned by `load_rules`. None of the engine itself is
fantasy-specific — `DMCore(event_bus, scenario_name, setting="Fantasy")`'s own `setting` param
picks which one to boot from (`Rules/<setting>/scenarios/<scenario_name>.toml` and every
sibling `Rules/<setting>/*.toml`), and it round-trips through a save file (`dm_state.json`'s
own `"setting"` key) so a resumed save reloads from the same setting it was saved under.
`Rules/Fantasy/` is the original, deep setting; `Rules/Zombie/` is a bare-bones second one (a
Left 4 Dead-inspired survival shooter — common/special infected, firearms/melee/throwables) that
exists specifically to prove the engine out as setting-agnostic rather than quietly fantasy-
coupled — see its own scenario at `Rules/Zombie/scenarios/rooftop.toml` (`python LLDM.py
rooftop --setting Zombie` boots straight into it). Every setting authors its own skills/rules/
races from scratch — nothing is shared or inherited between settings, deliberately, so one
setting's own data can never silently leak into another's. GUI-driven character creation
(Character → Create...) and NPC generation are still wired to `Rules/Fantasy/` only; a second
setting today is reachable only via `LLDM.py`'s own CLI quick-boot (`--setting`) or a save file
that already carries its own `"setting"`.

## Architecture

Six modules wired through `Event_Bus.py`, a synchronous pub/sub bus (`publish` calls every
subscriber immediately, over a snapshot of that event's subscriber list taken at the start of
the call — not the live list — so a handler that itself subscribes a new callback for the
event it's currently handling doesn't have that callback also invoked within the same
`publish`; it only starts firing on the *next* one). `LLDM.py` boots `NLPCore`, `LLMCore`,
`GUICore` in that order at startup, but **not** `DMCore` — see "Booting the game" below for
when and how it's actually constructed.

- **`DM_Core.py`** — `DMCore`'s `__init__` plus its three event handlers
  (`_on_turn_detected`, `_on_item_interaction_detected`, `_on_dialogue_detected`) and their
  direct helpers: the orchestration that spans every domain mixin. `_on_turn_detected` also
  calls `_on_item_interaction_detected` directly, once per item-kind clause in a mixed turn —
  see "Multiple actions" below. The class itself is
  composed from sibling mixin files, each owning one concern: `DM_Rules.py` (TOML/scenario/room
  loading), `DM_Combat.py` (dice rolling, opposed checks, damage, ability/behavior resolution),
  `DM_Status.py` (statuses/conditions, entity tests), `DM_Inventory.py` (currency/item transfer,
  plus the equip/unequip/drop/use/container item-interaction intents), `DM_Social.py`
  (attitudes, character description), `DM_Movement.py` (bands, range, plus the room-transition/
  formation item-interaction intents), `DM_Persistence.py` (save/load), `DM_CharacterCreation.py`
  (baking a finished character-creation result onto the player entity — see "Character
  creation" below), `DM_Dialogue.py` (resolving who's being directly addressed in
  free-form conversation — see "Dialogue" below), `DM_Help.py` (the reserved "ADaM"
  out-of-character help persona — see "ADaM (out-of-character help)" below), and
  `DM_Improvisation.py` (ad hoc entity creation/removal via LLM function calling — see "Ad hoc
  entity creation and removal" below). Python's MRO
  flattens every mixin method onto one `DMCore` instance, so `dm_core.<method>(...)` call sites
  don't care which file defines a given method.
- **`NLP_Core.py`** — `sentence-transformers` (`all-MiniLM-L6-v2`) embeds each skill's
  name/description/keywords as separate phrases, then cosine-matches player input against all
  of them. Also matches free text against item names/directions/save-load prefixes for
  non-skill intents (see "Items and movement as intents" below), against `DIALOGUE_KEYWORDS`
  for free-form conversational address (see "Dialogue" below), and against the reserved
  `ADAM_NAME_PATTERN` for the out-of-character help persona (see "ADaM (out-of-character help)"
  below) — also gated by `REMOVAL_KEYWORDS`, a cheap local pre-check for whether an
  ADaM-addressed message is worth a synchronous ad hoc-removal LLM call (see "Ad hoc entity
  creation and removal" below). An item-verb clause whose own item name doesn't match anything
  is tracked and, if the whole turn would otherwise resolve to nothing at all, published as
  `improvisation_requested` instead of `action_not_understood` — the ad hoc entity creation
  fallback (same section). Also incrementally re-registers a newly-created/reload-restored ad
  hoc entity's own name/description into its item-matching embeddings on `item_catalog_updated`,
  the one event here that isn't itself input-driven. Splits the input into one or
  more clauses (`_split_action_clauses`) and classifies each independently as an item
  interaction or a skill/ability action — merged into one `turn_detected {clauses: [{kind:
  "item", intent, item_name} | {kind: "action", skill, score, target?}, ...], input}` event,
  always a list, even for the ordinary single-clause input (see "Multiple actions" below for
  the full classification order, including where dialogue and exempt movement/formation
  clauses fit in); if no clause resolves to anything at all, publishes `action_not_understood`
  instead.
- **`LLM_Core.py`** — posts to LM Studio's OpenAI-compatible `/v1/chat/completions` on a
  background thread, with a rolling 100-message context window. Subscribes to eight narration
  triggers (see "Narration" below).
- **`GUI_Core.py`** — Tkinter window: history pane + tabbed Party/Notes/Map/Debug panels, plus
  three dropdown menus on the window's menu bar: Character (Create... only), File (Save.../
  Load...), and Scenario (Load... only). Character -> Create... opens the race/point-buy
  dialog (`Character_Creation_GUI.py`) and publishes `character_created`, then (if a game
  hasn't already started — see `_on_game_started` below) stashes the result as
  `self._pending_character` and unlocks Scenario -> Load...; File -> Load... opens a
  slot-picker popup (listing every subdirectory of `Saves/`) and publishes `load_requested`
  directly, since a save already carries its own scenario — it lives under File, not
  Character, since it's a save-file operation rather than a character one. Scenario -> Load...
  is `DISABLED` until a character is pending; picking it opens a popup listing every real
  scenario (`list_available_scenarios`, `DM_Rules.py` — `character_test` excluded) and, on a
  selection, publishes `scenario_selected {"scenario_name", "character"}` paired with
  `self._pending_character`, then locks itself shut again. `_on_game_started` (subscribed to
  `rules_loaded`, which fires once per `DMCore` construction across every boot route) sets
  `self._game_started` and locks Scenario -> Load... shut for the rest of the session, so a
  later Create... can't reopen it once a game already exists. `GUICore` never constructs a
  `DMCore` itself — it only ever publishes; see "Booting the game" below for who's listening.
  History mirrors `llm_response_ready`; Party redraws on `rules_loaded`/`party_status_changed`
  as a `ttk.Treeview` (one node per `is_player`/`is_party` entity, expanding into Equipment/
  Skills/Abilities/Inventory/Conditions — Equipment lists every valid slot for the member's own
  supertype/subtype, filled or `(empty)`, via `get_equip_slots`'s same override precedence as
  `get_attitude`, see "Data/TOML conventions"). Membership is filtered through the payload's
  own `"scenario_entities"` list, not `is_player`/`is_party` alone — `self.entities` also still
  holds every *uninstanced* template from every loaded TOML file (ex: `characters.toml`'s
  `anne`, `is_party = true`, but not part of `arena.toml`'s own entities list), which must not
  show up on the Party tab just for existing on disk; `DM_Combat.py`'s
  `get_party_challenge_rating` filters the same way (see "Challenge rating"). Notes is a
  free-typed scratchpad with its own save/load slice. Map is a free-form drawing canvas the
  engine never reads. Debug overwrites (not appends) the most recent LLM request/response on
  every `llm_debug_updated`.
- **`Textual_Core.py`** — a parallel, headless-testable mirror of `GUI_Core`'s output, driven
  the same way via `user_input_submitted`. Not part of `LLDM.py`'s boot sequence; run standalone.
  Used by `test_unit.py` for pilot-driven UI tests.
- **`Logger.py`** — subscribes to `log_info`/`log_error`, prints with timestamps.

## Action resolution pipeline

`user_input_submitted` → `NLPCore` → `turn_detected {clauses: [{kind: "item", intent,
item_name} | {kind: "action", skill, score, target?}, ...], input}` → `DMCore` resolves every
entry in `clauses` → `round_resolved` (combat) or `action_resolved` (no combat) → `LLMCore`
narrates → `llm_response_ready` → GUI/Textual display it. `clauses` is always a list, even for
the overwhelmingly common single-clause input, and always mixes item-interaction and
skill/ability entries freely — see "Multiple actions" below for why these two used to be
separate pipelines and why that changed, and for how more than one entry changes resolution.
`_on_turn_detected` and `_on_item_interaction_detected` (see "Items and movement as intents"
below) both also call `_publish_party_status`, which re-publishes `party_status_changed
{"entities": self.entities}` so `GUICore`'s Party tab redraws after anything that could have
changed a party member's own HP/equipment/inventory/conditions.

Inside `DMCore._on_turn_detected`, an item-kind entry is resolved immediately, in clause
order, via the ordinary `_on_item_interaction_detected` (narrating separately, right away —
see "Items and movement as intents"); an action-kind entry goes through:
1. Resolves the acting skill's ability (weapon/spell/technique/innate) via
   `resolve_named_ability`/`select_ability_skill` if the matched name is an ability, else
   `find_attack_ability` for a bare skill.
2. If the ability has a range and the target is out of it (`is_in_range`), the action fails
   immediately with `reason = "out_of_range"` — no roll happens.
3. Otherwise resolves against `self.current_target` (see "Combat" below), or against an
   item-level `[entity.test]` target one level deeper (a container's contents or something
   already in inventory — see "Entity tests"), or with no target at all (difficulty 0). Every
   dice roll here (opposed or item-test) is reduced by this turn's own `dice_penalty` — see
   "Multiple actions".
4. On a hit, `calculate_damage` rolls damage, resolves the `bonus` field (plain number or
   `"user.<rule>"` reference into `rules.toml`), applies armor/resistance reduction and
   vulnerability bonus, and `apply_damage` applies net damage to HP. Damage itself is never
   reduced by `dice_penalty` — only the skill/action roll that earned it.
5. `apply_damage` also calls `evaluate_statuses(entity_name, "on_damage")` (see "Status and
   conditions").

Once every action-kind entry has resolved, `DMCore` decides `round_resolved` vs.
`action_resolved` — and, for combat, runs every other scene entity's own turn — exactly once
for the whole batch, not once per entry (see "Multiple actions").

## Combat

Combat is a target being present *and* `is_hostile(target_name, player_name)`, which has two
distinct defaults, deliberately not collapsed into one: an entity with **no**
`[entity.attitudes]` table at all (ex: `creatures.toml`'s wolf/bandit, which declare no
attitude data whatsoever) is hostile unconditionally — a monster that never bothered to
author a disposition is still a monster. An entity that **does** declare attitude data
instead has to reach true hostility, `disposition <= -100`, to actually fight — a merely
wary/negative-but-not-murderous disposition (ex: -40) is dialogue, not combat ("you can
dislike someone and not be hostile"), which is what lets a generated NPC's own resolved
disposition (see "NPC generation") land anywhere from wary to warm without every non-friendly
roll turning into a fight. An entity with `supertype == "object"` is never treated as hostile
regardless of attitude data. `is_hostile(entity, player_name)` distinguishes enemies (attack
the player) from allies (attack `self.current_target` instead, if they carry their own
`[[entity.behavior]]` data).

If in combat, `round_number` increments and the round publishes as `round_resolved` (one
narration per round). Otherwise it publishes as `action_resolved` (one narration per skill
use) — the path a dialogue check against a friendly NPC also takes.

A `round_resolved` payload carries the player's own resolved actions (`"actions"`, a list —
see "Multiple actions") plus `"turns"`: every other living scene entity's own resolved action
via `resolve_behavior_action`
(`DM_Combat.py`), driven by each entity's `[[entity.behavior]]` table — a declaration-order
list of `{requirements, action}` entries, matched top-down (requirements compared the same
way `[[status]]` requirements are — see "Status and conditions"). `turns` is sorted by
initiative: `roll_initiative(entity_name)` pools every skill named in `rules.toml`'s
`[[initiative]]` list, rolling once per round; an entity lacking a listed skill defaults to
untrained (0D/0 pips). Initiative only orders narration — every actor resolves independently
against state as of the start of the round, not sequentially. `current_target` only advances
(to the next living hostile entity, or the first living non-player entity if none is hostile)
once, at the end of the round, if it died.

A behavior entry's `action` is either an ability name or one of two reserved movement words,
`"advance"`/`"retreat"` (`MOVEMENT_ACTIONS`, `DM_Combat.py`), routed to `move_toward_or_away`
instead of an ability lookup. An explicit `"retreat"` entry is how a creature values its own
life — checked ahead of its attack entry, ex: `creatures.toml`'s wolf/giant spider/bandit flee
once `hp_per_remain` drops under 0.40 (the same cutoff `rules.toml`'s `"wounded"` tier bottoms
out at); an undead/construct entity has no such entry and fights on regardless. Separately,
`resolve_behavior_action` falls back to `"advance"` on its own whenever its chosen action
can't currently reach its target (`is_in_range`) — closing distance instead of standing idle.

## Multiple actions

The player may attempt more than one action in a single turn — the West End Games D6
"multiple actions" rule: every action beyond the first, movement and speech excepted, costs
every one of that turn's actions a cumulative -1D (two actions: -1D each; three: -2D each;
...). Movement (`advance`/`retreat`) and speech (see "Dialogue") never reach
`_on_turn_detected` at all — they're their own diceless event/pipelines — so they're free by
construction, not by a special case inside this mechanic.

**Item interactions count too.** Drawing a weapon, picking something up, giving/trading/
opening/using an item are all "an action" in the same sense swinging a sword is — WEG doesn't
carve out an exception for them the way it does for movement/speech, so a diceless item
interaction (examine/equip/unequip/drop/take/give/trade/open/close/use — see "Items and
movement as intents") costs a turn slot exactly like a skill/ability entry does. It just never
receives `dice_penalty` itself, since it never rolled anything to begin with — it counts
toward the shared total without ever being reduced by it, the same treatment a diceless item
*test* would get if one existed with nothing to roll (today, every item test does roll — ex:
picking a lock, appraising a potion — so it both counts *and* gets penalized, exactly like an
opposed attack). This wasn't always true: item interactions and skill/ability actions used to
be two entirely separate, mutually exclusive pipelines — `NLPCore` resolved a whole input as
*either* one item interaction *or* a batch of skill clauses, never both, so `"give the sword
to Anne and attack the wolf"` silently dropped the attack entirely. They're now merged into
one shared per-turn economy (see "Detection" below) specifically to close that gap and to stop
letting a player chain unlimited free equip/take/give actions alongside a full-power attack.

**Detection.** `NLPCore._on_user_input` splits the input into clauses once
(`_split_action_clauses`, on `ACTION_CLAUSE_PATTERN`: `--`, `?`, `,`, `;`, `:`, and the
standalone words `"and"`/`"then"`, `\b`-anchored so a word merely containing one of those
substrings — ex: "handle", "sandbox" — never splits), after save/load and inter-room movement
have already had their whole-input shot ahead of it. Each clause is then classified
independently, in two passes:
1. **Item-interaction pass.** `_detect_item_intent` runs per clause. `EXEMPT_ITEM_INTENTS`
   (`advance`/`retreat`/`formation_behind`/`formation_abreast` — movement/directing-the-party,
   free per WEG's own exceptions) publish their own free-standing `item_interaction_detected`
   immediately, in clause order, and never join the shared turn at all — same as inter-room
   movement, just resolved per clause instead of against the whole input (so `"attack the wolf
   and retreat"` still lets the retreat through). Everything else that resolves as an item
   interaction — `NO_ITEM_LOOKUP_INTENTS`' `"open"`/`"close"` (act on the scene target
   directly, no `map_to_item` needed) or any other intent with a confidently-matched
   `item_name` — joins the shared clause list as a `{"kind": "item", ...}` entry. A clause
   that doesn't resolve as an item interaction at all is deferred to pass 2.
2. **Dialogue, then skill/ability matching.** Dialogue detection still runs once, against the
   *whole* input, not per clause (left that way deliberately — full multi-intent-type
   composition involving dialogue is still out of scope) — but only once pass 1 found nothing
   at all (no item interaction, no exempt movement/formation clause), preserving the exact
   priority the old single-clause code already gave item intents over dialogue (so a genuine
   item verb naming an entity, ex: `"give the sword to Anne"`, is never swallowed as dialogue).
   Whatever pass 1 didn't already claim is then matched via `map_to_action`/`map_to_target`,
   per clause, joining the same list as a `{"kind": "action", ...}` entry — a clause that
   misses `confidence_threshold` is simply dropped, not counted, and not reported as
   `action_not_understood` on its own (only the whole input is, and only if *nothing* matched
   at all, including no exempt clause). A plain single-clause, single-kind input (the
   overwhelming common case) always resolves to a list of exactly one entry — a strict
   generalization of the pre-existing single-match path, not a special case bolted on top of
   it. Same-skill-multiplier phrasing (ex: "attack it twice") is explicitly out of scope —
   only distinct clauses are detected as distinct actions.

**Resolution.** `dice_penalty = max(0, len(clauses) - 1)`, computed once per turn from the
*combined* item + action clause count, and threaded through every dice-rolling action-kind
entry: `resolve_action`/`resolve_opposed_action` (`DM_Combat.py`) both accept an optional
`dice_penalty` param that subtracts whole dice (never pips) from the *acting* entity's own
pool before rolling, floored at 0 dice. For an opposed roll, only the attacker's own roll is
reduced — the defender isn't the one splitting their attention across multiple actions this
turn, so `resolve_opposed_action`'s own difficulty roll is computed from `defender_stats`
before `dice_penalty` is ever applied. `_on_turn_detected` loops every entry in `clauses`: an
item-kind entry resolves immediately via the ordinary, entirely unchanged
`_on_item_interaction_detected` (its own resolution logic never rolled anything, so it needs
no `dice_penalty` awareness at all — it's simply called once per item-kind clause here instead
of being the sole top-level handler for a whole input); an action-kind entry resolves exactly
the way a lone action always has (same phase helpers — `_resolve_action_skill`,
`_try_item_test_action`, `_apply_target_redirect`, `_resolve_roll`, `_apply_damage_if_hit`,
`_attach_defender_details`), collecting into `player_actions`. The whole turn still only ever
calls `_resolve_combat_round` once, after every one of the player's own action-kind entries has
resolved (tracked via `engaged_combat_target`: an item-interaction/item-test-only turn never
engages `self.current_target` at all and must never trigger a round just because that
persistent, cross-turn field happens to already be hostile from some earlier turn). This is
what keeps a multi-action turn to exactly one round, no matter how many actions it contains —
everyone else in the scene still only ever gets their own one turn per round. Item-kind
entries narrate separately (via their own `item_interaction_resolved`, in clause order, ahead
of the batched action-kind entries) rather than folding into one merged prompt — two or more
sequential narration beats, same as any other multi-part turn, not a bigger prompt-engineering
problem for `LLM_Core.py` to solve.

`LLMCore._describe_player_actions` describes every entry in `"actions"` (one
`_describe_outcome` line each), preceded by a line naming the shared penalty whenever there's
more than one, so the narration reads as one character splitting their attention across
several things at once rather than several independent, equally-precise attacks.

## Challenge rating

`Challenge_Rating.py` is a pure, DMCore-independent module (same "pure, entity-shape-agnostic"
precedent `Character_Creation.py` sets) computing a single number for "how powerful is this
entity," built entirely from its own dice/pips — no separately-justified weighting constants.
`skill_rating(dice, pips)` is `dice * 3 + pips`, the shared "3 pips = 1 die" scale
`DM_Combat.py`'s `get_opposing_skill`/`select_ability_skill` already rated skills on before
this module extracted it into one place. `calculate_challenge_rating(skills, max_hp,
damage_dice=0, damage_pips=0, top_n=3)` sums three components on that same scale:
- **skill** — the average `skill_rating` of the entity's `top_n` (default 3) best-*trained*
  skills, not every skill it has. A flat sum would let a character trained broadly but
  shallowly across dozens of noncombat skills (ex: a full 2D-baseline skill table) outrank a
  boss creature authored with only 2-3 trained skills.
- **hp** — `max_hp // 3`, the same `/3` scale as pips-to-dice, so a flat stat with no dice of
  its own still lands in comparable units.
- **damage** — `skill_rating` of the entity's single best damage-dealing weapon/ability's own
  `dice`/`pips` — not its `bonus` field, which can be a `rules.toml` formula reference rather
  than a flat number, and isn't "dice and pips" in the first place.

`calculate_party_challenge_rating(member_ratings)` is a plain sum, not an average — a larger
party of individually modest ratings can still outrate one strong boss.

`DM_Combat.py`'s `get_challenge_rating(entity_name)`/`get_party_challenge_rating()` are the
DMCore-touching glue (the same split `DM_CharacterCreation.py` is to `Character_Creation.py`):
`_best_damage_dice_pips` finds the best `dice`/`pips` from every equipped item plus every
resolved ability with a `damage_value` (the same candidate pool `find_attack_ability` draws
from, just not filtered to one particular skill — nothing here is about to be rolled, so
there's no skill to disambiguate by), resolving `"user.weapon.<field>"` indirection
(`resolve_weapon_reference`) the same way a real attack would. `get_party_challenge_rating`
filters through `self.scenario_entities`, not a blind `is_player`/`is_party` scan of
`self.entities` — `self.entities` also still holds every *uninstanced* template from every
loaded TOML file (ex: `characters.toml`'s `anne`, `is_party = true`, but not part of
`arena.toml`'s own entities list), and those must not count just for existing on disk.

## NPC generation

`Rules/Fantasy/templates.toml` holds `[[entity_template]]` tables — a genuinely different
top-level key from `[[entity]]`, loaded by `load_rules` (`DM_Rules.py`) into their own
`self.entity_templates` dict, never `self.entities`. This is deliberate: keeping generation
stubs in a separate namespace is what makes one impossible to reference by accident — a
scenario/room entry names a real entity via `{ name = "wolf", band = 1 }` (looked up in
`self.entities`, unchanged) but an entity_template via `{ template = "generated_stranger", band =
2 }` (looked up in `self.entity_templates` instead) — `_instance_entities` (`DM_Rules.py`)
checks which key an entry actually has and resolves accordingly; a `name` that only exists as
an entity_template, or a `template` that only exists as a real entity, both fail the same
`log_error`-and-skip "unknown entity"/"unknown entity template" way a genuine typo would,
never silently resolving through the wrong dict. An entity_template skips authoring its own
`[entity.skills]`/`max_hp`/`name` — those are decided the moment it's instanced into a live
scene instead, via a local-LLM function call fit to a target challenge rating. Everything else
about it (`[entity.attitudes]`/`[[entity.behavior]]`/`abilities`/`equipped`/`is_party`/
`qualities.race`/...) is still hand-authored normally, exactly like any real entity —
generation only ever fills in the stat block + flavor text, and is deliberately varied per
template (different `qualities.race`, different attitude shape — some just a flat `default`,
others also carrying a specific `[[entity_template.attitudes.name]]`/`[[entity_template.
attitudes.supertype]]` override) so generated NPCs don't all read as the same person wearing a
different name tag. Referencing the same entity_template twice gets two independent instances
(`generated_stranger`, `generated_stranger_2`, via `_instance_entities`'s existing occurrence-
counting), each with its own LLM call — different name, different skills, not just a CR wobble.

**Varied fields.** Beyond the LLM-driven skill fit, an entity_template's own scalar fields can
be authored as a range or a weighted choice instead of a fixed value —
`NPC_Generation.py`'s `resolve_varied_value(value)` is the one shared vocabulary every such
field uses: `{min, max}` (a uniform random pick — an int result if both bounds are ints, else
a float) or a list of single-key `{"choice" = weight}` tables (a `random.choices` pick of the
*key*; weights are relative, not required to sum to 100 or 1). Applied per-leaf, not to a
whole structure at once — `DM_NpcGeneration.py`'s `_resolve_generated_qualities`/
`_resolve_generated_attitudes` walk `[entity_template.qualities]`'s own fields and
`[entity_template.attitudes]`'s six-axis arrays (`default` plus every `name`/`supertype`
override) element by element, which is what lets a template mix fixed and varied entries
freely (ex: `generated_stranger`'s own `default` keeping trust/confidence at a flat `0` while
disposition/intimacy each vary across `{min = -40, max = 40}`). `hint`/`cr_multiplier` are
resolved the same way, before the LLM call itself (they feed the prompt/target-CR math);
`qualities` resolves before the call too and is folded into the same prompt
(`_describe_qualities`, `NPC_Generation.py`) — gender/race/age have to already be concrete by
the time the LLM invents a name, or the two can disagree (ex: a name that reads as feminine
paired with a separately-rolled `gender = "male"`); `currency`/attitudes resolve independently,
any time after.

An `[[entity_template.attitudes.name]]` override can target the literal token `"player"`
instead of a real entity name — the same reserved placeholder `DM_Rules.py`'s
`PLAYER_PLACEHOLDER` uses for a scenario's own `entities` list, since a template authored
ahead of time can't know what a freshly-created or renamed character will actually be called
(`DM_CharacterCreation.py`). `_resolve_generated_attitudes` substitutes it for the live
`self.player_name` the moment the template is baked into an instance — the token is data
(`player`, lowercase), never the Python constant's own name.

Generation deliberately never touches `abilities`/`equipped`/`inventory` at all — whether (and
how) a generated NPC can fight or what it carries is still decided entirely by whoever authors
the entity_template, same as any hand-authored entity (a scenario author adds those fields to
the template directly, exactly like `characters.toml`'s `gladstone` does).

`Rules/Fantasy/reference/template_schema.toml` is the entity_template field reference (sibling
to `entity_schema.toml`, which still covers every field an entity_template shares with a real
`[[entity]]` unchanged) — it doubles as a worked-examples file for the varied-value vocabulary
below: a concrete `{min, max}` range and weighted-choice list for each field that supports one
(`cr_multiplier`, `currency`, `hint`, `qualities.race`/`gender`/`age`,
`attitudes.default`/`[[attitudes.name]]`), not just the prose description.

`Rules/Fantasy/npc_keywords.toml` is a small, hand-authored catalog of ~16 archetype keywords
(`warrior`, `trickster`, `scholar`, ...), each naming 3-4 real skills. `NPC_Generation.py`
(pure, same "pure, entity-shape-agnostic" precedent `Character_Creation.py`/
`Challenge_Rating.py` set) builds an OpenAI-style `tools` payload constraining the LLM's own
function call to 1-2 of those keyword *names* (an enum, not free text — far more reliable with
small local models than asking it to name skills directly), not skills themselves;
`generate_npc_stats` resolves the chosen keyword(s) to their union of real skills before
fitting. `fit_skills_to_cr(key_skills, target_cr, hp_share=0.3, damage_dice=0, damage_pips=0)`
is the deterministic inverse of `calculate_challenge_rating`: `hp_units = round(target_cr *
hp_share)`, `max_hp = hp_units * 3` (exact, no rounding loss); the remaining budget becomes a
single rating `R` (floored at 3 — a named "key skill" at 0D would read as broken, not weak)
given identically to each of the first 3 key skills (their average is then exactly `R`, no
drift, and this generalizes across 2/3/4 key skills with no special-casing); any 4th+ key skill
(CR-free, per `calculate_challenge_rating`'s own `top_n=3`) gets a lower flavor-only rating.
`generate_npc_stats` itself rolls `target_cr * cr_multiplier * random.uniform(1-variance,
1+variance)` before fitting — this, plus which keyword(s) the LLM happens to pick, is where
"random variance for uniqueness" actually comes from; `fit_skills_to_cr` stays fully
deterministic and directly testable. On any failure (no `tool_calls`, malformed JSON, network
error, timeout, or `skip_llm_generation=True` — see below) it falls back to a random keyword
pick with no network call at all — matching the rest of the app's "LM Studio is best-effort,
never blocks core gameplay" posture (`RagIndex` returns `[]` until ready;
`generate_load_failed_response` still narrates on failure).

`LLM_Client.py`'s `call_chat_completion` is a small, synchronous, stateless POST to LM
Studio's chat/completions endpoint — deliberately not shared with `LLM_Core.py`'s own async
`fetch_from_llm` (different payload/response shapes, and critically different failure
contracts: `fetch_from_llm` must never raise, this one must raise cleanly so
`generate_npc_stats`'s fallback triggers). It carries a hard `timeout` (default 20s) that
`fetch_from_llm` doesn't need — generation runs synchronously, in place, on whatever thread is
currently instancing the scene (`DMCore.__init__`/`enter_room`, always the GUI/main thread in
practice), so an unbounded call could freeze the whole app, not just error out. This is a
known, accepted v1 limitation: a scenario/room with one or more entity_template entries
visibly pauses for ~5s per generated NPC on a fresh load (a `log_info` line fires first so it
reads as a deliberate wait, not a hang). A true fix would need a two-phase "placeholder now,
patch via an event later" redesign — out of scope for now.

`DM_NpcGeneration.py`'s `NpcGenerationMixin` is the DMCore-touching glue (the same split
`DM_CharacterCreation.py` is to `Character_Creation.py`), called from `RulesMixin`'s
`_instance_entities` immediately after an instance resolved from an entity_template is stored
into `self.entities` (has to be stored first — the CR-fitting math calls back into
`get_challenge_rating`/`_best_damage_dice_pips`, both keyed off `self.entities[name]`).
`_resolve_npc_target_cr` resolves a template's own `target_cr` field — a number, or the
literal strings `"player"`/`"party"` (live `get_challenge_rating`/summed party CR, resolved
fresh at instancing time, not baked in at authoring time). `"party"` deliberately does not
call `get_party_challenge_rating()` directly: `self.scenario_entities` isn't finalized until
the whole `_instance_entities` call returns, so it's empty (fresh boot) or stale (a
`load_game` re-run) for exactly the entries being resolved mid-loop. Instead
`_instance_entities` threads a `party_pool` param (`load_scenario()` passes `[]`;
`_populate_room()` passes `self.persistent_entities`, already finalized by the time any room
is populated) that `_resolve_npc_target_cr` combines with whoever's been instanced earlier in
the same loop to build a live party roster, bypassing `self.scenario_entities` entirely for
this one computation. The result is tagged `entity["generated"] = True`.

**Save/load.** `skills`/`max_hp`/`name`/`description`/`qualities`/`attitudes` don't round-trip
for an ordinary entity (save_game only diffs `hp`/`active_conditions`/`currency`/`inventory`/
`equipped`/`band` — everything else re-derives from the static TOML template on reload) — fine normally,
broken for a generated entity, which has no static template to re-derive those from (and, for
`qualities`/`attitudes` specifically — randomly varied per entity_template, see "Varied
fields" above — re-deriving would mean a *different* value than what was actually saved, not
just a missing one). `save_game` conditionally saves those six fields too when
`entity["generated"]` is true; `load_game` threads
`skip_llm_generation=True` through its own `load_scenario()`/per-room `_instance_entities()`
calls (both already re-run on every load) so re-instancing takes generation's offline fallback
path unconditionally — instant, no network dependency — and the existing overlay loop applies
the real saved values on top, exactly like it already does for `hp`/`inventory`. This is
better than "regenerate then overwrite" (which would cost a real LLM round trip on every load
of a save with generated NPCs, forever, for a value about to be discarded).

One pre-existing bug fixed alongside this: `DM_Social.py`'s `describe_character` used to
prepend `entity_name` (the `self.entities` dict key) rather than `entity.get("name")` to its
roster/`defender_details` text. Every hand-authored template already has `name` equal to its
own dict key by construction, so this never mattered before — but it silently discarded a
generated NPC's own LLM-invented name from everything the player actually sees narrated. Fixed
to `entity.get("name", entity_name)`, backward-compatible for every existing template.

## Movement and range

Every scenario entity — the player included — has an objective, 1-indexed `band`: a position
on the current room's (or scenario's) band line, not a distance-from-player.
`get_distance_between(a, b)` is the absolute difference between two band numbers. The player
moves via `advance_or_retreat(direction)` (`DM_Movement.py`): shifts the player's own band by
up to their `speed` (default 1) toward or away from `current_target`. A creature/ally moves
the same way via `move_toward_or_away(entity_name, opponent_name, direction)`, just relative
to whichever opponent `resolve_behavior_action` resolved for it. Either way, only the one
entity that moved has its band changed (aside from party formation, below), but because gaps
are computed from both sides' bands, one move can change its distance to every other entity
at once — not always in the expected direction, since retreating from one opponent can carry
an entity toward something else. At a zero-gap tie, "advance" is a no-op; "retreat" prefers a
higher band number, falling back to a lower one only if higher is blocked.

`move_entity`'s floor is always band 1; its ceiling is the scene's own `bands` count,
enforced only when `enclosed` is true (the default). `enclosed = false` removes the ceiling
entirely — the mechanism for fleeing a scene: once the gap to every attacker's own `range` is
exceeded, nothing can reach the fleeing entity.

**Party formation.** Every `is_party` entity carries its own `follow_offset` (int, default 0),
read by `_apply_party_formation` to snap that entity's band to `player_band + follow_offset`.
`characters.toml`'s `thane` (`follow_offset = 0`) walks abreast; `anne` (`-1`) trails one band
behind to favor her ranged spellwork. This is a flat teleport, not a speed-limited move, and
only ever fires where the *player's* band changes (`advance_or_retreat`, `enter_room`) — never
from a creature/ally's own combat-turn movement, which stays free to drift out of formation
until the player's next move snaps it back. The player can override `follow_offset` in play:
"stay behind me"/"walk beside me" resolve to `item_interaction_detected` intents
`"formation_behind"`/`"formation_abreast"` (`DMCore._resolve_formation_intent`) — a party
member's own name either is or isn't literally present in the input (whole-word,
case-insensitive), so naming one addresses only them; naming none addresses the whole party.

`range` (int, in bands) lives on the weapon/spell/ability itself, absent/`0` meaning melee —
usable only in the target's own band. A reach weapon extends that by one band; a ranged
weapon/spell reaches however far its own data says, with no accuracy difference across that
range. `is_in_range` is `True` unconditionally when `ability` is `None` (a non-physical check).

## Character creation

Race/point-buy skill dice, applied to the player entity once, before any scenario loads.
`Character_Creation.py` is pure, UI- and DMCore-independent logic — its own
`load_character_creation_data(rules_dir)` re-scans `Rules/Fantasy/*.toml` for `[[skill]]`,
`[[race]]` (`races.toml`), and `rules.toml`'s `[character_creation]` table directly (its own
independent path resolution, same precedent `LLMCore._save_slot_dir` sets), since a character
has to be buildable *before* a `DMCore` exists to read that data off of. `[character_creation]`
holds `pool_dice` (15 — free dice to spend across skills) and `max_allocation_per_skill` (5).
Each race (`races.toml`) is its own complete, *absolute* `[race.skill_dice]` table, one entry
per skill (human included — no implicit "base_dice" default). `race_baseline_skills` reads a
skill's value off the race's table, floored at 0, falling back to `UNTRAINED_DICE` (0) if a
skill is missing from a race's own table. `elf`/`dwarf`/`half-orc`/`halfling` each raise four
skills to 3D and lower four others to 1D around the 2D baseline, netting even before any
allocation is spent. `validate_allocation` rejects an unknown skill, a negative entry,
anything over the per-skill cap, or a total that isn't *exactly* `pool_dice`;
`build_character_skills` is baseline + allocation for every skill.

`DM_CharacterCreation.py`'s `apply_character_creation(character)` — `character` being
`{"race", "allocation", "name"}` — is the one piece that touches `DMCore` state, called from
`DMCore.__init__` right after `_resolve_player_name()` and before any scenario loads.
`"allocation"`, if non-empty, is validated and overwrites `self.entities[self.player_name]
["skills"]` entirely and updates `qualities.race`; if absent/empty, the skill/race override is
skipped and the template's own hand-authored skills are left untouched (this is what lets
`LLDM.py`'s CLI quick-boot pass a bare `{"name": ...}` through this same method rather than
needing a separate rename-only path). `"name"`, if non-blank and different from the current
`player_name`, renames the player entity: `self.entities[self.player_name]` is popped and
re-inserted under the new key, and `self.player_name` repoints at it — so
`_instance_entities`' `"player"` placeholder (see "Scenarios and rooms") resolves to the new
name from the first scene onward. A name colliding with any other already-loaded entity is
rejected outright (`log_error`, not raised — the skill/race override still applies even when
the rename is rejected). Renaming doesn't touch any *other* entity's own
`[[entity.attitudes.name]]` override keyed to the old literal name — a renamed character just
falls back to that entity's `default` disposition, same as any name the override doesn't list.
`character=None` (every caller that omits it) is a complete no-op — `characters.toml`'s
`gladstone` stays exactly as-is; this system never retrofits existing NPCs/creatures.

`Character_Creation_GUI.py`'s `CharacterCreationDialog` (a modal `Toplevel`, same
`grab_set`/blocking-`wait_window` pattern `GUICore.request_load` uses) is the interactive
front end: an optional name field, a race dropdown, a per-skill allocation row, and a "dice
remaining" counter gating Create until it hits exactly zero. `self.result` is always
`{"race", "allocation", "name"}` once Create is pressed, or `None` if cancelled.
`GUICore.request_character_creation` runs this and, only when not cancelled, publishes
`"character_created"` — see "Booting the game" for what happens with the result.

## Booting the game

`LLDM.py`'s `main()` never constructs `DMCore` unconditionally — no scenario loads and
nothing is narrated until a player character *and* a chosen scenario exist, via whichever
route fires first:

1. **CLI quick-boot** — `python LLDM.py <scenario> [character_name] [--setting SETTING]`. Giving
   `scenario` skips the Character menu entirely and constructs `DMCore` immediately;
   `character_name`, if also given, is passed as `{"name": character_name}` (a rename, skills
   untouched). `--setting` (default `"Fantasy"`) picks which `Rules/<setting>/` data pack
   `scenario` is resolved against (ex: `--setting Zombie rooftop`) — see this doc's own top-level
   "settings" note. Omitting `scenario` leaves the window open with nothing loaded, for routes
   2/3 below.
2. **Character → Create... then Scenario → Load...** — a non-cancelled dialog result publishes
   `"character_created"` (see GUI_Core.py's own notes above), which `main()`'s
   `on_character_created` closure only logs a warning for (if `DMCore` already exists) — it
   doesn't construct anything. `GUICore` itself stashes the character and unlocks Scenario →
   Load...; picking a scenario from that popup publishes `"scenario_selected"
   {"scenario_name", "character"}`, which `main()`'s own `on_scenario_selected` closure reacts
   to (not `DMCore` — nothing exists yet to subscribe) by constructing
   `DMCore(scenario_name=..., character=...)`.
3. **File → Load...** — `GUICore.request_load` publishes `"load_requested"`. Before any
   `DMCore` exists, `main()`'s own `on_load_requested` closure handles this instead of
   `DMCore`'s usual `_on_load_requested`: it peeks the chosen slot's `dm_state.json` for its
   `"scenario_key"`/`"setting"` (`LLDM._peek_saved_scenario_key` — a plain file read, no live
   `DMCore` needed), constructs `DMCore` against that scenario/setting, then calls
   `dm_core.load_game(slot)` to overlay the rest of the saved state. This costs one throwaway `scenario_loaded` narration
   before `load_game`'s own `"game_loaded"` corrects it — the same double-narration cost as
   loading a save immediately after any ordinary new game, not a cost unique to this path.

`on_character_created`/`on_scenario_selected` no-op (the former with a logged warning) once
`DMCore` already exists — Create... (and, downstream of it, Scenario → Load...) only ever
starts the *first* game a session has; `GUICore`'s own `_on_game_started` (see above) is what
keeps Scenario → Load... from even being reachable again by that point. `on_load_requested`
no-ops silently instead, since File → Load... is meaningful at any time: every load after the
first is handled solely by `DMCore`'s own `_on_load_requested`, subscribed during its
`__init__` as always.

## Scenarios and rooms

`Rules/Fantasy/scenarios/*.toml` (`arena`, `tavern`, `field`, `dungeon`, `crypt`, plus
`character_test` — see "Testing") each hold one `[scenario]` table, kept in their own
subdirectory so multiple scenarios can coexist without the flat `load_rules` scan (which only
keeps the last `[scenario]` table it reads) overwriting one with another.

A scenario is either a **plain single room** (entities listed directly under `[scenario]`) or
a **multi-room dungeon** (`crypt`): one or more `[[room]]` tables, each with its own
`entities`/`bands`/`enclosed` plus `[[room.exit]]` sub-tables (`{band, direction, destination,
arrival_band}`), and `[scenario].start_room` naming the starting room. A room's own `entities`
list never includes the player — only room-local creatures/traps/chests; the player (and
anything meant to persist across the whole dungeon) is listed once at the scenario's top
level. `self.rooms` stays empty for a plain scenario, which is what lets
`load_scenario`/`enter_room` branch on room-graph vs. flat behavior without a separate flag.

**The player is referenced generically, never by a specific character's literal name.** Every
scenario/room's `entities` list names the player with the reserved sentinel `"player"`
(`DM_Rules.py`'s `PLAYER_PLACEHOLDER`), never a real template name like `"gladstone"`.
`_instance_entities` resolves it to `self.player_name` before the template lookup, so a
scenario keeps working regardless of which template is `is_player = true` or what a
freshly-created character was renamed to.

`DMCore.__init__(event_bus, scenario_name="arena")` picks which file loads via
`load_scenario_definition`, which raises `FileNotFoundError` for an unknown name (fatal on
purpose — an empty `self.scenario` would let the LLM hallucinate an opening scene with no real
content). `load_scenario()` deep-copies each named template into an independent instance,
tags it with its starting `band`, disambiguates duplicates (`wolf`, `wolf_2`, ...), and gives
each instance its own `entity_id`.

`enter_room(room_key, arrival_band)` — the only caller is
`DMCore._resolve_room_transition_intent`, gated on the current room declaring a matching exit
at the player's own band and on no living hostile remaining in the room. Moves only the
player's band; HP/inventory/currency/conditions carry over. A room visited before is restored
from `self.visited_rooms` rather than re-instanced, so a cleared trap, dead creature, or
looted chest stays that way on revisit.

**`self.entities` holds templates and live instances under the same keys** — instancing a
single-occurrence entity overwrites its template slot. `load_game` re-runs `load_rules()`
before re-instancing for this reason (see "Saving and loading").

## Status and conditions

`rules.toml`'s `[[status]]` table drives derived conditions. Each entry has:
- `trigger` — when to evaluate it; only `"on_damage"` is wired today, called from both
  `apply_damage` and `apply_healing` (see "Damage and healing" below).
- `requirements` — a list of `{field, operator, value}` comparisons (`COMPARATORS` in
  `DM_Status.py`: `>`, `<`, `>=`, `<=`, `==`, `!=`, `in`, `not_in`), ALL of which must hold.
  `field` is either derived (`"hp_per_remain"`) or a direct entity attribute.
- `apply` — `{condition, duration, dismiss}`, naming an entry in `[[condition]]`.

`entity_matches_requirements`/`get_comparable_value` are the shared engine behind both
`[[status]]`'s own requirements and `[[entity.behavior]]`'s; an optional `opponent_name` param
resolves the one opponent-relative derived field, `"distance_to_target"` (the band gap to
`opponent_name`) — used by a creature choosing *between* attack options by range, ex:
`creatures.toml`'s `bandit` favors its `short bow` while `distance_to_target > 0`, falling to
its `rusty shortsword` once that gap closes to 0.

`evaluate_statuses` finds every status matching a trigger whose requirements the entity
currently meets and calls `apply_condition`, storing it in `entity["active_conditions"]`
(seeded from the template's own `[entity.conditions]`). `dismiss_condition(entity_name,
condition_name)` is the general-purpose removal primitive.

`evaluate_statuses` also sweeps the *other* direction: after applying whatever matches now, it
dismisses any active condition (from the same trigger) whose requirements no longer hold —
ex: healing back above a "wounded" tier's hp_per_remain range dismisses "wounded" in the same
call. A condition is only eligible for this sweep if stored with a falsy `dismiss` — one
stored with a named mechanism (ex: `"dead"`'s `dismiss = "resurrection"`) is left alone, so
ordinary healing can't revive a dead entity through the same path that clears a wound tier.

## Damage and healing

`apply_damage` subtracts HP (floored at 0) and calls `evaluate_statuses(entity_name, "on_damage")`.
`apply_healing` adds HP (clamped at `max_hp`) and calls the same `evaluate_statuses("on_damage")` —
not to apply a *new* injury (healing only raises hp_per_remain, so no worse tier can newly
match) but so a wound tier's condition that no longer holds after the heal gets dismissed by
the stale-condition sweep above.

Nothing automatically re-evaluates a status-driven condition once its requirements stop
holding outside of `apply_damage`/`apply_healing`'s own calls.

## Entity tests

A `[entity.test]` block is a skill check against an entity itself (ex: `items.toml`'s `chest`
lock, `cursed dagger`'s curse-identification check; see `Rules/Fantasy/reference/
entity_schema.toml` for every field it and every other entity table can carry).
`is_test_available(target, test, skill_name)` gates it: `skill_name` must be in `test["skill"]`;
`requires_condition` (if set) must currently be active; `blocks_if_condition` (if set) must
not be. A skill not in `test["skill"]` isn't blocked — it just isn't a test, and falls through
to ordinary opposed-skill resolution instead.

A scene-level test (the target itself, via `self.current_target`) is resolved as a flat
difficulty check (`resolve_action`), not through `resolve_opposed_action`.
`_resolve_item_test_target`/`_resolve_item_test` handle the same mechanism one level deeper —
an item already in the player's inventory, or sitting in a reachable container — tried before
combat-target redirection so inspecting an item never becomes an attack.

`apply_test_outcome(entity_name, outcome)` dispatches on whichever keys are present in the
matched `pass`/`fail` table: `dismiss_condition` removes a condition, `condition` applies a
new one, `loot` transfers everything on the target via `loot_entity`, and `reveal` (truthy)
applies a permanent `"identified"` condition — the content it reveals is read back off the
entity's own `tags` field by whoever narrates it, not stored on the outcome itself.

## Inventory and currency

- **`transfer_currency(from_name, to_name, amount=None)`** — moves currency; `amount=None`
  moves all of it; clamps to what's available; no-ops on a missing entity.
- **`transfer_item(from_name, to_name, item_name)`** — moves one matching `inventory` entry;
  duplicates represent quantity, so callers loop for more than one.
- **`loot_entity(from_name, to_name)`** — sweeps all currency plus every inventory item.

## Items and movement as intents

Looking at, taking, giving, trading, opening, closing, using, equipping, dropping, moving
between rooms, and directing the party's own formation all bypass the *skill/dice* system
entirely — none of them warrant a roll. (Most of them still cost a turn action and share in
the multi-action penalty pool, though — see "Multiple actions" above for why that's a
different question from "does this roll dice.") `NLPCore._detect_item_intent` recognizes
phrase-level keywords for thirteen intents, run per clause once save/load and inter-room
movement have had their own whole-input shot (see "Multiple actions" for the full
clause-classification order): `examine`, `equip` (`equip`/`wear`/`wield`/`put on`), `unequip`
(`unequip`/`take off` — deliberately not a broader `remove`, which would collide with
items.toml's own trap names and finesse's `disarm`/`trap` keywords), `drop`
(`drop`/`discard`/`put down`), `take`, `give`, `trade`, `open`, `close`, `use` (currently
`drink`/`quaff`), `formation_behind`/`formation_abreast` (see "Party formation" above), and
direction/movement phrases for `advance`/`retreat`/`move`. `advance`/`retreat`/`formation_*`
are `EXEMPT_ITEM_INTENTS` — free per WEG's own movement/communication exceptions, published as
their own free-standing `item_interaction_detected` and never joining the shared turn;
`open`/`close` (`NO_ITEM_LOOKUP_INTENTS`) still cost a turn slot but, like the exempt four, act
on the current scene target directly rather than a named item; `move` is checked separately,
against the whole input, ahead of per-clause classification entirely (see "Multiple actions").
Every other intent runs through `NLPCore.map_to_item`, an embedding match against every
`supertype == "object"` entity's name/description (currency is checked first as a fixed
synonym list, returning the sentinel `"currency"`), and — if it resolves to a real item — joins
the same shared per-turn clause list a skill/ability action does.

`DMCore._on_item_interaction_detected` resolves with zero dice rolls:
- `"equip"`/`"unequip"`/`"drop"` are checked first, since none care about target_name/the
  locked gate below at all.
  - `_resolve_equip_intent` moves an item already in inventory into whichever
    `[entity.equipped]` slot its own `equip_slot` field resolves to for the player's
    supertype/subtype (`rules.toml`'s `[[equip_slot]]` via `get_equip_slots`). Denied
    `"not_present"`/`"not_equippable"`/`"cant_equip"` as appropriate. An item already sitting
    in the chosen slot is displaced (still in inventory) rather than refusing.
  - `_resolve_unequip_intent` only clears the slot mapping — denied `"not_equipped"` if it
    isn't equipped at all.
  - `_resolve_drop_intent` unequips if needed, then moves the item onto the current
    room/scene's own ground (`_current_ground_items`). **Known gap:** unlike
    `scenario_entities`, nothing in `"ground"` is saved/restored yet, so a drop since the last
    save doesn't survive a save/load round trip.
  - A later `"examine"`/`"take"` aimed at a ground item is resolved by `_resolve_ground_intent`
    before falling through to the ordinary target-based path below.
- A locked container denies everything (`reason: "locked"`).
- `item_name` equal to the current target's own name addresses the target itself, not
  something inside it.
- A closed (but unlocked) container denies reaching its contents (`reason: "closed"`) while
  still allowing examine/open.
- `"take"`/`"trade"` move an item to the player; `"give"` moves one to the target; `"trade"`
  additionally charges the item's TOML `value` (`reason: "cant_afford"` if unaffordable).
- `_resolve_open_close_intent` is gated to `subtype == "container"`; toggles `"closed"`,
  independent of `"locked"` — a picked lock still needs its own `"open"`. A successful open
  attaches `contents`: one flavor-description string per item inside.
- `_resolve_use_intent` activates/consumes an item, gated on a truthy `usable` field. Two
  effects are implemented: healing (`healing = {dice, pips}`, rolled through `apply_healing`)
  and poison (`poison = {dice, pips}`, rolled through the ordinary `calculate_damage`/
  `apply_damage` path instead, `damage_tags = ["poison"]`, self-inflicted — attacker and
  defender are both the player, so a poison-resistant/immune character correctly reduces or
  negates it exactly like a real attack, and `evaluate_statuses`' own wound-tier conditions
  still apply). An item can carry either, both, or neither; using an item also applies a
  permanent `"identified"` condition regardless. Consumption is charge-based
  (`_consume_charge`): no `charges` field means single-use; at zero charges the item is
  replaced by `replace_with` or simply removed.
- `_resolve_room_transition_intent` handles `"move"` (see "Scenarios and rooms").

Publishes `item_interaction_resolved` either way, with enough detail (`found`,
`reason`/`description`/`container`/`amount`/`price`/`contents`/`healed`/`charges_left`/
`replaced_with`/`slot`/`replaced` as applicable) for narration to explain a miss or a success.

## Social and attitudes

`get_attitude(entity, toward)` returns a six-value array (`disposition, trust, confidence,
respect, obligation, intimacy`, nominally -100..100; a `name` override beats `supertype` beats
`default`; no `[entity.attitudes]` table defaults to all-neutral). `get_attitude_tier(value)`
clamps to `[-150, 150]` and returns the first of seven `[[attitude_tier]]` bands whose range
contains it, in declaration order. `describe_attitude(entity, toward)` renders all six axes
as one sentence using each tier's own phrase per axis.

`describe_character(entity_name, toward_name=None)` builds a flavor-text roster line from
purely descriptive TOML fields (`description`, `qualities`, `memories`, `quotes`) plus, when
`toward_name` is given, the attitude sentence above — deliberately excluding mechanical data.
`DMCore.__init__` builds this roster into the `scenario_loaded` payload; `_on_turn_detected`
also attaches a fresh `result["defender_details"]` per action.

`self.player_name` is resolved once in `__init__` via `_resolve_player_name()`, which scans
loaded templates for the one with `is_player = true` and raises `ValueError` if none is marked.

## Dialogue

Directly addressing someone (`"talk to the innkeeper"`, `"ask the guard about the road"`) is a
third diceless channel, alongside skill/dice actions and item/scene intents — genuinely
distinct from both, not a fourteenth item-interaction intent: there's no item involved, the
addressee is resolved from the scene itself rather than looked up, and the result is a
generated in-character reply rather than a structured mechanical outcome. It's also distinct
from a *skill-based* social check (persuade/intimidate/deceive) — those still roll dice through
`resolve_opposed_action` and narrate in third person as the omniscient Game Master exactly as
before; free-form talking never rolls anything and always speaks as the addressed entity
instead. `NLPCore._detect_dialogue_intent` recognizes `DIALOGUE_KEYWORDS` phrases (`"talk
to"`/`"speak to"`/`"speak with"`/`"ask"`/`"tell"`/`"say to"`/`"greet"`/`"chat with"`), checked
after item-interaction detection has already had its shot (so a genuine item verb naming an
entity, ex: `"give the sword to Anne"`, is never swallowed as dialogue) and before skill
matching, publishing `dialogue_detected {input, score}` with no further resolution — unlike
item intents there's no name to look up here, since NLPCore has no visibility into who's
actually in the current scene.

`DMCore._on_dialogue_detected` is thin, delegating to `DM_Dialogue.py`'s `DialogueMixin`:
`_resolve_dialogue_target` searches the raw input for any currently-present entity's own name
(a literal, whole-word match against `self.scenario_entities`, excluding the player — the same
"DMCore, not NLPCore, decides who's named" approach `_resolve_formation_intent` already uses
for party positioning, just generalized to every entity, not only party members), falling back
to `_get_target_name()`'s own default scene target if none is named. `_resolve_dialogue` then
gates on the resolved target actually being present/alive/noticed (`reason: "not_present"`) and
not an inanimate `"object"` supertype (`reason: "cant_talk"`) — but deliberately **not** on
hostility: unlike combat targeting, addressing a hostile entity is allowed (shouting a question
mid-fight, taunting), and whatever the model produces is free to read as hostile/dismissive in
character rather than being denied outright. A found target's `persona`/`attitude`
(`describe_character`/`describe_attitude`, `DM_Social.py`) are attached for `LLMCore` to speak
from. Publishes `dialogue_resolved {target, input, found, present_entities, persona?,
attitude?, reason?}` — no `_publish_party_status()` call, since dialogue never changes HP/
equipment/inventory/conditions.

**Room-level presence.** Every DM-published narration-triggering event (`scenario_loaded`,
`action_resolved`/`round_resolved`, `item_interaction_resolved`, `dialogue_resolved`) now
carries `present_entities`: a snapshot of `self.scenario_entities` at publish time — who was
actually in the current room to witness it. `LLMCore` tags each `context_window` entry it
appends with this same snapshot (`"present"`, see `_queue_narration`'s own param) and
`generate_npc_dialogue` uses `_filter_present_history(target)` to ground a specific NPC's own
reply only in what that NPC has actually witnessed, rather than the DM's own always-full,
omniscient window (which stays untouched — the player's own point of view is deliberately still
everything, not scoped down). An entity instanced mid-dungeon, or left behind in a previous
room of a multi-room scenario, simply has no access to entries tagged before/without it — no
special-casing needed, since `present_entities` is already just whichever room-scoped list
`_populate_room` was already maintaining. An untagged entry (`action_not_understood`/
`game_load_failed` — neither ever goes through `DMCore`, so there's no `scenario_entities` to
tag them with) is excluded from every per-entity view rather than assumed witnessed. The
exchange itself (the player's question, the NPC's own reply) is still appended to the *shared*
`context_window`, tagged the same way any other narration is — so it becomes part of what
everyone in the room, including the omniscient narrator and any other NPC present, has now
witnessed, which is what lets a second NPC in the same room later recall what was just said to
the first one.

## ADaM (out-of-character help)

`"ADaM"` (Artificial Dungeon and Master) is a fourth diceless channel — a reserved,
always-available, explicitly out-of-character persona the player can address for guidance
(their own skills/abilities, a re-description of the current scene, available exits, and
general command/verb guidance), never an in-fiction one. Unlike Dialogue, there's no addressee
to resolve (ADaM isn't a scene entity) and no way for it to be denied — it always resolves.
It's also unconditionally free: unlike an item interaction, it never joins the shared
multi-action turn economy at all (see "Multiple actions"), the same "communication is free"
treatment movement/speech already get, just doubly so since it isn't even in-fiction speech.

`NLP_Core.py`'s `ADAM_NAME_PATTERN` (`\badam\b`, case-insensitive) is checked as its own
whole-input, pre-clause-split reserved word — right after save/load detection, ahead of both
inter-room direction detection and the item-interaction pass — so `"talk to ADaM"`/`"ask ADaM
about my skills"` reach the help channel rather than being swallowed by `DIALOGUE_KEYWORDS` or
an item verb first (Dialogue's own target resolution searches `scenario_entities`, where
"adam" is never found, so without this priority it would silently fall back to whatever the
default scene target happens to be). This reserves the literal name "adam" the same way
`DM_Rules.py`'s `PLAYER_PLACEHOLDER` reserves "player" — a known, accepted tradeoff (no future
entity in any setting can be named Adam without colliding). Publishes `help_detected {input}`.

`DM_Help.py`'s `HelpMixin._on_help_detected` gathers a fresh snapshot of live state every time
it fires (no memory of past ADaM exchanges — see below) and publishes `help_resolved`: the
player's own `[entity.skills]` formatted `"name: XD+Y"`, their `abilities` resolved via
`resolve_ability` (`DM_Combat.py`, the same lookup any ability use already goes through) to a
name/description, `equipped`/`inventory`, the current scene's `_current_scene_name`/
`_current_scene_description` (`DM_Rules.py`), `present` via the same
`_describe_scenario_characters()` roster the player is normally told, and `exits` — `[]` for a
flat scenario, else the current room's own `[[room.exit]]` entries with each `destination` key
resolved to that room's own friendly `name`. No `_publish_party_status()` call for the ordinary
informational path, same reasoning Dialogue already documents (nothing here mutates HP/
equipment/inventory/conditions) — except when a removal actually went through (see "Ad hoc
entity creation and removal" below), the one case that breaks this invariant on purpose.

**Deliberately excluded from `context_window`.** Every other narration trigger
(`_queue_narration`/`_queue_dialogue`) appends both the prompt and the reply to `LLMCore`'s
shared rolling window, which every future GM narration call replays in full (`_build_system_
message` has no presence filtering at all — only Dialogue's own `_filter_present_history`
does). ADaM's own `generate_adam_response`/`_build_adam_system_message`/`_queue_adam_response`
(`LLM_Core.py`) instead send a standalone `[system, user]` request straight to `_fetch_and_
publish(..., store_in_context=False)` — a new param that skips the usual `context_window.
append(...)` on the reply — and never append the prompt either. Two reasons, not one: (1) tone
— a meta/OOC exchange left in the shared window risks the GM later parroting mechanical facts
in-fiction, since nothing filters it back out for the omniscient narrator the way presence-
tagging does for a specific NPC; (2) budget — ADaM can be invoked repeatedly with dense
payloads (full skill lists, exits, etc.) that would otherwise crowd the finite 100-message
window, diluting real narrative history fast. The cost is that ADaM has no memory of its own
past replies — each invocation is a fresh, independent request built from whatever `DM_Help.py`
gathers off live state at that moment, not a continuation of an earlier ADaM conversation.

## Ad hoc entity creation and removal

ADaM's second capability: acting as a real DM by improvising the world itself, not just
explaining it. `AdHoc_Generation.py` is the pure, DMCore-independent LLM-calling half (same
"pure, entity-shape-agnostic" precedent `NPC_Generation.py`/`Challenge_Rating.py` set) —
`generate_ad_hoc_item`/`generate_ad_hoc_creature`/`decide_entity_removal`/`decide_entity_edit`,
each an OpenAI-style tool call (`LLM_Client.py`, synchronous, raises on failure) offering the
model a primary function plus a shared `decline` escape hatch, `tool_choice="auto"`. Unlike NPC
generation (which always needs *some* result and falls back to a random offline pick), every
function here defaults to declining on any failure — never fabricating an item, creature,
removal, or edit when the LLM is unreachable — on a tighter 8s timeout than NPC generation's 20s
default, since this can fire far more often mid-session than NPC generation's
handful-of-times-per-scene-load pattern. `DM_Improvisation.py`'s `ImprovisationMixin` is the
DMCore-touching glue (the same split `DM_NpcGeneration.py` is to `NPC_Generation.py`).

**Two different triggers, two different risk profiles — not one symmetric mechanic.** Every
capability here slots into one of two buckets, not a single symmetric mechanic:
- **Automatic fallback**, no need to address ADaM by name at all (low risk — an improvised prop
  or flavor line can't hurt anything): plain item creation, extended to a container/trap
  (still `generate_ad_hoc_item`, just a subtype carrying its own minimal `[entity.test]`) and to
  ambient scenery detail (a third `describe_scenery` outcome with no entity created at all).
- **ADaM-gated**, behind explicitly addressing ADaM by name (higher risk — can affect combat
  balance or mutate any existing entity, hand-authored included): entity removal (`removal_
  candidate`), creature/NPC conjuring (`creature_candidate` — a hostile one can fight, changing
  the scene's balance), and entity editing (`edit_candidate` — can rewrite any existing entity's
  own description or apply/dismiss a condition on it). See the previous section's own
  `removal_candidate` handling for the shape all three local keyword pre-checks share
  (`NLP_Core.py`'s `REMOVAL_KEYWORDS`/`CREATURE_KEYWORDS`/`EDIT_KEYWORDS`, each attached to
  `help_detected`'s own payload only once `ADAM_NAME_PATTERN` already matched).

**Creation.** `NLP_Core.py`'s `_on_user_input` tracks any clause where `_detect_item_intent`
matched a verb in `IMPROVISABLE_INTENTS` (`examine`/`take`/`give`/`equip`/`unequip`/`use`/
`drop`/`trade`) but whose own `map_to_item` found nothing. `trade` is included deliberately —
see its own paragraph below for why it needs a third placement path, not the two every other
intent uses. If the *whole turn* still resolves to nothing at all (no `turn_clauses`, no exempt
clause) once pass 2 also comes up empty, the first such candidate is published as
`improvisation_requested {intent, phrase, input}` **instead of** `action_not_understood` — the
one and only integration point; a clause that fails alongside another clause that *did* resolve
still silently drops today, unchanged, the same as before this feature existed.

`ImprovisationMixin._on_improvisation_requested` calls `generate_ad_hoc_item` (enum-constrained
`subtype` — `weapon`/`armor`/`potion`/`tool`/`trinket`/`misc`/`container`/`trap`;
enum-constrained `equip_slot`, built from `get_equip_slots(player_name)`, `lock_skill`/
`disarm_skill`, built from `self.skills.keys()`, and `damage_tag`, built from tags already in
real use across `Rules/Fantasy/*.toml` — the same enum-over-free-text reliability win
`NPC_Generation.py`'s own tool schema already banks on for small local models). On decline/
failure: publishes `action_not_understood`, exactly the outcome that would have happened without
this feature. On success: tags the new entity `ad_hoc = True` (drives its own save/load
treatment — see below), stores it into `self.entities`, and publishes `item_catalog_updated`
(see NLPCore's own handling, below) before resolving.

**Scenery.** The same tool call also offers `describe_scenery(description)`, the model's own
escape hatch for a phrase that names ambient detail (writing on a wall, an odor, the room's
layout) rather than a discrete, self-contained object — `generate_ad_hoc_item` returns
`{"created": False, "scenery": True, "description"}` on this branch, distinct from a plain
decline. `_on_improvisation_requested` publishes a bespoke `item_interaction_resolved
{found: True, description}` directly, with no entity created at all — a pure flavor beat, same
treatment an ordinary `"examine"` description already gets, nothing to persist.

**Containers and traps.** `subtype = "container"`/`"trap"` carry a minimal, LLM-authorable
subset of the same shape `items.toml`'s hand-authored `chest`/`dart trap` already use (see
"Entity tests" above) — built entirely inside `generate_ad_hoc_item` itself (a container gets
`active_conditions = {"closed": ..., "locked": ...}` if the model marked it `locked`, plus a
`[entity.test]` gating on `requires_condition = "locked"` whose `pass` dismisses it and whose
`fail` applies `"jammed"`, mirroring the chest exactly; a trap gets `active_conditions =
{"armed": ...}` plus a `[entity.test]` gating on `"armed"` whose `fail` also deals real damage
via the same `damage`/`damage_tags` shape `apply_test_outcome` already reads — but skips
`[entity.notice]`/`"hidden"` entirely, since a trap only gets conjured *because* the player
already described finding it, so there's nothing left to auto-roll a notice check against).
`_resolve_test_skill` picks the model's own `lock_skill`/`disarm_skill` if it's real, else falls
back to `"finesse"`, else drops the `[entity.test]` block (and, for a container, the `"locked"`
condition) entirely — a conjured container/trap must never land permanently unopenable/
undisarmable just because the model picked (or omitted) an invalid skill.

Placement is a fourth path, not a style choice: unlike a plain item, a container/trap becomes a
live, targetable scene participant (`SCENE_PLACED_SUBTYPES`, `DM_Improvisation.py`) — inserted
at the *front* of `self.scenario_entities` so `_get_target_name()` (what a bare
`"examine <the target itself>"`/`"open"`/`"close"` all resolve against) picks it immediately,
even with some other non-party entity (ex: a creature already mid-fight) also present. It also
claims `self.current_target` via the shared `_claim_current_target_if_free` helper (see
"Creature/NPC conjuring" below for its own use of the same helper) — necessary because
`self.current_target`, not `_get_target_name()`, is what a scene-level `[entity.test]` check
(picking a lock, disarming a trap) actually resolves against (`_resolve_roll`, `DM_Core.py`), so
without this the new entity could be examined/opened but never actually tested. Never steals the
target from a fight already engaged (a live, hostile-toward-the-player entity) — re-dispatches
through the ordinary, unchanged item-interaction pipeline either way, which is what makes the
very `"examine"`/`"take"` that triggered creation resolve exactly like it would against a
hand-authored container/trap (ex: a locked one denies `"examine"` too, same as `chest`).

An item can also opt into `"use"` (`usable = true`, plus `healing`/`poison` `{dice, pips}`
skill stats — see "Items and movement as intents"'s own `_resolve_use_intent` note) via the same
tool call's `is_healing`/`is_poisonous` flags. The tool schema's own description explicitly
tells the model not to default every improvised consumable to safe/beneficial — for balance, a
plausible fraction should come back poisonous instead, so freely asking to drink/use an
ad hoc-conjured item is never a guaranteed free heal.

*Three placement paths, not a style choice.* The model's own `location` choice (`"ground"` or
`"inventory"`) is real narrative judgment (a stone lying around vs. a match already in a
pocket), but which one actually *works* mechanically depends on the triggering intent, not just
the location: `DM_Core.py`'s `_on_item_interaction_detected` dispatcher only ever checks
`_current_ground_items()` for `"examine"`/`"take"`; `give`/`equip`/`unequip`/`use`/`drop` always
resolve against the *player's own* inventory regardless of source/destination direction; `trade`
alone resolves against the *current scene target's* own inventory as its source (buying
something means the seller has to have it). So `PLAYER_CENTRIC_INTENTS` (`give`/`equip`/
`unequip`/`use`/`drop`) always land directly in the player's inventory and re-dispatch through
the ordinary, otherwise-unchanged `_on_item_interaction_detected` — a ground placement here just
collapses "you spot it and immediately act on it" into one beat rather than a separate explicit
`take` first. `GROUND_AWARE_INTENTS` (`examine`/`take`) honor the model's own placement:
`"ground"` appends to `_current_ground_items()` and re-dispatches normally (already correct, via
the existing ground-check branch); `"inventory"` instead publishes a bespoke `item_interaction_
resolved {found: True, ...}` directly, plus a manual `_publish_party_status()` call — re-
dispatching would incorrectly check the scene's own default *target's* inventory instead of the
player's own (there's no existing dispatcher path for "examine/take something already in your
own inventory with no target involved" at all). `TARGET_CENTRIC_INTENTS` (`trade` alone) ignores
the model's own `location` field entirely (there's no "on the ground" or "in the player's
pocket" for something a shopkeeper is about to sell) and instead stocks the new entity directly
into `self._get_target_name()`'s own inventory before re-dispatching — this is what lets a
general store's shopkeeper sell "most general goods" without every possible good having to be
pre-authored on their own `[entity.inventory]` list (see `Rules/Fantasy/scenarios/shop.toml`);
the entity's own `value` field (set by the model) is what the ordinary `trade` dispatch charges
as a price. Short-circuits to a decline *before* ever asking the LLM if there's no current scene
target at all — there's no one to buy from, the same "nothing here could plausibly be removed"
short-circuit `decide_entity_removal` already applies on the removal side.

**Removal.** `NLP_Core.py`'s `REMOVAL_KEYWORDS` (`"remove"`, `"get rid of"`, `"destroy"`, ...)
is a cheap local pre-check, attached to `help_detected`'s own payload as `"removal_candidate"`
only when `ADAM_NAME_PATTERN` already matched — avoids paying for a synchronous removal-decision
LLM call on every ordinary "ADaM, what are my skills" question; the LLM's own `tool_choice=
"auto"`/`decline` is still the real arbiter of whether anything actually gets removed.
`DM_Help.py`'s `_on_help_detected` calls `ImprovisationMixin._attempt_entity_removal` *first*
when flagged — builds the real, current universe of removable names (every entity in
`scenario_entities`, every ground item, every known instance's own inventory/equipped item,
`player_name` always excluded — belt-and-suspenders on top of `remove_entity_from_scene`'s own
runtime guard) as an enum constraint for `decide_entity_removal`, so the model can't even
attempt to name something that doesn't exist. A real removal is folded into `help_resolved` as
`"removed"` (LLMCore's `_build_adam_system_message` mentions it happened) and triggers
`_publish_party_status()` — the one exception to "ADaM never mutates state."

`remove_entity_from_scene(name)` strips `name` from `scenario_entities`/`persistent_entities`/
every `visited_rooms` list/every ground list/every known instance's own inventory/equipped
values (via `_all_known_instance_names()`, the same "universe of instances with mutable state"
`DM_Persistence.py` already computes), then records it in `self.removed_entities` so a
scenario/room's own static `entities` list can never respawn it (see `DM_Rules.py`'s
`_instance_entities`, the one check point that consults this set). Deliberately does **not**
`del self.entities[name]` — leaves it orphaned/unreferenced, mirroring the existing precedent
that a fully-consumed item (`_consume_charge`, charges hit 0, no `replace_with`) already just
stops being referenced anywhere rather than being deleted outright; this is also what makes an
orphaned ad hoc entity self-clean out of future saves for free (collection is by *reachability*,
not a scan of `self.entities` — see persistence below). Hard guard: refuses if `name ==
player_name` — a technical necessity (the engine assumes `self.entities[player_name]` exists
everywhere), not a game-design restriction. Accepted consequence: removing a container also
orphans (and so stops persisting) anything still listed in its own `inventory` — intended, since
removal can target any entity including containers, not a bug.

**Creature/NPC conjuring.** `_attempt_creature_conjuring` (`DM_Improvisation.py`) resolves
`target_cr = self.get_challenge_rating(self.player_name)` — a single-target encounter framing,
not real NPC generation's own party-pool resolution (`DM_NpcGeneration.py`'s
`_resolve_npc_target_cr`), since there's no `entity_template` field to resolve against here —
and calls `generate_ad_hoc_creature` (`AdHoc_Generation.py`): one tool call, `create_creature`,
constrained to 1-2 real `npc_keywords.toml` archetype keywords (same reliability-over-free-text
win `NPC_Generation.py`'s own schema banks on), a `disposition` enum (`hostile`/`wary`/
`neutral`/`friendly`, mapped to a fixed attitude-default value — `hostile` is exactly `-100`,
the precise threshold `is_hostile` requires for real combat), and a `power` enum
(`weak`/`moderate`/`strong`, a flat multiplier on `target_cr`, the same role `cr_multiplier`
plays in real NPC generation). Unlike real NPC generation, there's no second LLM round trip for
stats — the keyword choice from this one call is reused directly with `NPC_Generation.py`'s own
deterministic `fit_skills_to_cr` (`rolled_cr = target_cr * power_multiplier *
random.uniform(0.85, 1.15)`, the same variance-then-deterministic-fit split real generation
uses). **Only a `"hostile"` disposition** gets an inline innate attack ability plus a
flee-under-0.4-hp_per_remain-then-attack `[[entity.behavior]]` pair attached, mirroring
`creatures.toml`'s own wolf/bandit shape exactly (real NPC generation never touches
abilities/behavior at all, since a hand-authored `entity_template` already supplies them where
wanted — an ad hoc creature has no such template, so this is the one capability here that
*does* author minimal combat data, deliberately scoped to the single case that actually needs
it to be fightable). A wary/neutral/friendly conjured NPC is dialogue-only, same as a template
author would choose for a peaceful NPC.

Since `_instance_entities` (`DM_Rules.py`) only ever runs at scenario/room load time, placement
mutates `self.entities`/`self.scenario_entities` directly instead — assigning `entity_id`/
`band` (the player's own current band, so it's always in melee range) the way `_instance_entities`
would for a fresh instance, tagging `ad_hoc = True` (so it saves/restores in full — see
"Persistence" below, unchanged from a plain ad hoc item), and appending to
`self.scenario_entities` so it's a live, targetable participant. A hostile creature also claims
`self.current_target` via `_claim_current_target_if_free` (shared with container/trap
placement, above) — so the very next player action can fight it without first having to
explicitly retarget, unless a fight is already engaged, in which case the claim is skipped
entirely (conjuring a curiosity mid-combat must never silently retarget the player away from
what they're actually fighting, and — for a non-hostile creature especially — would also wrongly
flip `_on_turn_detected`'s own round-vs-action narration choice, which reads
`self.current_target`'s own hostility).

**Entity editing.** `_attempt_entity_edit` (`DM_Improvisation.py`) builds the same editable-name
universe `_attempt_entity_removal` already builds (every present scene entity, every ground
item, every known instance's own inventory/equipped item, `player_name` always excluded — an
unrestricted edit target is exactly as risky as an unrestricted removal target) and asks
`decide_entity_edit` to pick one and how to change it, or decline. Scope is deliberately narrow:
a full `new_description` rewrite, plus `apply_condition`/`dismiss_condition` (reusing the
already-safe, already-reversible `apply_condition`/`dismiss_condition` primitives, `DM_Status.py`
— the same "use a condition for something that can plausibly change mid-scene" guidance this
file's own "Tags vs. conditions" section already gives) — never raw mechanical fields like
`skills`/`damage_value`, which would need far more validation to not silently break combat math.
A description change tags the entity `entity["edited"] = True` (see "Persistence" below) and
republishes `item_catalog_updated` so a later item-name match reflects the new text. Folded into
`help_resolved` as `"edited"` (`LLMCore`'s `_build_adam_system_message` mentions it happened) and
triggers `_publish_party_status()`, same as a real removal.

**Persistence.** An ad hoc entity (item or creature) has no static TOML template to re-derive
anything from on reload (unlike a generated NPC, which still has a real `entity_template`), so
`DM_Persistence.py`'s `_collect_ad_hoc_entities` saves every *reachable* one's complete dict
(not a diff) under `"ad_hoc_entities"`; `load_game` restores each with a full dict replacement
right alongside the existing `"ground"` restoration, then publishes `item_catalog_updated` once,
as a batch. `"removed_entities"` round-trips too — restored *before*
`load_scenario_definition`/`load_scenario` run (both call `_instance_entities`), the one
ordering requirement that keeps a removed entity from respawning on the very reload that's
supposed to remember it's gone. An *edited* hand-authored entity is different again — it still
has a real static template, so only the one field editing can actually touch (`"description"`)
needs its own save: `entity["edited"] = True` makes `save_game`'s own per-instance diff include
`"description"` explicitly (mirroring the `"generated"`-flag branch's own extra saved fields, an
`elif` alongside it since the two never need to stack), and `load_game`'s overlay loop restores
it the same way — without this, an edited description would silently revert to the static
template's own text on the very next reload.

**NLPCore catch-up.** `item_embeddings`/`item_indices` are otherwise only ever built once, from
`"rules_loaded"` — an ad hoc entity created (or restored) after that would be permanently
unreachable to `map_to_item` (a later "drop the stone" would miss and either wrongly re-trigger
creation or dead-end) without `item_catalog_updated`: `NLP_Core.py`'s own
`_on_item_catalog_updated` encodes each `{name, description}` pair the same two-phrase-per-item
shape `_on_rules_loaded` already builds, and appends onto the existing tensor/index list
(`torch.cat`) rather than rebuilding the whole catalog from scratch. Every capability above that
introduces a new name or changes a description republishes this event — a conjured creature
(so it can later be named directly), and an edited description (so a later match reflects the
new text) — not just plain item creation.

## Narration

`LLMCore` subscribes to narration-relevant events, sharing outcome-text building
(`_describe_outcome`) and background-fetch plumbing (`_queue_narration`/`_fetch_and_publish`):
- `scenario_loaded` → `generate_scene_intro` — once, from `DMCore.__init__`.
- `round_resolved` → `generate_round_response` — combat, once per round.
- `action_resolved` → `generate_response` — non-combat, once per skill use.
- `action_not_understood` → `generate_clarification_response` — acknowledges input that
  didn't resolve to any action.
- `item_interaction_resolved` → `generate_item_interaction_response` — covers examine/take/
  give/trade/open/close/use/equip/unequip/drop and room transitions.
- `dialogue_resolved` → `generate_npc_dialogue` — a found target routes through
  `_queue_dialogue` (see "Dialogue" above); a denied one falls back to an ordinary
  `_queue_narration` explanation, same shape as any other denied item interaction.
- `game_load_failed` → `generate_load_failed_response`.
- `help_resolved` → `generate_adam_response` — the reserved "ADaM" out-of-character help
  persona (see "ADaM (out-of-character help)" above); routes through `_queue_adam_response`,
  the one trigger here that never touches `context_window` at all.

The scenario/room setting and character roster are re-injected into the system message on
every request, so narration stays grounded even after the intro scrolls out of the rolling
100-message `context_window`. `generate_npc_dialogue`'s own system message (built by
`_build_dialogue_system_message`) is different in kind, not just content — it speaks as the
addressed entity, grounded in `persona`/`attitude` plus that entity's own presence-filtered
history, never the standing GM framing.

Every `_queue_narration`/`_queue_dialogue` call's background fetch also publishes
`llm_debug_updated {"query", "response"}` alongside `llm_response_ready` — consumed only by
`GUICore`'s Debug tab, never stored in `context_window` itself.

## Saving and loading

Three sibling JSON files per slot — `Saves/<slot>/dm_state.json`, `llm_state.json`,
`gui_state.json` — written/read independently by `DMCore`, `LLMCore`, and `GUICore`. `EventBus`
has no request/response mechanism, so each core owns and persists its own slice.

**Trigger:** `save_requested`/`load_requested {"slot": slot_name}`, published by
`NLPCore._detect_save_load_intent`, by `GUICore`'s File → Save... / Character → Load... popups
(see "Booting the game" for the cold-start case), or by `Textual_Core`'s Save/Load buttons.

`DMCore.save_game` writes a diff from a fresh instantiation: `setting`, `scenario_key`,
`player_name`, `round_number`, `current_room_key`, `scenario_entities`, `ground`, and
per-instance `{hp, active_conditions, currency, inventory, equipped, band}`. `load_game`
re-runs `load_rules()`, then the same scenario-load path `__init__` uses, then overlays each
saved instance's mutable fields; a saved instance with no post-reload match is skipped.
Publishes `game_loaded` on success (not `scenario_loaded`, which would re-narrate an opening
scene) or `game_load_failed {"slot", "reason"}` on failure, then re-publishes
`party_status_changed`.

`ground` (items dropped since the scenario started — see "Items and movement as intents")
round-trips too: a flat list for a single-room scenario, or a dict keyed by `room_key` for a
multi-room dungeon, mirroring the same branch `_current_ground_items` (`DM_Inventory.py`)
already makes between `self.scenario["ground"]` and `self.rooms[room_key]["ground"]`.

`LLMCore.save_game`/`load_game` persist/restore `context_window` plus scenario name/
description/characters; loading is silent. `GUICore.save_game`/`load_game` persist/restore the
Notes tab's free text, same way.

Slot names are run through `os.path.basename` before use, so a slot can't escape `Saves/`.

## Tags vs. conditions

- **Tags** are static classification data, fixed for an entity's lifetime: `damage_tags`/
  `armor_tags`, `resistance_value`/`resistance_tags` (rolled, partial reduction via
  `get_damage_reduction`), `immunity_tags` (absolute — `is_immune_to` zeroes net damage
  regardless of roll), and `vulnerability_value`/`vulnerability_tags` (rolled, extra damage
  added before reduction). Immunity wins outright over vulnerability if both match.
- **Conditions** (`active_conditions`, `apply_condition`/`dismiss_condition`) are dynamic —
  gained/lost during play via triggers or tests. Use a condition for something that can
  plausibly change mid-scene; use a tag for something permanent to what the entity is.

`abilities` is a flat list, each entry either a plain string naming a shared catalog entity
(`spells.toml`/`techniques.toml`) or an inline table for a one-off innate ability. `techniques.
toml`'s `cleave` exercises a multi-skill `skill = [...]` list and weapon-scaled damage
(`"user.weapon.dice"`/`"user.weapon.pips"`); see `ability_matches_skill`,
`resolve_weapon_reference`, `resolve_damage_value` in `DM_Combat.py`. Naming a technique/spell
directly in input can resolve it via `map_to_action` before a bare skill would.

## Data/TOML conventions

- `Rules/Fantasy/reference/entity_schema.toml` catalogs every field the engine reads off an
  entity; the sibling `template_schema.toml` does the same for an `[[entity_template]]`'s own
  fields (NPC generation — see "NPC generation"). Reference/documentation only, never loaded
  as game data (`load_rules` only `os.listdir()`s the top level of `Rules/Fantasy/`, one
  directory shallower).
- `load_rules` special-cases only `skill`, `entity`, and `entity_template` top-level keys;
  everything else in any flat `Rules/Fantasy/*.toml` file lands generically in
  `self.rules[key]`.
- `[entity.attitudes]` is `{default, name, supertype}`; `name`/`supertype` are TOML
  arrays-of-one-key-tables — `get_attitude` loops over the list checking `if toward_name in
  override`.
- `damage_value = {dice, pips, bonus}` — `bonus` is a flat number or `"user.<rule_name>"`,
  resolved via `resolve_bonus`. String `dice`/`pips` are not resolved and degrade to 0.
- `load_rules`'s per-file exception handling means a malformed TOML file fails quietly — a
  parse error loads that file with less data than expected, not a crash.

## LLM integration

Endpoint is LM Studio's OpenAI-compatible API. `/v1/models` lists the catalog, not what's
currently loaded — a chat completion can still 400 with `"No models loaded"` even when
`/v1/models` shows one. The request payload has no explicit `"model"` field, which only works
correctly when exactly one chat model is loaded.

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

The RAG query is the player's own raw input, not the full instruction-padded narration prompt
— embedding the padded prompt dilutes similarity enough to miss lore a bare-input query would
find. `generate_scene_intro` passes the scenario name+description instead (no player input
exists yet); `generate_load_failed_response` falls back to its own full prompt.

`vectorize_pdf.py` is a standalone CLI that builds this same cache ahead of time:
`python vectorize_pdf.py [pdf_or_dir] [--query "..."]`, defaulting to `Settings/Fantasy/`.
Reuses `RagIndex` directly via `RagIndex.wait_until_ready()`.

## Textual mirror (headless testing)

`Textual_Core.py` subscribes to the same events `GUI_Core` displays and adds its own `Input`
widget publishing `user_input_submitted`, so the app can be driven and asserted on headlessly
(`app.run_test()`/`Pilot`) without Tkinter or a display.

Practical constraints when touching this file:
1. Don't name an attribute `self._ready` — Textual's `App` reserves that name internally.
2. Pre-mount events (`DMCore` publishes `rules_loaded` synchronously during `__init__`, which
   can precede `compose()`) are buffered and flushed in `on_mount`.
3. `RichLog.lines` only reflects content once its tab is active — activate it
   (`tabbed_content.active = "tab_id"`, then `await pilot.pause()`) before reading a
   background tab.
4. Writes can arrive from a foreign thread (`LLMCore`'s background fetch). `call_safely` wraps
   everything through `self.call_from_thread`, falling back to a direct call.
5. Pilot has no `.type()` in the installed Textual version (8.2.8) — build a key list
   (`["space" if c == " " else c for c in text]`) and pass it to `pilot.press(*keys)`.
6. Joining a background thread from an `async def` must go through
   `await asyncio.to_thread(thread.join)`, not a bare `t.join()`, or the event loop deadlocks.

## Testing

- **`test_unit.py`** — offline `unittest.TestCase` classes, kept deliberately lean: one
  representative test per genuinely distinct mechanism/branch, not one per edge case or per
  flavor variant of an already-covered code path (ex: one hidden-hazard notice-roll test
  stands in for both the dart trap and the scythe trap). `TestGameBoot` and
  `TestNlpConfidenceThreshold` load the real `sentence-transformers` model via `setUpClass`.
  Most other classes share fixture setup via `DMTestCase` (`scenario_name` class attribute,
  plus `_capture`/`_capture_any` helpers) and `LLMTestCase`. `TestCharacterCreationRename`
  covers the "player" placeholder and the rename path against
  `Rules/Fantasy/scenarios/character_test.toml` — a minimal scenario built solely for this,
  kept separate from the real gameplay scenarios. `TestFreeformDialogue` covers
  `DialogueMixin`'s own addressee resolution/gating (named vs. default target, hostile-allowed,
  not-present/object denials); `TestRoomLevelPresenceScoping` wires a real `DMCore` and
  `LLMCore` together over one event bus (no `NLPCore`) against `crypt.toml`'s room graph to
  prove `present_entities` tagging/filtering actually scopes a specific NPC's own witnessed
  history to the room(s) it was present for, not the DM's own omniscient window.
  `TestMultipleActions` covers the West End Games multi-action penalty end to end: the dice
  math (`resolve_action`/`resolve_opposed_action`'s own `dice_penalty` param, including that a
  defender's own opposed roll is never penalized), that N actions in one turn still resolve as
  exactly one round, the `engaged_combat_target` regression this mechanic's own batching
  introduced (an item-test-only turn must never trigger a round just because
  `self.current_target` happens to already be hostile from an earlier turn), and that a
  diceless item-interaction clause counts toward the shared penalty without ever being
  penalized itself. `TestNlpConfidenceThreshold`'s own mixed-clause tests cover the pipeline
  merge from NLPCore's side: an item clause and an action clause joining one `turn_detected`
  event, an exempt movement/formation clause still publishing separately even when mixed with
  a genuine item clause, and a genuine item verb still taking priority over dialogue now that
  item detection runs per clause instead of once against the whole input.
- **`test_integration.py`** — every test needing a real, running LM Studio, gated on
  `_lm_studio_reachable()` so they skip together when nothing's listening on
  `127.0.0.1:1234`. `_LivePipelineTestCase`'s own optional `character` class attribute is
  forwarded straight into `DMCore`'s `character` param — used by
  `TestCreatedCharacterConversation` to check real narration/combat work end-to-end against a
  custom-named, custom-race character, not just `characters.toml`'s `gladstone`.
  `TestNpcGenerationLive` is a plain `unittest.TestCase` (no `_LivePipelineTestCase`, no
  NLPCore/LLMCore) since NPC generation runs synchronously during `DMCore`'s own construction
  — a real tool-calling round trip against `Rules/Fantasy/scenarios/npc_generation_test.toml`.
  The pure fitting math (`TestNpcGeneration`) and the DMCore-side wiring
  (`TestNpcGenerationDMCoreIntegration`, patching `NPC_Generation._real_call_chat_completion`
  with a deterministic fake) both live in `test_unit.py` instead, so most of NPC generation stays
  covered by the fast offline suite — only the "does the currently-loaded model actually
  return a valid tool call" question needs a live LM Studio.

`python -m pytest -q` runs both files; `python -m pytest -q test_unit.py` runs the fast,
offline subset only.

## Known gaps

- `NLP_Core.py` — a keyword-driven skill match can still dominate an unrelated whole-sentence
  embedding match (ex: "identify the dagger" resolves to the wrong skill); no multi-instance
  disambiguation (ex: "the wounded wolf" vs. "the other wolf").

## Extended goals

Not yet started, except where noted:
- Characters are language-dependent — an entity's own comprehension of the language it's
  addressed in should gate dialogue/narration, not just its attitude data.
- Dialogue sentiment sways attitudes — the sentiment of what the player says, not just which
  skill check they made, should be able to move an entity's `[entity.attitudes]` axes.
- Actions sway attitudes by varying degrees — a resolved action (combat, theft, a favor)
  should nudge attitude axes proportionally, not just be gated by attitude that already exists.
- Random encounters, enemy generator — procedurally populate a scene/room with creatures
  instead of every encounter being scenario-authored. **Partially started**: "NPC generation"
  above fits a `generate = true` template's *stats* to a target CR at instancing time, but the
  template itself (attitudes/behavior/abilities/equipment, and whether/where it appears at
  all) is still hand-authored — nothing yet actually populates a scene/room on its own.
- Scenario, quest, NPC, item, and location generators — procedurally author the TOML data
  itself rather than every scenario/entity being hand-written. Same caveat as above — NPC
  generation fills in runtime *data* on an existing template, it doesn't author TOML.
- A 'dungeon master' persona the LLM can speak directly to the player as.
- Tools that the LLM may call to directly interact with the scene.
