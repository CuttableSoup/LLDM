# LLDM Entity Schema Documentation

This document explains the schema used to define entities in the LLDM game. It is based on the `Entity` class and provides a structured way to define game entities.

## Root Object
The root of the YAML file is `entity`. All other fields are nested under this key. Can be a direct string/number or a reference (e.g., `reference(self:parameter.name)`).

## Core Identification
These fields define the basic identity of the entity.

- **`name`**: (String/Reference) The unique name of the entity. 
- **`supertype`**: (String) The broad category of the entity. Examples: `object`, `creature`, `item`, `supernatural`.
- **`type`**: (String) The specific type of the entity. Examples: `blade`, `humanoid`, `weapon`, `spell`.
- **`subtype`**: (String) A more specific classification. Examples: `longsword`, `human`, `sword`, `charm`.
- **`description`**: (String) A narrative description of the entity.

## Vitals
These fields track the entity's health, magic, and stamina.

- **`max_hp`**: (Integer) Maximum Hit Points.
- **`cur_hp`**: (Integer) Current Hit Points.
- **`max_mp`**: (Integer) Maximum Mana/Magic Points.
- **`cur_mp`**: (Integer) Current Mana/Magic Points.
- **`max_fp`**: (Integer) Maximum Fatigue/Stamina Points.
- **`cur_fp`**: (Integer) Current Fatigue/Stamina Points.

## Physicality & Stats
- **`size`**: (String) Size category. Allowed values: `fine`, `diminutive`, `tiny`, `small`, `medium`, `large` `huge`, `gargantuan`, `colossal`.
- **`weight`**: (Float) Weight of the entity.
- **`exp`**: (Integer) Unspent experience points (typically for players).
- **`total_exp`**: (Integer) Total accumulated experience points.
- **`value`**: (Integer) Monetary value.

## Movement
Defines how the entity moves and what can move through it.

- **`move`**: (Dictionary) Movement speeds for different terrains.
  - `land`: (Integer) Speed on land.
  - `water`: (Integer) Speed in water.
  - `air`: (Integer) Speed in air.
- **`passable`**: (Dictionary) Defines passability costs (0 is impassable).
  - `requirement`: (List) Conditions for passing through.
    - `ally`: (Dictionary) Example: `{ name: "*" }` allows any ally.
  - `land`: (Integer) Cost to pass through on land.
  - `water`: (Integer) Cost to pass through in water.
  - `air`: (Integer) Cost to pass through in air.

## Attributes & Qualities
- **`attribute`**: (Dictionary) Core attributes (e.g., `physique`, `intelligence`).
- **`skill`**: (Dictionary) Skills (e.g., `arcane`, `fortitude`).
- **`specialization`**: (Dictionary) Specialized skills (e.g., `evocation`).
- **`quality`**: (Dictionary) Physical qualities. Keys can include `body`, `eye`, `gender`, `hair`, `height`, `skin`, `age`, `material`.

## Social & Status
- **`ally`**: (List) List of allies, grouped by identifier (e.g., `name`).
- **`enemy`**: (List) List of enemies, grouped by identifier (e.g., `supertype`).
- **`attitude`**: (List) Defines disposition towards others.
  - `default`: (Dictionary) Default attitude values (`disposition`, `trust`, `confidence`, `respect`, `obligation`, `intimacy`).
  - Specific overrides based on `quality`, `supertype`, or `name` with `modifier` adjustments.
- **`language`**: (List) Languages known (e.g., `common`, `telepathy`).

## Interaction & Combat Rules
Defines active abilities and interactions.

### `interaction` / `ability`
These lists define actions the entity can take. `interaction` typically refers to active uses, while `ability` might refer to inherent capabilities, though they share a similar structure.

- **`type`**: (String) The type of interaction (e.g., `use`, `attack`).
- **`description`**: (String) Description of the action.
- **`range`**: (Integer) Distance in squares.
- **`target` / `user` / `self`**: (Dictionary) Defines effects and requirements for the target, user, and the entity itself.
  - **`effect`**: (List) Effects applied.
    - `name`: (String) Name of the effect (e.g., `fireball`, `slashing`).
    - `entity`: (String) Reference to a defined entity (e.g., `bleeding`).
    - `duration`: (Dictionary) Duration of the effect (`frequency`, `length`).
    - `magnitude`: (Dictionary) Magnitude of the effect (`source`, `reference`, `value`, `pre_mod`, `type`).
    - `apply`: (String) Apply a stat change (e.g., `damage_cur_hp/damage_cur_mp/damage_cur_fp`).
  - **`requirement`**: (List) Conditions required for the action.
    - `ally`: (Dictionary) Ally requirement.
    - `test`: (Dictionary) Skill check or save (`source`, `reference`, `value`, `pre_mod`, `type`).
      - `difficulty`: (Dictionary) Difficulty of the test.
      - `pass` / `fail`: (Dictionary) Outcomes (`description`, stat changes like `cur_hp`).
    - `cur_mp` / `cur_fp`: (Integer) Resource costs (negative values).
    - `or` / `not`: (List) Logical conditions for requirements.

### `trigger`
Events that happen automatically.

- **`frequency`**: (String) How often it triggers (e.g., `day`, `hour`).
- **`length`**: (String) Duration (e.g., `*` for indefinite).
- **`timestamp`**: (Empty/String) Last triggered time.
- **`target` / `user` / `self`**: (Dictionary) Similar to interaction, defines effects and requirements.
  - Example effect: `{ name: restore, inventory: { operation: add, list: [...] } }` or `{ name: restore, heal_cur_mp: 5 }`.
    - `operation`: (String) `add` (default), `set`, or `remove`.

## Inventory & Abilities
- **`slot`**: (List) Equipment slots occupied (e.g., `hand`).
- **`bulk`**: (Integer) Bulkiness/size for inventory purposes.
- **`inventory`**: (Dictionary)
  - `inventory_rules`: (Dictionary) Rules for what can be stored (`slots_only`, `quantity_allowed`, `type_allowed`).
  - `list`: (List) Items in inventory (`name`, `quantity`, `equipped`).

## Narrative
- **`memory`**: (List) Strings representing memories.
- **`quote`**: (List) Strings representing quotes.

## Other
- **`status`**: (List) Unique sub-entities.
  - Example: `intelligence` (defines AI level like `basic`, `advanced`).
- **`parameter`**: (Dictionary) Custom parameters (e.g., `name: flamebellow`).
- **`magnitude`**: (Dictionary) Default magnitude definition if applicable.
