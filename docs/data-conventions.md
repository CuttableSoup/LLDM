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

