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
  resolved via `resolve_bonus`. `dice`/`pips` each tolerate exactly one string apiece —
  `"user.weapon.dice"`/`"user.weapon.pips"` respectively, resolved off the attacker's own
  equipped weapon's `damage_value` by `resolve_weapon_reference` (`Combat_Resolution.py`). Any
  other string (a typo, or the wrong field's own literal) isn't resolved — it silently degrades
  *both* dice and pips to 0 at roll time (a `log_warning`, not a crash).
- `load_rules`'s per-file exception handling means a malformed TOML file fails quietly — a parse
  error loads that file with less data than expected, not a crash.

## Load-time validation

`DM_Validation.py`'s `ValidationMixin.validate_loaded_data()` runs once per full (re)load —
called from `DMCore.__init__` right after `load_scenario_definition` (every entity/
entity_template/location/skill/rule is loaded by then, nothing yet instanced), and from
`DM_Persistence.py`'s `load_game` at the same point, so a resumed save is re-checked against
whatever `Rules/<setting>/` looks like now, not whatever it looked like when the save was
written. It runs two independent passes, both driven off the same loaded data:

**Referential integrity** — does a name/skill/room/location a field claims to point at actually
resolve to something real. Checks: `[entity.equipped]` slot keys against `get_equip_slots`
(`_validate_equipped_slots`, moved here from the end of `load_rules` so a scenario-local entity
gets checked too); every `skill` field (an entity/ability's own, plus `[entity.test]`/
`[entity.craft]`/`[entity.notice]`) against `self.skills`; every `[[entity.behavior]]` `action`
against the real `resolve_named_ability` resolution (skipping `MOVEMENT_ACTIONS`/
`TRANSFER_ACTIONS`, the reserved movement/transfer words); every entity-name-shaped field
(`inventory`, `[entity.equipped]` values, `replace_with`, `summon.name`/`summon.template`,
`materials[].item`) against `self.entities`/`self.entity_templates`; `damage_value.bonus`'s
`"user.<rule>"` indirection against `self.rules`; an `entity_template` authoring `skills`/
`max_hp` (generation always overwrites these, so an authored value is silently discarded —
`name` is *not* in this list, since a template's own `name` is required as its
`self.entity_templates` lookup key, unlike the live instance's copy of it); and every
`[[location]]`/`[[location.room]]` graph reference (`start_room`, `return_to`, `exit`
`destination`/`arrival_room`, a room's own `exit` `destination`, and both levels' own `entities`
list). Deliberately skips `[[location.encounter]]`'s own weighted-choice keys — a real
entity/entity_template name, the reserved `"nothing"`, or pure flavor text are indistinguishable
without knowing the author's intent.

**Field shape/type checking** — `_validate_entity_shapes`/`_validate_location_shapes`, a
declarative pass against `entity_schema.toml`/`template_schema.toml`/`location_schema.toml`'s own
documented shapes (not a JSON-schema library — hand-rolled checks against the same module-level
tables driving everything else in this file). `SCALAR_FIELD_TYPES` maps a field name straight to
its expected Python type(s) (`max_hp`/`bulk`/etc. → `(int, float)`, `is_player`/`usable` → `bool`,
`mount` → `(str, list)`, ...); `LIST_OF_STRING_FIELDS` (`languages`, `inventory`, every `*_tags`
field, ...) checks each element is a string; `DICE_TABLE_FIELDS` (`damage_value`, `armor_value`,
`resistance_value`, `vulnerability_value`) checks `dice`/`pips` are numeric (or, mirroring the
convention above, the one literal `"user.weapon.<field>"` string apiece) and `bonus` is numeric
or a `"user.<rule>"` string. Dedicated `_check_*` methods cover the remaining compound shapes:
`[entity.skills]`/`[entity.equipped]` (dict of str → int/str), `[entity.attitudes]` (default/
name/supertype axis triples), `[[entity.behavior]]` (condition/action/priority entries),
`[entity.test].targets` (AoE/multi-target shape), `summon` (exactly one of `name`/`template`, not
both, not neither), `materials` (list of `{item, amount}`), and an `entity_template`'s own
generation-input fields (`target_cr`, `cr_multiplier`, `variance`, `hint`). Every check tolerates
a field being entirely absent (optional fields stay optional) and tolerates the "varied value"
shape (`_is_varied_value_shape` — a `{min, max}` range or a weighted-choice list) on the specific
`entity_template` fields `template_schema.toml` documents as allowing it, without trying to
enforce it's *only* legal there — that narrower authoring rule is `template_schema.toml`'s own
documentation, not something this pass re-derives.

Same non-blocking convention as everywhere else in this file: every problem is one `log_error`
publish, never a raised exception — "malformed data degrades quietly, on purpose" holds here too.
Entirely setting-agnostic (no `Rules/Fantasy`-specific assumption anywhere), so both passes apply
unchanged to `Rules/Zombie/`.

