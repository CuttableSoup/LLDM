from DM_Types import DMCoreProtocol


class SocialMixin(DMCoreProtocol):
    """!
    @brief Attitudes and character/flavor-text description (DMCore mixin -- only ever composed
        into DMCore, never instantiated on its own; relies on self.entities/self.rules,
        set up by DMCore.__init__). Inherits DMCoreProtocol purely so type checkers can
        resolve these shared attributes -- see DM_Types.py.
    """

    def get_attitude(self, entity_name, toward_name):
        """!
        @brief Resolves entity_name's six-value attitude array toward toward_name: a specific
            name override, then a supertype override, then the entity's default.
        @param entity_name The name of the entity whose attitude is being read.
        @param toward_name The name of the entity being regarded.
        @return The [disposition, trust, confidence, respect, obligation, intimacy] attitude array.
        """
        attitudes = self.entities.get(entity_name, {}).get("attitudes", {})

        for override in attitudes.get("name", []):
            if toward_name in override:
                return override[toward_name]

        toward_supertype = self.entities.get(toward_name, {}).get("supertype")
        for override in attitudes.get("supertype", []):
            if toward_supertype in override:
                return override[toward_supertype]

        return attitudes.get("default", [0, 0, 0, 0, 0, 0])

    def is_hostile(self, entity_name, toward_name):
        """!
        @brief Whether entity_name is hostile enough toward toward_name to be treated as a
            combat target rather than a dialogue partner. Two distinct defaults, deliberately
            not collapsed into one:
            - No `[entity.attitudes]` table at all (ex: arena.toml's wolf/field.toml's bandit,
              which declare no attitude data whatsoever) -- treated as hostile unconditionally. A
              monster that never bothered to author a disposition is still a monster; this is
              what keeps every existing hostile creature fighting exactly as before.
            - `[entity.attitudes]` *is* declared -- disposition has to actually reach true
              hostility (<= -100) to fight. A merely wary/negative-but-not-murderous
              disposition (ex: -40) is dialogue, not combat -- "you can dislike someone and
              not be hostile." This is what a generated NPC's own resolved disposition (see
              NPC_Generation.py's variance) is checked against.
            Inanimate objects (ex: a locked chest) are never hostile regardless of attitude
            data -- they have no combat intent, so a lockpicking attempt against one must not
            get batched into "round_resolved".
        @param entity_name The name of the entity being checked.
        @param toward_name The name of the entity it might be hostile toward.
        @return True if entity_name has no attitude data at all, or its disposition (the
            attitude array's first value) is -100 or lower.
        """
        entity = self.entities.get(entity_name, {})
        if entity.get("supertype") == "object":
            return False
        if "attitudes" not in entity:
            return True
        disposition = self.get_attitude(entity_name, toward_name)[0]
        return disposition <= -100

    def get_attitude_tier(self, value):
        """!
        @brief Finds the [[attitude_tier]] definition (rules.toml) whose minimum/maximum range
            contains a single attitude axis value, clamped to [-150, 150] first -- headroom
            past the nominal -100..100 range for whenever attitudes get modified at runtime
            (nothing does yet), so an extreme value still resolves to the correct outermost
            tier instead of matching nothing. Tiers are checked in TOML declaration order and
            the first match wins, same convention as choose_behavior -- ex: a value of exactly
            -100 sits on both "hostile"'s and "unfriendly"'s boundary, and resolves to
            whichever is declared first (hostile).
        @param value A single attitude axis value (ex: disposition).
        @return The matching attitude_tier definition, or None if none match (ex: no
                [[attitude_tier]] data is loaded at all).
        """
        clamped = max(-150, min(150, value))
        for tier in self.rules.get("attitude_tier", []):
            if tier.get("minimum", float("-inf")) <= clamped <= tier.get("maximum", float("inf")):
                return tier
        return None

    def describe_attitude(self, entity_name, toward_name):
        """!
        @brief Translates entity_name's six-value attitude array toward toward_name (from
            get_attitude) into prose via [[attitude_tier]] -- one phrase per axis, banded by
            get_attitude_tier, rather than handing the LLM raw numbers it has no way to
            calibrate ("38 disposition" means nothing to a language model; "is warm and
            well-disposed toward them" does).
        @param entity_name The name of the entity whose attitude is being described.
        @param toward_name The name of the entity it's directed toward.
        @return A prose fragment ("Attitude toward X: ..."), or "" if no attitude_tier data is
                loaded (ex: a malformed rules.toml -- see load_rules' per-file try/except note).
        """
        axes = ("disposition", "trust", "confidence", "respect", "obligation", "intimacy")
        values = self.get_attitude(entity_name, toward_name)
        phrases = []
        for axis, value in zip(axes, values):
            tier = self.get_attitude_tier(value)
            if tier and tier.get(axis):
                phrases.append(tier[axis])

        if not phrases:
            return ""
        return f"Attitude toward {toward_name}: " + ", ".join(phrases) + "."

    def describe_character(self, entity_name, toward_name=None):
        """!
        @brief Builds a flavor-text description of an entity for narration prompts, out of its
            purely descriptive data (description, qualities, memories, quotes) rather than
            mechanical data (skills/dice), since this is meant to tell the LLM who someone is.
        @param entity_name The name of the entity to describe.
        @param toward_name If given (and different from entity_name), appends
            describe_attitude(entity_name, toward_name) as an additional part -- ex: passing
            self.player_name lets a "pure mechanics" entity like wolf, which otherwise has no
            descriptive data at all, still surface something to the LLM (how hostile it is),
            rather than contributing nothing to the roster/defender_details.
        @return A formatted description string, or "" if the entity has no descriptive data
                (and no attitude phrase was added). Leads with the entity's own "name" field
                rather than entity_name (its self.entities dict key) when the two differ --
                every hand-authored template has "name" == its own dict key by construction,
                so this only actually changes anything for a generated NPC (DM_NpcGeneration.py),
                whose LLM-invented name would otherwise never reach this text at all.
        """
        entity = self.entities.get(entity_name, {})
        parts = []

        description = entity.get("description")
        if description:
            parts.append(description)

        qualities = entity.get("qualities")
        if qualities:
            parts.append("Qualities: " + ", ".join(f"{key} {value}" for key, value in qualities.items()))

        memories = entity.get("memories")
        if memories:
            parts.append("Memories: " + "; ".join(memories))

        quotes = entity.get("quotes")
        if quotes:
            parts.append("Known to say: " + "; ".join(f"\"{quote}\"" for quote in quotes))

        if toward_name and toward_name != entity_name:
            attitude = self.describe_attitude(entity_name, toward_name)
            if attitude:
                parts.append(attitude)

        if not parts:
            return ""
        return f"{entity.get('name', entity_name)} - " + " | ".join(parts)
