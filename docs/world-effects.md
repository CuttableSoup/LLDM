# World effects

Part of the [LLDM](../CLAUDE.md) docs — deliberate changes to the scene itself: setting a fire,
barricading a door, tying someone up, putting a fire out. Assumes
[action-resolution.md](action-resolution.md) and [combat.md](combat.md).

## The shape

A world effect is an ordinary maneuver-shaped `[[entity]]` (see `maneuvers.toml`), authored in
`Rules/<setting>/world_effects.toml` and listed under one skill's own `abilities` (`survival` lists
`set fire`; `athletics` lists `douse`, `barricade`, `bind`). A plain-words line reaches it the way
`trip` does — by the keywords/description in the matcher's embedding bank — and resolves through the
same roll, target and `on_pass`/`on_fail` machinery. There is no second pipeline and nothing a model
says happened: an effect either lands through one of the engine's own primitives or does not.

| Effect | Skill | Check | What it does |
|---|---|---|---|
| `set fire` | survival | flat 6 | `summon`s a `blaze` hazard (block-denominated, 3 blocks); `report_crime` `arson` |
| `douse` | athletics | flat 6 | `dispel`s a hazard-subtype entity; dismisses `burning` on a person |
| `barricade` | athletics | flat 6 | `summon`s a `barricade pile` (`blocks_exits`), standing until destroyed |
| `bind` | athletics | opposed | `bound` condition on the target (`assault = true`); `escape artist` frees |
| `cut` | blades | opposed | damages a present *object* (a rope entity); a creature or an absent rope is untouched |
| `topple` | athletics | opposed | damages a present *object* (a shelf entity); the same honesty |

A flat `difficulty` is deliberately authored: no model is asked to rate it, so the same line always
faces the same bar.

## Engine support

- **`summon = { …, unit = "blocks" }`** (`DM_Summoning.py`): the hazard counts down on the block
  clock (`summon_expires_in_blocks`, `_expire_summons_by_block`, from `advance_blocks`) rather than
  the combat round. Omit `duration` and it stands until something removes it. Hazards expire
  globally, like conditions do, whether or not the player is in the room.
- **`on_block` status trigger** (`DM_Time.py`'s `_evaluate_block_statuses`): `on_round`'s cadence
  outside combat. `statuses.toml`'s `blaze burns` / `blaze burns on the clock` apply `burning`
  (`side = "all"`, `radius = 0`) to whoever shares the blaze's band, each block and each round.
  Nothing ticks while no time passes: a fire is a clock, so `rest`, travel and waiting hurt.
- **`blocks_exits`** (`DM_Movement.py`'s `_barricade_standing`): a living scene entity authoring it
  denies every room transition and location travel with reason `barricaded` until its HP reaches 0
  (`sunder` damages any object). A deliberate simplification: one barricade blocks every way out of
  the scene, not one named door.
- **`arson`** is a `Law_Resolution.CRIMES` entry (no victim; `witness_severity.arson`; a
  `[[polity.law]]` per polity). It fires from the passed roll's `report_crime`, never from narration.

## Confirming a match

A similarity score proposes the effect, and one scored close enough that "light a candle for the
dead" matched `set fire`. An effect that changes the world or breaks a law therefore authors
`confirm_with_model = "<what does and does not count>"`; `IntentClassifier._classify_skill_pass`
asks `confirm_mechanic_fit` (one yes/no tool call, reasoning off, temperature 0) and drops the
clause on `no`. No answer leaves the match standing — a missing answer is not evidence against it.
A dropped clause falls to the adjudicator and the rules' own fallbacks (usually "not understood",
which is honest). Measured against the live model: 19/19 lines (7 genuine attempts and 12 near-misses
across the three confirmed effects) were judged correctly.

## Confirming a skill match

A skill's score cannot separate a real attack from a misfire (`attack the wolf with my sword` scored
0.63, `look for something I can use as a weapon` 0.54, `swing my sword at the orc` 0.54): good and
bad overlap, so no threshold works. `blades`, `axes`, `polearms`, `brawling`, `throwing` and
`missiles` therefore author `confirm_with_model` too, but a skill is only asked about when the line
names **no target** (`NLPCore.confirm_fit`'s `has_target`) — "attack the wolf" is plainly an attack,
and a bare "attack" or a search for a weapon is where the misfire lives. Measured live: 13/13
(7 genuine attacks, 6 searches/handling lines). A dropped clause falls through to the adjudicator.

## Roll-and-narrate lines

Not every free-form action changes the world. A stunt (`slide down the banister`, `vault the
counter`, `hang from the ledge`, `swing from the chandelier`), an animal (`feed the crow`, `whistle
for my horse`, `shoo the rats`), `eavesdrop at the keyhole`, `carve a symbol`, `headbutt`, `play dead`
and `hold my breath` have no state to change: they are a skill roll the narrator then describes. They
needed only keywords on the skill that already covers them (`acrobatics`, `husbandry`,
`observation`, `artistry`, `brawling`, `stealth`, `fortitude`), not an effect entry, and
`TestWorldEffectRouting` pins them with the real matcher. An unopposed one is rated by the usual
difficulty tiers (see [action-resolution.md](action-resolution.md)'s "Unopposed checks").

`treat wounds` also authors `confirm_with_model`: `carry the wounded man` and a hug both scored
close enough to roll medicine. The model judged 8/8 lines correctly (4 genuine tendings, 4 carrying/
hugging/lifting).

## Measured

Pathfinder, one NPC present, live local model, 76 lines of free-form world actions:
`action_not_understood` 31 → 17 (before the roll-and-narrate keywords above; they cover most of
the 17). Routed to a world effect with no keyword rule: `set fire to the
curtains`, `torch the barn`, `barricade the door`, `wedge it shut`, `pile furniture against the
door`, `tie the guard up`, `gag him`, `douse the lamp`.

## Known limits

- **Effects with no matching ability** (`collapse the bridge`, `flood the room`, `topple the shelf`,
  `cut the rope`) are rolled as whatever skill they matched, or not understood — an honest
  "plausible attempt, no mechanical change", never an invented result. Each is a new authored entry,
  not new code.
- **Not authored, deliberately:** `flood the room`, `collapse the bridge` and `light the fuse` need
  terrain, structure or timed-explosive state the engine has no primitive for. They are rolled as
  whatever skill they match or not understood, never given an invented result.
- **A skill gate costs a call** on every untargeted attack-skill line; a line that names a target
  skips it.
- **`bind` needs no rope** and `set fire` needs no flammable target; both are narrated, not modeled.
- **Fires do not spread** to other bands or rooms; a blaze is one band wide.
