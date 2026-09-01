# LLDM — Data/TOML Conventions

Part of the [LLDM](../CLAUDE.md) docs — how Rules/Fantasy/*.toml is structured and loaded.

## Data/TOML conventions

- `Rules/Fantasy/reference/entity_schema.toml` catalogs every field the engine reads off an
  entity; the sibling `template_schema.toml` does the same for an `[[entity_template]]`'s own
  fields. Reference/documentation only, never loaded as game data (`load_rules` only
  `os.listdir()`s the top level of `Rules/Fantasy/`, one directory shallower).
- `load_rules` special-cases only `skill`, `entity`, and `entity_template` top-level keys;
  everything else in any flat `Rules/Fantasy/*.toml` file lands generically in `self.rules[key]`.
- `[entity.attitudes]` is `{default, name, supertype}`; `name`/`supertype` are TOML
  arrays-of-one-key-tables — `get_attitude` loops over the list checking `if toward_name in
  override`.
- `damage_value = {dice, pips, bonus}` — `bonus` is a flat number or `"user.<rule_name>"`,
  resolved via `resolve_bonus`. String `dice`/`pips` are not resolved and degrade to 0.
- `load_rules`'s per-file exception handling means a malformed TOML file fails quietly — a parse
  error loads that file with less data than expected, not a crash.

## Load-time validation

`DM_Validation.py`'s `ValidationMixin.validate_loaded_data()` runs once per full (re)load —
called from `DMCore.__init__` right after `load_scenario_definition` (every entity/
entity_template/location/skill/rule is loaded by then, nothing yet instanced), and from
`DM_Persistence.py`'s `load_game` at the same point, so a resumed save is re-checked against
whatever `Rules/<setting>/` looks like now, not whatever it looked like when the save was
written. It's **referential integrity only** — does a name/skill/room/location a field claims to
point at actually resolve to something real — not a field-shape/type schema check against
`entity_schema.toml`/`template_schema.toml`/`location_schema.toml` themselves (a separate, larger
effort, not attempted). Checks: `[entity.equipped]` slot keys against `get_equip_slots`
(`_validate_equipped_slots`, moved here from the end of `load_rules` so a scenario-local entity
gets checked too); every `skill` field (an entity/ability's own, plus `[entity.test]`/
`[entity.craft]`/`[entity.notice]`) against `self.skills`; every `[[entity.behavior]]` `action`
against the real `resolve_named_ability` resolution (skipping the two reserved movement words);
every entity-name-shaped field (`inventory`, `[entity.equipped]` values, `replace_with`,
`summon.name`/`summon.template`, `materials[].item`) against `self.entities`/
`self.entity_templates`; `damage_value.bonus`'s `"user.<rule>"` indirection against `self.rules`;
an `entity_template` authoring `skills`/`max_hp` (generation always overwrites these, so an
authored value is silently discarded — `name` is *not* in this list, since a template's own
`name` is required as its `self.entity_templates` lookup key, unlike the live instance's copy of
it); and every `[[location]]`/`[[location.room]]` graph reference (`start_room`, `return_to`,
`exit` `destination`/`arrival_room`, a room's own `exit` `destination`, and both levels' own
`entities` list). Deliberately skips `[[location.encounter]]`'s own weighted-choice keys — a
real entity/entity_template name, the reserved `"nothing"`, or pure flavor text are
indistinguishable without knowing the author's intent.

Same non-blocking convention as everywhere else in this file: every problem is one `log_error`
publish, never a raised exception — "malformed data degrades quietly, on purpose" holds here too.
Entirely setting-agnostic (no `Rules/Fantasy`-specific assumption anywhere), so it applies
unchanged to `Rules/Zombie/`.

