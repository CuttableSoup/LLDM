# LLDM

An autonomous dungeon master: the player types free-text actions, NLP maps them to a skill,
a simplified D6 (West End Games) engine rolls dice and resolves outcomes, and a local LLM
(currently Gemma via Ollama at `http://127.0.0.1:11434`) narrates what happened. Skills,
entities, items, spells, rules, and scenarios are all data-driven via TOML, organized into
"settings" — self-contained sibling directories under `Rules/` (`Rules/Fantasy/`,
`Rules/Zombie/`), each independently scanned by `load_rules`. None of the engine itself is
fantasy-specific — `DMCore(event_bus, scenario_name, setting="Fantasy")`'s own `setting` param
picks which one to boot from (`Rules/<setting>/scenarios/<scenario_name>.toml` and every
sibling `Rules/<setting>/*.toml`), and it round-trips through a save file (`dm_state.json`'s
own `"setting"` key) so a resumed save reloads from the same setting it was saved under.
`Rules/Fantasy/` is the deep, primary setting; `Rules/Zombie/` is a bare-bones second one (a
Left 4 Dead-inspired survival shooter) that proves the engine is setting-agnostic — see
`Rules/Zombie/scenarios/rooftop.toml` (`python LLDM.py rooftop --setting Zombie`). Every
setting authors its own skills/rules/races from scratch — nothing is shared or inherited
between settings, deliberately, so one setting's data can never leak into another's.
GUI-driven character creation (Character → Create...) and NPC generation are still wired to
`Rules/Fantasy/` only; a second setting is reachable only via CLI quick-boot (`--setting`) or a
save file that already carries its own `"setting"`.

## Documentation map

The detailed architecture notes used to live entirely in this file; they're now split by topic
under [docs/](docs/) so a task touching one subsystem doesn't have to load all of them. Read the
relevant file(s) below before working in that area — each is self-contained and assumes only
this overview.

- [docs/architecture.md](docs/architecture.md) — the six modules wired through `Event_Bus.py`,
  and where each lives under `dm/`, `nlp/`, `llm/`, `gui/`, `resolution/`.
- [docs/action-resolution.md](docs/action-resolution.md) — the `user_input_submitted` →
  `turn_detected` → resolve → narrate pipeline, `ActionOutcome`/`Effect`, and the multi-action
  dice penalty.
- [docs/combat.md](docs/combat.md) — hostility/rounds/turns/initiative, challenge rating math,
  status/condition triggers and modifiers, damage/healing, and tags vs. conditions.
- [docs/npc-generation.md](docs/npc-generation.md) — `[[entity_template]]`, varied fields, and
  fitting generated stats to a target challenge rating.
- [docs/movement-scenarios.md](docs/movement-scenarios.md) — bands/range/formation, locations
  vs. rooms, location-to-location travel, and random encounter tables.
- [docs/downtime.md](docs/downtime.md) — the block clock (day/night, `DMCore.current_block`),
  rest, and grid-based travel/environments/world map; what's still not built (night watch).
- [docs/character-creation.md](docs/character-creation.md) — race/point-buy skill dice, and the
  three routes a game can actually start from (`LLDM.py`'s `main()`).
- [docs/inventory-items.md](docs/inventory-items.md) — entity tests, currency/item transfer, the
  diceless item-interaction intents, and crafting (the one item-adjacent check that rolls).
- [docs/social-dialogue.md](docs/social-dialogue.md) — the three-axis attitude model, action- and
  tone-driven attitude drift, free-form dialogue, and language barriers.
- [docs/adam-improvisation.md](docs/adam-improvisation.md) — ADaM (out-of-character help), ad hoc
  entity creation/removal/editing, and summoning.
- [docs/narration-llm.md](docs/narration-llm.md) — narration triggers, the Ollama bootstrap/model
  pull, and RAG sourcebook grounding.
- [docs/persistence.md](docs/persistence.md) — the three-file-per-slot save/load contract.
- [docs/data-conventions.md](docs/data-conventions.md) — how `Rules/Fantasy/*.toml` is structured
  and what `load_rules` special-cases.
- [docs/testing.md](docs/testing.md) — `test_unit.py` vs. `test_integration.py`, and the Textual
  headless mirror's own gotchas.
- [docs/extended-goals.md](docs/extended-goals.md) — not yet started, except where noted.
