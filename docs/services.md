# LLDM — Services

Part of the [LLDM](../CLAUDE.md) docs — priced services an entity offers: a room for the night, a
courtesan's company, a sellsword's sword.

## What a service is

A `[[entity.service]]` table on an entity (`Rules/<setting>/reference/entity_schema.toml` documents
every field). It is authored data, never something the narrator makes up: the **price** the
provider is told to quote is the price that is charged, so the two cannot disagree.

```toml
[[entity.service]]
name = "a night"
aliases = [ "the night", "her company" ]
price = 1.2                  # in the setting's value unit (gold pieces in Pathfinder)
min_disposition = 20         # refused below this disposition toward the player
overnight = true             # a whole night's blocks pass...
rest = true                  # ...as a rest, healing the party
content = "sexual"           # narration follows the player's content choice
```

Other fields: `blocks` (pass N blocks), `joins_party` + `follow_offset` (the provider joins and
follows the party), `attitude_event` (an `[[attitude_event]]` applied to them), `on_buy` (an
ordinary program run with `actor` = the player, `target` = the provider). No haggling: the price is
fixed.

## How it is bought

- **No new classification.** "buy a night", "hire the mercenary" and "pay for the room" already
  reach the `trade` intent: `TRADE_KEYWORDS` (now including `hire ` and `pay for `) and the
  adjudicator's `buy` verdict both send a phrase naming no real item to `improvisation_requested`.
- **The seam.** `DM_Improvisation.py`'s `_on_improvisation_requested` asks
  `ServiceMixin._try_service_purchase` before it asks the model to conjure an item. A phrase that
  names a service someone present offers is bought; anything else carries on exactly as before.
- **Matching** (`Service_Resolution.match_service`): the service's name or an alias (articles
  ignored) found in the phrase or the whole input, longest wins; a phrase that is only *part* of an
  alias scores just under a whole match, so "a night" buys Ysolde's night, not Garridan's "room
  night". Failing that, naming a provider who offers exactly one service buys it ("hire jorrick").
- **Providers** are people present, alive, in view, and not in the party — except someone already
  *hired*, who is kept as a provider so asking again is refused (`already_joined`) rather than
  falling through to conjuring an item called "his sword".

## What a purchase does

Refusals are stated reasons and nothing is charged: `cant_afford`, `too_cold` (their disposition is
below `min_disposition`), `hostile`, `enemies_near` (a time-passing service with a hostile in the
scene), `already_joined`. Otherwise, in order: the price moves from the player to the provider as a
real currency transfer; `attitude_event`; time passes (`overnight`/`blocks`, as a rest when `rest`);
`joins_party` makes them a party member (`is_party`, `hired`, snapped into formation); `on_buy` runs.
One `item_interaction_resolved` with `intent = "service"` carries the result.

**A hire is persistent.** `hired` entities are carried into every new scene
(`_carry_mounts_into_scene`, `DM_Rules.py`) and round-trip through save/load (`hired` and
`follow_offset` in the instance state — written only for hires).

## The provider quotes the real price

`describe_character` appends "Sells services at fixed prices — quote exactly these, never another
figure: a night: 1 gold piece, 2 silver pieces." (`service_offers_for`), the same route crime
knowledge takes into an NPC's prompt. Found by playtest: left to itself the model invented a
different price every turn, and the player spent twenty turns trying to settle one.

## Content choice

A service tagged `content = "sexual"` is narrated according to the **content level**, the player's
choice and never an author's:

| Level | What the narrator is told |
|---|---|
| `fade` (default) | Narrate the agreement and payment, then cut away with a time skip or a closed door. |
| `explicit` | Narrate it plainly and in full, without euphemism or cutting away. |

Set with `python LLDM.py --content explicit`, or the window's **Content** menu (changeable
mid-game; `content_level_selected` → `LLMCore.content_level`). Every sexual service carries, at
both levels, an instruction that every character involved is an adult who has agreed, and that if
anyone might be a minor the narrator cuts away at once — a line in the prompt (`ADULTS_ONLY`,
`intents/service.py`), since nothing in the data can know it. The tag changes nothing mechanical.

## Not built

Haggling (a skill contest moving the price within a range); recurring wages for a hire; dismissing
a hire; a service with a stock or a daily limit; a private room as a real place (an overnight
service passes time, it does not move the player).
