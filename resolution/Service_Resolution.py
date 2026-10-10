"""!
@file Service_Resolution.py
@brief Pure, DMCore-independent rules for a priced service an entity offers
    (`[[entity.service]]`, see docs/services.md): which service a phrase names, what it costs, and
    whether the player may buy it right now. Mirrors the "pure module, DMCore reaches in" shape of
    Social_Resolution.py/Inventory_Resolution.py -- the effects of a purchase (moving coin, time,
    joining the party) live in DM_Services.py, which owns the live state they change.

    A service is authored data, never invented by the narrator: its `price` is what a dialogue
    prompt is told to quote (DM_Services.py's service_offers_for) and what the purchase charges, so
    the two can't disagree.
"""

import re

# The only content a service may be tagged with. It changes nothing mechanical -- only how
# plainly the narrator is told to describe it (intents/service.py), per the player's content level.
CONTENT_TAGS = ("sexual",)
# Words that carry no meaning in matching a phrase to a service ("buy a night", "the night").
_FILLER = frozenset({"a", "an", "the", "some", "her", "his", "their", "my", "your", "for", "of", "to", "with"})


def services_of(entities, name):
    """!@brief The [[entity.service]] list of entity name ([] for none or a malformed field)."""
    services = (entities.get(name) or {}).get("service") or []
    return [service for service in services if isinstance(service, dict) and service.get("name")]


def _stem(word):
    """!@brief A word with a plural -s dropped ("services" -> "service"), so singular and plural match."""
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _words(text):
    return [_stem(word) for word in re.findall(r"[a-z']+", (text or "").lower()) if word not in _FILLER]


def match_service(entities, provider_names, phrase, input_text=""):
    """!
    @brief Which (provider, service) a purchase phrase names, among the people present who offer
        any. A service matches when its name or one of its aliases (articles dropped) appears in
        the phrase or the whole input, or is itself contained in the phrase ("night" for "a
        night"); the longest such alias wins, then declaration order. Failing that, naming a
        provider that offers exactly one service ("hire jorrick") buys that one.
    @param entities The live entities dict.
    @param provider_names Who is present and could sell, in scene order.
    @param phrase The player's words for what they are buying (the item phrase), or "".
    @param input_text The whole input, for a provider named outside the phrase.
    @return (provider_name, service_dict), or (None, None).
    """
    wanted = " ".join(_words(phrase))
    whole = " ".join(_words(input_text))
    best, best_length = (None, None), 0
    for provider in provider_names:
        for service in services_of(entities, provider):
            for alias in [service["name"], *service.get("aliases", [])]:
                alias_text = " ".join(_words(alias))
                if not alias_text:
                    continue
                if re.search(rf"\b{re.escape(alias_text)}\b", wanted) or re.search(rf"\b{re.escape(alias_text)}\b", whole):
                    score = len(alias_text)
                elif wanted and len(wanted) >= 3 and re.search(rf"\b{re.escape(wanted)}\b", alias_text):
                    # The phrase is only part of the alias ("night" for "room night"): a weaker hit,
                    # scored just under an alias of the phrase's own length so it never beats one.
                    score = len(wanted) - 0.5
                else:
                    continue
                if score > best_length:
                    best, best_length = (provider, service), score
    if best[0]:
        return best
    for provider in provider_names:
        offered = services_of(entities, provider)
        names = [(entities.get(provider) or {}).get("name", provider), provider, *(entities.get(provider) or {}).get("aliases", [])]
        if len(offered) == 1 and any(
            name and re.search(rf"\b{re.escape(str(name).lower())}\b", (input_text or "").lower()) for name in names
        ):
            return provider, offered[0]
    return None, None


def price_of(service):
    """!@brief A service's price in the setting's value unit (never negative)."""
    try:
        return max(0.0, float(service.get("price", 0)))
    except (TypeError, ValueError):
        return 0.0


def refusal(entities, player_name, provider_name, service, disposition):
    """!
    @brief Why the player cannot buy this service right now, or None if they can.
    @param entities The live entities dict.
    @param player_name The buyer.
    @param provider_name The seller.
    @param service One [[entity.service]] dict.
    @param disposition The provider's current disposition toward the player.
    @return "too_cold" (they won't deal with someone they feel this way about), "already_joined" (a
        hire who is already in the party), "cant_afford", or None.
    """
    if "min_disposition" in service and disposition < service["min_disposition"]:
        return "too_cold"
    if service.get("joins_party") and (entities.get(provider_name) or {}).get("is_party"):
        return "already_joined"
    if float((entities.get(player_name) or {}).get("currency", 0) or 0) + 1e-9 < price_of(service):
        return "cant_afford"
    return None


def offer_lines(entities, name, format_currency):
    """!
    @brief The "Offers: ..." fact a person's prompt carries, so they quote the price the engine will
        actually charge. Empty for someone who sells nothing.
    @param entities The live entities dict.
    @param name The provider.
    @param format_currency Callable spelling an amount in the setting's coins.
    @return A list of zero or one strings.
    """
    services = services_of(entities, name)
    if not services:
        return []
    offers = "; ".join(f"{service['name']}: {format_currency(price_of(service))}" for service in services)
    return [f"Sells services at fixed prices -- quote exactly these, never another figure: {offers}."]
