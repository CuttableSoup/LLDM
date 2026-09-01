# Pathfinder 1e → D6 engine conversion reference

Reference only — never loaded as game data (`load_rules` only scans `Rules/Fantasy/*.toml`
one level deep; this file lives in `reference/` for the same reason
`entity_schema.toml`/`template_schema.toml` do). A working document for expanding
`Rules/Fantasy/`'s conditions/items/creatures/spells: for each Pathfinder 1st Edition mechanic
category, what the actual D6 Fantasy/D6 Magic/D6 Fantasy Creatures rulebooks (`Settings/*.pdf`)
already define, what the engine (`DM_*.py`) currently implements of that, and a concrete recipe
for authoring the gap using real TOML fields — the same fields `entity_schema.toml` documents.

**Scope.** This is a mechanics-gap survey, not an exhaustive catalog port. It doesn't enumerate
every Pathfinder spell/monster/item — it enumerates *mechanic categories* (condition types,
damage-reduction shapes, item-bonus shapes, spell-duration shapes, creature special-ability
shapes) so a new piece of content can be authored by recognizing which category it falls into
and following that category's recipe, rather than needing a bespoke conversion each time.

**Sources.** Pathfinder 1e via aonprd.com (Archives of Nethys): Conditions, Universal Monster
Rules, Damage Reduction/Energy Resistance/Vulnerability, Magic Items, core Magic chapter.
D6 canon via `Settings/d6 Fantasy.pdf` (core rulebook), `Settings/d6 Fantasy Creatures.pdf`
(bestiary), `Settings/d6 Magic.pdf` (Vade Mecum of Magic supplement). Engine behavior verified
directly against `DM_Status.py`/`DM_Combat.py`/`rules.toml` as of 2026-08-22, not just inferred
from CLAUDE.md's prose.

---

## 0. Engine primitives (what's actually live today)

Every recipe below is built from these. Two items here are **verified gaps in the engine
itself** — not a missing Pathfinder feature, but authored data the code never reads — found
while researching this document, and worth fixing before leaning on them for new content:

- **`active_conditions`** (`entity["active_conditions"]`) is a presence-only dict:
  `{condition_name: {duration, dismiss}}`. Nothing reads `duration` as a timer — "fleeting"/
  "scene"/"permanent" are conventions in comments only, never swept by round count or real
  time. The only ways a condition is removed are an explicit `dismiss_condition` call (from
  `apply_test_outcome`, an ability, or code) or `evaluate_statuses`' own stale-condition sweep
  (only for `on_damage`-triggered conditions whose HP-tier requirements no longer hold).
- **✅ Fixed (2026-08-22).** `rules.toml`'s `[[condition]]` table (`modifier = {dice, pips,
  bonus}`) used to be dead data — no code read a per-condition dice modifier, so the wound
  track's own tiers narrated but never actually penalized a roll. `get_condition_modifier`
  (`DM_Status.py`) now sums the `modifier` of every active condition with a matching
  `[[condition]]` entry, and `resolve_action`/`resolve_opposed_action` (`DM_Combat.py`) fold it
  into every roll for whichever entity is acting — on the defending side of an opposed roll
  too, independent of `dice_penalty` (see CLAUDE.md's "Status and conditions"). §1's Pattern C
  recipes below are live, authorable content now, not "narration-only until wired."
- **✅ Fixed (2026-08-23).** `[[entity.behavior]]`/`[[status]]` requirements can now test
  `active_conditions` directly, via two new derived fields on `get_comparable_value`
  (`DM_Status.py`): `"has_condition:<name>"` (a boolean presence check against the checking
  entity's own `active_conditions`) and `"opponent_has_condition:<name>"` (the same check
  against `opponent_name`'s — resolves to `None`/never-matches with no `opponent_name` given,
  same as `distance_to_target`). Pair either with `operator = "=="`, `value = true`/`false` —
  no new operators needed, the existing `COMPARATORS` table already covers it. A creature's
  behavior can now react to *what conditions* something has, not just *how hurt* it is (skip a
  paralyzed creature's own turn, favor attacking a stunned/fleeing target, ...). See CLAUDE.md's
  "Status and conditions."
- **`requires_condition`/`blocks_if_condition`** exist, but only on `[entity.test]`
  (`is_test_available`) — gating whether a *test* can be attempted, not whether an *action* or
  *behavior entry* can. A locked chest or an armed trap can react to its own condition state;
  nothing else currently can.
- **Damage/resistance tags** (`damage_tags`/`armor_tags`/`resistance_tags`/`immunity_tags`/
  `vulnerability_tags`) match by **any overlap**, not exact set equality
  (`get_damage_reduction`/`is_immune_to`/`get_vulnerability_bonus`, `DM_Combat.py`).
  `resistance_tags`/`armor_tags` each also have an optional **bypass** counterpart
  (`resistance_bypass_tags`/`armor_bypass_tags`) — a matching bypass tag on the incoming hit
  skips that side's reduction outright, the Pathfinder "DR 10/magic" shape. See §2.
  `immunity_tags`/`vulnerability_tags` have no bypass counterpart of their own — immunity is
  already absolute, and nothing in Pathfinder's own vocabulary needs a "vulnerability, unless"
  case the way DR does.
- **Usable items**: `usable`, `charges` (absent = single-use), `replace_with`,
  `[entity.skills].healing`/`.poison` — one healing effect and one poison effect per item, no
  other effect shapes (buffs, debuffs, teleport, summon).
- **Behavior**: `[[entity.behavior]]`, a declaration-order `requirements`/`action` list, only
  ever consulted once per round per entity, for a single chosen action — see gaps above for
  what it can't currently see.
- **Skills** as the sole roll axis — no separate defense stat (no AC equivalent); combat is
  always attacker's skill vs. either a flat `difficulty` (`resolve_action`) or the defender's
  own best opposing skill (`resolve_opposed_action`, via `skills.toml`'s `opposes` lists).

---

## 1. Conditions / status effects

D6 Fantasy's own canonical wound track (Bruised → Stunned → Wounded → Severely Wounded →
Incapacitated → Mortally Wounded → Dead, gated on % of max Body Points) is **already** what
`rules.toml`'s `[[status]]` table implements almost verbatim (see CLAUDE.md's "Status and
conditions"). Pathfinder's condition list is a different axis entirely — not a wound-severity
ladder, but a catalog of ~30 independent, stackable effects (fear, paralysis, blindness,
grappling, ...), each with its own trigger and its own narrow mechanical effect. Converting one
means picking the right *pattern*, below, not inventing a new subsystem per condition.

Both §0 engine gaps are now fixed — a `[[condition]]` entry's `modifier` costs real dice the
moment it's authored, and `[[entity.behavior]]` can react to `active_conditions` directly via
`"has_condition:<name>"`/`"opponent_has_condition:<name>"`. Every pattern below is authorable,
mechanically live content today, including a creature's own AI reacting to a condition, not
just its roll being penalized by one.

### Pattern A — full incapacitation (can't act at all)
Pathfinder: Stunned, Dazed, Paralyzed, Unconscious, Helpless, Nauseated, Cowering, Sleep.

The engine's own `mortal` status already does this shape once (`apply = {condition =
"unconscious", duration = "scene"}`). Generalize it: author the condition in `[[condition]]`
with a crushing penalty (`dice = -99` is fine — `resolve_action`'s floor at 0 dice handles it
either way) rather than inventing a "can't act" flag — this actually zeroes the roll. Then, so a
creature stands down entirely while affected instead of "acting" at 0 dice and just failing,
gate every one of its own attack entries off while the condition holds — `choose_behavior`'s
existing "no matching entry = no action" fallback (the same one an entity with no behavior list
at all already gets) does the rest:

```toml
[[condition]]
name = "paralyzed"
modifier = { dice = -99, pips = 0, bonus = 0 }

[[entity.behavior]]
# A paralyzed creature fails this requirement and, with no other entry to fall through to,
# simply doesn't act this round -- resolve_behavior_action returns None, same as an entity
# with no behavior list at all.
requirements = [
    { field = "has_condition:paralyzed", operator = "==", value = false },
    { field = "hp_per_remain", operator = ">=", value = 0.01 },
]
action = "bite"
```

`creatures.toml`'s `wraith` is the shipped, tested version of this same gate (its own condition
name is `"warded"`, not `"paralyzed"`) — see `TestEntityBehavior.test_wraith_stands_down_
entirely_while_warded` (`test_unit.py`). It's also the shipped example for the `opponent_has_
condition` half of this fix (favors `"life drain"` over its plain claw once the target is
already `"wounded"`) and for `resistance_bypass_tags` (§2) — one creature demonstrating all
three fixes together.

### Pattern B — reduced but not zero action economy
Pathfinder: Staggered (move OR standard, not both), Disabled (same), Entangled (half speed, no
run, -2 attack/-4 Dex). No engine equivalent at all — action economy here is "one clause = one
turn slot" (see CLAUDE.md's "Multiple actions"), with no notion of a creature being restricted
to *fewer* slots than normal. Cheapest honest conversion: treat as Pattern C (a flat dice
penalty) rather than trying to model a real slot restriction — Entangled's "-2 attack, -4 Dex"
is directly a `[[condition]]` modifier; Staggered/Disabled's "one action only" has no clean
analog and is better left as a flavor condition (narration says the creature is staggered) than
forced into a mechanic that doesn't fit.

### Pattern C — flat dice-pool/roll penalty while active
Pathfinder: Shaken/Frightened/Panicked (fear ladder, -2/-2/-2 plus fleeing), Sickened/Nauseated's
milder cousin, Fatigued/Exhausted, Dazzled (-1 to sight-based rolls only), Deafened (-4 specific
rolls), Sickened (-2 everywhere). This is the **best-fitting** pattern for the engine's actual
data shape — a flat `{dice, pips, bonus}` modifier is exactly `[[condition]]`'s own shape.

```toml
[[condition]]
name = "shaken"
modifier = { dice = -1, pips = 0, bonus = 0 }

[[condition]]
name = "sickened"
modifier = { dice = -2, pips = 0, bonus = 0 }
```

A condition whose penalty only applies to *specific* rolls (Dazzled: sight-only; Deafened:
initiative/sound-Perception only) can't be expressed today — `[[condition]]`'s modifier is
global once wired in. Authoring it as a global (but smaller) penalty is the honest
simplification; a skill-scoped modifier would need a new field (ex: `applies_to = ["observation"]`)
on the condition entry.

### Pattern D — no-defense / helpless-to-hit
Pathfinder: Flat-Footed, Prone, Grappled, Pinned all key off AC, which the engine has no
equivalent of (opposed-roll defense only). The closest honest translation is **the defender's
opposing-skill roll gets zero dice** for the duration — a Pattern-A-strength `[[condition]]`
entry (`dice = -99`) applied to the defender. `resolve_opposed_action` now applies the
defender's own `active_conditions` modifiers to their own difficulty roll independently of
`dice_penalty` (which still only ever touches the attacker's side), so this is directly
authorable today — no further engine work needed.

### Pattern E — wound-track adjacent (bleed, dying, stabilize)
Pathfinder's Bleed/Dying/Stable/Disabled sit *inside* the same "how close to death" axis the
engine's wound track already owns. Rather than a new condition, these compose with the existing
tiers: `mortal`'s `apply = {condition = "unconscious", duration = "scene"}` is already
functionally "Pathfinder Dying, no bleed-out timer."

**✅ Fixed (2026-08-23): a recurring per-round damage-over-time effect** (Pathfinder's Bleed) is
now directly authorable — `run_round_upkeep`/`apply_round_upkeep` (`DM_Status.py`) is the
generic per-round hook, called once per combat round from `_resolve_combat_round`
(`DM_Core.py`), that applies every active condition's own `upkeep_heal`/`upkeep_damage` (see
CLAUDE.md's "Status and conditions"). A Bleed condition is the `upkeep_damage` mirror of
§5's `troll` regeneration example below:

```toml
[[condition]]
name = "bleeding"
upkeep_damage = { dice = 1, pips = 0, bonus = 0 }
# No upkeep_blocked_by_tags -- unlike regeneration, ordinary Bleed isn't suppressed by any
# particular damage type; it's dismissed by treatment instead (below).
```

Stopping it ("any effect that heals HP" in Pathfinder; a `[entity.test]` — ex: a DC-style
Medicine check — in this engine) is the same `dismiss_condition` primitive any other condition
already uses, ex: an `[entity.test]` whose `pass` outcome is `dismiss_condition = "bleeding"`.

### Pattern F — absolute/special
Pathfinder: Petrified (helpless + immune to most effects), Incorporeal (immune to nonmagical
attacks), Invisible (can't be targeted normally), Energy Drained (permanent stat loss). These
map onto **tags**, not conditions, since they're permanent-to-the-encounter defensive traits,
not something that fades: `immunity_tags` already gives an absolute block (Petrified/Incorporeal
as `immunity_tags = ["physical"]` layered with a narrower `resistance_tags` for magic). Invisible
has no engine equivalent — nothing gates whether an entity can be targeted at all (every scene
entity is always addressable via `map_to_target`); would need a new `is_targetable` gate, out of
scope for a data-only conversion.

### Full condition-to-pattern table

| Pathfinder condition | Pattern | Notes |
|---|---|---|
| Blinded | C (broad) | -4 several skills + can't act on sight-based checks; author as `-2` global as a rough fit |
| Bleed | E | `upkeep_damage` on a `[[condition]]` entry, cleared via `dismiss_condition` (ex: a passed `[entity.test]`) |
| Confused | F/behavior | needs actual randomized-target logic; no data-only fit |
| Cowering | A | |
| Dazed | A | |
| Dazzled | C | sight-only in PF; global penalty is the honest simplification |
| Deafened | C | initiative/sound-only in PF; global penalty is the honest simplification |
| Dying/Stable/Disabled | E | already close to `mortal`'s own shape |
| Energy Drained | F | permanent stat loss has no engine hook (skills are static per-instance dicts, no debuff-over-base) |
| Entangled | B→C | flatten to a dice penalty |
| Exhausted/Fatigued | C | |
| Fascinated | C/behavior | "stops acting unless threatened" is a behavior-requirement shape, not a roll penalty |
| Flat-Footed | D | |
| Frightened/Panicked/Shaken | C (+flee via behavior for creatures) | fear ladder = three severities of the same Pattern C condition |
| Grappled/Pinned | D | |
| Helpless/Paralyzed/Unconscious/Stunned/Nauseated | A | |
| Incorporeal | F (tags) | |
| Invisible | out of scope | needs a targeting gate, not condition data |
| Petrified | F (tags) + A | |
| Prone | D | |
| Sickened | C | |
| Staggered | B→C | flatten to a dice penalty |

---

## 2. Damage types & resistance/vulnerability

D6 Fantasy has **no typed-damage system** in canon — resistance is one undifferentiated
Physique-plus-armor roll against the incoming damage number; weapon "type" (edged/blunt) is
narrative flavor only, with one mechanical exception (edged weapons do half damage used as
blunt) and a handful of per-creature Achilles'-Heel disadvantages (silver, iron). The engine's
`damage_tags`/`armor_tags`/`resistance_tags`/`immunity_tags`/`vulnerability_tags` system is
already a deliberate **extension past D6 canon**, built specifically to support
Pathfinder-style typed damage — this is the one category where the engine is already ahead of
its own source material, not behind Pathfinder.

Mapping is close to 1:1 for the common cases:

| Pathfinder concept | Engine equivalent | Fit |
|---|---|---|
| Energy type (acid/cold/electricity/fire/sonic) | `damage_tags` entry | exact |
| Physical type (bludgeoning/piercing/slashing) | `damage_tags` entry | exact |
| Energy Resistance N (per-instance-of-damage cap) | `resistance_value` rolled reduction | close — engine rolls a die pool instead of a fixed cap, more variance than PF's flat number, but same "partial reduction" role |
| Immunity | `immunity_tags` | exact |
| Vulnerability (+50% damage) | `vulnerability_value`/`vulnerability_tags` | engine adds a *rolled* bonus rather than a fixed 50% multiplier — same role, different math shape, don't try to force an exact percentage |
| Hardness (object DR) | none | objects (containers, doors) have no damage-reduction field distinct from a creature's; `resistance_value` would work identically if authored on an object-`supertype` entity, just not a distinct concept today |

**✅ Fixed (2026-08-23): DR-with-bypass** (Pathfinder's "DR 10/magic", "DR 5/silver",
"DR 15/cold iron and good"). `get_damage_reduction` (`DM_Combat.py`) now checks an optional
`resistance_bypass_tags` (on the defending entity) / `armor_bypass_tags` (on an equipped item)
before applying that side's reduction — if any of the incoming hit's `damage_tags` matches a
bypass tag, that side's reduction is skipped for this hit entirely, even though
`resistance_tags`/`armor_tags` would otherwise have matched. Absent/empty on every entity that
doesn't author it, so this is purely additive — nothing already-shipped changed behavior.

```toml
# A creature that shrugs off mundane weapons but not enchanted ones -- Pathfinder's "DR 10/magic".
resistance_value = { dice = 3, pips = 1 }
resistance_tags = [ "slashing", "piercing", "bludgeoning" ]
resistance_bypass_tags = [ "magic" ]

# ...and the enchanted weapon that cuts through it -- an ordinary sword keeps damage_tags =
# ["slashing"] and still gets reduced; this one adds "magic" alongside its normal type tag.
[[entity]]
name = "enchanted longsword"
supertype = "object"
subtype = "weapon"
damage_value = { dice = 3, pips = 0, bonus = 0 }
damage_tags = [ "slashing", "magic" ]
```

---

## 3. Magic items

D6 Magic/D6 Fantasy treat every magic item as **a spell permanently bound into an object**,
using the same point-buy Spell Total math as any other spell, converted into a flat die-code
bonus (never an arbitrary flat number). Two charge models exist in canon: basic charges (max 5,
one release per round, expire after 24h) and improved charges (costlier, no expiry). Cursed
items are just an undesirable spell bound the same way a beneficial one is — no separate
mechanical subsystem. None of this — item-as-bound-spell, wards, alchemical-component crafting
— has an engine equivalent; the engine's item model (`usable`/`charges`/`replace_with`, a flat
`damage_value`/`armor_value` bonus) is a much narrower abstraction on purpose, and that's fine
for *using* an item — the gap only matters if the project ever wants in-fiction item *crafting*.

Pathfinder's slot-based wondrous-item system maps directly onto `equip_slot` +
`rules.toml`'s `[[equip_slot]]` table (already the exact mechanism — see CLAUDE.md's
"Inventory and currency" / entity_schema.toml's `equip_slot` field), so most Pathfinder magic
items are pure data — no new mechanism needed, just an item entity with the right bonus shape:

| Pathfinder item shape | Conversion recipe |
|---|---|
| **+N weapon** (flat attack/damage bonus) | Fold N into the weapon's own `damage_value.bonus` (a flat number) or its skill dice, since the engine has no separate "attack roll" step distinct from the skill check itself — a +1 longsword is just a longsword with a richer `damage_value`. |
| **Flaming/frost/shock** (+1d6 elemental on command) | Add the element to `damage_tags` and bump `damage_value.dice`/`.pips` — ex: a flaming longsword keeps `damage_tags = ["slashing"]` and adds `"fire"`, with dice bumped to cover the extra d6. |
| **Keen** (doubled threat range) | No engine equivalent — there's no critical-hit/threat-range mechanic at all (see §5's Wild Die note). Not authorable without a new crit subsystem. |
| **Holy/Unholy/Bane** (bonus vs. a creature type/alignment) | Not directly expressible — `damage_tags` matches by tag overlap against the *defender's* resistance/immunity, not by the defender's `supertype`/`subtype`/`qualities`. A "vs. undead" weapon would need the defender's `supertype == "undead"` to somehow gate a damage bonus, which nothing in `calculate_damage` currently reads. Closest approximation: give every undead entity `vulnerability_tags` including a shared tag (ex: `"holy"`) and author holy weapons with that damage tag — reuses the existing vulnerability mechanism instead of a new one. |
| **Vorpal** | No engine equivalent (no crit system to hook a "decapitate on crit" effect into). |
| **Wounding** (bleed on hit) | No ability field applies a condition *on a successful hit* directly yet (still needs `[entity.test]` as the trigger — see §5's Poison row), but the bleed itself is now authorable once applied — a `"bleeding"` condition with `upkeep_damage` (§1 Pattern E), dismissed the ordinary way. |
| **Ghost touch / Seeking** | No engine equivalent (no concealment/incorporeal-targeting mechanic to bypass). |
| **Fortification** (armor: % chance to negate a crit) | No engine equivalent (no crit system). |
| **Energy resistance armor** (fixed absorption) | Directly `resistance_value`/`resistance_tags` authored on the armor item itself, exactly like `items.toml`'s existing armor entries — already supported, no gap. |
| **Spell resistance item** (fixed SR while worn) | No engine equivalent — nothing rolls a caster-level check against a target before a spell/ability applies; every `resolve_opposed_action` cast already *is* the resistance check (the defender's own opposing skill), so a Pathfinder-style separate SR layer would be redundant with, not additive to, the existing opposed roll. Skip this one — the engine's spell resistance is already "beat their skill," don't try to bolt on a second layer. |
| **Ring/belt/wondrous stat bonus** | A flat bonus to a named skill — either `damage_value.bonus` for an offensive item, or (no current field for a passive skill-dice buff from a *worn, non-weapon* item) would need equipped items to be able to modify skill dice the way `resistance_value`/`armor_value` already let equipped items modify defense. Currently only weapons/armor equipped items are read by combat math (`get_equipped_weapon`/`get_damage_reduction`); a ring granting +1D to `observation` has no mechanism to apply that bonus to `resolve_action` today. Worth flagging as a scoped extension: an `equipped_skill_bonus = { skill = "observation", dice = 1 }` field, read by `resolve_action` alongside the entity's own base skill dice. |
| **Potions/scrolls** | Already fully supported — `usable`/`charges`/`[entity.skills].healing`/`.poison` is a potion; a scroll with a one-shot spell effect is really just a usable item whose "use" *is* casting — model it as `usable = true`, `charges = 1`, with the actual effect authored as a real ability the item's use resolves (not currently generic — `_resolve_use_intent` only knows healing/poison, so a scroll that casts an arbitrary spell effect would need `_resolve_use_intent` extended past its two hardcoded effect types). |
| **Wands/staves** (multi-charge, spell-slot item) | `charges` already covers "N uses, then gone/replaced" — a wand is mechanically identical to a health potion with more charges, same caveat as scrolls above (effect variety). |

---

## 4. Spellcasting

D6 Magic's core mechanic — a point-buy Spell Total design system that resolves to a fixed
`Difficulty`, cast by rolling the caster's own magic skill against it, with excess roll banked
into extra effect/range/duration — is **not** what the engine implements. The engine treats
every spell as a hand-authored `[[entity]]` with `supertype = "spell"`, a fixed `skill`/
`difficulty`/`range`/`damage_value`/`damage_tags`, resolved through the same
`resolve_action`/`resolve_opposed_action` path any ability uses (see `spells.toml`'s `fireball`/
`splash flow`). This is a deliberate simplification — no per-spell point budget, no banked
result points, no failure-consequence table — and Pathfinder's spell system doesn't map onto it
mechanic-for-mechanic either. What *does* transfer directly:

| Pathfinder concept | Engine equivalent | Recipe |
|---|---|---|
| Spell school (8 schools + subschools) | `subtype` on the spell entity | Already used this way — `spells.toml`'s `fireball` is `subtype = "evocation"`. Author Pathfinder's other 7 schools the same way; `skills.toml`'s `arcane`/`miracles` skills already list all 8 school names as `specializations`, so the vocabulary already exists, just unused by any real spell yet. |
| Saving throw type (Fort/Ref/Will) | The defender's opposing skill, via `skills.toml`'s `opposes` list | Already the mechanism — `arcane` opposes `["willpower", "arcane"]`; a spell resolved via `resolve_opposed_action` already picks the defender's best matching skill. A Fortitude-style spell would resolve against a defender skill like `fortitude`; there's no need to add "save types" as a separate concept — just pick the right opposing skill when authoring the spell's own `skill`. |
| Spell duration (instantaneous / rounds / permanent / concentration) | `duration` on `apply_condition`, free-text | Instantaneous spells (most damage spells) need nothing — `calculate_damage` already resolves in one shot. A buff/debuff spell (Pathfinder's "1 round/level") needs the §0 duration-timer fix to actually expire; author it as `duration = "scene"` today as the closest available approximation (expires only when the scene/encounter logic clears it, not on a round count) rather than inventing a fake round-counter in data. Concentration (must keep concentrating each turn or it ends) has no engine hook at all — nothing tracks "is this entity still concentrating"; skip modeling concentration explicitly, treat concentration spells as ordinary fixed-duration ones. |
| Spell Resistance | — | Skip (see §3's spell-resistance-item row — already redundant with the existing opposed roll). |
| Metamagic (Empower/Maximize/Quicken/etc.) | — | No engine equivalent — spells are static TOML entities, not built at cast-time from a base spell + modifiers. A "quickened fireball" would just be authored as a second, separate spell entity with adjusted numbers; there's no mechanism for the player to apply a metamagic-style modifier at the point of casting. |
| Area of effect | `targets = {number, aoe, side}` | `DM_Combat.py`'s `resolve_targets` widens the roll from `target_name` alone to every living entity within `aoe` bands of it (nearest-first), filtered by `side` (`"enemies"`/`"allies"` via `is_hostile`, or `"all"` for an indiscriminate blast) and capped at `number` (`0` = unlimited). `fireball`'s own `{aoe = 5, side = "all", number = 0}` is the shipped indiscriminate-blast example; `techniques.toml`'s `cleave` (`{number = 3, aoe = 0}`) is the discriminating multi-target case (no radius, just a cap on how many enemies sharing `target_name`'s own band get hit). A Pathfinder-style ally-only area effect (ex: channeling) authors `{aoe = <radius>, side = "allies"}`. |

---

## 5. Creature special abilities

D6 Fantasy Creatures expresses recurring creature traits as ranked "Special Abilities"
(`Flight (R#)`, `Fear (R#)`, `Natural Hand-to-Hand Weapon: <part>`, `Natural Armor`,
`Accelerated Healing`, ...) — narrower in count than Pathfinder's Universal Monster Rules
glossary, but the same *idea*: a small vocabulary of reusable creature traits, not
one-off-per-monster mechanics. The engine's own building blocks (`resistance_value`/
`immunity_tags`/`vulnerability_value` for defense, `[[entity.abilities]]` for attacks,
`[[entity.behavior]]` for AI, `usable`-style effects) already cover a decent slice of both
books' vocabularies. Recipes below are grouped by which existing primitive fits, not by which
book the ability came from — D6's and Pathfinder's versions of "this creature regenerates" want
the same conversion regardless of source.

| Ability (either system) | Primitive | Recipe |
|---|---|---|
| Natural armor / hardiness | `resistance_value`/`resistance_tags`, innate (not on an equipped item) | Already the exact shape — `creatures.toml`'s `fire elemental` does this today. |
| Elemental immunity/vulnerability (fire elemental's own fire immunity + water vulnerability) | `immunity_tags`/`vulnerability_value`+`vulnerability_tags` | Already supported, already shipped as the reference example. |
| Natural weapon (claws/bite/breath) | `[[entity.abilities]]`, inline `supertype = "innate"` | Already the exact shape — `gladstone`'s `punch`, the fire elemental's `flame touch`. |
| Multiple natural attacks in one action (PF's Rend, Rake) | — | No engine equivalent — an entity's behavior resolves to exactly one action per round; "hit with two claws, then bonus rend damage if both connect" would need multi-action-within-one-behavior-entry, which doesn't exist. Approximate with a single ability whose `damage_value` folds in the expected average of both hits, rather than trying to model two separate rolls. |
| Regeneration / Fast Healing | ✅ `upkeep_heal` on a `[[condition]]` entry, seeded permanently via `[entity.conditions.<name>]` | Fixed (2026-08-23) — `creatures.toml`'s `troll` is the shipped example: `[entity.conditions.regenerating]` (permanent) + `rules.toml`'s `"regenerating"` `[[condition]]` (`upkeep_heal = {dice = 2, pips = 0, bonus = 0}`, `upkeep_blocked_by_tags = ["fire"]` — the Pathfinder "Regeneration 10 (fire)" shape). `run_round_upkeep` (`DM_Status.py`) applies it once per combat round to every living scene entity; see CLAUDE.md's "Status and conditions." |
| Breath weapon with recharge limit | `[[entity.abilities]]` (the attack itself, already supported) + recharge (not supported) | The attack shape itself (an AoE-flavored high-damage ability) is authorable now (modulo §4's AoE gap); the "usable once every 1d4 rounds" recharge limiter has no mechanism — behavior requirements have no round-counter/cooldown field, only HP-tier, distance, and (as of the upkeep fix) condition presence. A creature could instead be authored with the breath weapon as its *only* high-priority behavior entry and a weaker fallback attack as a secondary entry, which loosely approximates "big attack sometimes, normal attack otherwise" without a real cooldown. |
| Poison (on-hit secondary effect, often delayed) | Partially fixed — `upkeep_damage` now covers the *ticking* half | A bite ability whose `[entity.test]`-style pass/fail (or a dedicated on-hit condition-apply, not yet a first-class ability field) applies a `"poisoned"` condition with its own `upkeep_damage` now ticks for real, dismissible via `dismiss_condition` (ex: a passed Fortitude-style test) — the same shape §1 Pattern E's `"bleeding"` example uses. Still missing: true onset delay (Pathfinder's "poison doesn't start for 1 round") and an ability field that applies a condition *directly* on a successful hit without going through `[entity.test]` — today the cleanest trigger is still folding an initial hit into `damage_value` and only the ongoing tick uses the new mechanism. |
| Fear aura / Frightful Presence | Pattern C condition (§1), applied on a passed opposed roll instead of on damage | Needs a new `[[status]]` trigger beyond the only one that exists today (`"on_damage"`) — ex: `"on_proximity"` or `"on_action"` — since Frightful Presence fires on the creature's own action (a roar/charge), not on the *target* taking damage. A scoped, additive engine change (one new trigger string, one new call site) rather than a fundamental redesign. |
| Damage Reduction / Spell Resistance | See §2/§4 | |
| Trample / Powerful Charge / Pounce (all charge-dependent) | — | No engine equivalent — there's no "charge" action distinct from an ordinary advance-then-attack; multi-action bonus-on-condition-of-movement has no hook. Skip modeling the charge-dependency; author the bonus damage as baked into the ability's own `damage_value` if the creature is expected to always fight at range-closing distance anyway. |
| Swallow Whole / Constrict / Grapple-chain effects | Partially fixed — the ongoing-damage half is now `upkeep_damage` | Still needs Pattern D's opposed-defense-zeroing (to actually restrain the target) combined with an `upkeep_damage` condition (for the ongoing crush/digest damage) — no single ability field applies a condition *and* zeroes defense *and* restricts movement all from one hit yet, so this is still multiple hand-authored pieces (an attack that applies both a Pattern-D "grappled"-style condition and a damage-over-time one), not one clean recipe. |
| Ferocity (fights on below 0 HP) | Partially — behavior requirements can check `hp_per_remain` down to any threshold | Already representable: give the creature a normal attack `[[entity.behavior]]` entry with `hp_per_remain >= 0.01` and simply *no* "dead" special-case beyond what `[[status]]`'s own `dead` tier already does at `hp_per_remain == 0`. The engine's wound track already means a creature keeps fighting all the way down to 0 HP unless it has an explicit `"retreat"` entry — Ferocity is arguably the engine's *default*, not a gap. |