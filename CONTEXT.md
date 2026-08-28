# LLDM

An autonomous dungeon master: free-text player actions resolve through a dice engine and
narrate through a local LLM. This glossary tracks vocabulary sharpened during architecture
work — see `CLAUDE.md` for how the system is actually built.

## Language

**ActionOutcome**:
The result of a player or creature's resolved action — an attack, a cast, a craft attempt, or
a failed attempt at one — handed from action resolution to narration. A closed set of variants
(a roll that happened, or one of several distinct reasons it didn't) rather than one loosely-
shaped result carrying whichever fields happened to apply.
_Avoid_: action_result, result dict

**Effect**:
A secondary consequence attached to an ActionOutcome whose roll actually happened and
succeeded — damage dealt, loot gained, a creature summoned, an item crafted, something
revealed. Composable: an outcome carries zero or more Effects rather than a fixed set of
optional fields that grows every time a new consequence is invented.
_Avoid_: attachment, result key
