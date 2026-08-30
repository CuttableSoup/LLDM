# LLDM — Extended Goals

Part of the [LLDM](../CLAUDE.md) docs — not yet started, except where noted.

## Extended goals

Not yet started, except where noted:
- Characters are language-dependent — an entity's own comprehension of the language it's
  addressed in should gate dialogue/narration, not just its attitude data. **Done for
  dialogue**: see "Dialogue"'s own "Language barriers" — `_detect_language_barrier` gates
  `_resolve_dialogue` on a shared `languages` entry, falling back to invented gibberish styled
  by `races.toml`'s own `nonsense_phrase` when nothing overlaps. Not extended to the omniscient
  third-person narrator voice, which by design still sees/relays everything regardless of what
  any one character understands (see "Room-level presence"'s own "the player's point of view is
  deliberately still everything").
- There's no mechanism yet for an *NPC's* own action (ex: an ally's combat hit) to sway anyone's
  attitude — only the player's own actions do (see "Action-driven attitude drift").
- Random encounters, enemy generator — procedurally populate a scene/room with creatures instead
  of every encounter being scenario-authored. **Partially started**: `[[location.encounter]]`
  (see "Random encounters") lets a location/room roll a weighted table of outcomes on entry —
  but the table itself, and every entity/template it can resolve to, is still hand-authored;
  there's no procedural encounter *design*, only randomized *selection* among authored options.
  "NPC generation" separately fits a `generate = true` template's *stats* to a target CR at
  instancing time, but the template itself (attitudes/behavior/abilities/equipment, and
  whether/where it appears at all) is still hand-authored too.
- Scenario, quest, NPC, item, and location generators — procedurally author the TOML data itself
  rather than every scenario/entity being hand-written. Same caveat as above.
- ADaM acting proactively, not just when addressed by name — today ADaM only reacts to an
  explicit "adam ..." turn; a fuller DM persona would also initiate content unprompted (a
  complication, a random encounter, a pacing beat). Deliberately not an always-eligible-to-act
  switch: an ambient "decide whether to act this turn" hook would run on every turn, and live
  probing found the currently-loaded model complies with an under-specified request readily
  unless explicitly told not to. If built, favor a narrow, rate-limited trigger fired only at
  specific, already-instrumented moments (room entry, N turns without incident) with its own
  decline-biased prompt. Not started.
- A 'dungeon master' persona the LLM can speak directly to the player as — distinct from both
  the omniscient third-person narrator voice and ADaM's own explicitly out-of-character one.
- Tools that the LLM may call to directly interact with the scene. **Partially started**: ADaM's
  own ad hoc generation (see "Ad hoc entity creation and removal") already gives the LLM real
  `tool_choice="auto"` function-calling access to create items/creatures and remove/edit
  entities — but only via `LLM_Client.py`'s narrow, single-purpose, DMCore-triggered calls
  (`generate_ad_hoc_item`/`generate_ad_hoc_creature`/`decide_entity_removal`/
  `decide_entity_edit`). `LLM_Core.py`'s own narrating GM voice — the one actually speaking to
  the player on every ordinary turn — never sends a `tools` payload at all and has no scene-
  mutation ability of its own; this goal is really about giving *that* voice general tool access,
  not another bespoke gated call.
