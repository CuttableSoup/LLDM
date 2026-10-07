# Law

Crimes are detected by the engine, only real witnesses know about them, and what they know is
stored as game state. Prompts are built from that state, never from the narrator's own
scrollback, so an NPC who didn't see a theft can't react to it. `dm/DM_Law.py` (`LawMixin`) is the
DMCore side; `resolution/Law_Resolution.py` holds the pure helpers. What guards do about it —
confronting, fines, jail, bribes, bluffs, resisting — is `dm/DM_Enforcement.py`
(`EnforcementMixin`; see "Enforcement" below).

## Where law applies

Laws belong to a polity (`polities.toml`). The polity in force is the current location's own
`polity` field if it has one, else the polity of the world-map region containing its grid point,
else the same for the location it `return_to`s, and so on up — a town's shop is under the town's
law (`DMCore.current_polity`, which `_current_polity_language` now shares). **No polity means no
law**: a crime there is never recorded, by anyone.

```toml
[[polity.law]]
crime = "theft"            # theft | assault | murder | banned_ability | banned_presence | resisting_arrest
fine = 5                   # in the setting's value unit (Pathfinder: gp)
acclaim = -1               # added to the offender's acclaim in this polity when filed
[[polity.law]]
crime = "banned_ability"
match = { subtypes = ["necromancy"] }   # names / tags / supertypes / subtypes
fine = 50
acclaim = -3
```

A location may add `[[location.law]]` entries. One with the same `crime` and `match` as a
polity law replaces it; any other is added (`Law_Resolution.merge_laws`). `rules.toml`'s `[law]`
table holds what every polity shares: the skills witnesses roll, the spell-identification
base, and the recognition bands. `Data_Validation.py` checks all of it.

Shipped data: Pathfinder's Varisia has theft, assault and murder laws with placeholder numbers
and no bans (bans are a per-setting lore choice). Fantasy has one test-only polity, "Test
Crown", referenced by no region and carrying every crime kind; tests point a location at it.

## Crimes and where they are detected

| Crime | Hook |
|---|---|
| theft | `take` from a living person or their goods (`DM_Inventory.py`), and a *failed* sleight of hand (its `on_fail` runs the `report_crime` op). Emptying a dungeon chest isn't theft — the victim must not be an object. A clean sleight of hand isn't noticed. |
| assault | The player striking someone who wasn't hostile (where the `"assaulted"` attitude event fires, `DM_Core.py`). Also marks the victim's `assaulted_by`. |
| murder | A kill (`calculate_damage`) of a victim whose `assaulted_by` includes the killer's side. A non-hostile turns hostile after one hit, so "killed while non-hostile" would almost never fire; the mark is what makes a fight the player started end in murder. Killing someone who struck first is no crime. On the record, a murder supersedes the assault on the same victim, so the total charge is the murder's, not both. |
| banned_ability | Any spell the player casts, pass or fail (`_finish_rolled_outcome`). See "Identifying a spell". |
| banned_presence | A party member — or something one has equipped — matching a ban, checked when the scene roster changes and when a disguise goes on or off. See "Recognition". |
| resisting_arrest | Refusing, fleeing or ignoring an enforcer's arrest demand, or attacking anyone while one is open. Filed at once. See "Enforcement". |

The `report_crime` program op (`Program_Interpreter.py`) only publishes `crime_committed`; the
interpreter knows nothing about polities, and `LawMixin` does the rest.

## Witnesses

A witness is anyone present who is alive, speaks a language (a wolf never calls the guard), is
not on the offender's side (a party never reports its own), and was not *authored* hostile to
the offender. Drift from play doesn't count: a shopkeeper who turned hostile because they were
just robbed still reports it, a bandit who was always hostile doesn't. The victim counts while
alive.

Each witness gets a `known_crimes` entry at once, and — except the victim, who already gets
`theft`/`assaulted` — the `witnessed_crime` attitude event toward the offender, scaled by
`[law].witness_severity` for that crime. Knowing alone doesn't change how an NPC acts (a
fact line can't outweigh an authored cheerful persona — found by playtest); fear and coldness
in the attitude do. Capped at the ordinary action drift cap, so even a murder witness ends up
frightened, not hostile. The drift is toward the person standing there, disguise or not.
The polity's record is filed:

- **immediately**, if a witness is an enforcer (tagged `law_enforcer`);
- otherwise **at the next `advance_blocks`**, if any witness is still alive. Silencing every
  witness before time passes keeps the record clean.

A record is `legal_records[polity][identity] = {bounty, acclaim, crimes}`. It only changes for a
crime committed inside that polity, it never decays, and (in the second pass) it can only be
settled inside that polity.

## Disguise

"Don a disguise" (a universal maneuver on the `disguise` skill) stores the roll as the
disguise's quality; a botched roll still disguises, just badly. "Drop the disguise" removes it.
Each disguise is a fresh identity (`"<name> (disguise N)"`, shown to NPCs as its alias).

A witness's `observation` roll must beat the quality to see through it — rolled once per witness
per disguise (`disguise_checks`), so standing around doesn't mean repeated chances to be caught.
A crime committed in disguise is filed against the disguise unless the witness saw through it, so
dropping the disguise shakes that bounty.

## Acclaim and recognition

Acclaim is signed: negative from crimes, positive from deeds (not built yet). An entity's own
authored `acclaim` is its base, the same everywhere; each polity record adds its own. Recognition
uses the **magnitude** `|base| + |polity|` (`Law_Resolution.effective_acclaim`), so a famous hero
who turns thief here is more recognizable, not cancelled out to 0. The sign is how someone is
known, for later use by attitudes.

Recognizing a banned presence: the witness first has to see through any disguise, then roll the
lore skill whose `lore_types` covers the subject (ex: `miracles` for undead), else
`[law].recognition_skill` (`streetwise`), against the `[[law.recognition]]` band for that
magnitude. Magnitude 0 can't be recognized at all; an `"automatic"` band needs no roll (anyone
knows a shambling corpse — an untrained roll would otherwise always fail). Checked once per
witness per subject per disguise (`presence_checks`).

## Identifying a spell

Every witness sees that a spell was cast. Knowing which takes the lore skill whose `lore_types`
covers spells (`arcane`, via `supertypes = ["spell"]`) against `[law].spell_identify_base` plus
the spell's own `level`. A witness who fails knows only that *a* spell was cast, which is a
crime only where a law bans every spell (`match = { supertypes = ["spell"] }`).

## What reaches prompts

- `legal_facts_for(entity)` — a witness's own `known_crimes` ("Saw a disguised stranger steal
  from shopkeeper.", or for the victim itself "Was robbed by a disguised stranger."), plus, for
  an enforcer, who is wanted in its polity. `describe_character`
  appends these, so dialogue personas carry exactly what that NPC knows.
- `crime_witnessed` — LLMCore folds "Seen by: …. Nobody else present noticed — only they may
  react to it." into the next narration prompt.

## Enforcement

An **enforcer** is any entity tagged `law_enforcer` (Sandpoint: Sheriff Belor Hemlock, the jailer
Vachedi, and the `Sandpoint Watchman` the town hub's `on_enter` patrol encounter can bring in).
`incorruptible` makes one refuse every bribe without a roll (Hemlock). A polity switches
enforcement on with `arrest_at`; `kill_on_sight_at` is optional:

```toml
[[polity]]
name = "Varisia"
arrest_at = 1            # bounty at which a recognizing enforcer confronts
kill_on_sight_at = 100   # ... and at which they attack instead
```

**When a confrontation starts.**

- An enforcer who **witnesses** a crime by the player acts on it at once — no `arrest_at`, no
  recognition roll (they saw it). A disguise only decides whose record it lands on; the guard
  still grabs whoever is in front of them.
- An enforcer who **meets** a wanted player (checked beside `check_presence`: on every roster
  change and disguise change) has to recognize them first. Each identity the player presents is
  tried: a disguise's own identity needs only the acclaim check; the real one beneath it needs
  the disguise seen through first. The acclaim check is the banned-presence one
  (`[law].recognition_skill` against the `[[law.recognition]]` band for `|base| + |polity|`
  acclaim), skipped when the enforcer personally saw that identity commit a crime. Once per
  enforcer per identity per disguise (`enforcement_checks`). A bounty under `arrest_at`, or one a
  bribe already bought this enforcer off for (`looked_away`), is ignored.
- At or past `kill_on_sight_at`, every enforcer present gets the `wanted_dead` attitude event —
  hostile through the ordinary combat machinery, no talking.

The confrontation is `LawEnforcement.pending_arrest` (saved; `DMCore.pending_arrest` reads through to it).
`resolution/Law_Enforcement.py`'s `LawEnforcement` owns the whole flow below plus `legal_records` and
`pending_reports`, and reaches the game only through its `LawWorld` port (`DMCoreLawWorld`, in
`DM_Enforcement.py`) — so it's tested against a fake world with no `DMCore`. `publish` is on the port
rather than returned from each method because surrender announces the outcome *and then* moves the
party to jail and advances the clock; narration reads the world when an event goes out. Crime
detection and witnesses stay in `DM_Law.py`, which calls in to file reports. It's announced only once the player's
input has fully resolved (NLPCore publishes `player_input_handled`), so the crime is narrated
before the guard steps in. Announcing publishes `arrest_confronted` (narrated by
`LLMCore.generate_arrest_response` from the record's own charges and amount) and
`arrest_awaiting`. The out-of-character line listing the replies rides on the narration as its
`notice`, which LLMCore shows right after it (`_publish_attached_notice`) — so the options never
appear ahead of the demand. A failed bribe or bluff carries the options the same way; a reply
that changed nothing ("You only have 2 gold pieces") gets its notice at once.

**The reply.** NLPCore reads the next input first (`_answer_arrest`): a line opening with an
offered word (`pay`, `surrender`, `bribe`, `bluff`, `resist`) is that reply; anything else goes to
the local model (`AdHoc_Generation.classify_arrest_reply`, enum-constrained like input
adjudication — "fine, take me in" is a surrender). A real reply is the whole turn. Anything else
(`other`) is played as an ordinary input and judged afterward: leaving the location is fleeing,
attacking anyone is resisting, and carrying on with another action twice (`STALL_LIMIT`) is
resisting too; talking or asking isn't a strike. Save/load and ADaM lines leave the question open.

| Reply | What happens |
|---|---|
| pay | The whole bounty goes to the enforcer; the record's bounty drops to 0 and its charges are marked `settled`. Acclaim stays. Not enough money: told so, the demand stands. |
| surrender | Pays what the purse holds; the shortfall is served as `ceil(shortfall × [law].jail_blocks_per_unit)` blocks (at least 1). The party is moved to the jail — the current location's own `jail`, else the first up its `return_to` chain, else the polity's — then `advance_blocks`. Settled either way. |
| bribe | Needs an amount in the reply ("bribe 5 gold", parsed mechanically by `parse_currency_amount`). `charisma` against the enforcer's `willpower`, shifted by the `[[law.bribe]]` band for the offer's share of the bounty; below every band it's refused outright. Taken: the money goes over and the enforcer looks away until the bounty rises — the record stands. Refused: a `refused_bribe` attitude drop, and the demand stands. Once per confrontation. |
| bluff | `trickery` against the enforcer's `observation`, plus `[law].bluff_witnessed_modifier` if they saw the crime. Fooled: that enforcer no longer takes the player for that identity. Once per confrontation. |
| resist | Every enforcer present, and the one who made the demand, gets `resisted_arrest` (hostile via `is_hostile`, like `assaulted`). The polity's `resisting_arrest` law, if any, is filed at once. Fleeing is told as an out-of-character notice, since the guard is no longer there to narrate. |

The `[law]` knobs: `jail_blocks_per_unit`, `bluff_witnessed_modifier`, `[[law.bribe]]`, and optional
skill overrides `bribe_skill`/`bribe_resist_skill`/`bluff_skill`/`bluff_resist_skill`. Sandpoint's
jail is its garrison; Magnimar's is the Hells.

## Persistence

Per entity: `known_crimes`, `assaulted_by`, `disguise`, `disguise_count`, `disguise_checks`,
`presence_checks`, `enforcement_checks`, `looked_away` (`LAW_INSTANCE_FIELDS`). Globally:
`legal_records`, `pending_reports`, `pending_arrest` (a reload asks the open question again).

## Not yet built

- Enforcers going after a wanted party member who isn't the player; only the player is
  confronted.
- Jail confiscating gear, or anything happening during the sentence.
- Failed bribery as a crime of its own.
- **Deeds** raising acclaim, and acclaim's sign shaping attitudes.
- Kills that don't go through `calculate_damage` (ex: a program's own `damage` op) aren't
  checked for murder.
