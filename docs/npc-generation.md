# LLDM — NPC Generation

Part of the [LLDM](../CLAUDE.md) docs — entity_template, varied fields, CR fitting.

## NPC generation

`[[entity_template]]` is a genuinely different top-level key from `[[entity]]`, loaded into its
own `self.entity_templates` dict, never `self.entities`. Every entity_template in
`Rules/Fantasy/` today is scenario-local (ex: `tavern_random.toml`'s own
`generated_innkeeper`/`generated_stranger`). Keeping generation stubs in a separate namespace
means a scenario/room entry names a real entity via `{ name = "wolf", band = 1 }` (looked up in
`self.entities`) but an entity_template via `{ template = "generated_stranger", band = 2 }`
(looked up in `self.entity_templates`) — `_instance_entities` (`DM_Rules.py`) checks which key
an entry has and resolves accordingly; a `name` that only exists as a template, or vice versa,
fails the same `log_error`-and-skip way a genuine typo would. An entity_template skips
authoring its own `[entity.skills]`/`max_hp`/`name` — those are decided the moment it's
instanced, via a local-LLM function call fit to a target challenge rating. Everything else
(`[entity.attitudes]`/`[[entity.behavior]]`/`abilities`/`equipped`/`is_party`/`qualities.race`)
is still hand-authored normally, deliberately varied per template so generated NPCs don't all
read as the same person wearing a different name tag. Referencing the same template twice gets
two independent instances (`generated_stranger`, `generated_stranger_2`), each with its own LLM
call.

**Varied fields.** An entity_template's scalar fields can be authored as a range or weighted
choice instead of a fixed value — `NPC_Generation.py`'s `resolve_varied_value(value)` is the
shared vocabulary: `{min, max}` (uniform random pick) or a list of single-key `{"choice" =
weight}` tables (`random.choices` pick of the key). Applied per-leaf, so a template can mix
fixed and varied entries freely (ex: `generated_stranger`'s `default` keeping `threat`
flat while disposition/familiarity vary). `hint`/`cr_multiplier`/`qualities` resolve before the LLM
call (they feed the prompt/target-CR math — gender/race/age have to be concrete before the LLM
invents a name); `currency`/attitudes resolve independently, any time after. An
`[[entity_template.attitudes.name]]` override can target the literal token `"player"` (same
reserved placeholder as `PLAYER_PLACEHOLDER`), substituted for `self.player_name` at instancing
time. `Rules/Fantasy/reference/template_schema.toml` doubles as a worked-examples file for this
vocabulary. Generation deliberately never touches `abilities`/`equipped`/`inventory` — whether
a generated NPC can fight or what it carries is still decided by whoever authors the template.

`Rules/Fantasy/npc_keywords.toml` is a small catalog of ~16 archetype keywords (`warrior`,
`trickster`, `scholar`, ...), each naming 3-4 real skills. `NPC_Generation.py` (pure) builds an
OpenAI-style `tools` payload constraining the LLM to 1-2 keyword *names* (an enum, not free
text — more reliable with small local models); `generate_npc_stats` resolves them to their
union of real skills before fitting. `fit_skills_to_cr(key_skills, target_cr, hp_share=0.3,
damage_dice=0, damage_pips=0)` is the deterministic inverse of `calculate_challenge_rating`:
`hp_units = round(target_cr * hp_share)`, `max_hp = hp_units * 3`; the remaining budget becomes
a single rating `R` (floored at 3) given identically to each of the first 3 key skills (any 4th+
gets a lower flavor-only rating). `generate_npc_stats` rolls `target_cr * cr_multiplier *
random.uniform(1-variance, 1+variance)` before fitting — that plus keyword choice is where
randomness comes from; `fit_skills_to_cr` itself stays deterministic and directly testable. On
any failure (no `tool_calls`, malformed JSON, network error, timeout, or
`skip_llm_generation=True`) it falls back to a random keyword pick with no network call —
matching the app's "Ollama is best-effort, never blocks core gameplay" posture.

`LLM_Client.py`'s `call_chat_completion` is a small, synchronous, stateless POST — deliberately
not shared with `LLM_Core.py`'s async `fetch_from_llm` (`fetch_from_llm` must never raise; this
one must raise cleanly so `generate_npc_stats`'s fallback triggers). Its hard 20s `timeout`
matters because generation runs synchronously on whatever thread is instancing the scene
(always the GUI/main thread in practice) — a known limitation: a scene with entity_template
entries visibly pauses ~5s per generated NPC on a fresh load. A two-phase "placeholder now,
patch later" redesign would fix this properly; out of scope for now.

`DM_Encounters.py`'s `_resolve_ambient_encounter` is the one call site with no such pause to
excuse it — unlike `_resolve_location_encounter`'s "on_enter" trigger (once, on a deliberate
location/room entry) or `DM_Travel.py`'s per-block environment roll (once per elapsed travel
block), "ambient" fires on an arbitrary player turn (`DM_Core.py`'s `_on_turn_detected`)
whenever no hostile is already present. A `generate=true` entity_template referenced from an
ambient-triggered entry is still resolved normally, but `_resolve_ambient_encounter` forces
`skip_llm_generation=True` through `_instance_entities`, so it always takes
`generate_npc_stats`' instant offline-fallback path instead of ever blocking on Ollama.
`Rules/Fantasy/scenarios/town.toml`'s own `generated_beggar` (an "on_enter" reference) is
unaffected — it's a real, shipped example of the tolerated case.

`DM_NpcGeneration.py`'s `NpcGenerationMixin` runs from `_instance_entities` right after an
instance is stored into `self.entities` (the CR-fitting math reads back off
`self.entities[name]`). `_resolve_npc_target_cr` resolves a template's `target_cr` — a number,
or `"player"`/`"party"` (live CR, resolved fresh at instancing time). `"party"` can't call
`get_party_challenge_rating()` directly since `self.scenario_entities` isn't finalized mid-loop;
instead `_instance_entities` threads a `party_pool` param that combines with whoever's been
instanced earlier in the same loop. The result is tagged `entity["generated"] = True`.

**Save/load.** `skills`/`max_hp`/`name`/`description`/`qualities`/`attitudes` don't round-trip
for an ordinary entity (`save_game` only diffs `hp`/`active_conditions`/`currency`/`inventory`/
`equipped`/`band` — everything else re-derives from the static template) — broken for a
generated entity, which has no static template. `save_game` conditionally saves those six
fields too when `entity["generated"]` is true; `load_game` threads `skip_llm_generation=True`
through re-instancing so it takes the offline fallback path unconditionally, and the existing
overlay applies the real saved values on top.

`DM_Social.py`'s `describe_character` uses `entity.get("name", entity_name)`, not the raw dict
key, so a generated NPC's LLM-invented name is what's actually narrated.

