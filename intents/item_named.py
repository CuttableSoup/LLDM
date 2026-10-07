"""!
@file item_named.py
@brief The item-named intents (see CONTEXT.md's "Item-interaction intent") -- examine, take, give,
    trade, use, equip, unequip, drop, open, close -- as one manifest, the counterpart of the
    free-standing registry in intents/registry.py. Each ItemIntent declares how it resolves, how
    its success is narrated, and the two pre-conditions that used to be hand-written branches in
    DMCore._on_item_interaction_detected:

    - gated: the scene target's locked-container gate applies (unless the player already owns the
      item). False for use/equip/unequip/drop, which only ever touch the player's own inventory.
    - ground_aware: an item lying on the current room's ground is reached directly, ahead of any
      target or gate (examine/take).

    A denied attempt is narrated by the shared failure text in Narration_Prompts.item_interaction,
    not per intent -- every item-named intent fails for the same handful of reasons -- so narrate
    here only ever describes a success. Pure data and prompt text: no DMCore or LLMCore import.
"""

from dataclasses import dataclass
from typing import Callable

from resolution.Inventory_Resolution import format_currency


def coin_text(data, key):
    """!
    @brief An item_interaction_resolved payload's price/amount as coin text -- DMCore's own
        price_text/amount_text (the setting's denominations) when it sent one, else plain coins.
    """
    return data.get(f"{key}_text") or format_currency(data.get(key, 0))


@dataclass(frozen=True)
class ItemIntent:
    """!
    @param resolve resolve(core, intent, item_name, target_name, resolved) -- resolves the intent
        against the world and reports through resolved(found, **extra).
    @param narrate narrate(data) -> prompt text for a successful attempt.
    @param gated The locked-container gate on the scene target applies.
    @param ground_aware An item on the room's ground is resolved directly, ahead of target and gate.
    """

    resolve: Callable
    narrate: Callable
    gated: bool = True
    ground_aware: bool = False


# ---- resolve ---------------------------------------------------------------------------------

def _resolve_use(core, intent, item_name, target_name, resolved):
    core._resolve_use_intent(item_name, resolved)


def _resolve_equip(core, intent, item_name, target_name, resolved):
    core._resolve_equip_intent(item_name, resolved)


def _resolve_unequip(core, intent, item_name, target_name, resolved):
    core._resolve_unequip_intent(item_name, resolved)


def _resolve_drop(core, intent, item_name, target_name, resolved):
    core._resolve_drop_intent(item_name, resolved)


def _resolve_open_close(core, intent, item_name, target_name, resolved):
    core._resolve_open_close_intent(intent, target_name, resolved)


def _resolve_transfer(core, intent, item_name, target_name, resolved):
    core._resolve_transfer_intent(intent, item_name, target_name, resolved)


# ---- narrate (success only) -------------------------------------------------------------------

def _narrate_examine(data):
    # "revealed" is only ever set once the item is identified (a passed [entity.test], ex: an
    # arcane check) -- a plain look never carries it, so a hidden property (ex: the cursed
    # dagger's curse) only reaches this prompt after a real roll actually earned it.
    revealed = data.get("revealed")
    revealed_text = f" Known properties: {', '.join(revealed)}." if revealed else ""
    return (
        f"The player examines \"{data.get('item_name')}\".\n"
        f"Description: {data.get('description', '')}{revealed_text}\n"
        f"Narrate what they observe in 2-3 sentences as the Game Master. This is only "
        f"looking -- nothing is taken, moved, or changed."
    )


def _narrate_open(data):
    # Real contents (DMCore._resolve_open_close_intent), never mechanical data -- without this the
    # LLM had nothing to narrate from and invented plausible-sounding treasure.
    container = data.get("container")
    contents = data.get("contents") or []
    if contents:
        return (
            f"The player opens {container}, revealing: {'; '.join(contents)}.\n"
            f"Narrate this in 1-2 sentences as the Game Master, describing only what's "
            f"actually there -- don't invent anything else."
        )
    return (
        f"The player opens {container}, and it's empty.\n"
        f"Narrate this in 1-2 sentences as the Game Master."
    )


def _narrate_close(data):
    return (
        f"The player closes {data.get('container')}.\n"
        f"Narrate this in 1-2 sentences as the Game Master."
    )


def _narrate_equip(data):
    # "replaced" is the item that previously occupied this slot, if any -- real state from
    # DMCore._resolve_equip_intent, not invented.
    replaced = data.get("replaced")
    replaced_text = f", replacing \"{replaced}\"" if replaced else ""
    return (
        f"The player equips \"{data.get('item_name')}\"{replaced_text}.\n"
        f"Narrate this in 1-2 sentences as the Game Master."
    )


def _narrate_unequip(data):
    return (
        f"The player unequips \"{data.get('item_name')}\".\n"
        f"Narrate this in 1-2 sentences as the Game Master."
    )


def _narrate_drop(data):
    return (
        f"The player drops \"{data.get('item_name')}\".\n"
        f"Narrate this in 1-2 sentences as the Game Master."
    )


def _narrate_use(data):
    # "healed"/"poisoned"/"remaining_hp"/"charges_left"/"replaced_with" are
    # DMCore._resolve_use_intent's own real roll/consumption results, never invented. "healed"/
    # "poisoned" are each 0 when the item carries no such effect -- worded to not claim an effect
    # that didn't happen. An ad hoc-conjured consumable (DM_Improvisation.py) can carry either, so
    # this is also where a "helpful-looking" improvised potion turning out to be poison reads as a
    # real twist to the player, not a silent stat change.
    healed = data.get("healed", 0)
    poisoned = data.get("poisoned", 0)
    if healed:
        effect_text = f", restoring {healed} HP (now at {data.get('remaining_hp', 0)} HP)"
    elif poisoned:
        effect_text = f", dealing {poisoned} poison damage (now at {data.get('remaining_hp', 0)} HP)"
    else:
        effect_text = ""
    charges_left = data.get("charges_left", 0)
    if charges_left > 0:
        aftermath = f" It has {charges_left} charge(s) left."
    elif data.get("replaced_with"):
        aftermath = f" All used up, it's left behind only a {data['replaced_with']}."
    else:
        aftermath = " It's completely used up."
    return (
        f"The player uses \"{data.get('item_name')}\"{effect_text}.{aftermath}\n"
        f"Narrate this in 1-2 sentences as the Game Master."
    )


def narrate_transfer(data):
    """!
    @brief The give/trade/take success prompt -- also what an intent with no entry of its own
        falls back to, as the closing "takes an item" branch always did.
    """
    intent = data.get("intent")
    item_name = data.get("item_name")
    container = data.get("container")
    if item_name == "currency":
        if intent == "give":
            return (
                f"The player gives {coin_text(data, 'amount')} to {container}.\n"
                f"Narrate this in 1-2 sentences as the Game Master."
            )
        return (
            f"The player takes {coin_text(data, 'amount')} and adds it to their own.\n"
            f"Narrate this in 1-2 sentences as the Game Master."
        )
    if intent == "give":
        return (
            f"The player gives \"{item_name}\" to {container}.\n"
            f"Narrate this in 1-2 sentences as the Game Master."
        )
    if intent == "trade":
        return (
            f"The player pays {coin_text(data, 'price')} to {container} in exchange "
            f"for \"{item_name}\".\n"
            f"Narrate this brief transaction in 1-2 sentences as the Game Master."
        )
    return (
        f"The player takes \"{item_name}\" and adds it to their own inventory.\n"
        f"Narrate this in 1-2 sentences as the Game Master."
    )


# intent string -> ItemIntent. Resolution order for a named item is declared by the flags, not by
# the order of branches in DMCore: ground-aware ahead of the gate, gated ahead of the resolver.
ITEM_NAMED = {
    "use": ItemIntent(_resolve_use, _narrate_use, gated=False),
    "equip": ItemIntent(_resolve_equip, _narrate_equip, gated=False),
    "unequip": ItemIntent(_resolve_unequip, _narrate_unequip, gated=False),
    "drop": ItemIntent(_resolve_drop, _narrate_drop, gated=False),
    "examine": ItemIntent(_resolve_transfer, _narrate_examine, ground_aware=True),
    "take": ItemIntent(_resolve_transfer, narrate_transfer, ground_aware=True),
    "give": ItemIntent(_resolve_transfer, narrate_transfer),
    "trade": ItemIntent(_resolve_transfer, narrate_transfer),
    "open": ItemIntent(_resolve_open_close, _narrate_open),
    "close": ItemIntent(_resolve_open_close, _narrate_close),
}

# What an intent string with no entry above resolves and narrates as -- a plain transfer.
DEFAULT_ITEM_INTENT = ItemIntent(_resolve_transfer, narrate_transfer)
